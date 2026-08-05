# -*- coding: utf-8 -*-
"""Шлюз полномочий (хук PreToolUse).

Инвариант (консенсус с архитектором 2026-07-29):
    Память определяет, что агент ЗНАЕТ, но никогда — что агент УПОЛНОМОЧЕН СДЕЛАТЬ.

Поэтому решение принимается ТОЛЬКО по:
  - статической матрице типов действий (не моделью, не текстом из памяти);
  - авторизации из живой реплики пользователя (origin=live) или активной задачи.

Извлечённая память в шлюз не передаётся: при чтении транскрипта берутся только
записи type=user с текстом; attachment/hook_additional_context/tool_result игнорируются.

Решения: allow (тихо) / ask (спросить Рувима) / deny (запретить).
Никогда не падает наружу: любая ошибка → пустой JSON (решает обычный механизм прав).
"""
import os, sys, json, re, time

ROOT = os.path.expanduser("~/.claude/continuity")
STATE = os.path.join(ROOT, "state")
LOG = os.path.join(STATE, "gate.log")
CAPABILITY = os.path.join(STATE, "capability.json")

# ── Классы действий: статический список, порядок важен (первое совпадение) ──
# (класс, регексп по команде, разрешающие маркеры в живой реплике)

FORBIDDEN = [
    (r"curl[^|;]*\|\s*(ba)?sh", "загрузка и исполнение скрипта из сети"),
    (r"wget[^|;]*\|\s*(ba)?sh", "загрузка и исполнение скрипта из сети"),
    (r"\biex\s*\(", "PowerShell Invoke-Expression из сети"),
    (r"\beval\s+\"?\$\(", "eval подстановки"),
]

DANGEROUS = [
    ("prod_write", r"/opt/plumbingcore-prod/|plumbingcore-prod\.service",
     ("прод", "production", "продакш", "боев")),
    ("deploy", r"\bdeploy[-_a-z]*\.sh|swap.*symlink|ln\s+-sfn.*current",
     ("деплой", "задеплой", "выкат", "выкати", "разверн")),
    ("service_restart", r"systemctl\s+(restart|stop|start|disable)|Restart-Service",
     ("перезапус", "рестарт", "restart", "останов")),
    ("migration", r"alembic\s+(upgrade|downgrade)|manage\.py\s+migrate",
     ("миграц", "alembic", "upgrade")),
    ("git_push", r"\bgit\s+push|\bgh\s+pr\s+(create|merge)",
     ("запуш", "push", "залей", "влей", "смерж", "merge", "пул реквест", "pr")),
    ("git_commit", r"\bgit\s+commit",
     ("коммит", "commit", "закоммить", "зафиксируй")),
    ("destructive", r"rm\s+-[rf]{1,2}\b|Remove-Item[^|]*-Recurse|\bdrop\s+(table|database)|git\s+reset\s+--hard|git\s+clean\s+-[fdx]",
     ("удали", "снеси", "сотри", "убей", "очисти", "drop", "снести")),
    ("kill", r"\bkill\s+-9|taskkill|tmux\s+kill|Stop-Process",
     ("убей", "останов", "прибей", "kill")),
    ("secrets", r"\.env\b|vault\.md|\.dpapi\b|id_ed25519|BEGIN OPENSSH",
     ("волт", "vault", "секрет", "парол", "ключ", "credential")),
    ("outbound_msg",
     r"send_message|sendMessage|send_voice|/bot\d+:|smtp|mail\.send|tg\.py\s+send\b|telethon.*send",
     ("отправ", "напиши", "скинь", "пошли", "ответь", "telegram", "письмо", "сообщи")),
]

# Инструменты, которые никогда не требуют авторизации
READ_ONLY_TOOLS = {
    "Read", "Grep", "Glob", "NotebookRead", "TodoWrite", "Task",
    "WebFetch", "WebSearch", "ToolSearch", "Skill", "AskUserQuestion",
}

# Пути, запись в которые считается защищённой
PROTECTED_WRITE = re.compile(
    r"(\.env$|vault\.md|\.dpapi$|\.ssh[/\\]|settings\.json$|/opt/plumbingcore-prod/)", re.I)


