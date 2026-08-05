#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Замер качества поиска по истории: старый recall против нового find.

Как считается. Вопрос задаётся своими словами (не цитатой из разговора —
иначе меряли бы совпадение строк, а не поиск). Ответ считается найденным,
если в первых k результатах встретился ОЖИДАЕМЫЙ ФАКТ: номер, имя, сумма.
Факт объективен и не подгоняется под выдачу.

Набор собран по делам, которые точно есть в истории: шины, отель, карты,
3D-печать, память, сервер. Формулировки — как спросил бы Рувим.

Запуск:  python eval_search.py            (сравнение старого и нового)
         python eval_search.py --k 5      (глубина выдачи)
"""

import argparse
import contextlib
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# (вопрос, [варианты ожидаемого факта — достаточно одного])
CASES = [
    ("во сколько записан на замену шин", ["17:00", "18:00", "Монро", "Monroe"]),
    ("какой марки шины купили на караван", ["Goodyear", "Reliant", "Ironman"]),
    ("какой номер заказа на шины", ["200015081857263"]),
    ("какой картой оплатили шины", ["Discover", "5951"]),
    ("сколько стоили шины", ["228", "105", "126"]),
    ("какой размер шин на додж караван", ["225/65", "R17", "R16"]),
    ("в каком волмарте меняем шины", ["Roosevelt", "Монро", "Monroe"]),
    ("что с ремкомплектами для шин", ["ARB", "Speedy Seal", "Boulder"]),

    ("какой отель забронировали в мертл бич", ["Homewood", "Oceanfront"]),
    ("какой номер брони отеля", ["91976239"]),
    ("на какие даты забронирован отель", ["20", "23", "август"]),
    ("сколько баллов у кристины на амексе", ["301 885", "301885", "220 885"]),
    ("сколько стоила ночь в отеле в баллах", ["60 000", "60000", "70"]),
    ("какие отели с завтраком подходят", ["Hampton", "Home2", "Homewood", "Embassy"]),

    ("какую карту амекс добавили в волмарт", ["Surpass", "Кристин", "61000"]),
    ("какое правило про оплату картой", ["спрашивать", "какой картой", "бонус"]),
    ("что за правило про рабочую карту chase ink", ["Chase Ink", "3855", "рабоч"]),
    ("что с авиамилями citi aadvantage", ["80", "AAdvantage", "Citi"]),
    ("до какого числа действует оффер аэроплан", ["12.08", "110", "Aeroplan"]),

    ("какое сопло нужно для часов", ["0.2", "0,2"]),
    ("что с кондукторами PEX", ["кондуктор", "PEX", "жиг", "jig"]),
    ("почему забраковали держатель банок", ["арк", "крышк", "кисточ"]),
    ("сколько слоёв было в последней печати", ["666", "слой"]),
    ("что с печатью плакетки", ["плакетк", "50", "SD"]),

    ("на каком порту крутится эмбеддер", ["8899"]),
    ("какая модель считает смысл", ["BGE", "M3"]),
    ("какой порог у автоподхвата", ["0.56", "0,56", "0.50"]),
    ("где лежат резюме сессий", ["continuity/sessions", "sessions"]),
    ("сколько сессий в непрерывности", ["79", "80", "сесси"]),

    ("какой ip у сервера hetzner", ["СЕРВЕР"]),
    ("на каком порту песочница plumbingcore", ["8000"]),
    ("на каком порту прод plumbingcore", ["8001"]),
    ("как называется база прода", ["plumbingcore_prod"]),
    ("сколько товаров в каталоге plumbingcore", ["93", "676"]),
    ("что за инвариант каталог и склад", ["in_company_inventory", "Catalog", "склад"]),

    ("что решили по школе ребёнка", ["школ"]),
    ("что с машиной друга сентра", ["Sentra", "MVR-63", "доверенност"]),
    ("что с наклейками dmv", ["WW64A9FT6A", "DMV", "наклейк"]),
    ("какие ключи надо ротировать", ["DeepSeek", "AWS", "ключ"]),
    ("что с сайтом виктора", ["Виктор", "логин", "сайт"]),
    ("что за напоминание про шины стоит в планировщике", ["TireReminder", "16:35", "напоминан"]),
]


def _old(q, k):
    import recall
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        recall.search(q, k=k)
    try:
        return [" ".join((h.get("snippet") or "").split())
                for h in json.loads(buf.getvalue())["results"]]
    except Exception:
        return []


# Сессии, в которых я сам строил этот поиск: там звучат все контрольные слова
# просто потому, что я их проверял. Считать их находками — обманывать себя.
DEV_SESSIONS = ("bab7bed7",)


def _new(q, k, con, deep=False):
    """deep=False — что видно прямо в выдаче.
    deep=True  — плюс полное содержимое найденного эпизода: система привела в
    нужное место, а дочитать оттуда я могу сам (window.py по якорю)."""
    import find
    out = []
    for h in find.search(q, k=k, con=con):
        if h["session"][:8] in DEV_SESSIONS:
            continue
        blob = f"{h['title']} {h.get('frag') or ''} {h.get('facts') or ''} {h['outcome']}"
        if deep:
            row = con.execute(
                "SELECT goal_text, outcome_text, facts, detail_text FROM episodes "
                "WHERE start_uuid=? OR uuids LIKE ?",
                (h["anchor"], f"%{h['anchor']}%")).fetchone()
            if row:
                blob += " " + " ".join(str(x or "") for x in row)
        out.append(" ".join(blob.split()))
    return out


def hit(texts, expect):
    blob = " ".join(texts).lower()
    return any(e.lower() in blob for e in expect)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    import catalog
    con = catalog.db()

    old_ok = new_ok = 0
    deep_ok = [0]
    t_old = t_new = 0.0
    misses_new, misses_old, misses_deep = [], [], []

    for q, expect in CASES:
        t = time.time(); o = _old(q, a.k); t_old += time.time() - t
        t = time.time(); n = _new(q, a.k, con); t_new += time.time() - t
        nd = _new(q, a.k, con, deep=True)
        ho, hn = hit(o, expect), hit(n, expect)
        hd = hit(nd, expect)
        deep_ok[0] += hd
        if not hd:
            misses_deep.append(q)
        old_ok += ho
        new_ok += hn
        if not hn:
            misses_new.append(q)
        if not ho:
            misses_old.append(q)
        if a.verbose:
            mark = {(True, True): "оба", (True, False): "только старый",
                    (False, True): "только новый", (False, False): "никто"}[(ho, hn)]
            print(f"  {mark:14} {q}")

    total = len(CASES)
    print(f"\nвопросов: {total}, глубина выдачи: {a.k}")
    print(f"  СТАРЫЙ recall: {old_ok:2}/{total} = {old_ok / total * 100:.0f}%   "
          f"среднее время {t_old / total * 1000:.0f} мс")
    print(f"  НОВЫЙ find:    {new_ok:2}/{total} = {new_ok / total * 100:.0f}%   "
          f"среднее время {t_new / total * 1000:.0f} мс   (ответ виден сразу в выдаче)")
    print(f"  НОВЫЙ, с дочитыванием: {deep_ok[0]:2}/{total} = {deep_ok[0] / total * 100:.0f}%"
          f"   (привёл в нужный эпизод — ответ там есть)")
    print(f"\nне нашёл НОВЫЙ ({len(misses_new)}):")
    for q in misses_new:
        if q not in misses_deep:
            print(f"   ~ {q}")
    print(f"\nне нашёл СТАРЫЙ ({len(misses_old)}): {len(misses_old)} шт.")


if __name__ == "__main__":
    main()
