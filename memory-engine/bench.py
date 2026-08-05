#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Воспроизводимый замер поиска на ЗАМОРОЖЕННОМ срезе базы.

Причина (совет архитектора 04.08): живой индекс меняется прямо во время
работы — фоновый проход досыпает новые сессии, — и два прогона подряд дают
разные цифры. На таком замере невозможно доказать, что правка не сделала
хуже: непонятно, изменился код или данные.

Здесь срез фиксируется через `VACUUM INTO` (а не копированием файла: при
активном журнале WAL копия получается рассогласованной), рядом кладётся
манифест версий. Дальше замеры гоняются против снимка.

    python bench.py freeze                  # сделать срез
    python bench.py run  --tag baseline     # прогон, сохранить результат
    python bench.py run  --tag entities
    python bench.py diff baseline entities  # что исправилось, что испортилось

Главное — не проценты, а поимённое сравнение: какой вопрос починился, какой
сломался. Процент может не измениться, скрыв внутри одну победу и одну потерю.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog  # noqa: E402

ROOT = os.path.expanduser("~/.claude/continuity")
BENCH = os.path.join(ROOT, "bench")
SNAP = os.path.join(BENCH, "frozen.sqlite")
MANIFEST = os.path.join(BENCH, "frozen.manifest.json")


def _code_fingerprint():
    """Отпечаток кода поиска: если менялся, цифры сравнивать корректно только
    с оговоркой. Берём размер+mtime, этого достаточно, чтобы заметить правку."""
    out = {}
    for name in ("find.py", "fts.py", "episodes.py", "memdocs.py", "entities.py",
                 "claims.py", "threads.py"):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        try:
            st = os.stat(p)
            out[name] = f"{st.st_size}:{int(st.st_mtime)}"
        except OSError:
            out[name] = "нет файла"
    return out


def freeze(verbose=True):
    os.makedirs(BENCH, exist_ok=True)
    if os.path.exists(SNAP):
        os.remove(SNAP)
    con = catalog.db()
    # VACUUM INTO делает согласованный снимок даже при активном WAL
    con.execute("VACUUM INTO ?", (SNAP,))
    con.close()

    snap = sqlite3.connect(SNAP)
    counts = {}
    for t in ("events", "episodes", "ep_fact", "work_threads", "mem_docs",
              "claim", "entity", "edge"):
        try:
            counts[t] = snap.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            counts[t] = None
    snap.close()

    man = {
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "size_mb": round(os.path.getsize(SNAP) / 1e6, 1),
        "counts": counts,
        "code": _code_fingerprint(),
        "embedding_model": "BGE-M3 @127.0.0.1:8899",
    }
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    if verbose:
        print(f"срез заморожен: {man['size_mb']} МБ")
        print("  " + ", ".join(f"{k}={v}" for k, v in counts.items() if v))
    return man


def _run_one(query, expect, con, k, deep):
    import find
    hits = find.search(query, k=k, con=con)
    blob = []
    for h in hits:
        if h["session"][:8] in ("bab7bed7",):     # сессия разработки — не зачёт
            continue
        s = (f"{h['title']} {h.get('frag') or ''} {h.get('facts') or ''} "
             f"{h['outcome']} {h.get('answer_frag') or ''}")
        if deep:
            row = con.execute(
                "SELECT goal_text, outcome_text, facts, detail_text FROM episodes "
                "WHERE start_uuid=? OR uuids LIKE ?",
                (h["anchor"], f"%{h['anchor']}%")).fetchone()
            if row:
                s += " " + " ".join(str(x or "") for x in row)
        blob.append(s)
    low = " ".join(blob).lower()
    return any(e.lower() in low for e in expect)


