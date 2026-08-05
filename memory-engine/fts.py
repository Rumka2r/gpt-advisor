#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Точный поиск по словам поверх каталога событий (SQLite FTS5).

Смысловой поиск (recall.py) находит «про что» и прощает разные формулировки,
но теряет точность: путь к файлу, код ошибки, номер брони, редкую фамилию.
Этот модуль — вторая половина пары: ищет буквально и возвращает те же якоря,
что понимает window.py.

Русская морфология. FTS5 не знает, что «шины» и «шинам» — одно слово,
поэтому к каждому слову запроса добавляется подстановочный хвост по основе
(«шин*»). Грубо, но работает без внешних словарей и стоит ноль.

В индекс идёт уже очищенный текст из каталога: секреты сюда не попадают.
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

# Служебные прогоны суммаризатора — это не разговор, а моя же кухня.
# Их присутствие в выдаче только зашумляет поиск.
NOISE = ("Ниже — скелет рабочей сессии", "Сделай СЖАТОЕ резюме")

# Хвост слова, который чаще всего меняется по падежам; отрезаем перед '*'.
_TAIL = re.compile(r"(ами|ями|ого|его|ому|ему|ыми|ими|ая|яя|ое|ее|ые|ие|ов|ев|ей|ам|ям|"
                   r"ах|ях|ом|ем|у|ю|а|я|ы|и|е|о|ь)$", re.I)


def build(con=None, verbose=True):
    """Строит FTS-таблицу заново из каталога."""
    con = con or catalog.db()
    t0 = time.time()
    con.execute("DROP TABLE IF EXISTS ev_fts")
    con.execute("CREATE VIRTUAL TABLE ev_fts USING fts5("
                "text, uuid UNINDEXED, tokenize='unicode61 remove_diacritics 2')")
    n = con.execute("""INSERT INTO ev_fts(text, uuid)
        SELECT text, uuid FROM events
        WHERE text != '' AND role IN ('user','assistant','tool')""").rowcount
    # Итоги эпизодов отдельной записью: там собраны номера броней, суммы и
    # подтверждения — то, что спрашивают дословно, а в потоке реплик оно
    # тонет среди рассуждений.
    try:
        n += con.execute("""INSERT INTO ev_fts(text, uuid)
            SELECT facts, start_uuid FROM episodes
            WHERE facts IS NOT NULL AND facts != ''""").rowcount
    except sqlite3.OperationalError:
        pass    # эпизодов ещё нет — не беда, соберутся следующим проходом

    # Файлы памяти тоже нужны в ТОЧНОМ поиске. До этого они искались только по
    # смыслу — и техника терялась: порт, адрес, версия записаны в памяти, но
    # запрос про них семантически далёк от текста раздела.
    try:
        con.execute("DROP TABLE IF EXISTS mem_fts")
        con.execute("CREATE VIRTUAL TABLE mem_fts USING fts5("
                    "text, doc_id UNINDEXED, tokenize='unicode61 remove_diacritics 2')")
        n += con.execute("""INSERT INTO mem_fts(text, doc_id)
            SELECT title || ' ' || COALESCE(section,'') || ' ' || text, id
            FROM mem_docs""").rowcount
    except sqlite3.OperationalError:
        pass    # памяти ещё нет
    con.commit()
    if verbose:
        print(f"FTS построен: {n} записей за {time.time() - t0:.1f} с")
    return n


# Составные значения: IP, версии, дроби, время. Токенизатор рвёт их по
# разделителям, и «0.56» превращалось в «56», а «СЕРВЕР» — в «161 55
# 171» с потерей первой части. Ищем такое фразой: порядок токенов сохранится.
_COMPOUND = re.compile(r"\b\d+(?:[.\-:/]\d+)+\b")


