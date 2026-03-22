"""CSS selectors for ChatGPT UI.

All selectors in one place — easy to update when ChatGPT changes its UI.
Each selector has a primary + fallback chain for resilience.
Last verified: 2026-03-22
"""

# ── Fallback chains ──────────────────────────────────────────────
# Each key maps to a list of selectors tried in order.
# First match wins. If all fail, the operation logs a warning.

INPUT_CHAIN = [
    "div#prompt-textarea[contenteditable='true']",
    "#prompt-textarea",
    "div[contenteditable='true'][data-placeholder]",
    "[contenteditable='true']",
]

SEND_BUTTON_CHAIN = [
    'button[data-testid="send-button"]',
    'button[aria-label="Send prompt"]',
    'button[aria-label="Отправить запрос"]',
    'form button[type="button"]:not([data-testid="stop-button"])',
]

STOP_BUTTON_CHAIN = [
    'button[data-testid="stop-button"]',
    'button[aria-label="Stop generating"]',
    'button[aria-label="Остановить генерацию"]',
]

ASSISTANT_MESSAGE_CHAIN = [
    'div[data-message-author-role="assistant"]',
    '[data-message-author-role="assistant"]',
]

ALL_MESSAGES_CHAIN = [
    'div[data-message-author-role]',
    '[data-message-author-role]',
]

SIDEBAR_TOGGLE_CHAIN = [
    'button[aria-label="Open sidebar"]',
    'button[aria-label="Close sidebar"]',
    'button[aria-label="Открыть боковую панель"]',
    'button[aria-label="Закрыть боковую панель"]',
]

FILE_INPUT_CHAIN = [
    'input[type="file"]',
    'input[accept*="image"]',
]

NOT_LOGGED_IN_CHAIN = [
    'button:has-text("Log in")',
    'button:has-text("Войти")',
    'button:has-text("Sign up")',
    'button:has-text("Зарегистрироваться")',
]

# ── Simple selectors (no fallback needed) ────────────────────────
# These use data-* attributes that are stable, or are structural queries.

# Primary selectors (kept for backward compatibility)
INPUT = "div#prompt-textarea[contenteditable='true']"
SEND_BUTTON = 'button[data-testid="send-button"]'
STOP_BUTTON = 'button[data-testid="stop-button"]'
ASSISTANT_MESSAGE = 'div[data-message-author-role="assistant"]'
USER_MESSAGE = 'div[data-message-author-role="user"]'
ALL_MESSAGES = 'div[data-message-author-role]'
THINKING_INDICATOR = 'div[data-message-author-role="assistant"] details summary'
SIDEBAR_PROJECT = 'nav a[href*="/project"]'
NEW_CHAT_BUTTON = 'a[data-testid="create-new-chat-button"]'
SIDEBAR_TOGGLE = 'button[aria-label="Open sidebar"], button[aria-label="Close sidebar"]'
CHAT_TITLE = 'nav ol li a'
FILE_INPUT = 'input[type="file"]'
FILE_UPLOAD_BUTTON = 'button:has-text("Add files")'
NOT_LOGGED_IN_BUTTONS = 'button:has-text("Войти"), button:has-text("Log in"), button:has-text("Sign up")'
MODEL_SELECTOR = 'button[data-testid="model-selector"]'
COMPOSER_ATTACHMENTS = '#prompt-textarea ~ div [data-testid], .composer-attachments, div[class*="attachment"]'
SIDEBAR_CHAT_LINKS = 'nav a[href*="/c/"]'
SIDEBAR_PROJECT_LINKS = 'nav a[href*="/g/"]'
