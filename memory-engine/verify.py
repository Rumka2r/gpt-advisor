#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Проверка собственных утверждений перед тем, как их сказать.

Отсюда растёт `answer-gate` Экзегета: там код сверяет ссылки с библиотекой до
показа человеку. Полного аналога у меня быть не может — в Claude Code нет
перехвата моего ответа, — поэтому это инструмент самопроверки: я прогоняю
через него то, что собираюсь утверждать как факт.

Три вопроса к утверждению:
  1. есть ли дословная опора в истории (строка, которую можно открыть);
  2. не противоречит ли оно действующему слоту факта;
  3. не выдаю ли я своё предположение за слова Рувима (и наоборот).

Статусы намеренно как у Экзегета — «не найдено» ≠ «ложно»:
  ПОДТВЕРЖДЕНО   — нашлась строка-опора, есть якорь
  ПРОТИВОРЕЧИЕ   — слот факта говорит иначе; это стоп-сигнал
  НЕ НАЙДЕНО     — опоры нет; утверждать можно, но без ссылки на историю
  ЧУЖИЕ СЛОВА    — приписано не тому: «Рувим сказал» о моей же реплике
"""

import argparse
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog  # noqa: E402
import claims  # noqa: E402

# Что делает утверждение проверяемым: числа, даты, время, суммы, имена
# латиницей. Общие слова не проверить — и не надо.
_ANCHORS = re.compile(
    r"\b\d{1,2}[:.]\d{2}\b|\b\d{4,}\b|\$\s?\d[\d.,]*|\b\d{1,2}\s?"
    r"(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*|"
    r"\b[A-Z][A-Za-z]{3,}\b", re.I | re.U)

ROLE_WORDS = {
    "user": ("рувим сказал", "рувим просил", "ты сказал", "ты просил", "по твоим словам"),
    "assistant": ("я сказал", "я предложил", "я посчитал", "мы решили"),
}


def key_bits(text):
    """Опорные куски утверждения — то, что вообще можно проверить."""
    return [m.group(0) for m in _ANCHORS.finditer(text or "")][:6]


def find_support(bit, con, limit=3, exclude_session=None):
    """Дословные вхождения куска в историю.

    Реплики и мои ответы идут раньше выводов инструментов: подтверждение из
    собственного вывода команды — это эхо, а не свидетельство. Текущая сессия
    исключается по той же причине.
    """
    con.row_factory = sqlite3.Row
    like = f"%{bit}%"
    q = ("SELECT role, uuid, substr(text,1,240) t, ts, session FROM events "
         "WHERE text LIKE ? AND sub=0 AND text NOT LIKE '[вызов%'")
    args = [like]
    if exclude_session:
        q += " AND session NOT LIKE ?"
        args.append(exclude_session[:8] + "%")
    q += " ORDER BY (role='tool') ASC, ts DESC LIMIT ?"
    args.append(limit)
    return con.execute(q, args).fetchall()


def _stems(text):
    """Грубая нормализация под падежи: «шин», «шины», «шинам» → один корень.
    Без неё проверка не срабатывала: слот назывался «шины», а в утверждении
    стояло «шин» — и противоречие проходило незамеченным."""
    try:
        import auto_recall
        return auto_recall.stems(text)
    except Exception:
        return {w[:4] for w in re.findall(r"\w{3,}", (text or "").lower(), re.U)}


def check_claims(text, con):
    """Не спорит ли утверждение с действующим слотом факта."""
    out = []
    low = text.lower()
    tstems = _stems(text)
    for c in claims.active(con=con):
        if not (_stems(c["subject"]) & tstems):
            continue
        # Мало совпасть темой: у одной темы много слотов, и «время замены»
        # не должно спорить со слотом «заказ», где своя дата. Требуем, чтобы
        # утверждение говорило именно об этом признаке.
        if not (_stems(c["predicate"]) & tstems):
            continue
        # Сравниваем значения ОДНОГО типа: время с временем, дату с датой.
        # Иначе утверждение про время спорит со слотом про номер заказа —
        # шум, из-за которого предупреждению перестают верить.
        mine = _typed(text)
        theirs = _typed(c["value"])
        clash = False
        for kind, vals in theirs.items():
            if kind in mine and vals and mine[kind] and not (vals & mine[kind]):
                clash = True
                break
        if clash:
            out.append(c)
    return out


_TYPES = {
    "время": re.compile(r"\b\d{1,2}[:.]\d{2}\b"),
    "дата": re.compile(r"\b\d{1,2}\s?(?:январ|феврал|март|апрел|ма[йя]|июн|июл|"
                       r"август|сентябр|октябр|ноябр|декабр)\w*", re.I | re.U),
    "сумма": re.compile(r"\$\s?\d[\d.,]*"),
    "номер": re.compile(r"\b\d{6,}\b"),
}


def _typed(text):
    """Опорные куски, разложенные по типам значений."""
    low = (text or "").lower()
    return {k: {m.group(0).strip() for m in rx.finditer(low)} for k, rx in _TYPES.items()}


def verify(text, con=None, exclude_session=None):
    con = con or catalog.db()
    bits = key_bits(text)
    result = {"statement": text, "bits": bits, "support": [], "conflicts": [], "role_warn": []}

    for b in bits:
        rows = find_support(b, con, exclude_session=exclude_session)
        if rows:
            r = rows[0]
            result["support"].append({
                "bit": b, "role": r["role"], "anchor": r["uuid"],
                "when": (r["ts"] or "")[:16].replace("T", " "),
                "line": " ".join(r["t"].split())[:200],
            })

    for c in check_claims(text, con):
        result["conflicts"].append({
            "subject": c["subject"], "predicate": c["predicate"],
            "active_value": c["value"], "anchor": c["source_uuid"],
        })

    low = text.lower()
    for phrase in ROLE_WORDS["user"]:
        if phrase in low:
            # если опора нашлась только в моих словах — приписано не тому
            roles = {s["role"] for s in result["support"]}
            if roles and "user" not in roles:
                result["role_warn"].append(
                    f"«{phrase}» — но опора найдена только в моих словах, не в репликах Рувима")
    return result


def status(res):
    if res["conflicts"]:
        return "ПРОТИВОРЕЧИЕ"
    if res["role_warn"]:
        return "ЧУЖИЕ СЛОВА"
    if res["support"]:
        return "ПОДТВЕРЖДЕНО"
    return "НЕ НАЙДЕНО"


def render(res):
    st = status(res)
    lines = [f"[{st}] {res['statement'][:120]}"]
    if not res["bits"]:
        lines.append("   проверяемых частей нет (ни чисел, ни имён) — сверять не с чем")
    for c in res["conflicts"]:
        lines.append(f"   ⛔ слот «{c['subject']} · {c['predicate']}» говорит иначе:")
        lines.append(f"      действует: {c['active_value']}")
        if c["anchor"]:
            lines.append(f"      первоисточник: {c['anchor']}")
    for w in res["role_warn"]:
        lines.append(f"   ⚠ {w}")
    for s in res["support"]:
        who = {"user": "Рувим", "assistant": "я", "tool": "инструмент"}.get(s["role"], s["role"])
        lines.append(f"   ✓ «{s['bit']}» — {who}, {s['when']}")
        lines.append(f"      {s['line'][:150]}")
        lines.append(f"      якорь: {s['anchor']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Проверить утверждение перед тем, как его сказать")
    ap.add_argument("statement", nargs="?", help="если не задано — читает построчно со stdin")
    ap.add_argument("--exclude", help="не искать опору в этой сессии (обычно текущей)")
    a = ap.parse_args()
    con = catalog.db()
    if a.statement:
        print(render(verify(a.statement, con, a.exclude)))
        return
    for line in sys.stdin:
        line = line.strip()
        if line:
            print(render(verify(line, con, a.exclude)))
            print()


if __name__ == "__main__":
    main()
