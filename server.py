"""GPT Advisor Agent — HTTP server.

Rewritten 2026-03-22: new endpoints for navigation, tail reading,
images, composer control. Debug endpoints guarded by DEBUG flag.

Usage:
    python server.py

API:
    POST /project/start     — navigate to persistent project chat
    GET  /project/list      — list projects in sidebar
    POST /project/open      — open project by name or URL
    GET  /chat/list         — list chats in current sidebar context
    POST /chat/find-open    — open chat by name/URL, optionally in project
    GET  /chat/current      — info about current chat
    POST /chat/open         — open a specific chat by URL
    POST /chat/rotate       — create new chat, save as persistent
    POST /send              — send message, wait for response
    POST /send-no-wait      — send message, don't wait
    GET  /read              — read messages (no reload, supports last_n)
    GET  /read-tail         — read last N messages
    GET  /read-since        — read messages after index
    GET  /images/list       — list images from recent messages
    POST /images/download   — download image by URL
    POST /images/download-from-message — download image from specific message
    GET  /screenshot        — take screenshot (viewport or stitched)
    POST /upload            — upload file to composer
    GET  /composer/state    — check composer state
    POST /composer/send     — send composer contents
    GET  /status            — check if GPT is generating
    GET  /health            — browser alive? logged in?
    POST /watch/start       — start watcher
    POST /watch/stop        — stop watcher
    GET  /watch/events      — get watcher events
    POST /monitor/start     — start chat monitor (trigger detection)
    POST /monitor/stop      — stop chat monitor
    GET  /monitor/status    — monitor status
    GET  /monitor/events    — get monitor trigger events
    POST /monitor/scan-now  — force immediate scan
"""

import logging
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import uvicorn

import config
from browser import ChatGPTBrowser

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gpt-advisor")

app = FastAPI(title="GPT Advisor Agent", version="2.0")
browser = ChatGPTBrowser()


# ── Schemas ──────────────────────────────────────────────────────────


class SendRequest(BaseModel):
    prompt: str


class UploadRequest(BaseModel):
    file_path: str


class ChatOpenRequest(BaseModel):
    url: str


class ChatFindOpenRequest(BaseModel):
    chat_name: Optional[str] = None
    url: Optional[str] = None
    project_name: Optional[str] = None