def _q(query):
    """Пользовательский запрос → выражение FTS5 с подстановкой по основе."""
    parts = []
    rest = query
    for m in _COMPOUND.finditer(query):
        parts.append('"%s"' % m.group(0))      # фраза целиком, без обрезки
        rest = rest.replace(m.group(0), " ")
    for w in re.findall(r"\w{2,}", rest, re.U):
        stem = _TAIL.sub("", w) if len(w) > 4 else w
        parts.append(f'"{stem}"*' if len(stem) >= 3 else f'"{w}"')
    # Латинские написания для продиктованных кириллицей названий: «волмарт» в
    # записях стоит как Walmart, и без этого точный поиск их не связывает.
    try:
        import translit
        for v in translit.expand(rest):
            if len(v) >= 4:
                parts.append(f'"{v}"*')
    except Exception:
        pass
    # Имена той же вещи из реестра сущностей: «караван» → caravan, dodge.
    # Реестр шире словаря произношений — он сводит вместе все известные имена.
    try:
        import entities
        for eid in entities.resolve(rest)[:4]:
            if len(eid) >= 4 and f'"{eid}"*' not in parts:
                parts.append(f'"{eid}"*')
    except Exception:
        pass
    return " OR ".join(parts) if parts else None


def search(query, k=8, role=None, con=None):
    con = con or catalog.db()
    con.row_factory = sqlite3.Row
    expr = _q(query)
    if not expr:
        return []
    sql = """SELECT e.uuid, e.role, e.ts, e.session, e.project, e.tool,
                    snippet(ev_fts, 0, '«', '»', '…', 18) AS frag,
                    bm25(ev_fts) AS score
             FROM ev_fts JOIN events e ON e.uuid = ev_fts.uuid
             WHERE ev_fts MATCH ?"""
    args = [expr]
    if role:
        sql += " AND e.role = ?"
        args.append(role)
    sql += " ORDER BY score LIMIT ?"
    args.append(k * 3)

    out = []
    for r in con.execute(sql, args):
        frag = r["frag"] or ""
        if any(nz in frag for nz in NOISE):
            continue
        out.append({
            "uuid": r["uuid"], "role": r["role"], "ts": (r["ts"] or "")[:16].replace("T", " "),
            "session": r["session"][:8], "project": r["project"], "tool": r["tool"],
            "score": round(r["score"], 2), "frag": " ".join(frag.split())[:240],
        })
        if len(out) >= k:
            break
    return out


def main():
    ap = argparse.ArgumentParser(description="Точный поиск по словам в истории разговоров")
    ap.add_argument("query", nargs="?")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--role", choices=["user", "assistant", "tool"])
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.build:
        build()
        if not a.query:
            return
    if not a.query:
        ap.error("нужен запрос или --build")

    hits = search(a.query, a.k, a.role)
    if a.json:
        print(json.dumps({"query": a.query, "results": hits}, ensure_ascii=False, indent=1))
        return
    if not hits:
        print("ничего не найдено")
        return
    for h in hits:
        who = {"user": "Рувим", "assistant": "Ассистент", "tool": f"Инструмент {h['tool'] or ''}"}[h["role"]]
        print(f"\n{h['ts']}  {who}  (сессия {h['session']})")
        print(f"   {h['frag']}")
        print(f"   якорь: {h['uuid']}")


if __name__ == "__main__":
    main()


def search_memory(query, k=4, con=None):
    """Точный поиск по файлам памяти — там живут порты, адреса, номера версий.

    Отдельная функция, а не расширение search(): у памяти другая единица
    (кусок файла, не событие) и другой якорь — путь, а не uuid сообщения.
    """
    con = con or catalog.db()
    con.row_factory = sqlite3.Row
    expr = _q(query)
    if not expr:
        return []
    try:
        rows = con.execute(
            "SELECT m.id, m.title, m.section, m.path, "
            "snippet(mem_fts, 0, '«', '»', '…', 20) AS frag, bm25(mem_fts) AS score "
            "FROM mem_fts JOIN mem_docs m ON m.id = mem_fts.doc_id "
            "WHERE mem_fts MATCH ? ORDER BY score LIMIT ?", (expr, k)).fetchall()
    except sqlite3.OperationalError as e:
        # 🔴 Раньше ЛЮБАЯ ошибка проглатывалась молча, включая повреждённый
        # mem_fts — здоровье о ней не узнавало никогда (замечание архитектора
        # 05.08). «Нет такой таблицы» — это ещё не собрано, остальное — поломка.
        if "no such table" in str(e).lower():
            return []
        raise
    return [{"id": r["id"], "title": r["title"], "section": r["section"],
             "path": r["path"], "score": round(r["score"], 2),
             "frag": " ".join((r["frag"] or "").split())[:260]} for r in rows]