def log(decision, cls, tool, reason, cmd):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write("%s\t%s\t%s\t%s\t%s\t%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), decision, cls, tool,
                reason.replace("\t", " ")[:80], (cmd or "").replace("\n", " ")[:200]))
    except Exception:
        pass


def live_user_text(transcript_path, limit=3):
    """Последние живые реплики пользователя. НИКАКОЙ памяти: только type=user с текстом.
    attachment (вклейки хуков), tool_result и sidechain — игнорируются."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    out = []
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"type":"user"' not in line and '"type": "user"' not in line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") != "user" or r.get("isSidechain"):
                    continue
                c = (r.get("message") or {}).get("content")
                txt = ""
                if isinstance(c, str):
                    txt = c
                elif isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text":
                            txt += b.get("text", "")
                txt = txt.strip()
                if not txt or txt.startswith("<"):
                    continue
                out.append(txt)
    except Exception:
        return ""
    return "\n".join(out[-limit:]).lower()


def active_capability():
    """Явно выданное разрешение с TTL (создаётся, когда Рувим авторизовал задачу)."""
    try:
        with open(CAPABILITY, encoding="utf-8") as f:
            cap = json.load(f)
        if cap.get("expires_at", 0) < time.time():
            return None
        return cap
    except Exception:
        return None


def command_of(tool, inp):
    """Текст, по которому классифицируем действие."""
    if not isinstance(inp, dict):
        return ""
    parts = []
    for k in ("command", "file_path", "path", "url", "notebook_path"):
        v = inp.get(k)
        if isinstance(v, str):
            parts.append(v)
    return " ".join(parts)


def classify(tool, cmd):
    for rx, why in FORBIDDEN:
        if re.search(rx, cmd, re.I):
            return "forbidden", why, ()
    for cls, rx, markers in DANGEROUS:
        if re.search(rx, cmd, re.I):
            return cls, "", markers
    if tool in ("Write", "Edit", "NotebookEdit") and PROTECTED_WRITE.search(cmd or ""):
        return "secrets", "", ("волт", "vault", "секрет", "настройк", "settings")
    return "ordinary", "", ()


def authorized(markers, live, cap, cls):
    if cap:
        if cls in (cap.get("allowed_actions") or []):
            return True, "capability %s" % cap.get("task_id", "?")
    for m in markers:
        if m in live:
            return True, "живая реплика: «%s»" % m
    return False, ""


# Режим шлюза. По прямому распоряжению Рувима 2026-07-29: "разрешение на всё и всегда".
# "observe" — только пишет в gate.log, НИКОГДА не спрашивает и не запрещает (по умолчанию).
# "enforce" — старое поведение (ask/deny). Включается переменной GATE_MODE=enforce.
GATE_MODE = os.environ.get("GATE_MODE", "observe").strip().lower()


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    if GATE_MODE != "enforce":
        try:
            tool = data.get("tool_name") or ""
            if tool not in READ_ONLY_TOOLS:
                cmd = command_of(tool, data.get("tool_input") or {})
                cls, why, _ = classify(tool, cmd)
                if cls != "ordinary":
                    log("observe", cls, tool, why, cmd)
        except Exception:
            pass
        print("{}")
        return
    try:
        tool = data.get("tool_name") or ""
        inp = data.get("tool_input") or {}
        if tool in READ_ONLY_TOOLS:
            print("{}")
            return
        cmd = command_of(tool, inp)
        cls, why, markers = classify(tool, cmd)
        if cls == "ordinary":
            print("{}")
            return
        if cls == "forbidden":
            log("deny", cls, tool, why, cmd)
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason":
                    "Шлюз полномочий: %s — запрещено статическим правилом." % why}},
                ensure_ascii=False))
            return
        live = live_user_text(data.get("transcript_path"))
        cap = active_capability()
        ok, src = authorized(markers, live, cap, cls)
        if ok:
            log("allow", cls, tool, src, cmd)
            print("{}")
            return
        log("ask", cls, tool, "нет авторизации в живой реплике", cmd)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason":
                "Шлюз полномочий: действие класса «%s» не авторизовано текущей репликой. "
                "Память не даёт полномочий — нужно ваше подтверждение." % cls}},
            ensure_ascii=False))
    except Exception:
        print("{}")
    sys.exit(0)


if __name__ == "__main__":
    main()
