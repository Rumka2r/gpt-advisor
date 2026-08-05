#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Нити: одна работа, растянутая на много сессий.

Эпизод живёт внутри одной сессии. Но настоящее дело так не живёт: шины
тянулись через четыре сессии и три дня, отель — через две недели. Без
связи между сессиями каждый заход выглядит как новый, и я каждый раз
начинаю выяснять заново.

Нить собирается автоматически: эпизоды идут по времени, каждый примыкает к
подходящей открытой нити или заводит новую. Похожесть считается не только по
смыслу — короткие реплики слишком близки друг к другу, — но и по общим файлам
и по тому, насколько давно нить трогали.

Пересборка полная и дешёвая (доли секунды на все эпизоды), поэтому нити
не «дрейфуют» после переиндексации: worker.py перестраивает их целиком при
каждом проходе, и никакого ручного шага не нужно.

Это НЕ замена `state/THREADS.md`. Тот файл — мои обязательства перед Рувимом,
он ведётся руками и осознанно. Здесь — автоматическая карта того, что с чем
связано в истории.
"""

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog  # noqa: E402

VERSION = 1

# Порог примыкания. Подобран по живым данным: ниже — в одну нить сползают
# соседние по времени, но разные дела; выше — рвётся продолжение работы,
# начатой неделю назад.
JOIN = 0.70
FILE_BONUS = 0.08        # общий содержательный файл — улика, что дело то же
STALE_DAYS = 10          # нить, которую не трогали дольше, закрыта для примыкания
RECENT_DAYS = 10         # нить активна, если её трогали недавно
TAIL = 3                 # сравниваем с хвостом нити, а не со всей её историей

# Файлы, которые я трогаю почти в каждом деле: память, нити, логи, конфиги.
# Как признак родства они бесполезны — по ним «связывается» вообще всё.
COMMON_FILES = {
    "THREADS.md", "MEMORY.md", "RECENT.md", "CLAUDE.md", "settings.json",
    "worker.log", "gate.log", "recall.py", "now.py", "hook.py", "auto_recall.py",
}


def _significant(files):
    return {f for f in files
            if f not in COMMON_FILES
            and not f.startswith("hub_")
            and not f.endswith((".log", ".lock", ".tmp"))}


# Реплики, которые не годятся в название нити: вложения, слэш-команды,
# односложные понукания. Название должно отвечать «про что это дело».
_BAD_TITLE = ("@\"", "[image:", "/compact", "/clear", "http", "статус", "продолж",
              "прочитай мои сообщения", ".", "да", "нет", "ок")


import re as _re

# Вложения и ссылки в начале реплики: сами по себе ничего не говорят о деле,
# но за ними обычно идёт нормальный текст — его и надо взять в название.
_ATTACH = _re.compile(
    r'@"[^"]*"|\[Image:[^\]]*\]|https?://\S+|@\S+\.(?:png|jpg|jpeg|pdf|stl|3mf|gcode)\b',
    _re.I)


def _clean_title(t):
    return " ".join(_ATTACH.sub(" ", t or "").split())


def _pick_title(eps):
    """Название нити — первая содержательная реплика, иначе самая длинная."""
    cleaned = [_clean_title(e["title"]) for e in eps]
    good = [t for t in cleaned
            if len(t) > 25 and not t.lower().startswith(_BAD_TITLE)]
    if good:
        return good[0]
    return max(cleaned + [""], key=len) or "(без названия)"


def _days(a, b):
    if not a or not b:
        return 999.0
    try:
        import datetime
        ta = datetime.datetime.fromisoformat(a.replace("Z", "+00:00"))
        tb = datetime.datetime.fromisoformat(b.replace("Z", "+00:00"))
        return abs((tb - ta).total_seconds()) / 86400.0
    except ValueError:
        return 999.0


def ensure_tables(con):
    con.execute("""CREATE TABLE IF NOT EXISTS work_threads(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, first_ts TEXT, last_ts TEXT,
        episodes INTEGER, sessions INTEGER, projects TEXT,
        files TEXT, built INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS thread_episode(
        thread_id INTEGER NOT NULL, episode_id INTEGER NOT NULL,
        PRIMARY KEY(thread_id, episode_id))""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_te_ep ON thread_episode(episode_id)")
    con.commit()


def build(con=None, verbose=True):
    """Полная пересборка нитей. Дёшево — вызывается из worker при каждом проходе."""
    import numpy as np
    con = con or catalog.db()
    ensure_tables(con)
    con.row_factory = sqlite3.Row
    t0 = time.time()

    rows = con.execute(
        "SELECT * FROM episodes WHERE vec_goal IS NOT NULL ORDER BY start_ts").fetchall()
    if not rows:
        return 0

    dim = len(rows[0]["vec_goal"]) // 4
    live = []      # открытые нити: {vec(сумма), n, files, last_ts, project, eps}

    last_of_session = {}     # сессия → нить последнего её эпизода

    for r in rows:
        v = np.frombuffer(r["vec_goal"], dtype="float32")
        vn = np.linalg.norm(v) + 1e-9
        files = _significant({f.strip() for f in (r["files"] or "").split(",") if f.strip()})

        # Эпизод, отрезанный только из-за длины, — это заведомо продолжение
        # той же работы: разрыв сделал ограничитель размера, а не смена темы.
        # Такие склеиваем в одну нить без оглядки на похожесть.
        if (r["cut_reason"] or "").startswith("длинный эпизод разделён"):
            th = last_of_session.get(r["session"])
            if th is not None:
                th["tail"] = (th["tail"] + [v])[-TAIL:]
                th["files"] |= files
                th["last_ts"] = r["start_ts"] or th["last_ts"]
                th["eps"].append(r)
                continue

        best, best_score = None, 0.0
        for th in live:
            if _days(th["last_ts"], r["start_ts"]) > STALE_DAYS:
                continue
            # Сравниваем с хвостом нити, а не со всей историей: усреднение по
            # сотне эпизодов превращает нить в магнит, к которому подходит всё.
            sim = max(float(v @ u / (vn * (np.linalg.norm(u) + 1e-9)))
                      for u in th["tail"])
            if files & th["files"]:
                sim += FILE_BONUS
            if sim > best_score:
                best, best_score = th, sim
        if best is not None and best_score >= JOIN:
            best["tail"] = (best["tail"] + [v])[-TAIL:]
            best["files"] |= files
            best["last_ts"] = r["start_ts"] or best["last_ts"]
            best["eps"].append(r)
        else:
            best = {"tail": [v], "files": set(files),
                    "last_ts": r["start_ts"], "first_ts": r["start_ts"],
                    "project": r["project"], "eps": [r], "title": r["title"]}
            live.append(best)
        last_of_session[r["session"]] = best

    con.execute("DELETE FROM work_threads")
    con.execute("DELETE FROM thread_episode")
    for th in live:
        sessions = {e["session"] for e in th["eps"]}
        projects = sorted({e["project"] for e in th["eps"]})
        cur = con.execute(
            "INSERT INTO work_threads(title,first_ts,last_ts,episodes,sessions,projects,files,built)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (_pick_title(th["eps"])[:150], th["first_ts"], th["last_ts"], len(th["eps"]),
             len(sessions), ", ".join(projects), ", ".join(sorted(th["files"])[:25]), VERSION))
        tid = cur.lastrowid
        con.executemany("INSERT OR REPLACE INTO thread_episode(thread_id,episode_id) VALUES(?,?)",
                        [(tid, e["id"]) for e in th["eps"]])
    con.commit()

    multi = sum(1 for t in live if len({e["session"] for e in t["eps"]}) > 1)
    if verbose:
        print(f"нитей: {len(live)} (сквозных, через >1 сессию: {multi}), "
              f"время {time.time() - t0:.1f} с")
    return len(live)


