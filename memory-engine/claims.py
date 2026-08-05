#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Слоты фактов: что считается верным СЕЙЧАС, с историей замен.

Заимствовано у памяти Экзегета (`memory_claims.py`) — там это решение
архитектора спасло от затирания фактов. У меня до этого «актуальность»
держалась на эвристике «есть ли позже в этой нити», а она не отвечает на
главный вопрос: какое значение действует и что именно его заменило.

Слот определяется четвёркой:
    (область, класс, о чём, признак)
и в каждый момент у слота не больше ОДНОГО активного значения.

Ничего не удаляется. Прежнее значение остаётся со статусом `superseded`
(заменено) или `revoked` (отменено), с ссылкой на замену — видна вся цепочка:
«запись на 3 августа 18:00» → «перенесена на 4 августа 17:00».

Происхождение проставляет КОД, а не модель: время, якорь сообщения, сессия,
хэш значения. Модель решает только, ЧТО записать. Это прямой урок Экзегета:
бот рапортовал «записал, проверил», а файл был пуст.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog  # noqa: E402

VERSION = 1

CLASSES = ("preference", "commitment", "project_state", "decision", "identity")
STATUS = ("active", "superseded", "revoked")

CLASS_RU = {
    "preference": "предпочтение/правило",
    "commitment": "обязательство (запись, бронь, заказ)",
    "project_state": "состояние работы",
    "decision": "принятое решение",
    "identity": "факт о человеке/вещи",
}

# 🔴 Срок годности — по классу, а не общий (решение архитектора 05.08).
# Повод: слот «текущая печать» девятнадцать часов подавался как «действует
# сейчас», хотя за это время прошли две другие печати. Уверенно неверный ответ
# хуже пустого: пустой отправит проверять, неверный — нет.
#   None — не протухает: кто человек, что он любит, что решено.
#   Часы — через сколько значение перестаёт считаться подтверждённым.
STALE_HOURS = {
    "identity": None,
    "preference": None,
    "decision": None,
    "commitment": 24 * 30,      # бронь живёт до своей даты; valid_to важнее
    "project_state": 12,        # состояние работы устаревает за полсмены
}


def _parse_when(s):
    """Разобрать отметку времени: «2026-08-05», «…T14:30», «…T14:30:00».

    🔴 Прежний разбор дополнял строку нулями через ljust и превращал «2026-08-05»
    в «2026-08-05000000» — дата не разбиралась вовсе, и обязательство считалось
    действующим ещё месяц после того, как срок прошёл (нашёл архитектор 05.08).
    Дата без времени = КОНЕЦ этого дня: запись на приём 5-го действует весь 5-й.
    """
    import datetime as _dt
    if not s:
        return None
    s = str(s).strip().replace(" ", "T")
    for fmt, end_of_day in (("%Y-%m-%dT%H:%M:%S", False), ("%Y-%m-%dT%H:%M", False),
                            ("%Y-%m-%d", True)):
        try:
            t = _dt.datetime.strptime(s[:len(_dt.datetime(2026, 1, 1).strftime(fmt))], fmt)
            return t.replace(hour=23, minute=59, second=59) if end_of_day else t
        except ValueError:
            continue
    return None


def freshness(row, now=None):
    """Насколько значению ещё можно верить.

    Возвращает (stale: bool, age_h: float|None). stale=True означает: показывать
    как «последнее известное на …», а НЕ как «действует сейчас».

    Порядок важности (решение архитектора 05.08 — общий TTL по классу слишком
    груб: четырёхчасовая печать успеет закончиться, а двадцатичасовая ещё идёт):
      1. expected_end   — когда дело должно закончиться (печать, поездка);
      2. valid_to       — до какой даты обязательство в силе;
      3. stale_after_hours — индивидуальный срок для этого слота;
      4. срок по классу — грубая страховка.
    """
    import datetime as _dt
    keys = row.keys()
    cls = row["claim_class"] if "claim_class" in keys else None
    now = now or _dt.datetime.now()

    ts = (row["recorded_at"] if "recorded_at" in keys else None) or ""
    # подтверждение сдвигает точку отсчёта: перепроверенное значение снова свежее
    if "last_verified_at" in keys and row["last_verified_at"]:
        ts = row["last_verified_at"]
    t = _parse_when(ts)
    age_h = (now - t).total_seconds() / 3600 if t else None

    for col in ("expected_end", "valid_to"):
        if col in keys and row[col]:
            end = _parse_when(row[col])
            if end:
                return now > end, age_h

    # 🔴 Колонка объявлена TEXT, и SQLite возвращает «12» строкой: сравнение
    # с числом падало TypeError и убивало весь блок слотов (воспроизвёл
    # архитектор 05.08). Приводим к числу явно и молча игнорируем мусор.
    own = row["stale_after_hours"] if "stale_after_hours" in keys else None
    try:
        own = float(own) if own not in (None, "") else None
    except (TypeError, ValueError):
        own = None
    limit = own if own else STALE_HOURS.get(cls, 24 * 7)
    if limit is None:
        return False, age_h
    if age_h is None:
        return True, None      # срок есть, а точки отсчёта нет — верить нельзя
    # Граница включительно: ровно на сроке значение уже считаем неподтверждённым.
    # Ошибиться в эту сторону дёшево (лишний раз проверю), в обратную — дорого.
    return age_h >= limit, age_h


