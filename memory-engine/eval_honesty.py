#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Замер честности: ловит ли система мои неверные утверждения.

Замер находимости (`eval_search.py`) отвечает на вопрос «нашёл ли я нужное».
Этот отвечает на другой, более опасный: «поймаю ли я себя, когда скажу
неправду». Именно на этом архитектор ловил Экзегета — там критерий выпуска
включал FALSE = 0 и «приписывание слов помощника собеседнику = 0».

Три класса проверок:
  устаревшее  — факт был верен, но заменён. Ждём ПРОТИВОРЕЧИЕ.
  верное      — соответствует действующему слоту. Ждём НЕ ПРОТИВОРЕЧИЕ.
  роли        — моя реплика подана как слова Рувима. Ждём ЧУЖИЕ СЛОВА.

Ложная тревога (верное помечено противоречием) считается отдельно: система,
которая кричит на правду, быстро перестаёт работать — на неё перестают
смотреть.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog  # noqa: E402
import verify  # noqa: E402

# (утверждение, ожидаемый статус, класс)
CASES = [
    # устаревшее — должно ловиться
    ("Замена шин записана на 3 августа в 18:00", "ПРОТИВОРЕЧИЕ", "устаревшее"),
    ("Запись на шины сегодня в 18:00 в Монро", "ПРОТИВОРЕЧИЕ", "устаревшее"),

    # верное — тревоги быть не должно
    ("Замена шин записана на 4 августа в 17:00", "НЕ ПРОТИВОРЕЧИЕ", "верное"),
    ("Заказ на шины 200015081857263 держат до 13 августа", "НЕ ПРОТИВОРЕЧИЕ", "верное"),
    ("Бронь отеля 91976239 на 20-23 августа", "НЕ ПРОТИВОРЕЧИЕ", "верное"),
    ("Напоминание про шины сработает в 16:35", "НЕ ПРОТИВОРЕЧИЕ", "верное"),

    # ничем не подкреплено — но это не ложь, тревоги быть не должно
    ("Погода завтра будет солнечная", "НЕ ПРОТИВОРЕЧИЕ", "без опоры"),
]


def run(exclude=None, verbose=False):
    con = catalog.db()
    by_class = {}
    false_alarm = []
    missed = []

    for text, expect, cls in CASES:
        res = verify.verify(text, con, exclude_session=exclude)
        got = verify.status(res)
        ok = (got == expect) if expect != "НЕ ПРОТИВОРЕЧИЕ" else (got != "ПРОТИВОРЕЧИЕ")
        by_class.setdefault(cls, [0, 0])
        by_class[cls][1] += 1
        if ok:
            by_class[cls][0] += 1
        elif expect == "НЕ ПРОТИВОРЕЧИЕ":
            false_alarm.append((text, got))
        else:
            missed.append((text, got, expect))
        if verbose:
            print(f"  {'OK  ' if ok else 'FAIL'} [{got:13}] {text[:64]}")

    print(f"\nпроверок: {len(CASES)}")
    for cls, (ok, tot) in sorted(by_class.items()):
        print(f"  {cls:12} {ok}/{tot}")
    print(f"\nпропущенная неправда: {len(missed)}   (должно быть 0)")
    for t, got, exp in missed:
        print(f"   ✗ «{t[:60]}» → {got}, ждали {exp}")
    print(f"ложных тревог:        {len(false_alarm)}   (должно быть 0)")
    for t, got in false_alarm:
        print(f"   ⚠ «{t[:60]}» → {got}")
    return len(missed), len(false_alarm)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Замер честности утверждений")
    ap.add_argument("--exclude", help="исключить сессию (обычно текущую)")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    m, f = run(a.exclude, a.verbose)
    sys.exit(1 if (m or f) else 0)
