"""GPT Advisor Agent — Chat Monitor for architect trigger detection.

Watches ChatGPT chats for mentions of "архитектор" and creates events
when the architect is called or replies. Supports two monitoring tiers:

- Tier 1 (fast): active chat — every 2-3s, no navigation, uses main page
- Tier 2 (slow): recent project chats — every 10-15s, uses dedicated monitor page

The monitor page is a separate browser tab sharing the same auth context,
so it can navigate freely without disrupting the active chat.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import ssl
import urllib.request
import urllib.parse

from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from playwright.async_api import Page

import chat_selectors as S
import config

if TYPE_CHECKING:
    from browser import ChatGPTBrowser

log = logging.getLogger("gpt-advisor.monitor")


# ── Trigger patterns ─────────────────────────────────────────────

# "архитектор" and all declensions (архитектору, архитектора, архитектором...)
# Leading \b prevents false matches inside words (e.g. "вархитекторе")
# No trailing \b to allow Russian case endings
TRIGGER_CALL = re.compile(r'\bархитектор\w*', re.IGNORECASE)

# Message starts with "[Я Архитектор]" or "Я архитектор" (any spacing/brackets)
TRIGGER_REPLY = re.compile(
    r'^\s*\[?\s*[Яя]\s+[Аа]рхитектор\s*\]?',
    re.MULTILINE,
)


@dataclass
class ChatState:
    """Tracked state for a single monitored chat."""
    url: str
    title: str = ""
    project: str = ""
    last_seen_count: int = 0
    last_message_hash: str = ""
    last_checked_at: float = 0.0
    last_trigger_hash: str = ""
    last_trigger_at: float = 0.0


class ChatMonitor:
    """Monitors ChatGPT chats for architect trigger mentions.

    Two-tier polling:
    - Tier 1 (fast): checks active chat via main page every N seconds
    - Tier 2 (slow): scans recent chats via dedicated monitor page
    """

    def __init__(self, browser: 'ChatGPTBrowser'):
        self._browser = browser
        self._monitor_page: Optional[Page] = None

        # State
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._mode = "project_recent"
        self._chat_states: dict[str, ChatState] = {}  # url -> ChatState
        self._events: list[dict] = []
        self._processed: set[str] = set()  # dedup keys (persistent)
        self._active_chat_url: str = ""
        self._dedup_file = os.path.join(
            os.path.dirname(__file__), "monitor-dedup.json"
        )

        # Stats
        self._started_at: Optional[float] = None
        self._scan_count = 0
        self._trigger_count = 0

        # Persistent chat states file
        self._chat_states_file = os.path.join(
            os.path.dirname(__file__), "monitor-chat-states.json"
        )

    # ── Lifecycle ──────────────────────────────────────────────────

    async def start(self, mode: str = "project_recent") -> dict:
        """Start the monitor.

        Modes:
            active_only     — only the current active chat
            project_recent  — recent chats in configured projects
            global_recent   — recent chats across all visible projects
        """
        if self._running:
            return {"ok": True, "status": "already_running", "mode": self._mode}

        self._mode = mode
        self._running = True
        self._started_at = time.time()
        self._events.clear()
        self._scan_count = 0
        self._trigger_count = 0

        # Load persistent state (dedup + chat states — preserves across restarts)
        self._load_dedup()
        self._load_chat_states()

        # Track active chat
        self._active_chat_url = self._browser._page.url

        # Create dedicated monitor page for tier-2 scanning
        if mode != "active_only":
            try:
                ctx = self._browser._context
                self._monitor_page = await ctx.new_page()
                self._monitor_page.set_default_timeout(config.NAVIGATION_TIMEOUT)
                # Navigate to ChatGPT so it's ready
                await self._monitor_page.goto(
                    config.CHATGPT_URL,
                    wait_until="domcontentloaded",
                    timeout=config.NAVIGATION_TIMEOUT,
                )
                await self._monitor_page.wait_for_timeout(2000)
                log.info("Monitor page created (total tabs: %d)",
                         len(ctx.pages))
            except Exception as e:
                log.error("Failed to create monitor page: %s", e)
                self._monitor_page = None

        # Seed active chat state so first tick doesn't trigger on old messages
        await self._seed_active_chat_state()

        self._task = asyncio.create_task(self._monitor_loop())
        log.info("Monitor started, mode=%s", mode)
        return {"ok": True, "status": "started", "mode": mode}

    async def stop(self) -> dict:
        """Stop the monitor and clean up."""
        if not self._running:
            return {"ok": True, "status": "not_running"}

        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # Close monitor page
        if self._monitor_page:
            try:
                await self._monitor_page.close()
            except Exception:
                pass
            self._monitor_page = None

        # Persist state before stopping
        self._save_chat_states()
        self._save_dedup()
        log.info("Monitor stopped (scans=%d, triggers=%d)",
                 self._scan_count, self._trigger_count)
        return {"ok": True, "status": "stopped",
                "total_scans": self._scan_count,
                "total_triggers": self._trigger_count}

    def get_status(self) -> dict:
        """Get current monitor status (includes browser diagnostics)."""
        status = {
            "running": self._running,
            "mode": self._mode,
            "started_at": self._started_at,
            "uptime_sec": int(time.time() - self._started_at) if self._started_at else 0,
            "scan_count": self._scan_count,
            "trigger_count": self._trigger_count,
            "tracked_chats": len(self._chat_states),
            "pending_events": len(self._events),
            "active_chat_url": self._active_chat_url,
            "has_monitor_page": self._monitor_page is not None,
            "processed_dedup_keys": len(self._processed),
        }
        # Include browser-level diagnostics
        if self._browser:
            status["last_error"] = self._browser._last_error
            status["last_error_at"] = self._browser._last_error_at
            status["last_sync_at"] = self._browser._last_sync_at
        return status

    def get_events(self, clear: bool = True) -> list[dict]:
        """Get accumulated monitor events."""
        events = list(self._events)
        if clear:
            self._events.clear()
        return events

    # ── Persistent chat states ──────────────────────────────────────

    def _load_chat_states(self):
        """Load persisted chat states (last_seen_count per URL) from disk."""
        try:
            if os.path.exists(self._chat_states_file):
                with open(self._chat_states_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for url, info in data.get("states", {}).items():
                    self._chat_states[url] = ChatState(
                        url=url,
                        title=info.get("title", ""),
                        project=info.get("project", ""),
                        last_seen_count=info.get("last_seen_count", 0),
                        last_checked_at=info.get("last_checked_at", 0),
                    )
                log.info("Loaded %d chat states from disk", len(self._chat_states))
        except Exception as e:
            log.warning("Failed to load chat states: %s", e)

    def _save_chat_states(self):
        """Save chat states to disk for persistence across restarts."""
        try:
            states = {}
            for url, state in self._chat_states.items():
                states[url] = {
                    "title": state.title,
                    "project": state.project,
                    "last_seen_count": state.last_seen_count,
                    "last_checked_at": state.last_checked_at,
                }
            with open(self._chat_states_file, "w", encoding="utf-8") as f:
                json.dump({"states": states, "updated_at": time.time()}, f)
        except Exception as e:
            log.warning("Failed to save chat states: %s", e)

    # ── Seed state ─────────────────────────────────────────────────

    async def _seed_active_chat_state(self):
        """Seed active chat state. Uses persisted state if available,
        otherwise records current count. Checks tail for missed triggers."""
        page = self._browser._page
        url = page.url
        if "/c/" not in url:
            return

        try:
            count = await page.evaluate(
                "() => document.querySelectorAll('[data-message-author-role]').length"
            )
        except Exception as e:
            log.warning("Failed to seed active chat: %s", e)
            return

        # Check if we have persisted state for this chat
        persisted = self._chat_states.get(url)
        if persisted and persisted.last_seen_count > 0:
            old_count = persisted.last_seen_count
            persisted.last_checked_at = time.time()
            self._active_chat_url = url

            # Check messages that arrived during downtime
            if count > old_count:
                missed = count - old_count
                log.info("Seed: %s had %d msgs, now %d (+%d missed). Checking triggers...",
                         url[-30:], old_count, count, missed)
                try:
                    async with self._browser._page_lock:
                        messages = await self._read_messages_from_page(
                            page, old_count, count
                        )
                    self._check_triggers(messages, url, persisted)
                    persisted.last_seen_count = count
                    self._save_chat_states()
                except Exception as e:
                    log.warning("Failed to check missed messages: %s", e)
                    persisted.last_seen_count = count
            else:
                log.info("Seed: %s — no missed messages (%d)", url[-30:], count)
        else:
            # First time ever — seed from scratch
            state = ChatState(url=url, last_seen_count=count,
                              last_checked_at=time.time())
            self._chat_states[url] = state
            self._active_chat_url = url
            log.info("Seeded active chat (fresh): %s (%d msgs)", url[-30:], count)

    # ── Main loop ─────────────────────────────────────────────────

    async def _monitor_loop(self):
        """Two-tier monitoring loop."""
        active_interval = getattr(config, 'MONITOR_ACTIVE_POLL_SEC', 3)
        recent_interval = getattr(config, 'MONITOR_RECENT_POLL_SEC', 8)

        ticks = 0
        recent_every = max(1, recent_interval // active_interval)

        while self._running:
            try:
                await asyncio.sleep(active_interval)
                if not self._running:
                    break

                ticks += 1

                # Tier 1: check active chat (every tick)
                try:
                    await self._check_active_chat()
                except Exception as e:
                    log.warning("Monitor tier-1 error: %s", e)

                # Tier 2: scan recent chats (every N ticks)
                if self._mode != "active_only" and ticks % recent_every == 0:
                    try:
                        # Recreate monitor page if it crashed
                        if not self._monitor_page or self._monitor_page.is_closed():
                            log.warning("Monitor page lost, recreating...")
                            try:
                                ctx = self._browser._context
                                self._monitor_page = await ctx.new_page()
                                self._monitor_page.set_default_timeout(
                                    config.NAVIGATION_TIMEOUT
                                )
                                await self._monitor_page.goto(
                                    config.CHATGPT_URL,
                                    wait_until="domcontentloaded",
                                    timeout=config.NAVIGATION_TIMEOUT,
                                )
                                await self._monitor_page.wait_for_timeout(2000)
                                log.info("Monitor page recreated")
                            except Exception as e:
                                log.error("Failed to recreate monitor page: %s", e)
                                self._monitor_page = None

                        await self._scan_recent_chats()
                    except Exception as e:
                        log.warning("Monitor tier-2 error: %s", e)

                self._scan_count += 1

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Monitor loop error: %s", e)
                await asyncio.sleep(5)

    # ── Tier 1: Active chat ───────────────────────────────────────

    async def _check_active_chat(self):
        """Check the active chat for new messages with triggers.

        Uses main page. Only JS evaluate for count (fast, no lock).
        Acquires page_lock only when reading message text.
        """
        page = self._browser._page
        url = page.url

        if "/c/" not in url:
            return

        # Track URL changes (user switched chats)
        if url != self._active_chat_url:
            self._active_chat_url = url
            # Seed new chat if unknown
            if url not in self._chat_states:
                try:
                    count = await page.evaluate(
                        "() => document.querySelectorAll('[data-message-author-role]').length"
                    )
                    self._chat_states[url] = ChatState(
                        url=url, last_seen_count=count,
                        last_checked_at=time.time(),
                    )
                except Exception:
                    pass
                return

        # Fast count check (no lock)
        try:
            count = await page.evaluate(
                "() => document.querySelectorAll('[data-message-author-role]').length"
            )
        except Exception:
            return

        state = self._chat_states.get(url)
        if not state:
            self._chat_states[url] = ChatState(
                url=url, last_seen_count=count,
                last_checked_at=time.time(),
            )
            return

        if count <= state.last_seen_count:
            state.last_checked_at = time.time()
            return

        # New messages detected — read them (needs lock)
        new_messages = []
        async with self._browser._page_lock:
            try:
                new_messages = await self._read_messages_from_page(
                    page, state.last_seen_count, count
                )
            except Exception as e:
                log.warning("Monitor: failed to read active chat messages: %s", e)
                return

        # Update state
        state.last_seen_count = count
        state.last_checked_at = time.time()
        if new_messages:
            state.last_message_hash = self._hash(
                new_messages[-1].get("text", "")
            )

        # Check triggers
        self._check_triggers(new_messages, url, state)

        # Persist state after update
        self._save_chat_states()

    # ── Tier 2: Recent chats ─────────────────────────────────────

    async def _scan_recent_chats(self):
        """Scan recent chats in project for trigger mentions.

        Uses the dedicated monitor page — does NOT touch main page.
        """
        if not self._monitor_page:
            return

        page = self._monitor_page

        # Check if GPT is generating on main page — if so, don't interfere
        try:
            main_page = self._browser._page
            generating = await main_page.evaluate("""() => {
                const btn = document.querySelector('button[data-testid="stop-button"]');
                return btn && btn.offsetParent !== null;
            }""")
            if generating:
                return  # Don't scan while GPT is generating
        except Exception:
            pass

        # Navigate to project to see sidebar chats
        project_url = getattr(config, "PROJECT_URL", None)
        if not project_url:
            return

        try:
            # Navigate monitor page to project
            await page.goto(
                project_url,
                wait_until="domcontentloaded",
                timeout=config.NAVIGATION_TIMEOUT,
            )
            await page.wait_for_timeout(2000)

            # Ensure sidebar is open
            try:
                sidebar = page.locator("nav")
                if await sidebar.count() == 0 or not await sidebar.first.is_visible():
                    toggle = page.locator(S.SIDEBAR_TOGGLE)
                    if await toggle.count() > 0:
                        await toggle.first.click()
                        await page.wait_for_timeout(1000)
            except Exception:
                pass

            # Get list of recent chats
            limit = getattr(config, 'MONITOR_RECENT_CHAT_LIMIT', 10)
            chats = await page.evaluate(f"""() => {{
                const links = document.querySelectorAll('nav a[href*="/c/"]');
                const result = [];
                const seen = new Set();
                for (let i = 0; i < links.length && result.length < {limit}; i++) {{
                    const a = links[i];
                    const url = a.href;
                    if (seen.has(url)) continue;
                    seen.add(url);
                    result.push({{
                        title: a.innerText.trim().substring(0, 80),
                        url: url,
                    }});
                }}
                return result;
            }}""")

            if not chats:
                return

            tail_size = getattr(config, 'MONITOR_TAIL_SIZE', 5)
            active_url = self._active_chat_url

            for chat_info in chats:
                if not self._running:
                    break

                chat_url = chat_info["url"]
                chat_title = chat_info.get("title", "")

                # Skip active chat (tier-1 handles it)
                if chat_url == active_url:
                    continue

                # Skip recently checked
                state = self._chat_states.get(chat_url)
                if state and (time.time() - state.last_checked_at) < 10:
                    continue

                # Navigate monitor page to this chat
                try:
                    await page.goto(
                        chat_url,
                        wait_until="domcontentloaded",
                        timeout=config.NAVIGATION_TIMEOUT,
                    )
                    # Wait for messages
                    try:
                        await page.locator(S.ALL_MESSAGES).first.wait_for(
                            state="visible", timeout=5000
                        )
                    except Exception:
                        await page.wait_for_timeout(2000)

                    # Count messages
                    count = await page.evaluate(
                        "() => document.querySelectorAll('[data-message-author-role]').length"
                    )

                    if not state:
                        # First time seeing this chat — seed and check tail
                        state = ChatState(
                            url=chat_url,
                            title=chat_title,
                            project=config.PROJECT_NAME,
                            last_seen_count=count,
                            last_checked_at=time.time(),
                        )
                        self._chat_states[chat_url] = state
                        # On first encounter, check tail for existing triggers
                        messages = await self._read_tail_from_page(page, tail_size)
                        self._check_triggers(messages, chat_url, state)
                        self._save_chat_states()
                        continue

                    # Update title if needed
                    if chat_title and not state.title:
                        state.title = chat_title

                    # Only read if count changed
                    if count != state.last_seen_count:
                        # Read new messages (or tail if too many)
                        read_from = max(state.last_seen_count, count - tail_size)
                        messages = await self._read_messages_from_page(
                            page, read_from, count
                        )

                        state.last_seen_count = count
                        state.last_checked_at = time.time()
                        if messages:
                            state.last_message_hash = self._hash(
                                messages[-1].get("text", "")
                            )

                        self._check_triggers(messages, chat_url, state)
                        self._save_chat_states()
                    else:
                        state.last_checked_at = time.time()

                except Exception as e:
                    log.warning("Monitor: error scanning chat '%s': %s",
                                chat_title or chat_url[-20:], e)
                    continue

        except Exception as e:
            log.warning("Monitor: scan_recent_chats error: %s", e)

    # ── Message reading ───────────────────────────────────────────

    async def _read_messages_from_page(
        self, page: Page, from_idx: int, to_idx: int
    ) -> list[dict]:
        """Read messages between indices from a page."""
        messages = []
        all_msgs = page.locator(S.ALL_MESSAGES)

        for i in range(from_idx, to_idx):
            try:
                msg = all_msgs.nth(i)
                role = await msg.get_attribute("data-message-author-role")
                text = await msg.evaluate("el => el.innerText || ''")
                messages.append({
                    "role": role or "unknown",
                    "text": text,
                    "index": i,
                })
            except Exception:
                continue

        return messages

    async def _read_tail_from_page(self, page: Page, n: int) -> list[dict]:
        """Read last N messages from a page."""
        try:
            count = await page.evaluate(
                "() => document.querySelectorAll('[data-message-author-role]').length"
            )
        except Exception:
            return []

        start = max(0, count - n)
        return await self._read_messages_from_page(page, start, count)

    # ── Trigger matching ──────────────────────────────────────────

    def _check_triggers(self, messages: list[dict], chat_url: str,
                        state: ChatState):
        """Check messages for architect triggers and create events."""
        for msg in messages:
            text = msg.get("text", "")
            role = msg.get("role", "unknown")
            index = msg.get("index", -1)

            if not text.strip():
                continue

            # Dedup key: url + index + full text hash
            dedup_key = f"{chat_url}:{index}:{self._hash(text)}"
            if dedup_key in self._processed:
                continue

            event = None

            # Check for architect reply first (more specific)
            if role == "user" and TRIGGER_REPLY.search(text):
                event = self._make_event(
                    "architect_reply", msg, chat_url, state
                )
            # Check for architect call (user mentions architect)
            elif role == "user" and TRIGGER_CALL.search(text):
                event = self._make_event(
                    "architect_call", msg, chat_url, state
                )

            if event:
                self._processed.add(dedup_key)
                self._events.append(event)
                self._trigger_count += 1

                # Update state
                state.last_trigger_hash = self._hash(text)
                state.last_trigger_at = time.time()

                # Persist dedup, log event, and notify
                self._save_dedup()
                self._log_event(event)

                # Telegram notify only for architect_call (not reply)
                if event["type"] == "architect_call":
                    self._notify_telegram(event)

                log.warning(
                    "TRIGGER [%s] in '%s' (msg #%d): %.80s",
                    event["type"],
                    state.title or chat_url[-30:],
                    index,
                    text.replace("\n", " "),
                )

    def _make_event(self, event_type: str, msg: dict, chat_url: str,
                    state: ChatState) -> dict:
        """Create a monitor event."""
        return {
            "type": event_type,
            "project": state.project or config.PROJECT_NAME,
            "chat_title": state.title,
            "chat_url": chat_url,
            "message_index": msg.get("index", -1),
            "role": msg.get("role", "unknown"),
            "text_preview": msg.get("text", "")[:300],
            "message_hash": self._hash(msg.get("text", "")),
            "timestamp": time.time(),
        }

    def _notify_telegram(self, event: dict):
        """Send Telegram notification for architect_call events."""
        if not getattr(config, 'MONITOR_TELEGRAM_NOTIFY', False):
            return
        token = getattr(config, 'TELEGRAM_BOT_TOKEN', '')
        chat_id = getattr(config, 'TELEGRAM_CHAT_ID', '')
        if not token or not chat_id:
            return

        event_type = event.get("type", "unknown")
        chat_title = event.get("chat_title", "") or "untitled"
        preview = event.get("text_preview", "")[:150]
        msg_idx = event.get("message_index", "?")

        text = (
            f"🔔 GPT → Архитектор\n"
            f"[{event_type}] в чате «{chat_title}»\n"
            f"msg#{msg_idx}: {preview}"
        )

        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": chat_id,
                "text": text,
            }).encode("utf-8")
            req = urllib.request.Request(url, data=data)
            # Disable SSL verify (Windows cert store issues)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            urllib.request.urlopen(req, timeout=10, context=ctx)
            log.info("Telegram notify sent for %s", event_type)
        except Exception as e:
            log.warning("Telegram notify failed: %s", e)

    def _log_event(self, event: dict):
        """Append event to persistent log file."""
        events_file = getattr(
            config, 'MONITOR_EVENTS_FILE',
            os.path.join(os.path.dirname(__file__), "monitor-events.jsonl"),
        )
        try:
            with open(events_file, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("Failed to log monitor event: %s", e)

    # ── Force scan ────────────────────────────────────────────────

    async def scan_now(self) -> dict:
        """Force immediate scan of all monitored chats."""
        results = {"active_chat": None, "recent_chats": None}

        try:
            await self._check_active_chat()
            results["active_chat"] = "checked"
        except Exception as e:
            results["active_chat"] = f"error: {e}"

        if self._mode != "active_only" and self._monitor_page:
            try:
                await self._scan_recent_chats()
                results["recent_chats"] = "scanned"
            except Exception as e:
                results["recent_chats"] = f"error: {e}"

        return {
            "ok": True,
            "results": results,
            "events_found": len(self._events),
        }

    # ── Utilities ─────────────────────────────────────────────────

    def _load_dedup(self):
        """Load persistent dedup set from disk."""
        try:
            if os.path.exists(self._dedup_file):
                with open(self._dedup_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._processed = set(data.get("keys", []))
                log.info("Loaded %d dedup keys from disk", len(self._processed))
        except Exception as e:
            log.warning("Failed to load dedup state: %s", e)

    def _save_dedup(self):
        """Save dedup set to disk."""
        try:
            # Keep last 5000 keys max to avoid unbounded growth
            keys = list(self._processed)
            if len(keys) > 5000:
                keys = keys[-5000:]
                self._processed = set(keys)
            with open(self._dedup_file, "w", encoding="utf-8") as f:
                json.dump({"keys": keys, "updated_at": time.time()}, f)
        except Exception as e:
            log.warning("Failed to save dedup state: %s", e)

    @staticmethod
    def _hash(text: str) -> str:
        """Short hash for dedup."""
        return hashlib.sha1(
            text.encode("utf-8", errors="replace")
        ).hexdigest()[:16]
