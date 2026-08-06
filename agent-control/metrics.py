#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Измеритель эксперимента: достаёт показатели ИЗ БАЗЫ, а не из журналов вручную.

🔴 Смысл: решение «окупается ли второй исполнитель» должно опираться на числа,
которые можно перепроверить, а не на ощущение «стало быстрее». Всё считается по
событиям координатора, поэтому любой результат воспроизводим.

Границей серии служит `state/experiment.json`: run_id и стартовый номер события.
События до границы в расчёт не берутся — старые прогоны не смешиваются с новыми.

    metrics.py --series EXP-A         показатели серии
    metrics.py --series EXP-A --json  то же машинно
"""

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time

ROOT = os.environ.get("CP_ROOT", "/opt/agent-control")
DB = os.environ.get("CP_DB", os.path.join(ROOT, "cp.db"))
BOUND = os.path.join(ROOT, "state", "experiment.json")


def boundary():
    try:
        with open(BOUND, encoding="utf-8") as f:
            b = json.load(f)
        return b.get("начало_события_id", 0), b.get("run_id", "?")
    except OSError:
        return 0, "?"


def med(xs):
    return round(statistics.median(xs), 1) if xs else None


def mad(xs):
    """Медианное абсолютное отклонение: при шести наблюдениях среднее слишком
    легко перекашивается одной зависшей задачей."""
    if len(xs) < 2:
        return None
    m = statistics.median(xs)
    return round(statistics.median([abs(x - m) for x in xs]), 1)


def collect(con, series, since_id):
    """Собрать по каждой задаче серии её временные точки."""
    rows = con.execute(
        "SELECT id, ts, agent_id, task_id, kind, payload FROM events "
        "WHERE id > ? AND task_id LIKE ? ORDER BY id", (since_id, series + "%")
    ).fetchall()
    tasks = {}
    for _id, ts, agent, task, kind, payload in rows:
        t = tasks.setdefault(task, {"agent": agent, "события": [],
                                    "конфликты": [], "отозвано": 0,
                                    "проверок": 0, "отказов": 0})
        t["события"].append((ts, kind))
        if agent and kind == "lease_acquired":
            t["agent"] = agent
        if kind == "lease_conflict":
            t["конфликты"].append(json.loads(payload or "{}"))
        if kind in ("task_lease_revoked", "lease_revoked_by_hold"):
            t["отозвано"] += 1
        if kind == "handoff_rejected":
            t["отказов"] += 1
    return tasks


def moment(t, kinds, last=False):
    got = [ts for ts, k in t["события"] if k in kinds]
    if not got:
        return None
    return max(got) if last else min(got)


def report(series, as_json=False):
    since_id, run_id = boundary()
    con = sqlite3.connect(DB)
    tasks = collect(con, series, since_id)
    if not tasks:
        print(f"в серии {series} после границы {run_id} событий нет")
        return 1

    per = []
    for task, t in sorted(tasks.items()):
        start = moment(t, {"lease_acquired"})
        offer = moment(t, {"handoff_offered"}, last=True)
        accept = moment(t, {"handoff_accepted"})
        first_offer = moment(t, {"handoff_offered"})
        per.append({
            "задача": task, "исполнитель": t["agent"],
            "старт": start, "предложен": offer, "принят": accept,
            "работа_с": (offer - start) if (start and offer) else None,
            "ожидание_приёмки_с": (accept - offer) if (accept and offer) else None,
            "конфликтов": len(t["конфликты"]),
            "ожидание_конфликтов_с": sum(c.get("expires_in_s", 0)
                                         for c in t["конфликты"]),
            "отозвано_аренд": t["отозвано"],
            "отказов_приёмки": t["отказов"],
            "принят_с_первого_раза": bool(accept and t["отказов"] == 0),
            "переделка_после_отказа_с": ((offer - first_offer)
                                         if (offer and first_offer
                                             and offer != first_offer) else None),
        })

    work = [p["работа_с"] for p in per if p["работа_с"] is not None]
    review = [p["ожидание_приёмки_с"] for p in per if p["ожидание_приёмки_с"] is not None]
    starts = [p["старт"] for p in per if p["старт"]]
    offers = [p["предложен"] for p in per if p["предложен"]]
    t_pair = (max(offers) - min(starts)) if (starts and offers) else None
    seq = sum(work) if work else None
    speedup = round(seq / t_pair, 2) if (seq and t_pair) else None
    conflicts = sum(p["конфликтов"] for p in per)

    out = {
        "серия": series, "граница": run_id, "задач": len(per),
        "работа_медиана_с": med(work), "работа_разброс_с":
            [min(work), max(work)] if work else None,
        "работа_отклонение_с": mad(work),
        "ожидание_приёмки_медиана_с": med(review),
        "последовательный_срок_с": seq, "фактический_срок_пары_с": t_pair,
        "ускорение": speedup,
        "конфликтов_всего": conflicts,
        "отозвано_аренд": sum(p["отозвано_аренд"] for p in per),
        "принято_с_первого_раза": sum(1 for p in per if p["принят_с_первого_раза"]),
        "задачи": per,
    }
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    print(f"Серия {series} · граница {run_id} · задач {len(per)}")
    print()
    print(f"{'задача':<22}{'кто':<8}{'работа,с':>10}{'приёмка,с':>11}"
          f"{'конфл':>7}{'отказы':>8}")
    for p in per:
        print(f"{p['задача']:<22}{str(p['исполнитель'] or '—'):<8}"
              f"{str(p['работа_с'] or '—'):>10}{str(p['ожидание_приёмки_с'] or '—'):>11}"
              f"{p['конфликтов']:>7}{p['отказов_приёмки']:>8}")
    print()
    print(f"  работа: медиана {out['работа_медиана_с']} с, "
          f"разброс {out['работа_разброс_с']}, отклонение {out['работа_отклонение_с']}")
    print(f"  ожидание приёмки: медиана {out['ожидание_приёмки_медиана_с']} с")
    if speedup:
        print(f"  последовательно вышло бы {seq} с, фактически {t_pair} с → "
              f"ускорение {speedup}")
    print(f"  конфликтов: {conflicts}"
          + ("  🔴 в независимой серии должно быть ноль"
             if conflicts and series.startswith("EXP-A") else ""))
    print(f"  отозвано аренд: {out['отозвано_аренд']}")
    print(f"  принято с первого раза: {out['принято_с_первого_раза']} из {len(per)}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="EXP-A")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    return report(a.series, a.json)


if __name__ == "__main__":
    sys.exit(main())
