#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Набор для замера из НАСТОЯЩИХ вопросов Рувима, а не из придуманных мной.

Две попытки до этого честно провалились:
  1. Вопросы сочинял я — и невольно подгонял под то, что система умеет.
     04.08: замер дал +4, но починились ровно те вопросы, под которые я перед
     этим добавил сущности в реестр.
  2. Вопросы собирались автоматически из фактов — вышел мусор вроде «какая
     сумма по DeepSeek → $2.5», где в строке это цена другой модели, и «какой
     код по P3-VANISHED-INBOX → VANISHED», где ответ сидит внутри вопроса.

Здесь берётся то, что подделать нельзя: РЕАЛЬНАЯ реплика-вопрос Рувима и
конкретное значение из МОЕГО ответа на неё в том же turn block. Формулировка
живая, со всеми «слушай» и «а помнишь» — именно так он и спрашивает.

Эталон засчитывается, только если значение действительно прозвучало в моём
ответе на этот вопрос: тогда «правильный ответ» — не мнение, а факт разговора.

    python qset.py build --size 200
    python qset.py show --part open
    python qset.py stats
"""

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog  # noqa: E402

QSET = os.path.expanduser("~/.claude/continuity/bench/qset.json")
SEED = 20260804

# Реплика — вопрос, а не приказ. «Сделай X» проверить нечем: там нет ответа.
QUESTION = re.compile(
    r"\?|^\s*(?:а\s+)?(?:что|чем|кто|где|когда|сколько|какой|какая|какие|каком|"
    r"каким|почему|зачем|как\b|во\s+сколько|напомни|помнишь|скажи)", re.I)

# Тип вопроса должен совпадать с типом ответа. Без этого набор врёт: на
# вопрос «как добавить тестировщиков» эталоном становилось 11:06 — время
# публикации, случайно попавшее в ответ. Такой «промах» ничего не говорит о
# поиске, а замер занижается шумом (проверено: 52% против 93% с дочитыванием).
ASKS = [
    ("сумма", re.compile(r"сколько|цен[ауые]|стоим|стоит|дорог|дешев|дешёв|плат|"
                         r"бюджет|долл|бакс|почём", re.I),
     re.compile(r"\$\s?\d[\d.,]{1,9}")),
    ("время", re.compile(r"во\s+сколько|час[уыоа]?\b|время|когда|успе|запис|"
                         r"назначен|прием|приём|финиш|законч", re.I),
     re.compile(r"\b\d{1,2}:\d{2}\b")),
    ("номер", re.compile(r"номер|заказ|брон|подтвержд|трекинг|отслеж|"
                         r"идентификат", re.I),
     re.compile(r"\b\d{6,20}\b")),
    ("адрес", re.compile(r"\bip\b|адрес|сервер|порт\b|хост", re.I),
     re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|\b\d{4,5}\b")),
]

# Если тип вопроса не опознан, эталон берётся «как получится». Такие вопросы
# помечаются слабыми и считаются ОТДЕЛЬНО: по ним нельзя судить о качестве
# поиска, но видно, находит ли система хоть что-то по живой формулировке.
ANY_VALUE = re.compile(r"\b\d{6,20}\b|\$\s?\d[\d.,]{1,9}|\b\d{1,2}:\d{2}\b")

# Разговоры про сам поиск и память — это про инструмент, а не про дела Рувима.
SELFTALK = re.compile(
    r"эпизод|индекс|замер|метрик|find\.py|fts|каталог событ|якор|"
    r"поисков|слот[аоуы]?\b|нит[ьи]\b|graphiti|провенанс", re.I)


def build(size=200, verbose=True):
    import episodes
    con = catalog.db()
    con.row_factory = sqlite3.Row

    sessions = [r[0] for r in con.execute(
        "SELECT DISTINCT session FROM events WHERE sub=0 AND text!='' ORDER BY session")]

    items, seen = [], set()
    for s in sessions:
        try:
            blocks = episodes.turn_blocks(con, s)
        except Exception:
            continue
        for b in blocks:
            q = " ".join(b["user"].split())
            if not (20 <= len(q) <= 220) or not QUESTION.search(q):
                continue
            if SELFTALK.search(q):
                continue
            # значение ищем в МОИХ словах, не в выводах инструментов
            said = " ".join(t for _u, t in b["assistant"]
                            if not t.lstrip().startswith("[вызов"))
            if not said or SELFTALK.search(said[:400]):
                continue
            # берём только тот тип значения, о котором и спрашивают
            kind = m = None
            for k_name, ask_rx, val_rx in ASKS:
                if ask_rx.search(q):
                    m = val_rx.search(said)
                    kind = k_name
                    break
            strong = m is not None
            if not m:
                m = ANY_VALUE.search(said)
                kind = "слабый"
            if not m:
                continue
            value = m.group(0).strip()
            # значение, уже прозвучавшее в самом вопросе, ничего не проверяет
            if value.strip("$ ") in q:
                continue
            key = hashlib.sha256(f"{q[:80]}|{value}".encode()).hexdigest()[:10]
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "id": key,
                "question": q,
                "expect": [value],
                "anchor": b["start_uuid"],
                "when": (b["ts"] or "")[:10],
                "kind": kind, "strong": strong,
                "answer_line": " ".join(said[max(0, m.start() - 90):m.start() + 110].split()),
            })

    rnd = random.Random(SEED)
    rnd.shuffle(items)
    items = items[:size]
    for i, it in enumerate(items):
        it["part"] = "hidden" if i % 10 < 3 else "open"   # 30% скрытых

    os.makedirs(os.path.dirname(QSET), exist_ok=True)
    with open(QSET, "w", encoding="utf-8") as f:
        json.dump({"seed": SEED, "total": len(items),
                   "source": "реальные вопросы Рувима из истории",
                   "items": items}, f, ensure_ascii=False, indent=1)
    if verbose:
        op = sum(1 for i in items if i["part"] == "open")
        print(f"набор: {len(items)} вопросов (открытых {op}, скрытых {len(items) - op})")
        if items:
            print(f"период: {min(i['when'] for i in items)} … {max(i['when'] for i in items)}")
    return items


def load(part=None):
    if not os.path.exists(QSET):
        return []
    items = json.load(open(QSET, encoding="utf-8"))["items"]
    return [i for i in items if not part or i["part"] == part]


def main():
    ap = argparse.ArgumentParser(description="Набор вопросов для замера")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("build")
    p.add_argument("--size", type=int, default=200)
    p = sub.add_parser("show")
    p.add_argument("--part", choices=["open", "hidden"])
    p.add_argument("--limit", type=int, default=10)
    sub.add_parser("stats")
    a = ap.parse_args()

    if a.cmd == "build":
        build(a.size)
    elif a.cmd == "show":
        for i in load(a.part)[:a.limit]:
            print(f"\n[{i['part']}] {i['question'][:120]}")
            print(f"   ждём: {i['expect'][0]}   ({i['when']})")
            print(f"   из ответа: …{i['answer_line'][:120]}…")
    else:
        items = load()
        if not items:
            print("набора нет: qset.py build")
            return
        op = sum(1 for i in items if i["part"] == "open")
        print(f"всего {len(items)}: открытых {op}, скрытых {len(items) - op}")


if __name__ == "__main__":
    main()
