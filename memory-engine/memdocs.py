#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Файлы памяти как источник поиска — рядом с историей разговоров.

Зачем отдельно от эпизодов. История отвечает на вопрос «что мы тогда делали и
решали». Действующие факты — сроки офферов, адреса, порты, номера машин —
живут в `memory/` и в CLAUDE.md, и в разговорах могут вообще не звучать
словами: они там мелькают внутри команд правки файла.

Проверено на замере: вопросы «до какого числа оффер Aeroplan», «какой IP у
сервера», «что с машиной Sentra» не находились в разговорах именно поэтому —
ответ был записан в память, а не проговорён.

Разница источников важна и подписывается в выдаче:
  ПАМЯТЬ  — то, что считается верным сейчас;
  ИСТОРИЯ — то, что говорилось тогда и могло устареть.

Файл режется на куски по заголовкам: искать надо раздел, а не файл целиком.
Секреты вычищаются тем же redact.py — в память они попадать не должны, но
проверка дешевле доверия.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog  # noqa: E402
import redact  # noqa: E402

STORE = os.path.expanduser("~/.claude/projects/C--Users-andri-work-osha/memory")
EXTRA = [os.path.expanduser("~/.claude/CLAUDE.md")]
EMB = "http://127.0.0.1:8899/embed"

CHUNK = 900           # кусок примерно в размер подраздела
MIN_CHUNK = 80

# Сводные разделы («что сейчас в работе») — это длинные перечни, где в одном
# абзаце соседствуют шины, отель и сервер. Одним куском их вектор размывается
# и не отвечает ни на один конкретный вопрос, поэтому режем ещё и по пунктам.
_ITEM_SEPS = ("\n\n", "\n- ", "\n* ", "\n• ", " · ", ". **")


