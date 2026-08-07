# -*- coding: utf-8 -*-
"""Слой «сейчас»: лента последних сессий + открытые нити + контекст для SessionStart.

  python now.py recent            — пересобрать RECENT.md из sessions/*.md
  python now.py context           — напечатать блок непрерывности (текст)
  python now.py hook              — то же в формате SessionStart-хука (JSON)
  python now.py thread add "..."  — добавить открытую нить (подписывается агентом)
  python now.py thread done N     — закрыть нить номер N (нумерация — как в threads)
  python now.py thread transfer N <agent>  — передать нить другому агенту
  python now.py threads [--all]   — показать нити (свои+legacy; --all — все)

Разграничение инстансов (вердикт 06.08.2026): контекст и нити фильтруются по
agent_id текущего процесса через continuity_identity. Сессии и нити других
агентов автоматически НЕ инжектятся; нити без метки владельца (legacy) видны
всем — это обязательства, заведённые до разграничения.
"""
import os, re, sys, json, glob, datetime, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import continuity_identity as ident
except Exception:       # реестр не должен уметь ронять сводку
    ident = None

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


def _owner_map():
    if not ident:
        return {}
    try:
        return ident.owner_map()
    except Exception:
        return {}


def load_sessions():
    items = []
    omap = _owner_map()
    for p in sorted(glob.glob(os.path.join(SESSIONS, "*.md"))):
        try:
            txt = open(p, encoding="utf-8").read()
        except OSError:
            continue
        fm, body = _fm(txt)
        sid = fm.get("session", "")
        # Владелец — по реестру привязок (авторитет), иначе по фронтматтеру
        # нового резюме; старые резюме без того и другого = legacy (None).
        owner = omap.get(sid[:8]) or fm.get("agent_id") or None
        if owner == "legacy-unowned":
            owner = None
        items.append({
            "path": p,
            "session": sid,
            "owner": owner,
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
        who = it.get("owner") or "владелец неизвестен"
        lines.append(f"## {it['ended'] or it['date']} — [{who}] {it['title']}")
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
    """Все строки нитей как есть (с метками владельца)."""
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
            "<!-- Одна строка = одно живое дело. Правит ассистент по ходу работы.\n"
            "     [agent:X] в начале строки — владелец нити; без метки — заведена\n"
            "     до разграничения (видна всем). Правь через now.py thread ... -->\n\n")
    open(THREADS, "w", encoding="utf-8").write(head + "\n".join(f"- {i}" for i in items) + "\n")


def _me():
    if not ident:
        return None
    try:
        return ident.current_agent_id()
    except Exception:
        return None


def threads_view(me=None, show_all=False):
    """[(индекс в файле, owner|None, чистый текст)] — что видит агент me.

    Видимость: свои + без метки (legacy). Чужие — только с show_all.
    Нумерация для thread done/transfer идёт ПО ЭТОМУ списку: агент называет
    номер из того, что ему показали, а не из полного файла.
    """
    me = me if me is not None else _me()
    out = []
    for i, raw in enumerate(read_threads()):
        owner, text = (ident.thread_owner(raw) if ident else (None, raw))
        if show_all or not ident or ident.thread_visible(owner, me):
            out.append((i, owner, text))
    return out


def context_text():
    items = load_sessions()
    me = _me()
    parts = []
    if me:
        parts.append(f"Ты — агент **{me}**. Ниже только ТВОИ сессии и нити "
                     "(+ общие, заведённые до разграничения). Чужие — по явному "
                     "запросу: `continuity_identity.py sessions --agent <имя>`.")
        parts.append("")
    view = threads_view(me)
    if view:
        parts.append("**Открытые нити:**")
        for n, (_, owner, text) in enumerate(view[:THREADS_IN_CONTEXT], 1):
            mark = "" if owner else " ⁽общая⁾"
            parts.append(f"{n}. {text}{mark}")
        parts.append("")
    # Автоматически — только СВОИ последние сессии (вердикт 06.08: чужие
    # summaries в контекст не подмешиваются никогда; legacy не входят в
    # «вспомни, что Я делал», они доступны через memoryctl recent).
    if me:
        mine = [it for it in items if it.get("owner") == me]
    else:
        mine = [it for it in items if not it.get("owner")]
    if mine:
        parts.append("**Последние сессии:**")
        for it in mine[:RECENT_IN_CONTEXT]:
            when = it["ended"] or it["date"]
            parts.append(f"— {when} · [{short_project(it['project'], it.get('agent'))}] {it['title']}")
            for s in it["sut"][:3]:
                parts.append(f"    · {s}")
            for s in it["open"][:2]:
                parts.append(f"    ⏳ {s}")
    elif me:
        parts.append(f"(своих завершённых сессий у {me} ещё нет; общая история "
                     "до разграничения — `memoryctl.py recent`)")
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


def _print_view(view):
    for n, (_, owner, text) in enumerate(view, 1):
        tag = f" [{owner}]" if owner else " [общая]"
        print(f"{n}.{tag} {text}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["recent", "context", "hook", "thread", "threads"])
    ap.add_argument("--all", action="store_true", help="нити всех агентов")
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
        _print_view(threads_view(show_all=a.all))
    elif a.cmd == "thread":
        me = _me()
        items = read_threads()
        if a.rest and a.rest[0] == "add":
            text = " ".join(a.rest[1:])
            # Новая нить подписывается владельцем автоматически: все сессии и
            # нити подписаны — требование Рувима 06.08.
            items.append(ident.thread_line(text, me) if ident and me else text)
            write_threads(items)
        elif a.rest and a.rest[0] in ("done", "transfer"):
            # Номер — из видимого списка агента (threads), не из сырого файла:
            # чужие нити скрыты, и по сырой нумерации агент закрыл бы чужую.
            view = threads_view(me, show_all=a.all)
            n = int(a.rest[1]) - 1
            if not (0 <= n < len(view)):
                print(f"нет нити номер {n + 1} (видимых: {len(view)})", file=sys.stderr)
                sys.exit(1)
            file_idx, owner, text = view[n]
            if a.rest[0] == "done":
                items.pop(file_idx)
            else:                                   # transfer N <agent>
                to_agent = a.rest[2]
                if ident and not ident.known_agent(to_agent):
                    print(f"агент {to_agent!r} не описан в agents.yaml", file=sys.stderr)
                    sys.exit(1)
                items[file_idx] = ident.thread_line(text, to_agent)
                try:
                    gen = ident.log_transfer(text, owner, to_agent, me)
                    print(f"нить передана {owner or 'без владельца'} → {to_agent} "
                          f"(generation {gen})")
                except Exception:
                    pass
            write_threads(items)
        _print_view(threads_view(me, show_all=a.all))


if __name__ == "__main__":
    main()
