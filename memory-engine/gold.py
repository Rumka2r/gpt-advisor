#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Ручная разметка эталонов (схема архитектора 04.08).

Приговор: до очистки набора дальнейшая оптимизация запрещена — измеритель
оказался шумнее самой системы. Автоматический сборщик брал первое число из
ответа, и эталоном становилось случайное: на «сколько ВРЕМЕНИ печатать» —
время старта, на «сколько ЗВЁЗД отель» — курортный сбор, на вопрос про слои
агента — «17:21», которое на деле ссылка на Мф. 17:21.

Категории:
  ANSWERABLE_STRONG — однозначный ответ и доказательный фрагмент
  MULTI_ANSWER      — ответ состоит из нескольких частей
  AMBIGUOUS         — без доп. контекста одного правильного ответа нет
  UNANSWERABLE      — в разговоре ответа нет (нужны для проверки отказа)
  BAD_GOLD          — автоматический эталон выбран неверно

Критерий годного эталона (формулировка архитектора): в исходном turn block
есть фрагмент, который ПРЯМО и ДОСТАТОЧНО отвечает на смысл вопроса, а
выбранное значение — минимальный полный ответ. Повторения слов вопроса в
предложении НЕ требуется: это сломало бы ответы с местоимениями.

Важно: размечать, НЕ глядя на текущую выдачу системы — иначе невольно
выберешь то, что она и так показывает.

Мерим conversation recall (что агент тогда ответил), а не factual correctness
(был ли тот ответ верным). Иначе поиск наказывается за старую ошибку.
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qset  # noqa: E402

GOLD = os.path.expanduser("~/.claude/continuity/bench/gold.json")
CATS = ("ANSWERABLE_STRONG", "MULTI_ANSWER", "AMBIGUOUS", "UNANSWERABLE", "BAD_GOLD")


def load():
    return json.load(open(GOLD, encoding="utf-8")) if os.path.exists(GOLD) else {}


def save(d):
    os.makedirs(os.path.dirname(GOLD), exist_ok=True)
    json.dump(d, open(GOLD, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def mark(qid, category, gold_answer=None, intent=None, note=None):
    if category not in CATS:
        raise ValueError(f"категория должна быть из {CATS}")
    d = load()
    d[qid] = {"category": category, "gold_answer": gold_answer,
              "intent": intent, "note": note}
    save(d)
    return d[qid]


def pending(limit=10):
    d = load()
    return [i for i in qset.load() if i["id"] not in d][:limit]


def stats():
    d = load()
    all_items = qset.load()
    by = {}
    for v in d.values():
        by[v["category"]] = by.get(v["category"], 0) + 1
    print(f"размечено {len(d)} из {len(all_items)}")
    for c in CATS:
        if by.get(c):
            print(f"   {c:18} {by[c]}")
    return by


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pending":
        for i in pending(int(sys.argv[2]) if len(sys.argv) > 2 else 10):
            print(f"\n=== {i['id']}  [{i['when']}]")
            print(f"ВОПРОС: {i['question'][:200]}")
            print(f"авто-эталон: «{i['expect'][0]}»")
            print(f"строка: …{i['answer_line'][:220]}…")
    else:
        stats()