def ensure_tables(con):
    con.execute("""CREATE TABLE IF NOT EXISTS mem_docs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL, title TEXT, section TEXT,
        text TEXT NOT NULL, mtime REAL, vec BLOB)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_mem_path ON mem_docs(path)")
    con.commit()


def _split(text):
    """Режем по заголовкам markdown; длинные куски добиваем по абзацам."""
    parts, cur, head = [], [], ""
    for line in text.split("\n"):
        if re.match(r"^#{1,4}\s+\S", line):
            if cur:
                parts.append((head, "\n".join(cur)))
                cur = []
            head = line.lstrip("# ").strip()
        else:
            cur.append(line)
    if cur:
        parts.append((head, "\n".join(cur)))

    out = []
    for head, body in parts:
        body = body.strip()
        while len(body) > CHUNK:
            cut = -1
            for sep in _ITEM_SEPS:            # ищем ближайшую осмысленную границу
                pos = body.rfind(sep, MIN_CHUNK, CHUNK)
                cut = max(cut, pos)
            cut = cut if cut > MIN_CHUNK else CHUNK
            out.append((head, body[:cut].strip()))
            body = body[cut:].strip()
        if len(body) >= MIN_CHUNK:
            out.append((head, body))
    return out


def _files():
    found = []
    for root, _dirs, names in os.walk(STORE):
        for n in names:
            if n.endswith(".md"):
                found.append(os.path.join(root, n))
    found += [p for p in EXTRA if os.path.exists(p)]
    return sorted(found)


def embed(texts, batch=32):
    import urllib.request
    out = []
    for i in range(0, len(texts), batch):
        req = urllib.request.Request(
            EMB, data=json.dumps({"texts": [t[:1800] for t in texts[i:i + batch]]}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            out.extend(json.loads(r.read())["vectors"])
    return out


def build(con=None, verbose=True, force=False):
    """Инкрементально: пересчитываются только изменившиеся файлы.

    Полная пересборка — почти три минуты (680 файлов), и гонять её в каждом
    фоновом проходе незачем: память меняется по одному-два файла за раз.
    """
    import array
    con = con or catalog.db()
    ensure_tables(con)
    t0 = time.time()

    # 🔴 Сначала УБЕЖДАЕМСЯ, что стор читается, и только потом что-то удаляем.
    # Если junction или папка временно недоступны, _files() возвращает пустой
    # список, все известные файлы считаются исчезнувшими и индекс памяти
    # стирается целиком — 1 запись превращалась в 0 (воспроизвёл архитектор
    # 05.08). Проверка обязана стоять ДО DELETE, в том числе при force=True.
    if not os.path.isdir(STORE):
        if verbose:
            print(f"память: стор недоступен ({STORE}) — индекс сохранён, ничего не трогаю")
        return -1                      # отрицательное = деградация, не «нет изменений»

    files = _files()
    if not files:
        if verbose:
            print("память: в сторе не найдено ни одного файла — "
                  "похоже на сбой доступа, индекс сохранён")
        return -1

    known = {}
    if not force:
        for p, m in con.execute("SELECT path, MAX(mtime) FROM mem_docs GROUP BY path"):
            known[p] = m
    else:
        con.execute("DELETE FROM mem_docs")

    # файлы, исчезнувшие с диска, из индекса убираем
    gone = set(known) - set(files)
    for p in gone:
        con.execute("DELETE FROM mem_docs WHERE path=?", (p,))

    rows, texts, touched = [], [], []
    for path in files:
        try:
            mt = os.path.getmtime(path)
            if not force and known.get(path) == mt:
                continue                      # не менялся
            raw = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        # 🔴 Любое удаление считаем изменением. Если файл сократили ниже порога
        # куска, старые строки стирались, новых не появлялось, и функция
        # выходила ДО commit — после переоткрытия базы старый текст возвращался
        # (воспроизвёл архитектор 05.08).
        if con.execute("DELETE FROM mem_docs WHERE path=?", (path,)).rowcount:
            touched.append(path)
        clean, _kinds = redact.redact(raw)
        title = os.path.basename(path)
        for section, body in _split(clean):
            rows.append((path, title, section, body, os.path.getmtime(path)))
            # заголовок файла и раздела подмешиваем в текст для поиска: он
            # часто и есть тема («hub_infra», «airline_miles_research»)
            texts.append(f"{title} {section}\n{body}")

    if not rows:
        # 🔴 Удаления надо ЗАКРЕПИТЬ, даже если новых кусков нет. Раньше выход
        # стоял до commit(), и удаление жило только внутри соединения: после
        # закрытия базы откатывалось, и стёртый файл памяти снова «находился».
        # Считаем и исчезнувшие файлы, и просто опустевшие (сокращённые ниже
        # порога куска) — второй случай архитектор поймал отдельно 05.08.
        n_gone = len(gone) + len(touched)
        if n_gone:
            con.commit()
            if verbose:
                print(f"память: вычищено файлов {n_gone} "
                      f"(исчезло {len(gone)}, опустело {len(touched)}), "
                      f"{time.time() - t0:.1f} с")
            return n_gone
        if verbose:
            print(f"память: изменений нет ({time.time() - t0:.1f} с)")
        return 0
    try:
        vecs = embed(texts)
    except Exception:
        vecs = [None] * len(rows)
    for r, v in zip(rows, vecs):
        con.execute("INSERT INTO mem_docs(path,title,section,text,mtime,vec) VALUES(?,?,?,?,?,?)",
                    (*r, array.array("f", v).tobytes() if v else None))
    con.commit()
    if verbose:
        print(f"память: обновлено {len(rows)} кусков из "
              f"{len(set(r[0] for r in rows))} файлов, {time.time() - t0:.1f} с"
              + (f", удалено файлов: {len(gone)}" if gone else ""))
    return len(rows)


_CACHE = {"rows": None, "M": None}


def search(query, k=4, con=None):
    import numpy as np
    con = con or catalog.db()
    ensure_tables(con)
    con.row_factory = sqlite3.Row
    if _CACHE["rows"] is None:
        rows = con.execute("SELECT * FROM mem_docs WHERE vec IS NOT NULL").fetchall()
        if not rows:
            return []
        dim = len(rows[0]["vec"]) // 4
        _CACHE["rows"] = rows
        _CACHE["M"] = np.frombuffer(b"".join(r["vec"] for r in rows),
                                    dtype="float32").reshape(len(rows), dim)
    rows, M = _CACHE["rows"], _CACHE["M"]
    qv = np.asarray(embed([query])[0], dtype="float32")
    sims = (M @ qv) / (np.linalg.norm(M, axis=1) * np.linalg.norm(qv) + 1e-9)

    # Смысловая близость у BGE-M3 лежит в узкой полосе, и нужный кусок может
    # уступить соседнему по формулировке. Поэтому поднимаем те, где буквально
    # встретилось редкое слово запроса — в том числе в латинском написании
    # («аэроплан» → Aeroplan): такое совпадение почти всегда по делу.
    try:
        import translit
        # Только редкие опознаваемые термины: названия в латинице (из запроса
        # или восстановленные из кириллицы) и числа-идентификаторы. Обычные
        # длинные слова («действует», «числа») в бонус не берём — они есть
        # почти везде и поднимают случайные куски (проверено: нужный ответ
        # вылетал из первой четвёрки).
        keys = [w.lower() for w in re.findall(r"\b[A-Za-z]{4,}\b|\b\d{4,}\b", query)]
        keys += [v for v in translit.expand(query)
                 if v in translit.BRANDS.values()]
        if keys:
            for i, r in enumerate(rows):
                low = r["text"].lower()
                n = sum(1 for kk in set(keys) if kk in low)
                if n:
                    sims[i] += 0.04 * min(n, 3)
    except Exception:
        pass

    out = []
    for i in np.argsort(-sims)[:k]:
        r = rows[int(i)]
        out.append({
            "score": float(sims[i]), "title": r["title"], "section": r["section"],
            "path": os.path.relpath(r["path"], os.path.dirname(STORE)),
            "text": " ".join(r["text"].split()),
        })
    return out


def main():
    ap = argparse.ArgumentParser(description="Поиск по файлам памяти")
    ap.add_argument("query", nargs="?")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--k", type=int, default=4)
    a = ap.parse_args()
    if a.build:
        build()
        if not a.query:
            return
    if not a.query:
        ap.error("нужен запрос или --build")
    for h in search(a.query, a.k):
        print(f"\n[{h['score']:.2f}] {h['title']} › {h['section'][:60]}")
        print(f"   {h['text'][:220]}")


if __name__ == "__main__":
    main()