def db(con=None):
    con = con or catalog.db()
    con.execute("""CREATE TABLE IF NOT EXISTS claim(
        claim_id      TEXT PRIMARY KEY,
        scope         TEXT NOT NULL DEFAULT 'ruvim',
        claim_class   TEXT NOT NULL,
        subject       TEXT NOT NULL,      -- о чём: «шины караван», «отель мертл-бич»
        predicate     TEXT NOT NULL,      -- признак: «время записи», «номер брони»
        value         TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'active',
        valid_from    TEXT,
        valid_to      TEXT,
        recorded_at   TEXT NOT NULL,
        supersedes    TEXT,               -- claim_id прежнего значения
        source_uuid   TEXT,               -- якорь сообщения-первоисточника
        source_session TEXT,
        value_hash    TEXT NOT NULL,
        note          TEXT)""")
    # один активный слот — гарантия на уровне БД, а не намерений
    con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_claim_active
        ON claim(scope, claim_class, subject, predicate)
        WHERE status='active'""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_claim_subj ON claim(subject)")
    # Подтверждение свежести: перепроверенное значение снова считается текущим.
    # Добавляем мягко, чтобы не ломать существующую базу.
    have = {r[1] for r in con.execute("PRAGMA table_info(claim)")}
    for col in ("last_verified_at", "verification_source",
                "expected_end", "stale_after_hours"):
        if col not in have:
            con.execute(f"ALTER TABLE claim ADD COLUMN {col} TEXT")
    con.commit()
    return con


def verify(subject, predicate, source=None, scope="ruvim", con=None):
    """Отметить, что значение перепроверено сейчас — счётчик протухания сбрасывается."""
    con = db(con)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    n = con.execute("UPDATE claim SET last_verified_at=?, verification_source=? "
                    "WHERE scope=? AND subject=? AND predicate=? AND status='active'",
                    (ts, source or "", scope, _norm(subject), _norm(predicate))).rowcount
    con.commit()
    # 🔴 Раньше возвращали время всегда, и «подтверждено» печаталось даже для
    # несуществующего слота (замечание архитектора 05.08).
    return ts if n else None


def _norm(s):
    """Ключи слота сравниваем без падежей и регистра — иначе «шины» и «шинам»
    заведут два разных слота и оба будут «активными»."""
    s = (s or "").strip().lower().replace("ё", "е")
    s = re.sub(r"\s+", " ", s)
    return s


def _cid(scope, cls, subj, pred, value, ts):
    raw = f"{scope}|{cls}|{_norm(subj)}|{_norm(pred)}|{value}|{ts}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def get(subject, predicate, scope="ruvim", con=None):
    """Действующее значение слота (или None)."""
    con = db(con)
    con.row_factory = sqlite3.Row
    return con.execute(
        "SELECT * FROM claim WHERE scope=? AND subject=? AND predicate=? AND status='active'",
        (scope, _norm(subject), _norm(predicate))).fetchone()


def put(subject, predicate, value, claim_class="commitment", scope="ruvim",
        source_uuid=None, source_session=None, note=None, valid_from=None,
        valid_to=None, expected_end=None, stale_hours=None, con=None):
    """Записать значение в слот.

    Возвращает (действие, claim_id): ADD — слот был пуст, NOOP — то же самое
    значение уже стоит, SUPERSEDE — прежнее заменено и помечено.
    """
    con = db(con)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    vh = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    cur = get(subject, predicate, scope, con)

    if cur and cur["value_hash"] == vh:
        # 🔴 Значение то же, но СРОКИ могли измениться: печать перезапустили,
        # приём перенесли. Раньше NOOP выходил сразу и терял новые
        # expected_end / valid_to / TTL и свежий якорь (воспроизвёл архитектор
        # 05.08) — то есть слот навсегда оставался с прошлым сроком.
        upd, args = [], []
        for col, val in (("valid_to", valid_to), ("expected_end", expected_end),
                         ("stale_after_hours", stale_hours),
                         ("source_uuid", source_uuid), ("source_session", source_session),
                         ("note", note)):
            if val not in (None, ""):
                upd.append(f"{col}=?")
                args.append(str(val))
        if upd:
            # обновление сроков — это тоже подтверждение: значение перепроверено
            upd += ["last_verified_at=?"]
            args += [ts]
            args.append(cur["claim_id"])
            con.execute(f"UPDATE claim SET {', '.join(upd)} WHERE claim_id=?", args)
            con.commit()
            return "REFRESH", cur["claim_id"]
        return "NOOP", cur["claim_id"]

    cid = _cid(scope, claim_class, subject, predicate, value, ts)
    if cur:
        # снимаем прежнее с активности ДО вставки нового: уникальный индекс
        # не даст двум активным ужиться, и это правильно
        con.execute("UPDATE claim SET status='superseded', valid_to=? WHERE claim_id=?",
                    (ts, cur["claim_id"]))
    con.execute("""INSERT INTO claim(claim_id,scope,claim_class,subject,predicate,value,
        status,valid_from,valid_to,expected_end,stale_after_hours,
        recorded_at,supersedes,source_uuid,source_session,value_hash,note)
        VALUES(?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?,?,?)""",
        (cid, scope, claim_class, _norm(subject), _norm(predicate), value,
         valid_from or ts, valid_to, expected_end, stale_hours,
         ts, cur["claim_id"] if cur else None,
         source_uuid, source_session, vh, note))
    con.commit()
    return ("SUPERSEDE" if cur else "ADD"), cid