def thread_of(con, episode_id):
    r = con.execute("SELECT thread_id FROM thread_episode WHERE episode_id=?",
                    (episode_id,)).fetchone()
    return r[0] if r else None


def show(con, tid):
    """Хронология нити: что просили и чем кончилось, по сессиям."""
    con.row_factory = sqlite3.Row
    t = con.execute("SELECT * FROM work_threads WHERE id=?", (tid,)).fetchone()
    if not t:
        return f"нити {tid} нет"
    eps = con.execute("""SELECT e.* FROM episodes e JOIN thread_episode te ON te.episode_id=e.id
                         WHERE te.thread_id=? ORDER BY e.start_ts""", (tid,)).fetchall()
    lines = [f"НИТЬ {tid}: {t['title']}",
             f"  {(t['first_ts'] or '')[:10]} → {(t['last_ts'] or '')[:10]} · "
             f"эпизодов {t['episodes']}, сессий {t['sessions']}"]
    if t["files"]:
        lines.append(f"  файлы: {t['files'][:120]}")
    lines.append("")
    for e in eps:
        lines.append(f"  [{(e['start_ts'] or '')[:16].replace('T', ' ')}] сессия {e['session'][:8]}")
        lines.append(f"     просил: {' '.join(e['title'].split())[:100]}")
        out = " ".join((e["outcome_text"] or "").split())
        if out:
            lines.append(f"     итог:   {out[:150]}")
        lines.append(f"     якорь:  {e['start_uuid']}")
    return "\n".join(lines)


def active(con, days=RECENT_DAYS, limit=15):
    """Нити, которых касались недавно — кандидаты в «что сейчас в работе»."""
    con.row_factory = sqlite3.Row
    import datetime
    edge = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
    return con.execute(
        "SELECT * FROM work_threads WHERE last_ts >= ? AND episodes > 1"
        " ORDER BY last_ts DESC LIMIT ?", (edge, limit)).fetchall()


def main():
    ap = argparse.ArgumentParser(description="Нити: связь эпизодов через сессии")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--show", type=int, metavar="ID")
    ap.add_argument("--active", action="store_true")
    ap.add_argument("--top", action="store_true", help="самые длинные сквозные нити")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    con = catalog.db()
    if a.build:
        build(con)
        return
    ensure_tables(con)
    con.row_factory = sqlite3.Row

    if a.show:
        print(show(con, a.show))
    elif a.active:
        rows = active(con)
        print(f"активные нити (последние {RECENT_DAYS} дней):")
        for r in rows:
            print(f"  #{r['id']:<4} {(r['last_ts'] or '')[:10]}  эп={r['episodes']:<3} "
                  f"сес={r['sessions']:<2}  {r['title'][:78]}")
    else:
        rows = con.execute("SELECT * FROM work_threads WHERE sessions > 1"
                           " ORDER BY sessions DESC, episodes DESC LIMIT 20").fetchall()
        if a.json:
            print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1))
            return
        print("самые сквозные нити (через несколько сессий):")
        for r in rows:
            print(f"  #{r['id']:<4} сессий={r['sessions']:<2} эп={r['episodes']:<3} "
                  f"{(r['first_ts'] or '')[:10]}→{(r['last_ts'] or '')[:10]}  {r['title'][:66]}")


if __name__ == "__main__":
    main()