def _cases(source):
    """Откуда брать вопросы: старый ручной набор или живые вопросы Рувима."""
    if source == "qset":
        import qset
        items = qset.load("open")          # скрытую часть при настройке НЕ трогаем
        return [(i["question"], i["expect"]) for i in items]
    if source == "gold":
        # Только вручную подтверждённые эталоны: 33 из 79. Остальное — мусор
        # автосборщика (41%), по которому мерить нельзя.
        import qset, gold
        g = gold.load()
        out = []
        # ТОЛЬКО открытая часть. 04.08 я по недосмотру прогнал сильные вопросы
        # целиком и сжёг 12 скрытых — они больше не являются честным test.
        for i in qset.load("open"):
            mk = g.get(i["id"])
            if not mk or mk["category"] != "ANSWERABLE_STRONG":
                continue
            out.append((i["question"], [mk["gold_answer"] or i["expect"][0]]))
        return out
    if source == "hidden":
        import qset
        items = qset.load("hidden")        # только финальная проверка
        return [(i["question"], i["expect"]) for i in items]
    import eval_search as E
    return E.CASES


def run(tag, k=5, verbose=True, source="manual"):
    if not os.path.exists(SNAP):
        print("сначала сделай срез: bench.py freeze", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(SNAP)
    con.row_factory = sqlite3.Row
    res = {}
    for q, expect in _cases(source):
        res[q] = {
            "expect": expect[0],
            "shallow": _run_one(q, expect, con, k, deep=False),
            "deep": _run_one(q, expect, con, k, deep=True),
        }
    con.close()

    sh = sum(1 for v in res.values() if v["shallow"])
    dp = sum(1 for v in res.values() if v["deep"])
    out = {
        "tag": tag, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "k": k, "source": source,
        "total": len(res), "shallow": sh, "deep": dp,
        "code": _code_fingerprint(), "results": res,
    }
    path = os.path.join(BENCH, f"run_{tag}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    if verbose:
        print(f"[{tag}] сразу {sh}/{len(res)} = {sh/len(res)*100:.0f}%   "
              f"с дочитыванием {dp}/{len(res)} = {dp/len(res)*100:.0f}%")
    return out


def diff(tag_a, tag_b):
    pa = os.path.join(BENCH, f"run_{tag_a}.json")
    pb = os.path.join(BENCH, f"run_{tag_b}.json")
    for p in (pa, pb):
        if not os.path.exists(p):
            print(f"нет прогона: {os.path.basename(p)}", file=sys.stderr)
            sys.exit(1)
    a = json.load(open(pa, encoding="utf-8"))
    b = json.load(open(pb, encoding="utf-8"))

    print(f"{tag_a}: сразу {a['shallow']}/{a['total']}, глубоко {a['deep']}/{a['total']}")
    print(f"{tag_b}: сразу {b['shallow']}/{b['total']}, глубоко {b['deep']}/{b['total']}")
    if a["code"] != b["code"]:
        changed = [k for k in a["code"] if a["code"].get(k) != b["code"].get(k)]
        print(f"код менялся между прогонами: {', '.join(changed)}")

    fixed = broke = 0
    for q, va in a["results"].items():
        vb = b["results"].get(q)
        if not vb:
            continue
        for level in ("shallow", "deep"):
            if not va[level] and vb[level]:
                print(f"  ✅ ПОЧИНИЛОСЬ ({level}): {q}")
                fixed += 1
            elif va[level] and not vb[level]:
                print(f"  ❌ СЛОМАЛОСЬ ({level}): {q}   (ждали: {va['expect']})")
                broke += 1
    print(f"\nитог: починилось {fixed}, сломалось {broke}, "
          f"чистый вклад {fixed - broke:+d}")
    return fixed, broke


def main():
    ap = argparse.ArgumentParser(description="Замер поиска на замороженном срезе")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("freeze", help="сделать срез базы")
    p = sub.add_parser("run", help="прогнать набор на срезе")
    p.add_argument("--tag", required=True)
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--source", choices=["manual", "qset", "hidden", "gold"], default="manual",
                   help="manual — старые 41; qset — открытая часть живых вопросов; "
                        "hidden — скрытая часть, только для финальной проверки")
    p = sub.add_parser("diff", help="сравнить два прогона поимённо")
    p.add_argument("a"); p.add_argument("b")
    sub.add_parser("info", help="что в срезе")
    a = ap.parse_args()

    if a.cmd == "freeze":
        freeze()
    elif a.cmd == "run":
        run(a.tag, a.k, source=a.source)
    elif a.cmd == "diff":
        diff(a.a, a.b)
    elif a.cmd == "info":
        if os.path.exists(MANIFEST):
            print(open(MANIFEST, encoding="utf-8").read())
        else:
            print("среза нет")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