def revoke(subject, predicate, scope="ruvim", note=None, con=None):
    """Отменить факт: дело закрыто, значение больше не действует."""
    con = db(con)
    cur = get(subject, predicate, scope, con)
    if not cur:
        return "NOTHING", None
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    con.execute("UPDATE claim SET status='revoked', valid_to=?, note=COALESCE(?,note)"
                " WHERE claim_id=?", (ts, note, cur["claim_id"]))
    con.commit()
    return "REVOKE", cur["claim_id"]


def history(subject, predicate=None, scope="ruvim", con=None):
    """Вся цепочка значений слота, от старых к новым."""
    con = db(con)
    con.row_factory = sqlite3.Row
    q = "SELECT * FROM claim WHERE scope=? AND subject=?"
    args = [scope, _norm(subject)]
    if predicate:
        q += " AND predicate=?"
        args.append(_norm(predicate))
    return con.execute(q + " ORDER BY recorded_at", args).fetchall()


def active(scope="ruvim", claim_class=None, con=None):
    con = db(con)
    con.row_factory = sqlite3.Row
    q = "SELECT * FROM claim WHERE scope=? AND status='active'"
    args = [scope]
    if claim_class:
        q += " AND claim_class=?"
        args.append(claim_class)
    return con.execute(q + " ORDER BY claim_class, subject", args).fetchall()


def render(rows):
    if not rows:
        return "(пусто)"
    out = []
    for r in rows:
        mark = {"active": "✅", "superseded": "↩", "revoked": "✖"}.get(r["status"], "?")
        line = f"{mark} [{r['claim_class']}] {r['subject']} · {r['predicate']} = {r['value']}"
        if r["status"] != "active":
            line += f"   (до {(r['valid_to'] or '')[:16]})"
        out.append(line)
        if r["source_uuid"]:
            out.append(f"     первоисточник: {r['source_uuid']}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Слоты фактов: что верно сейчас")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("set", help="записать значение")
    p.add_argument("subject"); p.add_argument("predicate"); p.add_argument("value")
    p.add_argument("--class", dest="cls", default="commitment", choices=CLASSES)
    p.add_argument("--uuid"); p.add_argument("--session"); p.add_argument("--note")
    p.add_argument("--valid-to", dest="valid_to",
                   help="до какой даты обязательство в силе: 2026-08-20 или с временем")
    p.add_argument("--expected-end", dest="expected_end",
                   help="когда дело должно закончиться (печать, поездка)")
    p.add_argument("--stale-hours", dest="stale_hours", type=int,
                   help="свой срок годности в часах")

    p = sub.add_parser("get", help="действующее значение")
    p.add_argument("subject"); p.add_argument("predicate")

    p = sub.add_parser("history", help="цепочка замен")
    p.add_argument("subject"); p.add_argument("predicate", nargs="?")

    p = sub.add_parser("revoke", help="отменить факт")
    p.add_argument("subject"); p.add_argument("predicate"); p.add_argument("--note")

    p = sub.add_parser("verify", help="подтвердить, что значение перепроверено сейчас")
    p.add_argument("subject"); p.add_argument("predicate")
    p.add_argument("--source", help="чем подтверждено")

    p = sub.add_parser("list", help="все действующие")
    p.add_argument("--class", dest="cls", choices=CLASSES)

    a = ap.parse_args()
    if a.cmd == "set":
        act, cid = put(a.subject, a.predicate, a.value, a.cls,
                       source_uuid=a.uuid, source_session=a.session, note=a.note, valid_to=getattr(a, 'valid_to', None),
                    expected_end=getattr(a, 'expected_end', None),
                    stale_hours=getattr(a, 'stale_hours', None))
        print(f"{act}  {cid}  {a.subject} · {a.predicate} = {a.value}")
    elif a.cmd == "get":
        r = get(a.subject, a.predicate)
        print(f"{r['value']}   (записано {r['recorded_at'][:16]})" if r else "нет такого слота")
    elif a.cmd == "history":
        print(render(history(a.subject, a.predicate)))
    elif a.cmd == "revoke":
        act, cid = revoke(a.subject, a.predicate, note=a.note)
        print(f"{act} {cid or ''}")
    elif a.cmd == "verify":
        ts = verify(a.subject, a.predicate, source=a.source)
        print(f"подтверждено {ts}: {a.subject} · {a.predicate}" if ts
              else f"НЕТ такого действующего слота: {a.subject} · {a.predicate}")
    elif a.cmd == "list":
        print(render(active(claim_class=a.cls)))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
