# -*- coding: utf-8 -*-
"""Резюме сессии из транскрипта. Эвристический скелет + сжатие моделью (claude -p, haiku).

Использование:
  python summarize.py <transcript.jsonl> [--no-llm]
  python summarize.py --catchup [--days 14] [--limit 5]   # досуммаризировать пропущенные
"""
import argparse, os, re, subprocess, sys, json, datetime, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import transcript as T

ROOT = os.path.expanduser("~/.claude/continuity")
SESSIONS = os.path.join(ROOT, "sessions")
CLAUDE_CMD = os.path.expanduser("~/AppData/Roaming/npm/claude.cmd")
SUM_MODEL = "claude-haiku-4-5-20251001"

PROMPT = """Ниже — скелет рабочей сессии между Рувимом (пользователь) и его ассистентом.
Сделай СЖАТОЕ резюме на русском строго в этом формате, без вступлений:

## Суть
- 3-6 пунктов: о чём говорили и что реально сделано (конкретика: имена файлов, цифры, названия).

## Итог
- 1-3 пункта: чем закончилось, что работает, что сломано.

## Открыто
- 0-4 пункта: что не доделано, чего ждём, следующий шаг. Если ничего — напиши "нет".

Правила: только факты из текста; без воды; каждый пункт ≤ 160 символов; не выдумывай.
=== СКЕЛЕТ ===
"""


def ensure_dirs():
    os.makedirs(SESSIONS, exist_ok=True)


def out_path(info, project):
    date = (info.get("started") or "")[:10] or datetime.date.today().isoformat()
    return os.path.join(SESSIONS, f"{date}_{info['session_id'][:8]}.md")


def skeleton(info, limit=45000):
    tools, files = T.tool_summary(info["actions"])
    parts = [
        f"Заголовок: {info.get('title') or '—'}",
        f"Начало: {T.human_dt(info.get('started'))}  Конец: {T.human_dt(info.get('ended'))}",
        f"Реплик пользователя: {info['user_count']}",
        f"Инструменты: " + ", ".join(f"{k}×{v}" for k, v in tools),
        f"Файлы: " + ", ".join(k for k, _ in files[:10]),
        "",
        "--- РЕПЛИКИ ПОЛЬЗОВАТЕЛЯ ---",
    ]
    for ts, txt in info["users"]:
        parts.append(f"[{T.human_dt(ts)}] {txt}")
    parts.append("")
    parts.append("--- ПОСЛЕДНИЕ ОТВЕТЫ АССИСТЕНТА ---")
    parts.extend(info["assistant_tail"][-25:])
    s = "\n".join(parts)
    if len(s) > limit:  # середину выкидываем, края важнее
        s = s[: limit // 2] + "\n\n…[середина опущена]…\n\n" + s[-limit // 2:]
    return s


def llm_condense(skel, timeout=240):
    if not os.path.exists(CLAUDE_CMD):
        return None
    tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    tmp.write(PROMPT + skel)
    tmp.close()
    try:
        with open(tmp.name, encoding="utf-8") as fh:
            r = subprocess.run(
                [CLAUDE_CMD, "-p", "--model", SUM_MODEL],
                stdin=fh, capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        if r.returncode == 0 and r.stdout and "## Суть" in r.stdout:
            return r.stdout.strip()
        return None
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def fallback_body(info):
    """Без модели: первые и последние реплики пользователя."""
    us = [t for _, t in info["users"]]
    head = us[:4]
    tail = us[-4:] if len(us) > 8 else []
    lines = ["## Суть", "(резюме без модели — сырые реплики)"]
    lines += [f"- {u[:200]}" for u in head]
    if tail:
        lines.append("...")
        lines += [f"- {u[:200]}" for u in tail]
    lines += ["", "## Итог", "- (не сжато моделью)", "", "## Открыто", "- нет"]
    return "\n".join(lines)


def summarize_one(path, project, use_llm=True):
    info = T.parse(path)
    # у Codex «проект» берётся из cwd самой сессии, а не из имени папки
    project = info.get("project") or project
    # Codex часто вызывают одним длинным брифом «сделай и отчитайся» — такая сессия
    # содержательна при одной реплике, поэтому порог для него мягче.
    floor = 1 if info.get("agent") == "codex" and info["actions"] else 2
    if info["user_count"] < floor:
        return None
    dest = out_path(info, project)
    body = llm_condense(skeleton(info)) if use_llm else None
    engine = "haiku" if body else "raw"
    if not body:
        body = fallback_body(info)
    tools, files = T.tool_summary(info["actions"])
    fm = [
        "---",
        f"session: {info['session_id']}",
        f"project: {project}",
        f"agent: {info.get('agent') or 'claude'}",
        f"date: {(info.get('started') or '')[:10]}",
        f"title: {json.dumps(info.get('title') or '', ensure_ascii=False)}",
        f"started: {T.human_dt(info.get('started'))}",
        f"ended: {T.human_dt(info.get('ended'))}",
        f"turns: {info['user_count']}",
        f"engine: {engine}",
        "---",
        "",
        f"# {info.get('title') or 'Сессия ' + info['session_id'][:8]}",
        "",
    ]
    tail = ["", "## Артефакты", "- Инструменты: " + ", ".join(f"{k}×{v}" for k, v in tools[:8])]
    if files:
        tail.append("- Файлы: " + ", ".join(k for k, _ in files[:8]))
    with open(dest, "w", encoding="utf-8") as f:
        f.write("\n".join(fm) + body + "\n" + "\n".join(tail) + "\n")
    return dest


def already_done():
    ensure_dirs()
    done = set()
    for n in os.listdir(SESSIONS):
        m = re.match(r"\d{4}-\d{2}-\d{2}_([0-9a-f]{8})\.md$", n)
        if m:
            done.add(m.group(1))
    return done


FRESH_SEC = 900  # транскрипт, писавшийся минуты назад — вероятно, живая сессия


def catchup(days=14, limit=5, use_llm=True, skip_session=None):
    import time
    done = already_done()
    made = []
    now_ts = time.time()
    for mtime, size, p in T.transcripts(days=days):
        if T.is_codex(p):
            m = T.CODEX_ID.search(os.path.basename(p))
            sid = m.group(1) if m else os.path.basename(p)
        else:
            sid = os.path.basename(p).replace(".jsonl", "")
        if sid[:8] in done or sid == skip_session:
            continue
        if now_ts - mtime < FRESH_SEC:       # ещё идёт — резюмировать рано
            continue
        project = os.path.basename(os.path.dirname(p))
        try:
            d = summarize_one(p, project, use_llm=use_llm)
            if d:
                made.append(d)
        except Exception as e:
            print("ERR", p, repr(e)[:120], file=sys.stderr)
        if len(made) >= limit:
            break
    return made


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--catchup", action="store_true")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--skip-session", default=None)
    a = ap.parse_args()
    ensure_dirs()
    if a.catchup:
        for d in catchup(a.days, a.limit, not a.no_llm, a.skip_session):
            print("SUMMARY", d)
        return
    if not a.path:
        ap.error("нужен путь к транскрипту или --catchup")
    project = os.path.basename(os.path.dirname(a.path))
    d = summarize_one(a.path, project, use_llm=not a.no_llm)
    print("SUMMARY", d)


if __name__ == "__main__":
    main()
