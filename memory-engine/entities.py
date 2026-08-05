#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Реестр сущностей и связей между ними — вместо полноценного графа.

Решение принято 04.08.2026 после разбора с GPT-архитектором (чат «Память и
поиск системы»). Полный граф (Graphiti/Neo4j) отвергнут по замеру: доля
запросов, где нужен обход связей, — 0.2% из 1512 реальных реплик. Цена же
реальная: извлечение сущностей моделью на каждый эпизод плюс графовая база.

Показательный аргумент: Mem0 в новой версии сам отказался от внешнего
графового хранилища в пользу лёгкого связывания сущностей.

Что здесь есть:
  1. РЕЕСТР — у одной вещи много имён: «караван», «Dodge Grand Caravan»,
     «додж». Поиск по любому имени должен находить всё.
  2. ПЯТЬ типов связей, строго типизированных:
       SAME_ENTITY     — одно и то же разными словами
       PART_OF_THREAD  — дело входит в работу
       SUPERSEDES      — значение заменило прежнее
       DEPENDS_ON      — одно требует другого
       PRODUCED_BY     — факт получен в таком-то эпизоде

Чего здесь НЕТ намеренно (прямой запрет архитектора): связей «упоминались
вместе», «похожи по смыслу» и любых свободных отношений от модели. Они
превращают реестр в плотную паутину, где связь перестаёт что-либо значить.
Нить уже сама является контейнером и не требует соединять всех со всеми.
"""

import argparse
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog  # noqa: E402

RELATIONS = ("SAME_ENTITY", "PART_OF_THREAD", "SUPERSEDES", "DEPENDS_ON", "PRODUCED_BY")


def db(con=None):
    con = con or catalog.db()
    con.execute("""CREATE TABLE IF NOT EXISTS entity(
        entity_id TEXT PRIMARY KEY,     -- каноническое имя, нормализованное
        label     TEXT NOT NULL,        -- как показывать человеку
        kind      TEXT,                 -- вещь | место | человек | проект | документ
        created_at TEXT NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS entity_alias(
        alias     TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        source    TEXT,                 -- откуда узнали: словарь | слот | вручную
        PRIMARY KEY(alias, entity_id))""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_alias ON entity_alias(alias)")
    con.execute("""CREATE TABLE IF NOT EXISTS edge(
        source_id     TEXT NOT NULL,
        relation      TEXT NOT NULL,
        target_id     TEXT NOT NULL,
        confidence    REAL DEFAULT 1.0,
        source_anchor TEXT,             -- якорь сообщения-основания
        created_at    TEXT NOT NULL,
        invalidated_at TEXT,            -- связь перестала действовать
        PRIMARY KEY(source_id, relation, target_id))""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_edge_src ON edge(source_id)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_edge_tgt ON edge(target_id)")
    con.commit()
    return con


def norm(s):
    s = (s or "").strip().lower().replace("ё", "е")
    return re.sub(r"\s+", " ", s)


def add_entity(label, kind=None, aliases=(), con=None):
    con = db(con)
    eid = norm(label)
    con.execute("INSERT OR IGNORE INTO entity(entity_id,label,kind,created_at) VALUES(?,?,?,?)",
                (eid, label, kind, time.strftime("%Y-%m-%dT%H:%M:%S")))
    rows = [(norm(a), eid, "вручную") for a in (list(aliases) + [label]) if norm(a)]
    con.executemany("INSERT OR IGNORE INTO entity_alias(alias,entity_id,source) VALUES(?,?,?)", rows)
    con.commit()
    return eid


def resolve(text, con=None):
    """Какие известные сущности упомянуты в тексте. Возвращает список entity_id."""
    con = db(con)
    con.row_factory = sqlite3.Row
    low = norm(text)
    found = []
    for r in con.execute("SELECT alias, entity_id FROM entity_alias"):
        a = r["alias"]
        if len(a) < 3:
            continue
        if re.search(r"(?<!\w)" + re.escape(a[:max(4, len(a) - 2)]), low):
            if r["entity_id"] not in found:
                found.append(r["entity_id"])
    return found


def link(source_id, relation, target_id, anchor=None, confidence=1.0, con=None):
    if relation not in RELATIONS:
        raise ValueError(f"неизвестный тип связи: {relation}. Разрешены: {RELATIONS}")
    con = db(con)
    con.execute("""INSERT OR REPLACE INTO edge(source_id,relation,target_id,confidence,
        source_anchor,created_at) VALUES(?,?,?,?,?,?)""",
        (norm(source_id), relation, norm(target_id), confidence, anchor,
         time.strftime("%Y-%m-%dT%H:%M:%S")))
    con.commit()


def neighbours(entity_id, con=None, include_invalid=False):
    con = db(con)
    con.row_factory = sqlite3.Row
    q = ("SELECT relation, target_id AS other, 'вперёд' AS dir, source_anchor, invalidated_at "
         "FROM edge WHERE source_id=? UNION ALL "
         "SELECT relation, source_id AS other, 'назад' AS dir, source_anchor, invalidated_at "
         "FROM edge WHERE target_id=?")
    rows = con.execute(q, (norm(entity_id), norm(entity_id))).fetchall()
    return [r for r in rows if include_invalid or not r["invalidated_at"]]


def seed(con=None, verbose=True):
    """Первичное наполнение из того, что уже известно системе.

    Берём словарь произношений (голосовой ввод) и темы слотов — то есть
    ничего не выдумываем, а сводим воедино уже проверенные соответствия.
    """
    con = db(con)
    n_ent = n_edge = 0

    # 1) имена вещей из словаря голосового ввода: «волмарт» → Walmart
    try:
        import translit
        groups = {}
        for cyr, lat in translit.BRANDS.items():
            groups.setdefault(lat, []).append(cyr)
        for lat, cyrs in groups.items():
            add_entity(lat, kind="имя", aliases=cyrs, con=con)
            n_ent += 1
    except Exception:
        pass

    # 2) темы, по которым уже заведены слоты фактов
    try:
        import claims
        for c in claims.active(con=con):
            add_entity(c["subject"], kind="дело", con=con)
            n_ent += 1
    except Exception:
        pass

    # 3) SUPERSEDES — цепочки замен в слотах: это готовая, доказанная связь
    try:
        con.row_factory = sqlite3.Row
        for r in con.execute("SELECT claim_id, supersedes, subject, predicate, source_uuid "
                             "FROM claim WHERE supersedes IS NOT NULL"):
            link(f"{r['subject']} · {r['predicate']} @{r['claim_id'][:8]}", "SUPERSEDES",
                 f"{r['subject']} · {r['predicate']} @{r['supersedes'][:8]}",
                 anchor=r["source_uuid"], con=con)
            n_edge += 1
    except Exception:
        pass

    # 4) PART_OF_THREAD — дело входит в нить (нить уже вычислена, не выдумываем)
    try:
        for r in con.execute("""SELECT t.id, t.title, e.title AS ep FROM work_threads t
                                JOIN thread_episode te ON te.thread_id=t.id
                                JOIN episodes e ON e.id=te.episode_id
                                WHERE t.sessions > 1 LIMIT 200"""):
            ent = resolve(r["ep"] or "", con)
            for eid in ent[:1]:
                link(eid, "PART_OF_THREAD", f"нить #{r['id']}", con=con)
                n_edge += 1
    except Exception:
        pass

    if verbose:
        print(f"сущностей: {con.execute('SELECT COUNT(*) FROM entity').fetchone()[0]}, "
              f"псевдонимов: {con.execute('SELECT COUNT(*) FROM entity_alias').fetchone()[0]}, "
              f"связей: {con.execute('SELECT COUNT(*) FROM edge').fetchone()[0]}")
    return n_ent, n_edge


def main():
    ap = argparse.ArgumentParser(description="Реестр сущностей и типизированных связей")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("seed", help="наполнить из словарей и слотов")

    p = sub.add_parser("add", help="добавить сущность")
    p.add_argument("label"); p.add_argument("--kind"); p.add_argument("--alias", nargs="*", default=[])

    p = sub.add_parser("resolve", help="какие сущности упомянуты в тексте")
    p.add_argument("text")

    p = sub.add_parser("near", help="связи сущности")
    p.add_argument("entity")

    p = sub.add_parser("link", help="создать связь")
    p.add_argument("source"); p.add_argument("relation", choices=RELATIONS); p.add_argument("target")
    p.add_argument("--anchor")

    sub.add_parser("list", help="все сущности")

    a = ap.parse_args()
    con = db()
    if a.cmd == "seed":
        seed(con)
    elif a.cmd == "add":
        print("добавлено:", add_entity(a.label, a.kind, a.alias, con))
    elif a.cmd == "resolve":
        r = resolve(a.text, con)
        print("упомянуты:", ", ".join(r) if r else "(ничего известного)")
    elif a.cmd == "near":
        for n in neighbours(a.entity, con):
            print(f"  {n['relation']:15} {n['dir']:7} {n['other']}")
    elif a.cmd == "link":
        link(a.source, a.relation, a.target, a.anchor, con=con)
        print("связь создана")
    else:
        con.row_factory = sqlite3.Row
        for r in con.execute("SELECT * FROM entity ORDER BY kind, label"):
            al = [x[0] for x in con.execute(
                "SELECT alias FROM entity_alias WHERE entity_id=? AND alias<>?",
                (r["entity_id"], r["entity_id"]))]
            print(f"  [{r['kind'] or '—'}] {r['label']}" + (f"   ← {', '.join(al[:6])}" if al else ""))


if __name__ == "__main__":
    main()
