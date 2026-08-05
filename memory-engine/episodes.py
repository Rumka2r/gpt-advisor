#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Нарезка сессий на рабочие эпизоды.

Эпизод — непрерывный участок разговора с одной практической целью. «Почему
тест падает» → «покажи файл» → «исправь» → «прогони снова» это ОДИН эпизод,
хотя слова и инструменты по дороге меняются полностью.

Два уровня:
  turn block — реплика Рувима и всё, что я делал в ответ, до следующей его
               реплики. Собирается детерминированно, по цепочке parent_uuid.
  эпизод     — несколько подряд идущих turn blocks с общей целью.

Границы решаются правилами, а не моделью: правила бесплатны, воспроизводимы
и объяснимы («разрезано из-за /clear и паузы 4 часа»), а модель на этом
месте дала бы плавающий результат и деньги за каждый прогон.

Признаки, по которым принимается решение, — в decide(). Ключевой из них
семантический, но реплика сравнивается НЕ голой: «да, делай» само по себе
не значит ничего, поэтому к ней подмешивается текущая цель и файлы в работе.
Иначе любое короткое согласие разрывало бы очевидное продолжение.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog  # noqa: E402

EMB = "http://127.0.0.1:8899/embed"
VERSION = 1          # версия сегментатора; при правке правил — поднять

# --- признаки --------------------------------------------------------------

# Реплика, начинающаяся с этих слов, почти наверняка продолжает предыдущую.
ANAPHORA = re.compile(
    r"^\s*(?:а\b|и\b|но\b|да\b|нет\b|ок\b|окей|хорошо|ладно|теперь|потом|дальше|ещё|еще|"
    r"это|тут|там|тогда|значит|давай|продолж|сделай|исправ|поправ|попроб|проверь|"
    r"почему|зачем|как\b|что\b|а\s+если|только|не\b|плюс|также|заодно)", re.I)

# Явное объявление новой темы — сильный сигнал границы.
NEW_TOPIC = re.compile(
    r"(?:друг|нов|отдельн|перекл|смен)\w*\s+(?:тем|вопрос|задач|дел)|"
    r"^\s*(?:теперь\s+)?(?:про|насчёт|насчет|по\s+поводу)\s+друг|"
    r"забудь\s+(?:про|об)|закончили\s+с|перейд[ёе]м\s+к", re.I)

CLEAR = re.compile(r"<command-name>/(?:clear|compact)</command-name>", re.I)

# В роли «user» лежат не только реплики Рувима, но и служебные вставки
# харнесса: уведомления фоновых задач, напоминания, обёртки слэш-команд,
# отметки о прерывании. Считать их репликами нельзя — они дробят разговор на
# куски и, будучи почти одинаковыми, дают ложное сходство 0.95+.
SYSTEM_NOISE = re.compile(
    r"^\s*(?:<task-notification|<local-command-caveat|<system-reminder|<command-name|"
    r"<command-message|<local-command-std|\[Request interrupted|"
    r"Caveat: The messages below|<user-prompt-submit-hook)", re.I)

# Вставки, которые приходят внутри настоящей реплики — вырезаем, оставляя текст.
INLINE_NOISE = re.compile(
    r"<system-reminder>.*?</system-reminder>|<local-command-[^>]*>.*?</local-command-[^>]*>|"
    r"<command-(?:name|message|args)>.*?</command-(?:name|message|args)>",
    re.S | re.I)


# Прогоны суммаризатора: я сам скармливаю модели скелет сессии. Это не
# разговор с Рувимом, а внутренняя кухня, и в выдаче она только мешает.
SUMMARIZER_RUN = re.compile(r"Ниже\s+—\s+скелет рабочей сессии|Сделай СЖАТОЕ резюме", re.I)


def is_system(text):
    t = text or ""
    return bool(SYSTEM_NOISE.match(t)) or bool(SUMMARIZER_RUN.search(t[:400]))


def clean_user(text):
    """Реплика Рувима без служебных вставок."""
    return INLINE_NOISE.sub(" ", text or "").strip()

