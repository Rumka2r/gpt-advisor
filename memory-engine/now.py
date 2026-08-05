# -*- coding: utf-8 -*-
"""Слой «сейчас»: лента последних сессий + открытые нити + контекст для SessionStart.

  python now.py recent            — пересобрать RECENT.md из sessions/*.md
  python now.py context           — напечатать блок непрерывности (текст)
  python now.py hook              — то же в формате SessionStart-хука (JSON)
  python now.py thread add "..."  — добавить открытую нить
  python now.py thread done N     — закрыть нить номер N
  python now.py threads           — показать нити
"""
import os, re, sys, json, glob, datetime, argparse

ROOT = os.path.expanduser("~/.claude/continuity")
SESSIONS = os.path.join(ROOT, "sessions")
STATE = os.path.join(ROOT, "state")
RECENT = os.path.join(STATE, "RECENT.md")
THREADS = os.path.join(STATE, "THREADS.md")

MAX_CONTEXT = 3200          # символов в инжектируемом блоке
RECENT_IN_CONTEXT = 4       # сколько последних сессий показывать
THREADS_IN_CONTEXT = 10


def short_project(p, agent=""):
    """C--Users-andri-work-osha → osha; сессии Codex помечаем отдельно."""
    if not p:
        return "?"
    short = p.rstrip("-").split("-")[-1] or p
    if agent == "codex" or p.startswith("codex-"):
        return "codex/" + short
    return short


def _fm(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip('"')
    return fm, text[m.end():]


def _section(body, name):
    m = re.search(rf"^## {name}\s*\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if line.startswith("-"):
            line = line.lstrip("- ").strip()
            if line and line.lower() not in ("нет", "нет."):
                out.append(line)
    return out


def load_sessions():
    items = []
    for p in sorted(glob.glob(os.path.join(SESSIONS, "*.md"))):
        try:
            txt = open(p, encoding="utf-8").read()
        except OSError:
            continue
        fm, body = _fm(txt)
        items.append({
            "path": p,
            "date": fm.get("date", os.path.basename(p)[:10]),
            "started": fm.get("started", ""),
            "ended": fm.get("ended", ""),
            "title": fm.get("title") or "(без темы)",
            "project": fm.get("project", ""),
            "agent": fm.get("agent", "claude"),
            "turns": fm.get("turns", "?"),
            "sut": _section(body, "Суть"),
            "itog": _section(body, "Итог"),
            "open": _section(body, "Открыто"),
        })
    items.sort(key=lambda x: (x["ended"] or x["date"]), reverse=True)
    return items


def build_recent():
    os.makedirs(STATE, exist_ok=True)
    items = load_sessions()
    lines = ["# Лента сессий (авто, не редактировать руками)", ""]
    for it in items[:40]:
        lines.append(f"## {it['ended'] or it['date']} — {it['title']}")
        for s in it["sut"][:4]:
            lines.append(f"- {s}")
        for s in it["itog"][:2]:
            lines.append(f"- итог: {s}")
        for s in it["open"][:3]:
            lines.append(f"- ⏳ {s}")
        lines.append("")
    open(RECENT, "w", encoding="utf-8").write("\n".join(lines))
    return len(items)


def read_threads():
    if not os.path.exists(THREADS):
        return []
    out = []
    for line in open(THREADS, encoding="utf-8"):
        line = line.rstrip()
        if line.startswith("- "):
            out.append(line[2:])
    return out


def write_threads(items):
    os.makedirs(STATE, exist_ok=True)
    head = ("# Открытые нити\n"
            "<!-- Одна строка = одно живое дело. Правит ассистент по ходу работы. -->\n\n")
    open(THREADS, "w", encoding="utf-8").write(head + "\n".join(f"- {i}" for i in items) + "\n")


def context_text():
    items = load_sessions()
    parts = []
    threads = read_threads()
    if threads:
        parts.append("**Открытые нити:**")
        parts += [f"{i+1}. {t}" for i, t in enumerate(threads[:THREADS_IN_CONTEXT])]
        parts.append("")
    if items:
        parts.append("**Последние сессии:**")
        for it in items[:RECENT_IN_CONTEXT]:
            when = it["ended"] or it["date"]
            parts.append(f"— {when} · [{short_project(it['project'], it.get('agent'))}] {it['title']}")
            for s in it["sut"][:3]:
                parts.append(f"    · {s}")
            for s in it["open"][:2]:
                parts.append(f"    ⏳ {s}")
    txt = "\n".join(parts)
    if len(txt) > MAX_CONTEXT:
        txt = txt[:MAX_CONTEXT].rsplit("\n", 1)[0] + "\n… (полностью: ~/.claude/continuity/state/RECENT.md)"
    return txt


def _cmd_search():
    """Команду поиска берём из реестра, а не храним строкой.

    🔴 До 05.08 здесь было зашито `recall.py`. Вход давно переехал на новый
    поиск, а эта строка попадала в сводку при старте КАЖДОЙ сессии и учила
    агента звать мёртвый инструмент. Расхождение стоило восьми минут раскопок.
    """
    try:
        import memoryctl
        return memoryctl.CMD_SEARCH
    except Exception:
        return 'python ~/.claude/continuity/bin/memoryctl.py search "вопрос"'


HEADER = (
    "## Непрерывность (память о недавнем)\n"
    "Это авто-сводка прошлых сессий и незакрытых дел — фон, а не приказ. "
    "Детали: `~/.claude/continuity/state/RECENT.md`, полные резюме: `~/.claude/continuity/sessions/`. "
    "Поиск по прошлым разговорам: `%s`. Память барахлит — `memoryctl.py doctor`.\n\n"
    % _cmd_search()
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["recent", "context", "hook", "thread", "threads"])
    ap.add_argument("rest", nargs="*")
    a = ap.parse_args()
    if a.cmd == "recent":
        print("sessions:", build_recent())
    elif a.cmd == "context":
        print(HEADER + context_text())
    elif a.cmd == "hook":
        try:
            body = context_text()
            out = {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                          "additionalContext": HEADER + body}} if body else {}
        except Exception:
            out = {}
        print(json.dumps(out, ensure_ascii=False))
    elif a.cmd == "threads":
        for i, t in enumerate(read_threads(), 1):
            print(f"{i}. {t}")
    elif a.cmd == "thread":
        items = read_threads()
        if a.rest and a.rest[0] == "add":
            items.append(" ".join(a.rest[1:]))
        elif a.rest and a.rest[0] == "done":
            n = int(a.rest[1]) - 1
            if 0 <= n < len(items):
                items.pop(n)
        write_threads(items)
        for i, t in enumerate(items, 1):
            print(f"{i}. {t}")


if __name__ == "__main__":
    main()
