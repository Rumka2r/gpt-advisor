#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Теневое извлечение фактов в слоты: кандидаты, а не сразу боевая память.

Решение архитектора у Экзегета (`shadow_writer.py`), перенятое сюда: прежде
чем что-то попадёт в действующие факты, оно копится в отдельном ящике со
статусом «ожидает». В `claims.py` ничего не пишется, пока я не подтвердил.

Разделение обязанностей — то же самое и по той же причине:
  • КОД берёт из каталога якорь, время, сессию, дословную строку и хэш;
  • ШАБЛОН определяет тип факта (номер брони, заказ, время записи…);
  • решение «применить» принимаю я, глядя на первоисточник.

Извлекаем только то, что распознаётся однозначно: номера, суммы, даты и
время рядом с ключевым словом. Всё остальное остаётся в истории — её и так
покрывает поиск, и выдумывать «предпочтения» из пересказа опасно.

    python shadow.py --scan          # набрать кандидатов из фактов эпизодов
    python shadow.py --report        # показать, что накопилось
    python shadow.py --apply <id>    # перенести кандидата в действующие слоты
    python shadow.py --reject <id> "причина"
"""

import argparse
import hashlib
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog  # noqa: E402
import claims  # noqa: E402

# (признак слота, класс, регулярка). Порядок важен: сначала специфичное.
PATTERNS = [
    ("номер брони", "commitment",
     re.compile(r"(?:номер\s+брони|бронь|confirmation)\D{0,12}?`?(\d{6,12})`?", re.I)),
    ("номер заказа", "commitment",
     re.compile(r"заказ\w*\s*(?:№|номер|#)?\s*`?(\d{8,20})`?", re.I)),
    ("трекинг", "commitment",
     re.compile(r"(?:tracking|трекинг|отслеж\w+)\D{0,12}?([A-Z0-9]{8,20})\b")),
    ("время записи", "commitment",
     re.compile(r"запис\w+[^.!?]{0,80}?(\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|"
                r"август|сентябр|октябр|ноябр|декабр)\w*)[^.!?]{0,40}?(\d{1,2}:\d{2})", re.I)),
    ("сумма оплаты", "commitment",
     re.compile(r"оплач\w+[^.!?]{0,40}?\$\s?([\d][\d.,]{1,12})", re.I)),
]

# Слова, по которым видно, что строка — не факт, а рассуждение или план.
NOT_A_FACT = re.compile(
    r"\b(?:если|возможно|наверн\w+|планир\w+|предлаг\w+|можно|стоит\s+ли|"
    r"попробу\w+|хочешь|давай\s+я)\b", re.I)


def ensure_tables(con):
    con.execute("""CREATE TABLE IF NOT EXISTS claim_candidate(
        cand_id     TEXT PRIMARY KEY,
        subject     TEXT NOT NULL,
        predicate   TEXT NOT NULL,
        value       TEXT NOT NULL,
        claim_class TEXT NOT NULL,
        quote       TEXT NOT NULL,      -- дословная строка-опора
        source_uuid TEXT,
        source_ts   TEXT,
        thread_id   INTEGER,
        status      TEXT NOT NULL DEFAULT 'pending',   -- pending|applied|rejected
        note        TEXT,
        found_at    TEXT NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_cand_status ON claim_candidate(status)")
    con.commit()


# Слова, которыми Рувим открывает реплику, а не называет предмет. Брать их в
# тему нельзя: получались слоты вроде «скажи пожалуйста тебя · номер заказа».
_SUBJ_STOP = {
    "скажи", "пожалуйста", "слушай", "смотри", "давай", "нужно", "надо", "можешь",
    "тебя", "тебе", "меня", "мне", "теперь", "вопрос", "другой", "может", "быть",
    "просто", "сейчас", "сегодня", "вчера", "завтра", "потом", "ещё", "еще",
    "хочу", "думаю", "понял", "поняла", "ладно", "хорошо", "значит", "вообще",
    "который", "которая", "которые", "этот", "эта", "это", "того", "чтобы",
    "проверь", "сделай", "найди", "запиши", "покажи", "напиши", "посмотри",
    "информацию", "информация", "память", "памяти", "файл", "файлы",
}


# Предметные ориентиры: (что искать в тексте, как назвать слот). Список узкий
# и пополняемый вручную — зато тема получается такой, какой её назвал бы
# человек, а не набором частотных соседей («заказа номер», «есть сразу»).
TOPICS = [
    (re.compile(r"\bшин\w*|\btire|caravan|карава\w+|goodyear|walmart\s+auto", re.I), "шины караван"),
    (re.compile(r"отел\w+|hotel|homewood|hilton|мертл|myrtle|бронь\s+отел", re.I), "отель мертл-бич"),
    (re.compile(r"amex|american\s+express|hilton\s+honors|баллы|мили|aeroplan", re.I), "баллы и мили"),
    (re.compile(r"discover|chase\s+ink|карт\w+\s+опла|оплач\w+\s+карт", re.I), "оплата картой"),
    (re.compile(r"кондуктор|jig|принтер|печат\w+|филамент|сопло", re.I), "печать кондукторов"),
    (re.compile(r"sentra|сентра|dmv|титул|доверенност|caravan\s+title", re.I), "машина и документы"),
    (re.compile(r"plumbingcore|песочниц\w+|прод\b|деплой", re.I), "plumbingcore"),
    (re.compile(r"эпизод\w*|нит[ьи]|поиск\w*|память|индекс", re.I), "память и поиск"),
]


def _subject_of(con, episode_id, quote=""):
    """Тема дела — по содержанию, а не по первой фразе.

    Сначала ищем известный предмет по словарю: так тема называется словами
    человека. Если не опознан — запасной способ по частоте значимых слов,
    потому что заголовок эпизода часто начинается с «Скажи пожалуйста…».
    """
    for rx, name in TOPICS:
        if rx.search(quote or ""):
            return name
    # в самой строке предмет не назван («Спроси номер заказа …») — смотрим,
    # о чём был весь эпизод
    _r = con.execute("SELECT title, goal_text, facts FROM episodes WHERE id=?",
                     (episode_id,)).fetchone()
    ctx = " ".join(str(x or "") for x in (_r or ()))[:4000]
    for rx, name in TOPICS:
        if rx.search(ctx):
            return name
    r = con.execute("SELECT title, goal_text, facts, files FROM episodes WHERE id=?",
                    (episode_id,)).fetchone()
    pool = " ".join(str(x or "") for x in (quote, r[0] if r else "", r[1] if r else "",
                                           r[2] if r else ""))
    freq = {}
    for w in re.findall(r"[А-Яа-яЁёA-Za-z]{4,}", pool):
        lw = w.lower()
        if lw in _SUBJ_STOP:
            continue
        freq[lw] = freq.get(lw, 0) + 1
    # слова из самой опоры важнее: именно там стоит предмет факта
    for w in re.findall(r"[А-Яа-яЁёA-Za-z]{4,}", quote):
        lw = w.lower()
        if lw in freq:
            freq[lw] += 3
    top = sorted(freq.items(), key=lambda x: (-x[1], x[0]))[:2]
    return " ".join(w for w, _ in top) or "без темы"


def scan(con=None, limit=None, verbose=True, exclude_session=None):
    """exclude_session — пропустить сессию, где я сам строил эту систему:
    там мои же примеры («Спроси номер заказа … — не найдёт ничего») выглядят
    как факты о деле, хотя это объяснение, а не событие."""
    con = con or catalog.db()
    ensure_tables(con)
    con.row_factory = sqlite3.Row
    q = ("SELECT f.id, f.episode_id, f.uuid, f.text, e.start_ts, e.session "
         "FROM ep_fact f JOIN episodes e ON e.id=f.episode_id")
    args = []
    if exclude_session:
        q += " WHERE e.session NOT LIKE ?"
        args.append(exclude_session[:8] + "%")
    rows = con.execute(q + " ORDER BY e.start_ts DESC", args).fetchall()
    added = 0
    for r in rows[:limit] if limit else rows:
        line = " ".join((r["text"] or "").split())
        if len(line) < 15 or NOT_A_FACT.search(line):
            continue
        for pred, cls, rx in PATTERNS:
            m = rx.search(line)
            if not m:
                continue
            value = " ".join(g for g in m.groups() if g).strip()
            if not value:
                continue
            subj = _subject_of(con, r["episode_id"], line)
            cid = hashlib.sha256(f"{subj}|{pred}|{value}".encode("utf-8")).hexdigest()[:12]
            if con.execute("SELECT 1 FROM claim_candidate WHERE cand_id=?", (cid,)).fetchone():
                continue
            th = con.execute("SELECT thread_id FROM thread_episode WHERE episode_id=?",
                             (r["episode_id"],)).fetchone()
            con.execute("""INSERT INTO claim_candidate(cand_id,subject,predicate,value,
                claim_class,quote,source_uuid,source_ts,thread_id,found_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (cid, subj, pred, value, cls, line[:300], r["uuid"], r["start_ts"],
                 th[0] if th else None, time.strftime("%Y-%m-%dT%H:%M:%S")))
            added += 1
    con.commit()
    if verbose:
        print(f"кандидатов найдено новых: {added}")
    return added


def report(con=None, status="pending", limit=40):
    con = con or catalog.db()
    ensure_tables(con)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM claim_candidate WHERE status=? "
                       "ORDER BY source_ts DESC LIMIT ?", (status, limit)).fetchall()
    if not rows:
        print(f"кандидатов со статусом «{status}» нет")
        return rows
    for r in rows:
        cur = claims.get(r["subject"], r["predicate"], con=con)
        mark = ""
        if cur:
            mark = "  ← уже есть слот: " + (cur["value"][:50] +
                                            ("…" if len(cur["value"]) > 50 else ""))
        print(f"\n[{r['cand_id']}] {r['subject']} · {r['predicate']} = {r['value']}{mark}")
        print(f"   опора: {r['quote'][:150]}")
        print(f"   якорь: {r['source_uuid']}   ({(r['source_ts'] or '')[:16].replace('T',' ')})")
    return rows


def apply(cand_id, con=None):
    con = con or catalog.db()
    ensure_tables(con)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM claim_candidate WHERE cand_id=?", (cand_id,)).fetchone()
    if not r:
        return None, f"нет кандидата {cand_id}"
    if r["status"] != "pending":
        return None, f"кандидат уже {r['status']}"
    act, claim_id = claims.put(
        r["subject"], r["predicate"], r["value"], r["claim_class"],
        source_uuid=r["source_uuid"], note=f"из тени, опора: {r['quote'][:120]}", con=con)
    con.execute("UPDATE claim_candidate SET status='applied', note=? WHERE cand_id=?",
                (f"{act} → {claim_id}", cand_id))
    con.commit()
    return act, None


def reject(cand_id, reason, con=None):
    con = con or catalog.db()
    ensure_tables(con)
    con.execute("UPDATE claim_candidate SET status='rejected', note=? WHERE cand_id=?",
                (reason, cand_id))
    con.commit()
    return True


def main():
    ap = argparse.ArgumentParser(description="Теневое извлечение фактов в слоты")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--exclude", help="пропустить сессию (обычно текущую)")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--status", default="pending")
    ap.add_argument("--apply", metavar="ID")
    ap.add_argument("--reject", nargs=2, metavar=("ID", "ПРИЧИНА"))
    a = ap.parse_args()

    con = catalog.db()
    if a.scan:
        scan(con, exclude_session=a.exclude)
    if a.apply:
        act, err = apply(a.apply, con)
        print(err if err else f"перенесено в слоты: {act}")
    if a.reject:
        reject(a.reject[0], a.reject[1], con)
        print("отклонено")
    if a.report or not (a.scan or a.apply or a.reject):
        report(con, a.status)


if __name__ == "__main__":
    main()
