"""GPT Advisor Agent — Playwright browser automation for ChatGPT.

Rewritten 2026-03-22: stability fixes, no reload, hash fingerprints,
navigation by project/chat name, composer control, image extraction.
"""

import asyncio
import hashlib
import json
import logging
import os
import time

from playwright.async_api import async_playwright, Page, BrowserContext

import config
import chat_selectors as S

log = logging.getLogger("gpt-advisor.browser")


class ChatGPTBrowser:
    """Manages a persistent Chromium session with ChatGPT."""

    def __init__(self):
        self._playwright = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._ready = False
        self._page_lock = asyncio.Lock()
        # Watcher state
        self._watching = False
        self._watcher_task: asyncio.Task | None = None
        self._last_msg_count = 0
        self._my_sent_hashes: list[str] = []  # sha1 hashes of messages we sent
        self._events: list[dict] = []
        # Monitor (lazy init after start)
        self._monitor = None
        # Diagnostics
        self._last_error: str = ""
        self._last_error_at: float = 0
        self._last_sync_at: float = 0

    # ── Lifecycle ──────────────────────────────────────────────────

    async def start(self):
        """Launch browser with persistent context."""
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=config.BROWSER_DATA_DIR,
            headless=config.HEADLESS,
            viewport={"width": 1280, "height": 900},
            locale="ru-RU",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        self._page.set_default_timeout(config.NAVIGATION_TIMEOUT)
        # Init monitor (needs context + page)
        from monitor import ChatMonitor
        self._monitor = ChatMonitor(self)
        log.info("Browser started, data dir: %s", config.BROWSER_DATA_DIR)

    async def stop(self):
        """Close browser gracefully."""
        if self._monitor and self._monitor._running:
            await self._monitor.stop()
        if self._watching:
            await self.stop_watching()
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
        log.info("Browser stopped")

    # ── Internal helpers ──────────────────────────────────────────

    async def _find_element(self, selectors: list[str],
                            state: str = "visible",
                            timeout: int = 5000) -> "Locator | None":
        """Try a chain of selectors, return first match or None.

        Args:
            selectors: list of CSS selectors to try in order
            state: Playwright wait state (visible, attached, etc.)
            timeout: ms to wait for each selector
        """
        page = self._page
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    first = loc.first
                    if state == "visible":
                        if await first.is_visible():
                            return first
                    else:
                        return first
            except Exception:
                continue
        # Log which selectors failed
        log.warning("_find_element: no match in %d selectors", len(selectors))
        return None

    async def _ensure_chat_page(self, retries: int = 2) -> bool:
        """Check we're on a chat page. If not, try to recover with retries."""
        url = self._page.url
        if "/c/" in url:
            editor = await self._find_element(S.INPUT_CHAIN, timeout=5000)
            if editor:
                return True

        log.warning("Not on chat page (url=%s), recovering...", url)
        chat_url = self._read_persistent_chat_url()

        for attempt in range(retries + 1):
            if chat_url and "/c/" in chat_url:
                try:
                    await self._page.goto(chat_url, wait_until="domcontentloaded",
                                          timeout=config.NAVIGATION_TIMEOUT)
                    await self._page.wait_for_timeout(2000)
                    editor = await self._find_element(S.INPUT_CHAIN, timeout=5000)
                    if editor:
                        log.info("Recovered to chat (attempt %d): %s",
                                 attempt + 1, chat_url)
                        return True
                except Exception as e:
                    self._last_error = f"Recovery attempt {attempt + 1}: {e}"
                    self._last_error_at = time.time()
                    log.error("Recovery attempt %d failed: %s", attempt + 1, e)

            # Try project URL as last resort
            if attempt == retries:
                project_url = getattr(config, "PROJECT_URL", None)
                if project_url:
                    try:
                        await self._page.goto(project_url,
                                              wait_until="domcontentloaded",
                                              timeout=config.NAVIGATION_TIMEOUT)
                        await self._page.wait_for_timeout(3000)
                        log.info("Recovered to project page")
                        return "/c/" in self._page.url
                    except Exception:
                        pass

            if attempt < retries:
                await self._page.wait_for_timeout(2000)

        self._last_error = "All recovery attempts failed"
        self._last_error_at = time.time()
        return False

    async def _ensure_sidebar_open(self):
        """Make sure the sidebar is visible."""
        try:
            sidebar = self._page.locator("nav")
            if await sidebar.count() > 0 and await sidebar.first.is_visible():
                return
            toggle = await self._find_element(S.SIDEBAR_TOGGLE_CHAIN)
            if toggle:
                await toggle.click()
                await self._page.wait_for_timeout(1000)
        except Exception as e:
            log.warning("Could not ensure sidebar open: %s", e)

    @staticmethod
    def _hash_text(text: str) -> str:
        """Create a stable hash fingerprint for a message."""
        return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()

    async def _extract_text(self, element) -> str:
        """Extract text from element using fast evaluate instead of inner_text()."""
        try:
            return await element.evaluate("el => el.innerText || ''")
        except Exception:
            try:
                return await element.inner_text()
            except Exception:
                return ""

    async def _extract_images(self, element) -> list[dict]:
        """Extract images from a message element."""
        images = []
        try:
            img_els = element.locator("img")
            img_count = await img_els.count()
            for j in range(img_count):
                img = img_els.nth(j)
                src = await img.get_attribute("src") or ""
                alt = await img.get_attribute("alt") or ""
                width = await img.evaluate("el => el.naturalWidth || el.width || 0")
                if width > 50 and src:
                    images.append({
                        "src": src,
                        "alt": alt,
                        "width": width,
                        "is_blob": src.startswith("blob:"),
                    })
            # Also check for background images
            bg_els = element.locator("div[style*='background-image']")
            bg_count = await bg_els.count()
            for j in range(bg_count):
                bg = bg_els.nth(j)
                style = await bg.get_attribute("style") or ""
                if "url(" in style:
                    url = style.split("url(")[1].split(")")[0].strip("'\"")
                    if url:
                        images.append({
                            "src": url,
                            "alt": "",
                            "width": 0,
                            "is_blob": url.startswith("blob:"),
                        })
        except Exception as e:
            log.warning("Image extraction error: %s", e)
        return images

    async def _count_messages(self) -> int:
        """Count messages via fast JS evaluation."""
        try:
            return await self._page.evaluate(
                "() => document.querySelectorAll('[data-message-author-role]').length"
            )
        except Exception:
            return 0

    # ── Login check ───────────────────────────────────────────────

    async def is_logged_in(self) -> bool:
        """Check if logged in WITHOUT navigating away from current page."""
        try:
            current_url = self._page.url
            if "chatgpt.com" in current_url:
                # Check for login buttons using fallback chain
                for sel in S.NOT_LOGGED_IN_CHAIN:
                    btn = self._page.locator(sel)
                    if await btn.count() > 0:
                        for i in range(await btn.count()):
                            if await btn.nth(i).is_visible():
                                return False
                # Check for input field
                editor = await self._find_element(S.INPUT_CHAIN)
                if editor:
                    return True
                return True
            await self._page.goto(config.CHATGPT_URL, wait_until="domcontentloaded",
                                  timeout=config.NAVIGATION_TIMEOUT)
            await self._page.wait_for_timeout(3000)
            login_btns = self._page.locator(S.NOT_LOGGED_IN_BUTTONS)
            if await login_btns.count() > 0:
                for i in range(await login_btns.count()):
                    if await login_btns.nth(i).is_visible():
                        return False
            textarea = self._page.locator(S.INPUT)
            if await textarea.count() > 0 and await textarea.first.is_visible():
                return True
            return False
        except Exception as e:
            log.error("Login check failed: %s", e)
            return False

    # ── Persistent chat URL ───────────────────────────────────────

    def _read_persistent_chat_url(self) -> str | None:
        path = getattr(config, "PERSISTENT_CHAT_URL_FILE", None)
        if not path or not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            url = f.read().strip()
        return url if url else None

    def _save_persistent_chat_url(self, url: str):
        path = getattr(config, "PERSISTENT_CHAT_URL_FILE", None)
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(url)
            log.info("Saved persistent chat URL: %s", url)

    # ── Navigation ────────────────────────────────────────────────

    async def navigate_to_project(self, new_chat: bool = False) -> dict:
        """Navigate to the persistent shared chat (or create new if new_chat=True)."""
        async with self._page_lock:
            page = self._page

            chat_url = self._read_persistent_chat_url()
            if chat_url and not new_chat:
                await page.goto(chat_url, wait_until="domcontentloaded",
                                timeout=config.NAVIGATION_TIMEOUT)
                await page.wait_for_timeout(3000)
                log.info("Opened persistent chat: %s", chat_url)

                actual_url = page.url
                if "/c/" in chat_url and "/c/" not in actual_url:
                    log.error("Navigation failed: wanted %s, got %s", chat_url, actual_url)
                    project_url = getattr(config, "PROJECT_URL", None)
                    if project_url:
                        await page.goto(project_url, wait_until="domcontentloaded",
                                        timeout=config.NAVIGATION_TIMEOUT)
                        await page.wait_for_timeout(2000)
            else:
                project_url = getattr(config, "PROJECT_URL", None)
                if project_url:
                    await page.goto(project_url, wait_until="domcontentloaded",
                                    timeout=config.NAVIGATION_TIMEOUT)
                    await page.wait_for_timeout(2000)
                else:
                    await page.goto(config.CHATGPT_URL, wait_until="domcontentloaded",
                                    timeout=config.NAVIGATION_TIMEOUT)
                    await page.wait_for_timeout(2000)
                log.info("Opened new chat in project '%s'", config.PROJECT_NAME)

            try:
                editor = page.locator(S.INPUT)
                await editor.wait_for(state="visible", timeout=10000)
            except Exception:
                log.warning("Input field not found after navigation")

            if "/c/" in page.url:
                self._save_persistent_chat_url(page.url)

            await self.start_watching()
            return {"ok": True, "project": config.PROJECT_NAME, "url": page.url}

    async def list_projects(self) -> list[dict]:
        """List projects visible in sidebar."""
        async with self._page_lock:
            await self._ensure_sidebar_open()
            result = await self._page.evaluate("""() => {
                const links = document.querySelectorAll('nav a[href*="/g/"]');
                const projects = [];
                const seen = new Set();
                links.forEach(a => {
                    const href = a.href;
                    if (href.includes('/project') || seen.has(href)) return;
                    seen.add(href);
                    const title = a.innerText.trim().substring(0, 80);
                    if (title) projects.push({title, url: href});
                });
                return projects;
            }""")
            return result

    async def open_project(self, name: str = None, url: str = None) -> dict:
        """Open a project by name or URL."""
        async with self._page_lock:
            if url:
                await self._page.goto(url, wait_until="domcontentloaded",
                                      timeout=config.NAVIGATION_TIMEOUT)
                await self._page.wait_for_timeout(2000)
                return {"ok": True, "url": self._page.url}

            if not name:
                return {"ok": False, "error": "Provide name or url"}

            await self._ensure_sidebar_open()
            await self._page.wait_for_timeout(500)

            # Find project by text in sidebar
            loc = self._page.get_by_text(name, exact=False).first
            try:
                await loc.wait_for(state="visible", timeout=5000)
                await loc.click()
                await self._page.wait_for_timeout(2000)
                return {"ok": True, "url": self._page.url, "project": name}
            except Exception as e:
                return {"ok": False, "error": f"Project '{name}' not found: {e}"}

    async def list_chats(self) -> list[dict]:
        """List chats visible in current sidebar context."""
        async with self._page_lock:
            await self._ensure_sidebar_open()
            await self._page.wait_for_timeout(500)
            result = await self._page.evaluate("""() => {
                const links = document.querySelectorAll('nav a[href*="/c/"]');
                const chats = [];
                links.forEach(a => {
                    const title = a.innerText.trim().substring(0, 80);
                    if (title) chats.push({title, url: a.href});
                });
                return chats;
            }""")
            return result

    async def open_chat(self, chat_name: str = None, url: str = None,
                        project_name: str = None) -> dict:
        """Open a chat by name or URL. Optionally open project first."""
        async with self._page_lock:
            if project_name:
                # Release lock temporarily to call open_project
                pass  # handled below

        if project_name:
            result = await self.open_project(name=project_name)
            if not result.get("ok"):
                return result

        async with self._page_lock:
            if url:
                await self._page.goto(url, wait_until="domcontentloaded",
                                      timeout=config.NAVIGATION_TIMEOUT)
                await self._page.wait_for_timeout(3000)
                if "/c/" in self._page.url:
                    self._save_persistent_chat_url(self._page.url)
                return {"ok": True, "url": self._page.url}

            if not chat_name:
                return {"ok": False, "error": "Provide chat_name or url"}

            await self._ensure_sidebar_open()
            await self._page.wait_for_timeout(500)

            loc = self._page.get_by_text(chat_name, exact=False).first
            try:
                await loc.wait_for(state="visible", timeout=5000)
                await loc.click()
                await self._page.wait_for_timeout(3000)
                if "/c/" in self._page.url:
                    self._save_persistent_chat_url(self._page.url)
                return {"ok": True, "url": self._page.url, "chat": chat_name}
            except Exception as e:
                return {"ok": False, "error": f"Chat '{chat_name}' not found: {e}"}

    async def get_current_chat_info(self) -> dict:
        """Return info about the current chat."""
        page = self._page
        url = page.url
        msg_count = await self._count_messages()

        # Try to extract chat title from page
        title = ""
        try:
            title = await page.evaluate("""() => {
                const h = document.querySelector('h1, [data-testid="conversation-title"]');
                return h ? h.innerText.trim() : '';
            }""")
        except Exception:
            pass

        return {
            "url": url,
            "title": title,
            "message_count": msg_count,
            "is_chat": "/c/" in url,
        }

    # ── Safe sync (pull external messages) ───────────────────────

    async def sync_chat(self) -> dict:
        """Safely sync current chat to pick up messages from other devices.

        Only acts when safe:
        - Must be on a /c/ chat URL
        - GPT must NOT be generating
        - Composer must be empty (no draft / attachments)

        Performs a same-URL navigation (not full reload) to force
        ChatGPT to re-render the full conversation.
        """
        async with self._page_lock:
            page = self._page
            url = page.url

            if "/c/" not in url:
                return {"ok": False, "reason": "not_on_chat", "url": url}

            # Check if GPT is generating
            stop_btn = page.locator(S.STOP_BUTTON)
            if await stop_btn.count() > 0 and await stop_btn.first.is_visible():
                return {"ok": False, "reason": "generating"}

            # Check composer state (don't lose draft)
            try:
                editor = page.locator(S.INPUT)
                if await editor.count() > 0:
                    text = await editor.evaluate("el => el.innerText || ''")
                    if text.strip():
                        return {"ok": False, "reason": "composer_has_text"}

                has_attachments = await page.evaluate("""() => {
                    const composer = document.querySelector('#prompt-textarea');
                    if (!composer) return false;
                    const parent = composer.closest('form') || composer.parentElement?.parentElement;
                    if (!parent) return false;
                    const imgs = parent.querySelectorAll('img, [data-testid*="attachment"]');
                    return imgs.length > 0;
                }""")
                if has_attachments:
                    return {"ok": False, "reason": "composer_has_attachments"}
            except Exception:
                pass

            count_before = await self._count_messages()

            # Same-URL navigation to force chat re-render
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=config.NAVIGATION_TIMEOUT)
                # Wait for messages to appear in DOM
                try:
                    await page.locator(S.ALL_MESSAGES).first.wait_for(
                        state="visible", timeout=10000
                    )
                except Exception:
                    await page.wait_for_timeout(3000)
                # Wait for input to appear (chat fully loaded)
                try:
                    await page.locator(S.INPUT).wait_for(
                        state="visible", timeout=5000
                    )
                except Exception:
                    pass
                # Scroll to bottom for lazy-loaded messages
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)
            except Exception as e:
                log.error("sync_chat navigation failed: %s", e)
                return {"ok": False, "reason": "navigation_error", "error": str(e)}

            count_after = await self._count_messages()

            self._last_sync_at = time.time()
            log.info("sync_chat: %d -> %d messages", count_before, count_after)
            return {
                "ok": True,
                "before": count_before,
                "after": count_after,
                "new": count_after - count_before,
            }

    # ── Reading messages ──────────────────────────────────────────

    async def read_all_messages(self, refresh: bool = False,
                                last_n: int = 0,
                                include_images: bool = False,
                                sync: bool = False) -> list[dict]:
        """Read messages from current chat. NO page reload.

        Args:
            refresh: if True, scroll to bottom to trigger lazy loading (no reload!)
            last_n: return only last N messages (0 = all)
            include_images: extract image URLs from messages
            sync: if True, do safe sync first (re-navigate to pick up external messages)
        """
        if sync:
            # sync_chat acquires its own lock
            await self.sync_chat()

        async with self._page_lock:
            page = self._page

            if not await self._ensure_chat_page():
                return []

            # Soft refresh: scroll to bottom to trigger lazy-load, NO reload
            if refresh:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)

            all_msgs = page.locator(S.ALL_MESSAGES)
            count = await all_msgs.count()

            start_idx = 0
            if last_n > 0 and count > last_n:
                start_idx = count - last_n

            messages = []
            for i in range(start_idx, count):
                msg = all_msgs.nth(i)
                role = await msg.get_attribute("data-message-author-role")
                text = await self._extract_text(msg)

                entry = {"role": role or "unknown", "text": text, "index": i}

                if include_images:
                    images = await self._extract_images(msg)
                    if images:
                        entry["images"] = images

                messages.append(entry)

            return messages

    async def read_tail_messages(self, last_n: int = 20,
                                 include_images: bool = False,
                                 sync: bool = False) -> list[dict]:
        """Read last N messages. With sync=True, first pulls external messages."""
        return await self.read_all_messages(
            refresh=False, last_n=last_n, include_images=include_images,
            sync=sync,
        )

    async def read_messages_since(self, after_index: int,
                                  include_images: bool = False) -> list[dict]:
        """Read messages after a specific index."""
        async with self._page_lock:
            page = self._page
            if not await self._ensure_chat_page():
                return []

            all_msgs = page.locator(S.ALL_MESSAGES)
            count = await all_msgs.count()

            messages = []
            for i in range(after_index + 1, count):
                msg = all_msgs.nth(i)
                role = await msg.get_attribute("data-message-author-role")
                text = await self._extract_text(msg)

                entry = {"role": role or "unknown", "text": text, "index": i}
                if include_images:
                    images = await self._extract_images(msg)
                    if images:
                        entry["images"] = images

                messages.append(entry)

            return messages

    # ── Images ────────────────────────────────────────────────────

    async def list_images(self, last_n: int = 30) -> list[dict]:
        """List images from the last N messages."""
        async with self._page_lock:
            page = self._page
            if not await self._ensure_chat_page():
                return []

            all_msgs = page.locator(S.ALL_MESSAGES)
            count = await all_msgs.count()
            start = max(0, count - last_n)

            result = []
            for i in range(start, count):
                msg = all_msgs.nth(i)
                role = await msg.get_attribute("data-message-author-role") or "unknown"
                images = await self._extract_images(msg)
                for img_idx, img in enumerate(images):
                    result.append({
                        "message_index": i,
                        "role": role,
                        "image_index": img_idx,
                        "src": img["src"],
                        "alt": img.get("alt", ""),
                        "is_blob": img.get("is_blob", False),
                        "downloadable": not img.get("is_blob", False),
                    })
            return result

    async def download_image(self, url: str, save_path: str) -> dict:
        """Download an image through the authenticated browser session."""
        page = self._page
        try:
            resp = await page.request.get(url)
            if resp.ok:
                body = await resp.body()
                with open(save_path, "wb") as f:
                    f.write(body)
                log.info("Downloaded image (%d bytes) to %s", len(body), save_path)
                return {"ok": True, "path": save_path, "size": len(body)}
            else:
                return {"ok": False, "error": f"HTTP {resp.status}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def download_image_from_message(self, message_index: int,
                                          image_index: int,
                                          save_path: str) -> dict:
        """Download a specific image from a specific message."""
        async with self._page_lock:
            page = self._page
            if not await self._ensure_chat_page():
                return {"ok": False, "error": "Not on chat page"}

            all_msgs = page.locator(S.ALL_MESSAGES)
            count = await all_msgs.count()
            if message_index >= count:
                return {"ok": False, "error": f"Message {message_index} not found (total: {count})"}

            msg = all_msgs.nth(message_index)
            images = await self._extract_images(msg)
            if image_index >= len(images):
                return {"ok": False, "error": f"Image {image_index} not found (total: {len(images)})"}

            src = images[image_index]["src"]
            if src.startswith("blob:"):
                return {"ok": False, "error": "Cannot download blob: URL directly"}

        return await self.download_image(src, save_path)

    # ── Screenshots ───────────────────────────────────────────────

    async def screenshot_chat(self, save_path: str,
                              mode: str = "viewport") -> dict:
        """Take a screenshot.

        Modes:
            viewport — current visible area (fast, reliable)
            stitched — scroll through chat capturing multiple frames
        """
        try:
            if mode == "stitched":
                return await self._screenshot_stitched(save_path)
            else:
                await self._page.screenshot(path=save_path, full_page=False)
                log.info("Viewport screenshot saved to %s", save_path)
                return {"ok": True, "path": save_path, "mode": "viewport"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _screenshot_stitched(self, save_path: str) -> dict:
        """Scroll through chat taking screenshots. Returns last frame."""
        page = self._page
        try:
            # Scroll to top first
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(500)

            frames = []
            frame_dir = os.path.dirname(save_path)
            max_frames = 20

            for i in range(max_frames):
                frame_path = os.path.join(frame_dir, f"screenshot_frame_{i}.png")
                await page.screenshot(path=frame_path, full_page=False)
                frames.append(frame_path)

                # Check if we've reached the bottom
                at_bottom = await page.evaluate("""() => {
                    return (window.innerHeight + window.scrollY) >= document.body.scrollHeight - 10;
                }""")
                if at_bottom:
                    break

                await page.evaluate("window.scrollBy(0, window.innerHeight - 100)")
                await page.wait_for_timeout(500)

            # Copy last frame as main screenshot
            if frames:
                import shutil
                shutil.copy2(frames[-1], save_path)

            # Scroll back to bottom
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

            return {
                "ok": True,
                "path": save_path,
                "mode": "stitched",
                "frames": frames,
                "frame_count": len(frames),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Sending messages ──────────────────────────────────────────

    async def send_message(self, text: str, wait_for_response: bool = True) -> dict:
        """Send a message to ChatGPT and optionally wait for response."""
        async with self._page_lock:
            page = self._page
            start_time = time.time()

            if not await self._ensure_chat_page():
                return {"ok": False, "error": "Not on chat page, recovery failed"}

            pre_count = await page.locator(S.ASSISTANT_MESSAGE).count()

            # Fill the input (with fallback chain)
            editor = await self._find_element(S.INPUT_CHAIN)
            if not editor:
                return {"ok": False, "error": "Input field not found (all selectors failed)"}
            await editor.click()
            await page.wait_for_timeout(300)

            # Clear input
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await page.wait_for_timeout(200)

            # Paste long texts via clipboard, type short ones
            if len(text) > 500:
                await page.evaluate("""([text, sel]) => {
                    const el = document.querySelector(sel);
                    if (el) {
                        el.focus();
                        const dt = new DataTransfer();
                        dt.setData('text/plain', text);
                        const event = new ClipboardEvent('paste', {
                            clipboardData: dt,
                            bubbles: true,
                            cancelable: true,
                        });
                        el.dispatchEvent(event);
                    }
                }""", [text, S.INPUT])
            else:
                await editor.fill(text)

            await page.wait_for_timeout(500)

            # Send via composer_send (internal, no lock needed since we hold it)
            await self._do_composer_send(page)

            log.info("Message sent (%d chars)", len(text))
            self._my_sent_hashes.append(self._hash_text(text))

            # Save URL if new chat was created
            await page.wait_for_timeout(2000)
            if "/c/" in page.url:
                current_saved = self._read_persistent_chat_url()
                if current_saved != page.url:
                    self._save_persistent_chat_url(page.url)

            if not self._watching:
                await self.start_watching()

            if not wait_for_response:
                return {
                    "ok": True,
                    "waited": False,
                    "message": "Message sent, not waiting for response",
                }

            # Wait for response (release lock for waiting)
        response_text, status = await self._wait_for_response(pre_count)
        elapsed_ms = int((time.time() - start_time) * 1000)

        return {
            "ok": True,
            "waited": True,
            "response": response_text,
            "status": status,
            "duration_ms": elapsed_ms,
        }

    async def _do_composer_send(self, page: Page):
        """Press the send button or fallback to Enter."""
        send_btn = await self._find_element(S.SEND_BUTTON_CHAIN)
        if send_btn:
            try:
                if await send_btn.is_enabled():
                    await send_btn.click()
                    return
            except Exception:
                pass
        # Fallback: Enter key
        log.warning("Send button not found/disabled, using Enter")
        editor = await self._find_element(S.INPUT_CHAIN)
        if editor:
            await editor.press("Enter")
        else:
            await page.keyboard.press("Enter")

    async def _wait_for_response(self, pre_count: int) -> tuple[str, str]:
        """Wait for GPT to finish responding.

        Primary signals:
        - stop button gone
        - assistant message count increased
        - last message text stable for RESPONSE_STABLE_CYCLES polls

        No dependency on thinking indicator as primary signal.
        """
        page = self._page
        start = time.time()
        notified_long_wait = False
        stable_cycles = getattr(config, "RESPONSE_STABLE_CYCLES", 2)
        last_text = ""
        stable_count = 0

        while True:
            elapsed_ms = (time.time() - start) * 1000

            # Check stop button
            stop_btn = page.locator(S.STOP_BUTTON)
            is_generating = await stop_btn.count() > 0 and await stop_btn.first.is_visible()

            if not is_generating:
                post_count = await page.locator(S.ASSISTANT_MESSAGE).count()
                if post_count > pre_count:
                    # Get last message text
                    last_msg = page.locator(S.ASSISTANT_MESSAGE).last
                    current_text = await self._extract_text(last_msg)

                    # Check stability: text unchanged for N cycles
                    if current_text == last_text and current_text:
                        stable_count += 1
                    else:
                        stable_count = 0
                        last_text = current_text

                    if stable_count >= stable_cycles:
                        return current_text, "complete"

            # Timeout
            if elapsed_ms > config.RESPONSE_MAX_WAIT:
                post_count = await page.locator(S.ASSISTANT_MESSAGE).count()
                if post_count > pre_count:
                    last_msg = page.locator(S.ASSISTANT_MESSAGE).last
                    text = await self._extract_text(last_msg)
                    return text, "timeout_partial"
                return "", "timeout_empty"

            # Long wait notification
            if elapsed_ms > config.LONG_WAIT_THRESHOLD and not notified_long_wait:
                notified_long_wait = True
                log.warning("GPT thinking for >2 min (%.0fs)...", elapsed_ms / 1000)

            await page.wait_for_timeout(config.RESPONSE_POLL_INTERVAL)

    # ── Composer control ──────────────────────────────────────────

    async def get_composer_state(self) -> dict:
        """Check the state of the composer (input area)."""
        async with self._page_lock:
            page = self._page
            try:
                editor = page.locator(S.INPUT)
                has_text = False
                if await editor.count() > 0:
                    text = await editor.evaluate("el => el.innerText || ''")
                    has_text = bool(text.strip())

                send_btn = page.locator(S.SEND_BUTTON)
                send_enabled = False
                if await send_btn.count() > 0:
                    send_enabled = await send_btn.is_enabled()

                # Check for pending attachments
                has_attachments = await page.evaluate("""() => {
                    const composer = document.querySelector('#prompt-textarea');
                    if (!composer) return false;
                    const parent = composer.closest('form') || composer.parentElement?.parentElement;
                    if (!parent) return false;
                    const imgs = parent.querySelectorAll('img, [data-testid*="attachment"]');
                    return imgs.length > 0;
                }""")

                return {
                    "has_text": has_text,
                    "has_attachments": has_attachments,
                    "send_enabled": send_enabled,
                }
            except Exception as e:
                return {"has_text": False, "has_attachments": False,
                        "send_enabled": False, "error": str(e)}

    async def composer_send(self) -> dict:
        """Send whatever is currently in the composer."""
        async with self._page_lock:
            page = self._page
            pre_count = await page.locator(S.ASSISTANT_MESSAGE).count()
            await self._do_composer_send(page)
            log.info("Composer sent")
            return {"ok": True, "pre_assistant_count": pre_count}

    # ── File upload ───────────────────────────────────────────────

    async def upload_file(self, file_path: str) -> dict:
        """Upload a file to the current chat via hidden file input."""
        async with self._page_lock:
            page = self._page
            try:
                # Primary: use hidden input[type=file]
                file_input = page.locator(S.FILE_INPUT)
                if await file_input.count() > 0:
                    await file_input.first.set_input_files(file_path)
                    await page.wait_for_timeout(3000)
                    log.info("File uploaded via input[type=file]: %s", file_path)
                    return {"ok": True, "file": file_path, "method": "file_input"}

                # Fallback: try to find attach button and use file chooser
                attach_btn = page.get_by_role("button").filter(has_text="Attach")
                if await attach_btn.count() == 0:
                    attach_btn = page.locator('button[aria-label*="Attach"], button[aria-label*="attach"]')

                if await attach_btn.count() > 0:
                    async with page.expect_file_chooser() as fc_info:
                        await attach_btn.first.click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(file_path)
                    await page.wait_for_timeout(3000)
                    log.info("File uploaded via attach button: %s", file_path)
                    return {"ok": True, "file": file_path, "method": "attach_button"}

                return {"ok": False, "error": "No file input or attach button found"}
            except Exception as e:
                return {"ok": False, "error": str(e)}

    # ── Status ────────────────────────────────────────────────────

    async def get_status(self) -> dict:
        """Check current state: is GPT generating? How many messages?"""
        page = self._page
        stop_btn = page.locator(S.STOP_BUTTON)
        is_generating = await stop_btn.count() > 0 and await stop_btn.first.is_visible()

        msg_count = await self._count_messages()

        # Chat title
        title = ""
        try:
            title = await page.evaluate("""() => {
                const h = document.querySelector('h1, [data-testid="conversation-title"]');
                return h ? h.innerText.trim() : '';
            }""")
        except Exception:
            pass

        return {
            "generating": is_generating,
            "message_count": msg_count,
            "url": page.url,
            "chat_title": title,
            "monitor_running": self._monitor._running if self._monitor else False,
            "last_error": self._last_error,
            "last_error_at": self._last_error_at,
            "last_sync_at": self._last_sync_at,
        }

    async def eval_js(self, js_code: str) -> str:
        """Evaluate JavaScript on the page and return result as string."""
        try:
            result = await self._page.evaluate(js_code)
            return str(result)
        except Exception as e:
            return f"ERROR: {e}"

    # ── Chat Watcher ──────────────────────────────────────────────

    async def start_watching(self) -> dict:
        """Start background polling to detect external messages."""
        if self._watching:
            return {"ok": True, "status": "already_watching"}
        self._watching = True
        self._last_msg_count = await self._count_messages()
        self._events.clear()
        self._watcher_task = asyncio.create_task(self._watch_loop())
        log.info("Watcher started, initial msg count: %d", self._last_msg_count)
        return {"ok": True, "status": "started", "initial_count": self._last_msg_count}

    async def stop_watching(self) -> dict:
        """Stop the background watcher."""
        if not self._watching:
            return {"ok": True, "status": "not_watching"}
        self._watching = False
        if self._watcher_task:
            self._watcher_task.cancel()
            self._watcher_task = None
        log.info("Watcher stopped")
        return {"ok": True, "status": "stopped"}

    def get_events(self, clear: bool = True) -> list[dict]:
        """Return accumulated watcher events."""
        events = list(self._events)
        if clear:
            self._events.clear()
        return events

    async def _watch_loop(self):
        """Poll DOM every 2s to detect external messages. Auto-sync when idle."""
        ticks_since_check = 0
        ticks_since_sync = 0
        CHECK_EVERY_TICKS = 5   # 10s between health checks
        SYNC_EVERY_TICKS = 15   # 30s between auto-syncs
        while self._watching:
            try:
                await asyncio.sleep(2)
                if not self._watching:
                    break

                ticks_since_check += 1

                # Periodic health check (every ~10s)
                if ticks_since_check >= CHECK_EVERY_TICKS:
                    ticks_since_check = 0
                    async with self._page_lock:
                        if not await self._ensure_chat_page():
                            log.error("Watcher: lost chat, cannot recover")
                            continue
                        try:
                            alive = await self._page.evaluate(
                                "() => !!document.querySelector('[data-message-author-role]') "
                                "|| document.querySelector('#prompt-textarea') !== null"
                            )
                            if not alive:
                                log.warning("Watcher: DOM not ready, skipping tick")
                                continue
                        except Exception:
                            log.warning("Watcher: page may have navigated away")
                            continue

                ticks_since_sync += 1

                # Auto-sync: every ~30s, if idle, re-navigate to pick up
                # messages from other devices (phone, etc.)
                if ticks_since_sync >= SYNC_EVERY_TICKS:
                    ticks_since_sync = 0
                    try:
                        result = await self.sync_chat()
                        if result.get("ok") and result.get("new", 0) > 0:
                            log.info("Watcher auto-sync found %d new msgs",
                                     result["new"])
                    except Exception as e:
                        log.warning("Watcher auto-sync error: %s", e)

                # Count messages (fast JS, no lock needed for read)
                current_count = await self._count_messages()

                if current_count > self._last_msg_count:
                    async with self._page_lock:
                        new_msgs = []
                        all_msgs = self._page.locator(S.ALL_MESSAGES)
                        for i in range(self._last_msg_count, current_count):
                            msg = all_msgs.nth(i)
                            role = await msg.get_attribute("data-message-author-role")
                            text = await self._extract_text(msg)
                            new_msgs.append({
                                "role": role or "unknown",
                                "text": text[:500],
                                "index": i,
                            })

                    # Determine if external
                    external = False
                    for m in new_msgs:
                        if m["role"] == "user":
                            msg_hash = self._hash_text(m["text"])
                            if msg_hash in self._my_sent_hashes:
                                self._my_sent_hashes.remove(msg_hash)
                            else:
                                external = True

                    event = {
                        "type": "external_message" if external else "new_messages",
                        "new_count": current_count - self._last_msg_count,
                        "messages": new_msgs,
                        "total_count": current_count,
                        "timestamp": time.time(),
                    }
                    self._events.append(event)

                    if external:
                        log.warning("EXTERNAL message detected! %d new msgs", len(new_msgs))
                    else:
                        log.info("Watcher: %d new msgs (ours)", len(new_msgs))

                    events_file = os.path.join(os.path.dirname(__file__), "events.jsonl")
                    with open(events_file, "a", encoding="utf-8", newline="\n") as f:
                        f.write(json.dumps(event, ensure_ascii=False) + "\n")

                    self._last_msg_count = current_count

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Watcher error: %s", e)
                await asyncio.sleep(5)

    # ── Monitor delegates ─────────────────────────────────────────

    async def start_monitor(self, mode: str = "project_recent") -> dict:
        """Start the chat monitor."""
        if not self._monitor:
            return {"ok": False, "error": "Monitor not initialized"}
        return await self._monitor.start(mode=mode)

    async def stop_monitor(self) -> dict:
        """Stop the chat monitor."""
        if not self._monitor:
            return {"ok": False, "error": "Monitor not initialized"}
        return await self._monitor.stop()

    def get_monitor_status(self) -> dict:
        """Get monitor status."""
        if not self._monitor:
            return {"running": False, "error": "Monitor not initialized"}
        return self._monitor.get_status()

    def get_monitor_events(self, clear: bool = True) -> list[dict]:
        """Get monitor events."""
        if not self._monitor:
            return []
        return self._monitor.get_events(clear=clear)

    async def monitor_scan_now(self) -> dict:
        """Force immediate monitor scan."""
        if not self._monitor:
            return {"ok": False, "error": "Monitor not initialized"}
        return await self._monitor.scan_now()
