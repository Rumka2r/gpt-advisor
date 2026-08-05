# -*- coding: utf-8 -*-
"""Поиск по прошлым разговорам (резюме сессий + реплики) — смысловой, локальный.

  python recall.py "запрос" [--k 8] [--days 120]
  python recall.py --index [--days 120]     — обновить индекс (инкрементально)

Эмбеддинги: BGE-M3 на 127.0.0.1:8899 (локально). Если сервер лежит — падаем в grep-режим.
"""
import os, sys, json, sqlite3, argparse, urllib.request, datetime, hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import transcript as T

ROOT = os.path.expanduser("~/.claude/continuity")
DB = os.path.join(ROOT, "index", "recall.sqlite")
EMB = "http://127.0.0.1:8899/embed"
SESSIONS = os.path.join(ROOT, "sessions")


def embed(texts, batch=32):
    out = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        req = urllib.request.Request(
            EMB, data=json.dumps({"texts": chunk}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        out.extend(json.load(urllib.request.urlopen(req, timeout=120))["vectors"])
    return out


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS chunks(
        id INTEGER PRIMARY KEY, source TEXT, session TEXT, date TEXT,
        title TEXT, kind TEXT, text TEXT, vec BLOB)""")
    con.execute("""CREATE TABLE IF NOT EXISTS seen(
        path TEXT PRIMARY KEY, mtime REAL, size INTEGER)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_sess ON chunks(session)")
    return con


def ensure_usage(con):
    """Учёт полезности (идея подсмотрена у Codex: usage_count + отбор во вторую фазу).

    Индекс знает, ЧТО когда-то говорилось, но не знает, что из этого реально
    всплывает в работе. Считаем показы: часто всплывающее — кандидат на перенос
    из ленты разговоров в постоянную память (memory/), см. `recall.py --hot`.
    """
    con.execute("""CREATE TABLE IF NOT EXISTS usage(
        key TEXT PRIMARY KEY, session TEXT, date TEXT, title TEXT, kind TEXT,
        snippet TEXT, shown INTEGER NOT NULL DEFAULT 0, last_ts REAL)""")
    return con


def bump_usage(hits):
    """+1 показ каждому выданному фрагменту. Тихо: сбой учёта не должен ломать выдачу."""
    import time
    try:
        con = db()
        ensure_usage(con)
        for h in hits:
            key = hashlib.md5(((h.get("text") or h.get("snippet") or "")[:200]).encode("utf-8")).hexdigest()[:12]
            con.execute("""INSERT INTO usage(key,session,date,title,kind,snippet,shown,last_ts)
                           VALUES(?,?,?,?,?,?,1,?)
                           ON CONFLICT(key) DO UPDATE SET shown=shown+1, last_ts=excluded.last_ts""",
                        (key, h.get("session", ""), h.get("date", ""), h.get("title", ""),
                         h.get("kind", ""), (h.get("text") or h.get("snippet") or "")[:300], time.time()))
        con.commit()
        con.close()
    except Exception:
        pass


def hot(limit=15, min_shown=2):
    """Что всплывает чаще всего — сырьё для консолидации в memory/."""
    con = db()
    ensure_usage(con)
    rows = con.execute("""SELECT shown,date,title,kind,snippet,datetime(last_ts,'unixepoch','localtime')
                          FROM usage WHERE shown>=? ORDER BY shown DESC, last_ts DESC LIMIT ?""",
                       (min_shown, limit)).fetchall()
    con.close()
    print(json.dumps({"hot": [
        {"shown": r[0], "date": r[1], "title": r[2], "kind": r[3],
         "snippet": (r[4] or "")[:200], "last": r[5]} for r in rows]},
        ensure_ascii=False, indent=1))


def _add(con, rows):
    """rows: (source, session, date, title, kind, text)"""
    if not rows:
        return 0
    import numpy as np
    vecs = embed([r[5][:1800] for r in rows])
    for r, v in zip(rows, vecs):
        con.execute("INSERT INTO chunks(source,session,date,title,kind,text,vec) VALUES(?,?,?,?,?,?,?)",
                    (*r, np.asarray(v, dtype="float32").tobytes()))
    con.commit()
    return len(rows)


def index(days=120, verbose=True):
    con = db()
    total = 0
    # 1) резюме сессий
    for name in sorted(os.listdir(SESSIONS)) if os.path.isdir(SESSIONS) else []:
        if not name.endswith(".md"):
            continue
        p = os.path.join(SESSIONS, name)
        st = os.stat(p)
        row = con.execute("SELECT mtime,size FROM seen WHERE path=?", (p,)).fetchone()
        if row and abs(row[0] - st.st_mtime) < 1 and row[1] == st.st_size:
            continue
        con.execute("DELETE FROM chunks WHERE source=?", (p,))
        txt = open(p, encoding="utf-8").read()
        title = ""
        for line in txt.splitlines():
            if line.startswith("title:"):
                title = line.split(":", 1)[1].strip().strip('"')
                break
        date = name[:10]
        sid = name[11:19]
        body = txt.split("---", 2)[-1]
        total += _add(con, [(p, sid, date, title, "summary", body[:1800])])
        con.execute("INSERT OR REPLACE INTO seen VALUES(?,?,?)", (p, st.st_mtime, st.st_size))
        con.commit()
    # 2) реплики пользователя из транскриптов
    for mtime, size, p in T.transcripts(days=days):
        row = con.execute("SELECT mtime,size FROM seen WHERE path=?", (p,)).fetchone()
        if row and abs(row[0] - mtime) < 1 and row[1] == size:
            continue
        try:
            info = T.parse(p)
        except Exception:
            continue
        con.execute("DELETE FROM chunks WHERE source=?", (p,))
        sid = info["session_id"][:8]
        date = (info.get("started") or "")[:10]
        title = info.get("title") or ""
        rows = []
        buf = []
        for ts, txt in info["users"]:
            buf.append(txt)
            if sum(len(x) for x in buf) > 900:      # склейка коротких реплик в чанк
                rows.append((p, sid, date, title, "user", "\n".join(buf)))
                buf = []
        if buf:
            rows.append((p, sid, date, title, "user", "\n".join(buf)))
        total += _add(con, rows[:80])
        con.execute("INSERT OR REPLACE INTO seen VALUES(?,?,?)", (p, mtime, size))
        con.commit()
        if verbose:
            print(f"indexed {os.path.basename(p)[:8]} +{len(rows)}", flush=True)
    con.close()
    return total


def search(q, k=8):
    import numpy as np
    con = db()
    rows = con.execute("SELECT id,session,date,title,kind,text,vec FROM chunks").fetchall()
    if not rows:
        print(json.dumps({"error": "индекс пуст — сначала recall.py --index"}, ensure_ascii=False))
        return
    try:
        qv = np.asarray(embed([q])[0], dtype="float32")
    except Exception:
        ql = q.lower()
        hits = [r for r in rows if ql in (r[5] or "").lower()][:k]
        print(json.dumps({"mode": "grep", "results": [
            {"date": h[2], "title": h[3], "kind": h[4], "snippet": h[5][:300]} for h in hits]},
            ensure_ascii=False, indent=1))
        return
    M = np.frombuffer(b"".join(r[6] for r in rows), dtype="float32").reshape(len(rows), -1)
    sims = (M @ qv) / (np.linalg.norm(M, axis=1) * np.linalg.norm(qv) + 1e-9)
    order = np.argsort(-sims)[: k * 3]
    seen_sess, out = set(), []
    for i in order:
        r = rows[int(i)]
        key = (r[1], r[4])
        if key in seen_sess:
            continue
        seen_sess.add(key)
        out.append({"score": round(float(sims[i]), 3), "date": r[2], "session": r[1],
                    "title": r[3], "kind": r[4], "snippet": r[5][:400].replace("\n", " ")})
        if len(out) >= k:
            break
    print(json.dumps({"query": q, "results": out}, ensure_ascii=False, indent=1))
    con.close()
    bump_usage(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?")
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--hot", action="store_true", help="что чаще всего всплывает (кандидаты в memory/)")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--k", type=int, default=8)
    a = ap.parse_args()
    if a.index:
        print("chunks added:", index(a.days))
        return
    if a.hot:
        hot(limit=a.k * 2)
        return
    if not a.query:
        ap.error("нужен запрос или --index")
    search(a.query, a.k)


if __name__ == "__main__":
    # 🔴 Вход выведен из эксплуатации 05.08.2026. Модуль ещё импортируется
    # (stems, bump_usage, замеры в eval_search), но звать его из командной
    # строки нельзя: при мёртвом эмбеддере он молча отдавал
    # {"mode":"grep","results":[]} — пустоту, неотличимую от «такого нет».
    # Именно на этом 05.08 потерялись восемь минут.
    sys.stderr.write(
        "\n🔴 recall.py выведен из эксплуатации.\n"
        "   Он молча возвращал пустоту, когда лежал эмбеддер.\n\n"
        "   Используй единственный вход:\n"
        "       python ~/.claude/continuity/bin/memoryctl.py search \"вопрос\"\n"
        "       python ~/.claude/continuity/bin/memoryctl.py doctor\n\n")
    raise SystemExit(3)