class ProjectOpenRequest(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None


class DownloadImageRequest(BaseModel):
    url: str
    save_path: str


class DownloadFromMessageRequest(BaseModel):
    message_index: int
    image_index: int = 0
    save_path: str


class FillFieldRequest(BaseModel):
    selector: str
    value: str


class ClickTextRequest(BaseModel):
    text: str
    tag: str = ""


# ── Project endpoints ────────────────────────────────────────────────


@app.post("/project/start")
async def project_start():
    """Open the persistent shared chat."""
    try:
        result = await browser.navigate_to_project()
        return result
    except Exception as e:
        log.exception("project/start failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/project/list")
async def project_list():
    """List projects visible in sidebar."""
    try:
        projects = await browser.list_projects()
        return {"ok": True, "projects": projects, "count": len(projects)}
    except Exception as e:
        log.exception("project/list failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/project/open")
async def project_open(body: ProjectOpenRequest):
    """Open a project by name or URL."""
    try:
        result = await browser.open_project(name=body.name, url=body.url)
        return result
    except Exception as e:
        log.exception("project/open failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ── Chat endpoints ───────────────────────────────────────────────────


@app.post("/chat/open")
async def chat_open(body: ChatOpenRequest):
    """Open a specific chat by URL."""
    try:
        result = await browser.open_chat(url=body.url)
        if result.get("ok"):
            messages = await browser.read_tail_messages(last_n=10)
            result["messages"] = messages
            result["count"] = len(messages)
        return result
    except Exception as e:
        log.exception("chat/open failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/chat/find-open")
async def chat_find_open(body: ChatFindOpenRequest):
    """Open a chat by name, URL, or within a project."""
    try:
        result = await browser.open_chat(
            chat_name=body.chat_name,
            url=body.url,
            project_name=body.project_name,
        )
        return result
    except Exception as e:
        log.exception("chat/find-open failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/chat/list")
async def chat_list():
    """List chats in current sidebar context."""
    try:
        chats = await browser.list_chats()
        return {"ok": True, "chats": chats, "count": len(chats)}
    except Exception as e:
        log.exception("chat/list failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/chat/current")
async def chat_current():
    """Get info about the current chat."""
    try:
        info = await browser.get_current_chat_info()
        return {"ok": True, **info}
    except Exception as e:
        log.exception("chat/current failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/chat/rotate")
async def chat_rotate():
    """Create a new chat and save as persistent."""
    try:
        await browser.stop_watching()
        result = await browser.navigate_to_project(new_chat=True)
        page_url = browser._page.url
        browser._save_persistent_chat_url(page_url)
        return {"ok": True, "new_url": page_url}
    except Exception as e:
        log.exception("chat/rotate failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ── Chat sync ────────────────────────────────────────────────────────


@app.post("/chat/sync")
async def chat_sync():
    """Safe-sync current chat to pick up messages from other devices."""
    try:
        result = await browser.sync_chat()
        return result
    except Exception as e:
        log.exception("chat/sync failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ── Message reading ──────────────────────────────────────────────────


@app.get("/read")
async def read_messages(refresh: bool = False, last_n: int = 0,
                        include_images: bool = False, sync: bool = False):
    """Read messages. Supports last_n, include_images, and sync.

    sync=true: safe re-navigate to pull external messages before reading.
    """
    try:
        messages = await browser.read_all_messages(
            refresh=refresh, last_n=last_n, include_images=include_images,
            sync=sync,
        )
        return {"ok": True, "messages": messages, "count": len(messages)}
    except Exception as e:
        log.exception("read failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/read-tail")
async def read_tail(last_n: int = 20, include_images: bool = False,
                    sync: bool = False):
    """Read last N messages. sync=true pulls external messages first."""
    try:
        messages = await browser.read_tail_messages(
            last_n=last_n, include_images=include_images, sync=sync,
        )
        return {"ok": True, "messages": messages, "count": len(messages)}
    except Exception as e:
        log.exception("read-tail failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/read-since")
async def read_since(after_index: int, include_images: bool = False):
    """Read messages after a specific index."""
    try:
        messages = await browser.read_messages_since(
            after_index=after_index, include_images=include_images
        )
        return {"ok": True, "messages": messages, "count": len(messages)}
    except Exception as e:
        log.exception("read-since failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ── Send ─────────────────────────────────────────────────────────────


@app.post("/send")
async def send_message(body: SendRequest):
    """Send message and wait for GPT response."""
    try:
        result = await browser.send_message(body.prompt, wait_for_response=True)
        return result
    except Exception as e:
        log.exception("send failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/send-no-wait")
async def send_no_wait(body: SendRequest):
    """Send message without waiting for response."""
    try:
        result = await browser.send_message(body.prompt, wait_for_response=False)
        return result
    except Exception as e:
        log.exception("send-no-wait failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ── Images ───────────────────────────────────────────────────────────


@app.get("/images/list")
async def images_list(last_n: int = 30):
    """List images from the last N messages."""
    try:
        images = await browser.list_images(last_n=last_n)
        return {"ok": True, "images": images, "count": len(images)}
    except Exception as e:
        log.exception("images/list failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/images/download")
async def images_download(body: DownloadImageRequest):
    """Download an image by URL through authenticated session."""
    try:
        result = await browser.download_image(body.url, body.save_path)
        return result
    except Exception as e:
        log.exception("images/download failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/images/download-from-message")
async def images_download_from_message(body: DownloadFromMessageRequest):
    """Download a specific image from a specific message."""
    try:
        result = await browser.download_image_from_message(
            body.message_index, body.image_index, body.save_path
        )
        return result
    except Exception as e:
        log.exception("images/download-from-message failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ── Screenshots ──────────────────────────────────────────────────────


@app.get("/screenshot")
async def screenshot_chat(mode: str = "viewport"):
    """Take a screenshot. Modes: viewport (default), stitched."""
    try:
        save_path = os.path.join(os.path.dirname(__file__), "screenshot.png")
        result = await browser.screenshot_chat(save_path, mode=mode)
        return result
    except Exception as e:
        log.exception("screenshot failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ── Upload & Composer ────────────────────────────────────────────────


@app.post("/upload")
async def upload_file(body: UploadRequest):
    """Upload a file to the current chat composer."""
    try:
        result = await browser.upload_file(body.file_path)
        return result
    except Exception as e:
        log.exception("upload failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/composer/state")
async def composer_state():
    """Check what's in the composer right now."""
    try:
        state = await browser.get_composer_state()
        return {"ok": True, **state}
    except Exception as e:
        log.exception("composer/state failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/composer/send")
async def composer_send():
    """Send whatever is currently in the composer."""
    try:
        result = await browser.composer_send()
        return result
    except Exception as e:
        log.exception("composer/send failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ── Status & Health ──────────────────────────────────────────────────


@app.get("/status")
async def get_status():
    """Check if GPT is generating, message count, etc."""
    try:
        status = await browser.get_status()
        return {"ok": True, **status}
    except Exception as e:
        log.exception("status failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/health")
async def health_check():
    """Check if browser is alive and logged in."""
    try:
        logged_in = await browser.is_logged_in()
        status = await browser.get_status()
        return {
            "ok": True,
            "logged_in": logged_in,
            "status": "ready" if logged_in else "login_required",
            "url": status.get("url", ""),
            "message_count": status.get("message_count", 0),
        }
    except Exception as e:
        log.exception("health check failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ── Watcher ──────────────────────────────────────────────────────────


@app.post("/watch/start")
async def watch_start():
    """Start watching for external messages."""
    try:
        result = await browser.start_watching()
        return result
    except Exception as e:
        log.exception("watch/start failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/watch/stop")
async def watch_stop():
    """Stop the background watcher."""
    try:
        result = await browser.stop_watching()
        return result
    except Exception as e:
        log.exception("watch/stop failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/watch/events")
async def watch_events(clear: bool = True):
    """Get accumulated watcher events."""
    events = browser.get_events(clear=clear)
    return {"ok": True, "events": events, "count": len(events)}


# ── Monitor ─────────────────────────────────────────────────────


class MonitorStartRequest(BaseModel):
    mode: str = "project_recent"


@app.post("/monitor/start")
async def monitor_start(body: MonitorStartRequest):
    """Start the chat monitor."""
    try:
        result = await browser.start_monitor(mode=body.mode)
        return result
    except Exception as e:
        log.exception("monitor/start failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/monitor/stop")
async def monitor_stop():
    """Stop the chat monitor."""
    try:
        result = await browser.stop_monitor()
        return result
    except Exception as e:
        log.exception("monitor/stop failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.get("/monitor/status")
async def monitor_status():
    """Get monitor status."""
    status = browser.get_monitor_status()
    return {"ok": True, **status}


@app.get("/monitor/events")
async def monitor_events(clear: bool = True):
    """Get accumulated monitor events."""
    events = browser.get_monitor_events(clear=clear)
    return {"ok": True, "events": events, "count": len(events)}


@app.post("/monitor/scan-now")
async def monitor_scan_now():
    """Force immediate scan of all monitored chats."""
    try:
        result = await browser.monitor_scan_now()
        return result
    except Exception as e:
        log.exception("monitor/scan-now failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ── Debug endpoints (guarded by DEBUG flag) ──────────────────────────


@app.get("/debug/dom")
async def debug_dom(selector: str = "nav"):
    """Inspect page DOM. Only available when DEBUG=True."""
    if not config.DEBUG:
        return JSONResponse(status_code=403,
                            content={"ok": False, "error": "Debug endpoints disabled. Set GPT_ADVISOR_DEBUG=1"})
    js = f"""
    (() => {{
        const els = document.querySelectorAll('{selector}');
        const results = [];
        els.forEach((el, i) => {{
            results.push({{
                index: i,
                tag: el.tagName,
                id: el.id,
                className: el.className?.substring?.(0, 100) || '',
                text: el.innerText?.substring(0, 500) || '',
                href: el.href || null,
                childCount: el.children.length,
            }});
        }});
        return results;
    }})()
    """
    result = await browser.eval_js(js)
    return {"ok": True, "selector": selector, "result": result}


@app.get("/debug/js")
async def debug_js(code: str):
    """Run arbitrary JS. Only available when DEBUG=True."""
    if not config.DEBUG:
        return JSONResponse(status_code=403,
                            content={"ok": False, "error": "Debug endpoints disabled. Set GPT_ADVISOR_DEBUG=1"})
    result = await browser.eval_js(code)
    return {"ok": True, "result": result}


@app.post("/debug/fill-field")
async def debug_fill_field(body: FillFieldRequest):
    """Fill a text field. Only available when DEBUG=True."""
    if not config.DEBUG:
        return JSONResponse(status_code=403,
                            content={"ok": False, "error": "Debug endpoints disabled"})
    try:
        page = browser._page
        field = page.locator(body.selector)
        await field.fill(body.value)
        await page.wait_for_timeout(500)
        return {"ok": True, "selector": body.selector, "length": len(body.value)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/debug/click-text")
async def debug_click_text(body: ClickTextRequest):
    """Click element by text. Only available when DEBUG=True."""
    if not config.DEBUG:
        return JSONResponse(status_code=403,
                            content={"ok": False, "error": "Debug endpoints disabled"})
    try:
        page = browser._page
        if body.tag:
            loc = page.get_by_role(body.tag, name=body.text) if body.tag in ("button", "link") \
                else page.locator(f"{body.tag}:has-text('{body.text}')")
        else:
            loc = page.get_by_text(body.text, exact=False)
        count = await loc.count()
        if count == 0:
            return JSONResponse(status_code=404,
                                content={"ok": False, "error": f"No element with text '{body.text}'"})
        await loc.first.click()
        await page.wait_for_timeout(1000)
        return {"ok": True, "text": body.text, "matched": count}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/sources/add-text")
async def sources_add_text(body: dict):
    """Add a text source to project. Only available when DEBUG=True."""
    if not config.DEBUG:
        return JSONResponse(status_code=403,
                            content={"ok": False, "error": "Debug endpoints disabled"})
    try:
        page = browser._page
        title = body.get("title", "Untitled")
        content = body.get("content", "")
        btn = page.locator("button.btn").filter(has_text="Добавить источники")
        await btn.click()
        await page.wait_for_timeout(2000)
        text_btn = page.get_by_text("Ввод текста", exact=True)
        await text_btn.wait_for(timeout=5000)
        await text_btn.click()
        await page.wait_for_timeout(2000)
        title_input = page.locator("#project-text-title")
        await title_input.wait_for(timeout=5000)
        await title_input.fill(title)
        await page.wait_for_timeout(500)
        content_input = page.locator("#project-text-content")
        await content_input.fill(content)
        await page.wait_for_timeout(500)
        save_btn = page.get_by_text("Сохранить", exact=False)
        await save_btn.click()
        await page.wait_for_timeout(3000)
        return {"ok": True, "title": title, "content_length": len(content)}
    except Exception as e:
        log.exception("sources/add-text failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/debug/upload-to-input")
async def debug_upload_to_input(body: UploadRequest):
    """Upload file to file input. Only available when DEBUG=True."""
    if not config.DEBUG:
        return JSONResponse(status_code=403,
                            content={"ok": False, "error": "Debug endpoints disabled"})
    try:
        page = browser._page
        file_input = page.locator("#upload-files")
        await file_input.set_input_files(body.file_path)
        await page.wait_for_timeout(3000)
        return {"ok": True, "file": body.file_path}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


# ── Lifecycle ────────────────────────────────────────────────────────


@app.on_event("startup")
async def on_startup():
    log.info("Starting GPT Advisor Agent v2.0...")
    if config.DEBUG:
        log.warning("DEBUG mode enabled — debug endpoints are accessible")
    await browser.start()
    try:
        result = await browser.navigate_to_project()
        log.info("Auto-opened chat: %s", result)
    except Exception:
        log.warning("Auto-open failed, navigating to ChatGPT homepage")
        try:
            await browser._page.goto(config.CHATGPT_URL, wait_until="domcontentloaded",
                                      timeout=config.NAVIGATION_TIMEOUT)
        except Exception:
            log.warning("Initial navigation to ChatGPT timed out (may need login)")
    log.info("Server ready at http://%s:%d", config.HOST, config.PORT)


@app.on_event("shutdown")
async def on_shutdown():
    log.info("Shutting down...")
    await browser.stop()


# ── Main ─────────────────────────────────────────────────────────────


if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host=config.HOST,
        port=config.PORT,
        log_level="info",
    )
