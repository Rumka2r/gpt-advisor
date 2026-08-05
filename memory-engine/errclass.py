#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Разбор провалов замера по четырём группам (схема архитектора 04.08).

Без такой разметки правки делаются вслепую: непонятно, чинить показ, поиск
или сам набор. Группы:

  ПОКАЗ         — значение есть в найденных эпизодах, но не попало в выдачу
  ПОИСК         — значение есть в базе, но в другом эпизоде, который не нашли
  ДИЗАМБИГУАЦИЯ — значение показано, но выбрано не то из нескольких
  НАБОР         — значения нет нигде: либо эталон плохой, либо ответа нет

Работать надо с первой: она даёт самый дешёвый прирост.
"""
import json, os, sqlite3, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qset  # noqa: E402

BENCH = os.path.expanduser("~/.claude/continuity/bench")
SNAP = os.path.join(BENCH, "frozen.sqlite")


def classify(tag="answer_span", part="open"):
    run = json.load(open(os.path.join(BENCH, f"run_{tag}.json"), encoding="utf-8"))
    items = {i["question"]: i for i in qset.load(part)}
    con = sqlite3.connect(SNAP)
    con.row_factory = sqlite3.Row
    import find

    groups = {"ПОКАЗ": [], "ПОИСК": [], "ДИЗАМБИГУАЦИЯ": [], "НАБОР": []}
    for q, res in run["results"].items():
        if res["shallow"]:
            continue                      # не провал
        it = items.get(q)
        if not it:
            continue
        gold = it["expect"][0]

        # есть ли эталон вообще в корпусе
        in_corpus = con.execute(
            "SELECT COUNT(*) FROM events WHERE text LIKE ? AND sub=0",
            (f"%{gold}%",)).fetchone()[0]
        if not in_corpus:
            groups["НАБОР"].append((q, gold, "значения нет в истории"))
            continue

        # есть ли эталон внутри найденных эпизодов
        hits = find.search(q, k=5, con=con)
        inside = False
        for h in hits:
            row = con.execute(
                "SELECT goal_text, outcome_text, facts, detail_text FROM episodes "
                "WHERE start_uuid=? OR uuids LIKE ?", (h["anchor"], f"%{h['anchor']}%")
            ).fetchone()
            if row and gold.lower() in " ".join(str(x or "") for x in row).lower():
                inside = True
                break
        if not inside:
            groups["ПОИСК"].append((q, gold, "нужный эпизод не найден"))
            continue

        shown = any(h.get("answer_value") for h in hits)
        if shown:
            groups["ДИЗАМБИГУАЦИЯ"].append(
                (q, gold, "показано другое значение: " +
                 str(next((h["answer_value"] for h in hits if h.get("answer_value")), "?"))))
        else:
            groups["ПОКАЗ"].append((q, gold, "значение в эпизоде есть, но не выделено"))
    con.close()
    return groups


if __name__ == "__main__":
    g = classify(sys.argv[1] if len(sys.argv) > 1 else "answer_span")
    total = sum(len(v) for v in g.values())
    print(f"провалов разобрано: {total}\n")
    for name in ("ПОКАЗ", "ДИЗАМБИГУАЦИЯ", "ПОИСК", "НАБОР"):
        rows = g[name]
        print(f"{name}: {len(rows)}")
        for q, gold, why in rows[:4]:
            print(f"   · ждали «{gold}» — {why}")
            print(f"     {q[:88]}")
        print()
