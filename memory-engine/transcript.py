# -*- coding: utf-8 -*-
"""Разбор транскриптов Claude Code и Codex (.jsonl) — общая утилита слоя непрерывности.

Оба агента пишут построчный JSON, но схемы разные:
  Claude — {type: user|assistant, message:{content:[...]}}
  Codex  — {type: event_msg|response_item, payload:{type: user_message|agent_message|...}}
Наружу отдаём один и тот же словарь (см. parse), чтобы резюме, лента и поиск
работали по общей истории обоих агентов.
"""
import json, os, re, glob, datetime

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")


def iter_records(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _text_of(content):
    """message.content → плоский текст (без thinking/base64)."""
    if isinstance(content, str):
        return content
    out = []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                out.append(b.get("text", ""))
            elif t == "tool_use":
                name = b.get("name", "")
                inp = b.get("input", {}) or {}
                bits = []
                for k in ("file_path", "command", "pattern", "url", "path", "description", "prompt"):
                    v = inp.get(k)
                    if isinstance(v, str) and v.strip():
                        bits.append(f"{k}={v[:160]}")
                out.append(f"[{name}] " + "; ".join(bits))
            elif t == "tool_result":
                pass  # результаты не тянем — шумно и объёмно
    return "\n".join(x for x in out if x)


NOISE = (
    "<local-command-caveat>", "<command-name>", "<system-reminder>",
    "<local-command-stdout>", "[Image:", "Caveat: The messages below",
    # служебные прогоны самого слоя непрерывности (claude -p для сжатия сессии) —
    # иначе резюме индексируется как «реплика Рувима» и всплывает в подхвате
    "Ниже — скелет рабочей сессии", "=== СКЕЛЕТ ===",
)

# Служебные вставки внутри реплик: вырезаем, но саму реплику сохраняем —
# рядом с ними часто стоит живой текст Рувима.
SCRUB = [
    re.compile(r"<task-notification>.*?</task-notification>", re.S),
    re.compile(r"<(task-id|tool-use-id|output-file|status|summary)>.*?</\1>", re.S),
    re.compile(r"This session is being continued from a previous conversation.*?Summary:", re.S),
    re.compile(r"[A-Za-z]:\\\\?[\w\\\-\. ]{20,}"),          # длинные windows-пути
    re.compile(r"@?\"?[A-Za-z]:[\\/][^\s\"]{20,}\"?"),
    re.compile(r"https?://\S{40,}"),
]


ORPHAN = re.compile(r"@?\"\s*\"|@\s")   # кавычки, осиротевшие после вырезания путей


def scrub(txt):
    for rx in SCRUB:
        txt = rx.sub(" ", txt)
    txt = ORPHAN.sub(" ", txt)
    return re.sub(r"\s{2,}", " ", txt).strip()


def is_codex(path):
    return os.path.basename(path).startswith("rollout-")


def parse(path, max_user=400):
    """Скелет сессии: заголовок, время, реплики пользователя, действия."""
    if is_codex(path):
        return parse_codex(path, max_user)
    title = None
    users, actions, assistant_tail = [], [], []
    ts_first = ts_last = None
    models = set()
    for d in iter_records(path):
        t = d.get("type")
        if t == "ai-title" and d.get("aiTitle"):
            title = d["aiTitle"]
            continue
        ts = d.get("timestamp")
        if ts:
            ts_first = ts_first or ts
            ts_last = ts
        msg = d.get("message") or {}
        if t == "user" and not d.get("isSidechain"):
            txt = _text_of(msg.get("content"))
            if not txt or any(n in txt for n in NOISE):
                continue
            txt = scrub(txt)
            if len(txt) > 8:
                users.append((ts, txt[:1500]))
        elif t == "assistant" and not d.get("isSidechain"):
            if msg.get("model"):
                models.add(msg["model"])
            txt = _text_of(msg.get("content"))
            if not txt:
                continue
            for line in txt.splitlines():
                if line.startswith("["):
                    actions.append(line[:200])
                elif line.strip():
                    assistant_tail.append(line.strip()[:400])
    return {
        "path": path,
        "session_id": os.path.basename(path).replace(".jsonl", ""),
        "title": title,
        "started": ts_first,
        "ended": ts_last,
        "models": sorted(models),
        "users": users[:max_user],
        "user_count": len(users),
        "actions": actions,
        "assistant_tail": assistant_tail[-40:],
    }


CODEX_ID = re.compile(r"rollout-.*?-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$", re.I)
# Служебные вставки Codex приходят как обычные user-сообщения — их в реплики Рувима не берём.
CODEX_NOISE = ("<recommended_plugins>", "<permissions instructions>", "<skills_instructions>",
               "<user_instructions>", "<environment_context>", "## My request for Codex:")


def parse_codex(path, max_user=400):
    """То же, что parse, но для rollout-файла Codex."""
    users, actions, assistant_tail = [], [], []
    ts_first = ts_last = None
    cwd = ""
    models = set()
    for d in iter_records(path):
        ts = d.get("timestamp")
        if ts:
            ts_first = ts_first or ts
            ts_last = ts
        t, pl = d.get("type"), d.get("payload") or {}
        if t == "session_meta":
            cwd = (pl.get("cwd") or "").replace("\\\\?\\", "")
            if pl.get("model"):
                models.add(pl["model"])
        elif t == "turn_context" and pl.get("model"):
            models.add(pl["model"])
        elif t == "event_msg":
            kind = pl.get("type")
            txt = (pl.get("message") or "").strip()
            if not txt:
                continue
            if kind == "user_message":
                if any(n in txt for n in CODEX_NOISE):
                    continue
                txt = scrub(txt)
                if len(txt) > 8:
                    users.append((ts, txt[:1500]))
            elif kind == "agent_message":
                assistant_tail.append(txt[:400])
        elif t == "response_item":
            kind = pl.get("type")
            if kind == "custom_tool_call":
                inp = pl.get("input")
                cmd = inp if isinstance(inp, str) else json.dumps(inp, ensure_ascii=False)
                actions.append(f"[{pl.get('name') or 'tool'}] command={cmd[:160]}")
            elif kind == "function_call":
                args = pl.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                actions.append(f"[{pl.get('name') or 'call'}] {args[:160]}")
    m = CODEX_ID.search(os.path.basename(path))
    sid = (m.group(1) if m else os.path.basename(path))[:36]
    return {
        "path": path,
        "session_id": sid,
        "agent": "codex",
        "project": "codex-" + (os.path.basename(cwd.rstrip("\\/")) or "home"),
        "title": (users[0][1].lstrip("# ").splitlines()[0][:80] if users else None),
        "started": ts_first,
        "ended": ts_last,
        "models": sorted(models),
        "users": users[:max_user],
        "user_count": len(users),
        "actions": actions,
        "assistant_tail": assistant_tail[-40:],
    }


def tool_summary(actions, top=12):
    """Частоты инструментов + затронутые файлы."""
    tools, files = {}, {}
    for a in actions:
        m = re.match(r"\[(\w+)\]", a)
        if m:
            tools[m.group(1)] = tools.get(m.group(1), 0) + 1
        for fm in re.finditer(r"file_path=([^;]+)", a):
            p = fm.group(1).strip()
            files[p] = files.get(p, 0) + 1
    return (
        sorted(tools.items(), key=lambda x: -x[1])[:top],
        sorted(files.items(), key=lambda x: -x[1])[:top],
    )


def project_dirs():
    return [d for d in glob.glob(os.path.join(PROJECTS_DIR, "*")) if os.path.isdir(d)]


def codex_transcripts(days=30):
    """Rollout-файлы Codex за N дней (~/.codex/sessions/ГГГГ/ММ/ДД/rollout-*.jsonl)."""
    cutoff = datetime.datetime.now().timestamp() - days * 86400
    out = []
    for p in glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", "rollout-*.jsonl")):
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_mtime >= cutoff and st.st_size > 2000:
            out.append((st.st_mtime, st.st_size, p))
    return out


def transcripts(days=30, project=None, include_codex=True):
    """Транскрипты за N дней по всем (или одному) проектам, свежие первыми.
    Сюда же подмешиваются сессии Codex — память и поиск у агентов общие."""
    cutoff = datetime.datetime.now().timestamp() - days * 86400
    out = []
    dirs = [os.path.join(PROJECTS_DIR, project)] if project else project_dirs()
    for d in dirs:
        for p in glob.glob(os.path.join(d, "*.jsonl")):
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime >= cutoff and st.st_size > 2000:
                out.append((st.st_mtime, st.st_size, p))
    if include_codex and not project:
        out.extend(codex_transcripts(days))
    out.sort(reverse=True)
    return out


def human_dt(ts):
    if not ts:
        return "?"
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts[:16]
