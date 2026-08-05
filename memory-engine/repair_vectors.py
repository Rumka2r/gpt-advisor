#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Достроить векторы, пропущенные пока лежал эмбеддер.

Дыра (нашёл GPT-архитектор 05.08.2026, подтверждено прогоном `doctor`): если
сервис эмбеддингов недоступен, `episodes.build()` пишет эпизод с `vec_goal=NULL`,
а `memdocs.build()` — кусок памяти с `vec=NULL`. После восстановления сервиса
исходный файл не изменился, поэтому сборщик считает его уже обработанным и
вектор НИКОГДА не строит. Такие записи выпадают из смыслового поиска навсегда.

На момент написания в базе было 85 эпизодов и 339 кусков памяти без вектора.

Запускать: сам после подъёма сервиса (см. worker.py) и вручную —
    python memoryctl.py doctor      — сколько дыр
    python repair_vectors.py        — залатать
"""
import array
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import catalog  # noqa: E402
import episodes  # noqa: E402
import health  # noqa: E402

BATCH = 32


def _blob(vec):
    return array.array('f', vec).tobytes() if vec else None


def repair_episodes(con, verbose=True):
    """Эпизоды: цель, итог и подробности — ТРИ отдельных вектора.

    🔴 Первая версия искала только `vec_goal IS NULL` и `vec_detail` не писала
    вовсе: после «ремонта» получалось goal=1, outcome=1, detail=0, и поиск по
    подробностям оставался слепым (воспроизвёл архитектор 05.08).
    """
    # 🔴 Берём и заголовок: если текста поля нет, вектор всё равно строим по
    # заголовку — как делает обычная сборка. Иначе NULL оставался навсегда,
    # воркер видел его дырой и звал ремонт снова и снова, до бесконечности
    # (воспроизвёл архитектор 05.08).
    rows = con.execute(
        "SELECT id, goal_text, outcome_text, detail_text, title FROM episodes "
        "WHERE vec_goal IS NULL OR vec_outcome IS NULL OR vec_detail IS NULL").fetchall()
    if not rows:
        return 0
    done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        # Пустой текст эмбеддить нечем — такие пропускаем, иначе сервис вернёт
        # мусор, и запись будет выглядеть починенной, не будучи ею.
        keep = [r for r in chunk if (r[1] or r[2] or r[3] or r[4] or '').strip()]
        if not keep:
            continue

        def vecs_for(idx):
            """Векторы поля. Пусто — берём заголовок, чтобы не оставить NULL."""
            texts = [((r[idx] or '').strip() or (r[4] or '').strip() or '—') for r in keep]
            return episodes.embed(texts)

        vg, vo, vd = vecs_for(1), vecs_for(2), vecs_for(3)
        for r, g, o, d in zip(keep, vg, vo, vd):
            con.execute("UPDATE episodes SET vec_goal=?, vec_outcome=?, vec_detail=? WHERE id=?",
                        (_blob(g), _blob(o), _blob(d), r[0]))
            done += 1
        con.commit()
        if verbose:
            print(f'  эпизоды: {done}/{len(rows)}', flush=True)
    return done


def repair_memdocs(con, verbose=True):
    rows = con.execute("SELECT id, text FROM mem_docs WHERE vec IS NULL").fetchall()
    if not rows:
        return 0
    import memdocs
    done = 0
    for i in range(0, len(rows), BATCH):
        # То же правило для кусков памяти: пустой текст заменяем прочерком,
        # чтобы запись получила вектор и перестала числиться дырой.
        chunk = [(r[0], (r[1] or '').strip() or '—') for r in rows[i:i + BATCH]]
        if not chunk:
            continue
        vecs = memdocs.embed([t for _, t in chunk])
        for (i_, _), v in zip(chunk, vecs):
            con.execute("UPDATE mem_docs SET vec=? WHERE id=?", (_blob(v), i_))
            done += 1
        con.commit()
        if verbose:
            print(f'  куски памяти: {done}/{len(rows)}', flush=True)
    return done


def repair(verbose=True):
    if not health.embedder_alive():
        if verbose:
            print('эмбеддер лежит — чинить нечем, пропускаю')
        return 0, 0
    con = catalog.db()
    e = repair_episodes(con, verbose)
    m = repair_memdocs(con, verbose)
    if verbose:
        print(f'достроено: эпизодов {e}, кусков памяти {m}')
    return e, m


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    repair()