# Пути к файлам из вызовов инструментов — общие файлы связывают блоки.
FILE_RX = re.compile(r'"(?:file_path|path|notebook_path)"\s*:\s*"([^"]+)"')


def _files(text):
    out = set()
    for m in FILE_RX.finditer(text or ""):
        out.add(os.path.basename(m.group(1).replace("\\", "/")))
    return out


def embed(texts, batch=32):
    """BGE-M3 локально. Пусто/недоступен → None, тогда решаем без семантики."""
    vecs = []
    for i in range(0, len(texts), batch):
        chunk = [t[:1800] for t in texts[i:i + batch]]
        req = urllib.request.Request(
            EMB, data=json.dumps({"texts": chunk}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            vecs.extend(json.loads(r.read())["vectors"])
    return vecs


def _cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return s / (na * nb) if na and nb else 0.0


# --- сборка turn blocks ----------------------------------------------------

def turn_blocks(con, session):
    """Реплика Рувима + вся работа по ней, до следующей его реплики.

    Идём по seq в пределах одного файла: внутри сессии это тот же порядок,
    что и цепочка parent_uuid, а на разорванных звеньях seq устойчивее.
    """
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM events WHERE session=? AND text!='' AND sub=0 ORDER BY seq",
        (session,)).fetchall()
    blocks, cur = [], None
    pending_cut = False          # /clear встретился — следующий блок начинает эпизод
    for r in rows:
        if r["role"] == "user" and is_system(r["text"]):
            # Служебная вставка: не реплика. Границу от /clear запоминаем,
            # остальное просто прицепляем к текущему блоку как контекст.
            if CLEAR.search(r["text"]):
                pending_cut = True
            elif cur:
                cur["end_uuid"] = r["uuid"]
                cur["uuids"].append(r["uuid"])
            continue
        if r["role"] == "user":
            said = clean_user(r["text"])
            if not said:
                continue
            if cur:
                blocks.append(cur)
            cur = {
                "start_uuid": r["uuid"], "end_uuid": r["uuid"],
                "ts": r["ts"], "session": session, "project": r["project"],
                "user": said, "assistant": [], "tools": [], "tool_texts": [], "files": set(),
                "uuids": [r["uuid"]], "hard_cut": pending_cut,
            }
            pending_cut = False
            cur["files"] |= _files(r["text"])
        elif cur:
            cur["end_uuid"] = r["uuid"]
            cur["uuids"].append(r["uuid"])
            cur["files"] |= _files(r["text"])
            if r["role"] == "assistant":
                cur["assistant"].append((r["uuid"], r["text"]))
                if r["tool"]:
                    cur["tools"].append(r["tool"])
            elif r["role"] == "tool":
                # Текст возврата инструмента: там живут порты, адреса, версии,
                # коды ошибок — то, что потом спрашивают, но чего нет ни в
                # реплике Рувима, ни в моём итоговом ответе.
                cur["tool_texts"].append(r["text"])
    if cur:
        blocks.append(cur)
    return blocks


def _goal_text(b):
    """Представление блока для сравнения: реплика + чем занимались.

    Голая реплика «да, делай» неотличима от любой другой короткой фразы,
    поэтому подмешиваем файлы и инструменты — они и есть контекст работы.
    """
    parts = [b["user"][:900]]
    if b["files"]:
        parts.append("файлы: " + ", ".join(sorted(b["files"])[:8]))
    if b["tools"]:
        parts.append("инструменты: " + ", ".join(sorted(set(b["tools"]))[:8]))
    tail = b["assistant"][-1][1][:400] if b["assistant"] else ""
    if tail:
        parts.append(tail)
    return "\n".join(parts)


def _minutes(a, b):
    if not a or not b:
        return 0.0
    try:
        import datetime
        ta = datetime.datetime.fromisoformat(a.replace("Z", "+00:00"))
        tb = datetime.datetime.fromisoformat(b.replace("Z", "+00:00"))
        return abs((tb - ta).total_seconds()) / 60.0
    except ValueError:
        return 0.0


def decide(prev, cur, cos):
    """Граница между блоками? Возвращает (bool, причина).

    Осторожность асимметрична в сторону склейки: лишний разрыв рвёт работу
    на куски без начала и конца, а лишняя склейка максимум добавит хвост.
    """
    text = cur["user"]
    if cur.get("hard_cut"):
        return True, "/clear"
    if NEW_TOPIC.search(text[:200]):
        return True, "объявлена новая тема"

    gap = _minutes(prev["ts"], cur["ts"])
    shared = prev["files"] & cur["files"]
    anaph = bool(ANAPHORA.match(text)) and len(text) < 400

    if shared and gap < 180:
        return False, f"общие файлы: {', '.join(sorted(shared)[:3])}"
    if anaph and gap < 90:
        return False, "продолжающая реплика"
    if cos is not None:
        if cos >= 0.55:
            return False, f"близко по смыслу ({cos:.2f})"
        if cos < 0.38 and gap > 45:
            return True, f"смысл разошёлся ({cos:.2f}) и пауза {gap:.0f} мин"
        if cos < 0.30:
            return True, f"смысл разошёлся ({cos:.2f})"
    if gap > 240:
        return True, f"пауза {gap / 60:.1f} ч"
    return False, "продолжение по умолчанию"


def segment(con, session, use_emb=True):
    """Список эпизодов сессии. Каждый — группа turn blocks."""
    blocks = turn_blocks(con, session)
    if not blocks:
        return []
    cosines = [None] * len(blocks)
    if use_emb and len(blocks) > 1:
        try:
            vecs = embed([_goal_text(b) for b in blocks])
            cosines = [None] + [_cos(vecs[i - 1], vecs[i]) for i in range(1, len(blocks))]
        except Exception:
            pass  # эмбеддер лежит — решаем по остальным признакам

    episodes, cur = [], [blocks[0]]
    idx = [0]                      # позиции блоков текущего эпизода
    reasons = ["начало сессии"]
    for i in range(1, len(blocks)):
        cut, why = decide(blocks[i - 1], blocks[i], cosines[i])
        if cut:
            episodes.append((cur, reasons[-1]))
            cur, idx, reasons = [blocks[i]], [i], reasons + [why]
        else:
            cur.append(blocks[i])
            idx.append(i)
    episodes.append((cur, reasons[-1]))

    return _split_long(episodes, cosines, blocks)


# Длинная работа над одной целью — законный эпизод, но пятичасовой кусок
# бесполезен как единица поиска: находка в нём снова обрывок без адреса.
# Поэтому переросшие эпизоды делим по самому слабому шву внутри.
MAX_BLOCKS = 8
MAX_MINUTES = 100


def _split_long(episodes, cosines, blocks):
    pos = {id(b): i for i, b in enumerate(blocks)}
    out = []
    queue = list(episodes)
    while queue:
        blks, why = queue.pop(0)
        span = _minutes(blks[0]["ts"], blks[-1]["ts"])
        if len(blks) <= MAX_BLOCKS and span <= MAX_MINUTES or len(blks) < 4:
            out.append((blks, why))
            continue
        # слабейший шов: минимальное сходство, не с самого края эпизода
        inner = range(1, len(blks))
        weakest, best = None, 2.0
        for j in inner:
            if min(j, len(blks) - j) < 2:      # не отрезаем по одному блоку
                continue
            c = cosines[pos[id(blks[j])]]
            c = 1.0 if c is None else c
            if c < best:
                best, weakest = c, j
        if weakest is None:
            out.append((blks, why))
            continue
        queue.insert(0, (blks[weakest:], f"длинный эпизод разделён (сходство {best:.2f})"))
        queue.insert(0, (blks[:weakest], why))
    return out


# --- карточки --------------------------------------------------------------

# Ответ, начинающийся с «[вызов …]», — это техническое действие, а не то, что
# я сказал Рувиму. В outcome_text должно попасть сказанное, иначе поиск «что
# ты тогда посчитал» опять упрётся в аргументы инструментов.
def _said(pairs):
    """Оставляет то, что я СКАЗАЛ, отбрасывая технические вызовы инструментов."""
    return [(u, t) for u, t in pairs if not t.lstrip().startswith("[вызов")]


# Признаки того, что предложение несёт РЕЗУЛЬТАТ, а не рассуждение: отметка
# о выполнении, деньги, номер брони/заказа, время записи, явная неудача.
_FACT = _re_fact = re.compile(
    r"(✅|❌|⚠|\$\s?\d|\d+\s?(?:₽|руб|USD)|"
    r"\b(?:готово|сделано|записан\w*|подтвержд\w+|забронирован\w*|оплачен\w*|отправлен\w*|"
    r"заказ|бронь|брони|номер\s+брони|подтверждение|отменен\w*|отменён\w*|не\s+удалось|"
    r"ошибка|провал\w*|исправлен\w*|установлен\w*|создан\w*|удалён\w*|перенесен\w*|перенесён\w*)\b)",
    re.I)
# Номера, которые потом ищут дословно: заказы, брони, трекинги.
_IDNUM = re.compile(r"\b[A-Z0-9]{6,}\d[A-Z0-9]*\b|\b\d{7,}\b")


def extract_facts(blocks, limit=14):
    """Итоги эпизода отдельной строкой: то, что потом спрашивают дословно.

    Берём из МОИХ ответов предложения с признаками результата. Это дешевле и
    честнее пересказа: строки идут как есть, ничего не додумывается.
    """
    out, seen = [], set()
    for b in blocks:
        for uid, t in _said(b["assistant"]):
            for sent in re.split(r"(?<=[.!?])\s+|\n", t):
                s = " ".join(sent.split())
                if not (12 < len(s) < 300):
                    continue
                if not (_FACT.search(s) or _IDNUM.search(s)):
                    continue
                key = s[:60].lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append((uid, s))
                if len(out) >= limit:
                    return out
    return out


def card(blocks, why=""):
    """Карточка эпизода: две поисковые проекции + якоря."""
    goal = "\n".join(b["user"] for b in blocks)
    out = []
    for b in blocks:
        out.extend(t for _u, t in _said(b["assistant"])[-2:])
    outcome = "\n".join(out)
    files, tools, uuids = set(), [], []
    for b in blocks:
        files |= b["files"]
        tools.extend(b["tools"])
        uuids.extend(b["uuids"])
    # Третья проекция — техническая изнанка эпизода: что реально делали и что
    # вернули команды. Проверено: запрос «на каком порту крутится эмбеддер»
    # близок к такому тексту на 0.43, а к цели и итогу эпизода — лишь на 0.24.
    detail = []
    for b in blocks:
        detail.extend(t for _u, t in b["assistant"])
        detail.extend(b["tool_texts"])
    detail_text = "\n".join(detail)[:6000]

    title = " ".join(blocks[0]["user"].split())[:110]
    return {
        "session": blocks[0]["session"], "project": blocks[0]["project"],
        "start_uuid": blocks[0]["start_uuid"], "end_uuid": blocks[-1]["end_uuid"],
        "start_ts": blocks[0]["ts"], "end_ts": blocks[-1]["ts"],
        "blocks": len(blocks), "title": title,
        "goal_text": goal[:6000], "outcome_text": outcome[:6000],
        # Факты храним и текстом (для поиска), и парами с якорями: каждое
        # утверждение можно открыть в исходном разговоре и проверить дословно.
        "detail_text": detail_text,
        "fact_pairs": extract_facts(blocks),
        "facts": "\n".join(t for _u, t in extract_facts(blocks))[:2500],
        "files": ", ".join(sorted(files)[:20]),
        "tools": ", ".join(sorted(set(tools))[:15]),
        "uuids": ",".join(uuids), "cut_reason": why,
    }


def ensure_tables(con):
    con.execute("""CREATE TABLE IF NOT EXISTS episodes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session TEXT, project TEXT,
        start_uuid TEXT, end_uuid TEXT, start_ts TEXT, end_ts TEXT,
        blocks INTEGER, title TEXT,
        goal_text TEXT, outcome_text TEXT, facts TEXT, detail_text TEXT,
        files TEXT, tools TEXT, uuids TEXT, cut_reason TEXT,
        segmenter INTEGER,
        vec_goal BLOB, vec_outcome BLOB)""")
    # Мягкая миграция: таблица могла быть создана прошлой версией без этих полей.
    for col, decl in (("facts", "TEXT"), ("detail_text", "TEXT"), ("vec_detail", "BLOB")):
        try:
            con.execute(f"ALTER TABLE episodes ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass    # колонка уже есть
    con.execute("CREATE INDEX IF NOT EXISTS ix_ep_session ON episodes(session)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_ep_ts ON episodes(start_ts)")
    # Обратная связь «событие → эпизод». Нужна, чтобы находку точного поиска
    # (она приходит с uuid сообщения) можно было привести к той же единице,
    # что и находку смыслового, и сложить обе выдачи в один список.
    # Проверяемые факты: строка ровно так, как я её сказал, плюс якорь на
    # сообщение. Позволяет открыть первоисточник, а не верить пересказу.
    con.execute("""CREATE TABLE IF NOT EXISTS ep_fact(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        episode_id INTEGER NOT NULL, uuid TEXT NOT NULL, text TEXT NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_fact_ep ON ep_fact(episode_id)")
    con.execute("""CREATE TABLE IF NOT EXISTS ep_uuid(
        uuid TEXT PRIMARY KEY, episode_id INTEGER NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_epu_ep ON ep_uuid(episode_id)")
    con.commit()


def link_uuids(con, verbose=False):
    """Заполняет ep_uuid из поля episodes.uuids."""
    con.execute("DELETE FROM ep_uuid")
    n = 0
    for eid, uu in con.execute("SELECT id, uuids FROM episodes WHERE uuids!=''"):
        rows = [(u, eid) for u in uu.split(",") if u]
        con.executemany("INSERT OR REPLACE INTO ep_uuid(uuid,episode_id) VALUES(?,?)", rows)
        n += len(rows)
    con.commit()
    if verbose:
        print(f"связей событие→эпизод: {n}")
    return n


def build(sessions=None, verbose=True):
    """Режет сессии на эпизоды, считает карточки и вектора."""
    import array
    con = catalog.db()
    ensure_tables(con)
    con.row_factory = sqlite3.Row
    if sessions is None:
        sessions = [r[0] for r in con.execute(
            "SELECT DISTINCT session FROM events WHERE text!='' AND sub=0 ORDER BY session")]
    else:
        # из инкрементального списка отсеиваем субагентов: эпизод — это диалог
        # с Рувимом, а у субагента реплики «пользователя» пишу я сам
        real = {r[0] for r in con.execute("SELECT DISTINCT session FROM events WHERE sub=0")}
        # 🔴 Сессии, событий которых больше НЕТ (транскрипт удалён или усечён
        # до нуля), раньше просто отбрасывались — и их старые эпизоды продолжали
        # жить и находиться поиском (воспроизвёл архитектор 05.08). Сначала
        # чистим за ними, и только потом решаем, есть ли из чего строить новые.
        for s in sessions:
            if s not in real:
                con.execute("DELETE FROM ep_fact WHERE episode_id IN "
                            "(SELECT id FROM episodes WHERE session=?)", (s,))
                con.execute("DELETE FROM ep_uuid WHERE episode_id IN "
                            "(SELECT id FROM episodes WHERE session=?)", (s,))
                con.execute("DELETE FROM thread_episode WHERE episode_id IN "
                            "(SELECT id FROM episodes WHERE session=?)", (s,))
                con.execute("DELETE FROM episodes WHERE session=?", (s,))
        con.commit()
        sessions = [s for s in sessions if s in real]
    t0, total = time.time(), 0
    for s in sessions:
        # Всё, что ссылается на эпизод, удаляем вместе с ним, иначе остаются
        # сироты: факты без хозяина, ep_uuid, ведущие в никуда, и связи нитей
        # с несуществующими эпизодами (критерий приёмки архитектора 05.08).
        con.execute("DELETE FROM ep_fact WHERE episode_id IN "
                    "(SELECT id FROM episodes WHERE session=?)", (s,))
        con.execute("DELETE FROM ep_uuid WHERE episode_id IN "
                    "(SELECT id FROM episodes WHERE session=?)", (s,))
        con.execute("DELETE FROM thread_episode WHERE episode_id IN "
                    "(SELECT id FROM episodes WHERE session=?)", (s,))
        con.execute("DELETE FROM episodes WHERE session=?", (s,))
        eps = segment(con, s)
        if not eps:
            continue
        cards = [card(bl, why) for bl, why in eps]
        try:
            vg = embed([c["title"] + "\n" + c["goal_text"] for c in cards])
            vo = embed([c["outcome_text"] or c["title"] for c in cards])
            vd = embed([c["detail_text"] or c["title"] for c in cards])
        except Exception as e:
            print(f"  эмбеддер недоступен ({type(e).__name__}) — вектора пропущены", file=sys.stderr)
            vg = vo = vd = [None] * len(cards)
        for c, g, o, dv in zip(cards, vg, vo, vd):
            cur = con.execute("""INSERT INTO episodes(session,project,start_uuid,end_uuid,start_ts,
                end_ts,blocks,title,goal_text,outcome_text,facts,detail_text,files,tools,uuids,cut_reason,
                segmenter,vec_goal,vec_outcome,vec_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (c["session"], c["project"], c["start_uuid"], c["end_uuid"], c["start_ts"],
                 c["end_ts"], c["blocks"], c["title"], c["goal_text"], c["outcome_text"], c["facts"], c["detail_text"],
                 c["files"], c["tools"], c["uuids"], c["cut_reason"], VERSION,
                 array.array("f", g).tobytes() if g else None,
                 array.array("f", o).tobytes() if o else None,
                 array.array("f", dv).tobytes() if dv else None))
            if c.get("fact_pairs"):
                con.executemany(
                    "INSERT INTO ep_fact(episode_id,uuid,text) VALUES(?,?,?)",
                    [(cur.lastrowid, u, t) for u, t in c["fact_pairs"]])
        con.commit()
        total += len(cards)
        if verbose:
            print(f"  {s[:8]} → эпизодов {len(cards)}", flush=True)
    link_uuids(con, verbose)
    if verbose:
        print(f"\nвсего эпизодов: {total}, время: {time.time() - t0:.1f} с")
    return total


# Матрица векторов кэшируется в процессе: на хуке поиск идёт на каждую реплику,
# и пересборка из BLOB'ов каждый раз стоила бы больше самого поиска.
_CACHE = {"rows": None, "goal": None, "outcome": None, "detail": None}


def _matrix(con):
    import numpy as np
    if _CACHE["rows"] is not None:
        return _CACHE["rows"], _CACHE["goal"], _CACHE["outcome"], _CACHE["detail"]
    rows = con.execute("SELECT * FROM episodes WHERE vec_goal IS NOT NULL").fetchall()
    if not rows:
        return [], None, None, None
    dim = len(rows[0]["vec_goal"]) // 4

    def stack(field):
        # где вектора нет — подставляем вектор цели, иначе форма матрицы
        # разъедется с числом строк
        return np.frombuffer(
            b"".join((r[field] if field in r.keys() and r[field] else r["vec_goal"])
                     for r in rows), dtype="float32").reshape(len(rows), dim)

    goal = np.frombuffer(b"".join(r["vec_goal"] for r in rows), dtype="float32").reshape(len(rows), dim)
    outcome = stack("vec_outcome")
    detail = stack("vec_detail")
    _CACHE.update({"rows": rows, "goal": goal, "outcome": outcome, "detail": detail})
    return rows, goal, outcome, detail


def search(query, k=6, con=None):
    """Смысловой поиск по эпизодам: цель ИЛИ результат, что ближе."""
    import numpy as np
    con = con or catalog.db()
    con.row_factory = sqlite3.Row
    rows, G, O, D = _matrix(con)
    if not rows:
        return []
    qv = np.asarray(embed([query])[0], dtype="float32")
    qn = np.linalg.norm(qv) + 1e-9
    sg = (G @ qv) / (np.linalg.norm(G, axis=1) * qn + 1e-9)
    so = (O @ qv) / (np.linalg.norm(O, axis=1) * qn + 1e-9)
    # третья проекция — техническая изнанка: порты, адреса, версии, коды
    sd = (D @ qv) / (np.linalg.norm(D, axis=1) * qn + 1e-9)
    best = np.maximum(np.maximum(sg, so), sd)
    order = np.argsort(-best)[:max(k * 3, 30)]

    def which(i):
        return "цель" if best[i] == sg[i] else ("результат" if best[i] == so[i] else "детали")

    hits = [(float(best[i]), which(i), rows[int(i)]) for i in order]
    return [{
        "score": round(s, 3), "matched": what, "title": r["title"],
        "start_ts": (r["start_ts"] or "")[:16].replace("T", " "),
        "session": r["session"][:8], "project": r["project"],
        "start_uuid": r["start_uuid"], "blocks": r["blocks"],
        "files": r["files"], "outcome": " ".join((r["outcome_text"] or "").split())[:300],
    } for s, what, r in hits[:k]]


def main():
    ap = argparse.ArgumentParser(description="Эпизоды: нарезка, карточки, поиск")
    ap.add_argument("query", nargs="?")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--session", help="только одна сессия")
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.build:
        build([a.session] if a.session else None)
        if not a.query:
            return
    if not a.query:
        ap.error("нужен запрос или --build")

    hits = search(a.query, a.k)
    if a.json:
        print(json.dumps(hits, ensure_ascii=False, indent=1))
        return
    for h in hits:
        print(f"\n[{h['score']:.2f} по {h['matched']}] {h['start_ts']}  "
              f"(сессия {h['session']}, блоков {h['blocks']})")
        print(f"   {h['title']}")
        if h["outcome"]:
            print(f"   итог: {h['outcome'][:170]}")
        print(f"   якорь: {h['start_uuid']}")


if __name__ == "__main__":
    main()

def prune_orphans(con=None, verbose=True):
    """Убрать эпизоды сессий, которых больше нет в событиях, и висячие ссылки.

    🔴 Нужна отдельно от build(): та чистит только сессии, попавшие в список
    изменившихся. Эпизоды сессий, чьи транскрипты удалили ДО появления этой
    проверки, оставались навсегда и находились поиском (критерий приёмки
    архитектора 05.08 — ноль сирот в ep_fact, ep_uuid, thread_episode).
    """
    con = con or catalog.db()
    dead = [r[0] for r in con.execute(
        "SELECT DISTINCT session FROM episodes "
        "WHERE session NOT IN (SELECT DISTINCT session FROM events)")]
    n_ep = 0
    for s in dead:
        con.execute("DELETE FROM ep_fact WHERE episode_id IN "
                    "(SELECT id FROM episodes WHERE session=?)", (s,))
        con.execute("DELETE FROM ep_uuid WHERE episode_id IN "
                    "(SELECT id FROM episodes WHERE session=?)", (s,))
        con.execute("DELETE FROM thread_episode WHERE episode_id IN "
                    "(SELECT id FROM episodes WHERE session=?)", (s,))
        n_ep += con.execute("DELETE FROM episodes WHERE session=?", (s,)).rowcount
    # висячие ссылки на несуществующие эпизоды — независимо от сессий
    hang = 0
    for tbl in ("ep_fact", "ep_uuid", "thread_episode"):
        try:
            hang += con.execute(
                f"DELETE FROM {tbl} WHERE episode_id NOT IN (SELECT id FROM episodes)").rowcount
        except Exception:
            pass
    con.commit()
    if verbose and (n_ep or hang):
        print(f"сироты убраны: эпизодов {n_ep} из {len(dead)} сессий, висячих ссылок {hang}")
    return n_ep, hang
