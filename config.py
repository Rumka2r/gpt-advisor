"""GPT Advisor Agent — configuration."""

import os

# Server
HOST = "127.0.0.1"
PORT = 8765

# Debug mode — enables /debug/* endpoints
DEBUG = os.environ.get("GPT_ADVISOR_DEBUG", "").lower() in ("1", "true", "yes")

# Browser
BROWSER_DATA_DIR = os.path.join(os.path.dirname(__file__), "browser-data")
CHATGPT_URL = "https://chatgpt.com"
HEADLESS = False  # Must be False for ChatGPT (Cloudflare + login)

# Timeouts (ms)
NAVIGATION_TIMEOUT = 30_000
RESPONSE_POLL_INTERVAL = 2_000  # check every 2s if GPT finished
RESPONSE_MAX_WAIT = 300_000     # 5 min max wait, then notify user
LONG_WAIT_THRESHOLD = 120_000   # notify after 2 min

# Stability: how many consecutive stable polls before declaring response complete
RESPONSE_STABLE_CYCLES = 2

# Project
PROJECT_NAME = "бот"
PROJECT_URL = "https://chatgpt.com/g/g-p-698fa0794eb881918de3651fc2a395a4/project"

# Persistent chat — the ONE shared chat for user + architect + GPT
# Updated by /chat/rotate endpoint
PERSISTENT_CHAT_URL_FILE = os.path.join(os.path.dirname(__file__), "active-chat-url.txt")

# Python executable (for subprocess if needed)
PYTHON_EXE = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Programs", "Python", "Python313", "python.exe",
)

# ── Monitor ─────────────────────────────────────────────────────
# Tier 1: active chat polling interval (seconds)
MONITOR_ACTIVE_POLL_SEC = 3
# Tier 2: recent chats polling interval (seconds)
MONITOR_RECENT_POLL_SEC = 8
# How many recent chats to scan in tier 2
MONITOR_RECENT_CHAT_LIMIT = 10
# How many messages to read from tail when checking a chat
MONITOR_TAIL_SIZE = 5
# Auto-open chat when trigger is found
MONITOR_AUTO_OPEN = True
# Projects to monitor (empty = current project only)
MONITOR_PROJECTS = []
# Events log file
MONITOR_EVENTS_FILE = os.path.join(os.path.dirname(__file__), "monitor-events.jsonl")
# Telegram notify on architect_call
MONITOR_TELEGRAM_NOTIFY = True
TELEGRAM_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "7941518546:AAE3n14JXGBDlJ9WbgPcRrqMSR4620-YOik")
TELEGRAM_CHAT_ID = os.environ.get("ERROR_NOTIFY_CHAT_ID", "6048652039")
