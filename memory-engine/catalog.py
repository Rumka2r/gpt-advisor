#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Каталог событий — фундамент исторического индекса.

Транскрипты (.jsonl) остаются источником истины и не меняются. Каталог —
это карта поверх них: у каждого сообщения запоминается якорь (проект, файл,
uuid, время, порядковый номер) и очищенный текст.

Зачем отдельный слой, а не сразу эмбеддинги:
  * по нему строится read_window() — дочитывание контекста вокруг находки;
  * по нему пойдёт точный поиск по словам (FTS), где важны пути и числа;
  * эпизоды режутся по turn blocks, а их надо сперва собрать.

Цепочка разговора восстанавливается по parent_uuid, а НЕ по порядку строк:
при ветвлении (правка реплики, откат) соседние строки файла могут
принадлежать разным веткам, и физический порядок склеил бы чужие куски.

Текст прогоняется через redact.py на входе: в каталог секреты не попадают.
"""

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import redact  # noqa: E402
import transcript as T  # noqa: E402

ROOT = os.path.expanduser("~/.claude/continuity")
DB = os.path.join(ROOT, "index", "catalog.sqlite")

# Выводы инструментов не храним целиком: 75% объёма архива и почти нулевая
# ценность для поиска. Держим голову и хвост — там команда и её итог.
TOOL_HEAD = 400
TOOL_TAIL = 400

# Сколько байт начала файла берём в отпечаток подмены.
HEAD_BYTES = 512


def db(path=DB):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # timeout/busy_timeout: при параллельной работе (воркер + поиск + хук) writer
    # должен ЖДАТЬ освобождения, а не падать с «database is locked» — на этом
    # 05.08 сорвалась сборка исторического индекса.
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("""CREATE TABLE IF NOT EXISTS events(
        uuid TEXT PRIMARY KEY,
        parent_uuid TEXT,
        session TEXT NOT NULL,
        project TEXT NOT NULL,
        role TEXT NOT NULL,          -- user | assistant | tool
        ts TEXT,
        seq INTEGER NOT NULL,        -- порядок строки в файле
        text TEXT,                   -- очищенный
        tool TEXT,                   -- имя инструмента для role='tool'
        secrets TEXT,                -- типы вычищенного, через запятую
        sub INTEGER DEFAULT 0,       -- 1 = транскрипт субагента, не диалог с Рувимом
        chars INTEGER,
        src TEXT NOT NULL,           -- путь к .jsonl
        redactor INTEGER NOT NULL    -- версия правил очистки
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_ev_session ON events(session, seq)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_ev_parent  ON events(parent_uuid)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_ev_ts      ON events(ts)")
    con.execute("""CREATE TABLE IF NOT EXISTS sources(
        src TEXT PRIMARY KEY, mtime REAL, size INTEGER,
        events INTEGER, indexed_at REAL, redactor INTEGER)""")
    con.commit()
    return con


def _text_of(content):
    """Достаёт человекочитаемый текст из message.content (строка или блоки)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            out.append(b.get("text") or "")
        elif t == "thinking":
            continue  # размышления в каталог не кладём
        elif t == "tool_use":
            args = json.dumps(b.get("input") or {}, ensure_ascii=False)
            out.append(f"[вызов {b.get('name')}] {args[:600]}")
        elif t == "tool_result":
            c = b.get("content")
            s = c if isinstance(c, str) else _text_of(c)
            if len(s) > TOOL_HEAD + TOOL_TAIL:
                s = f"{s[:TOOL_HEAD]}\n…[{len(s) - TOOL_HEAD - TOOL_TAIL} знаков опущено]…\n{s[-TOOL_TAIL:]}"
            out.append(s)
    return "\n".join(x for x in out if x)


def _tool_name(content):
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                return b.get("name")
    return None


def _role_of(d, content):
    t = d.get("type")
    if t == "assistant":
        return "assistant"
    if t == "user":
        # user-запись с tool_result — это возврат инструмента, не реплика человека
        if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return "tool"
        return "user"
    return "other"


def _ensure_tail_columns(con):
    """Позиция и номер последнего разобранного события — для дочитывания хвоста."""
    have = {r[1] for r in con.execute("PRAGMA table_info(sources)")}
    for col in ("last_offset", "last_seq", "head_hash"):
        if col not in have:
            typ = "TEXT" if col == "head_hash" else "INTEGER"
            con.execute(f"ALTER TABLE sources ADD COLUMN {col} {typ}")
    con.commit()


def _head_hash(path, n=HEAD_BYTES):
    """Отпечаток начала файла — чтобы отличить дописывание от подмены целиком.

    Файл могли атомарно заменить другим, который случайно оказался больше: по
    одному лишь размеру это неотличимо от дописывания, и мы бы приклеили чужой
    хвост к старым событиям (замечание архитектора 05.08).
    """
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read(n)).hexdigest()[:16]
    except OSError:
        return ""


def index_file(con, path, project, force=False, sub=False):
    """Заносит один транскрипт.

    Возвращает словарь: changed / added / deleted / session / reason.
    🔴 Не число: усечение и удаление НЕ добавляют событий, но обязаны помечать
    сессию изменённой — иначе её эпизоды остаются от прежнего содержимого
    (нашёл архитектор 05.08).

    sub=True — транскрипт субагента. Он наследует sessionId родителя, поэтому
    ключ сессии подменяем на имя файла: иначе события субагентов перемешаются
    с основным разговором (нумерация строк у каждого файла своя) и разорвут
    эпизоды. В эпизоды такие события не идут, в точный поиск — идут.
    """
    session = os.path.basename(path).replace(".jsonl", "")
    res = {"changed": False, "added": 0, "deleted": 0, "session": session, "reason": ""}

    st = os.stat(path)
    _ensure_tail_columns(con)
    row = con.execute("SELECT mtime, size, redactor, last_offset, last_seq, head_hash "
                      "FROM sources WHERE src=?", (path,)).fetchone()
    if row and not force and row[0] == st.st_mtime and row[1] == st.st_size and row[2] == redact.VERSION:
        return res  # не менялся и очищен текущими правилами

    # 🔴 Отпечаток берём по ПРЕЖНЕМУ размеру файла, а не по фиксированным 512
    # байтам: у файла короче 512 «начало» — это он весь, и любое дописывание
    # меняло отпечаток, из-за чего дочитывание вырождалось в полную пересборку
    # (поймано собственным регрессионным тестом 05.08).
    prev_size = int(row[1] or 0) if row else 0
    head_prev = _head_hash(path, min(HEAD_BYTES, prev_size)) if prev_size else ""
    head = _head_hash(path, min(HEAD_BYTES, st.st_size))
    # 🔴 Дочитывание хвоста вместо полного перечитывания (архитектор 05.08).
    # Транскрипт активной сессии только РАСТЁТ, а раньше любое изменение
    # заставляло стирать все события файла и разбирать его заново. На живом
    # разговоре это слишком дорого, поэтому индекс отставал на часы — и свежая
    # работа в поиск не попадала. Продолжаем читать, только если совпало ВСЁ:
    # версия чистки, отпечаток начала файла, и файл именно вырос.
    tail = bool(row and not force and row[2] == redact.VERSION
                and row[3] and st.st_size > row[1]
                and (row[5] or head_prev) == head_prev)
    res["reason"] = ("дописан" if tail else
                     "усечён" if (row and st.st_size < row[1]) else
                     "переписан" if row else "новый")

    rows = []
    seq = row[4] if tail else 0
    start = row[3] if tail else 0

    # Транзакция на один файл: до неё исключение после DELETE могло уехать в
    # общий коммит соседнего файла и оставить события стёртыми.
    con.execute("SAVEPOINT idx_file")
    try:
        if not tail:
            res["deleted"] = con.execute("DELETE FROM events WHERE src=?", (path,)).rowcount

        def make_row(d, seq_no):
            uuid = d.get("uuid")
            if not uuid:
                return None   # служебные записи (mode, file-history-snapshot)
            msg = d.get("message") or {}
            content = msg.get("content")
            raw = _text_of(content)
            # Записи без текста (только размышления, служебные) всё равно заносим:
            # на них ссылаются parent_uuid следующих сообщений, и без этих звеньев
            # цепочка разговора рвётся, а дочитывание упирается в пустоту.
            clean, kinds = redact.redact(raw) if raw.strip() else ("", [])
            return (uuid, d.get("parentUuid"),
                    session if sub else (d.get("sessionId") or session), project,
                    _role_of(d, content), d.get("timestamp"), seq_no, clean,
                    _tool_name(content), ",".join(kinds), 1 if sub else 0,
                    len(clean), path, redact.VERSION)

        # Двоичный режим: в текстовом нельзя одновременно перебирать строки и
        # спрашивать позицию (tell), а позиция нам и нужна для следующего раза.
        with open(path, "rb") as fh:
            fh.seek(start)
            # 🔴 Позицию двигаем ТОЛЬКО за дописанными до конца строками:
            # транскрипт пишется прямо сейчас, и сдвиг за обрезанной строкой
            # потерял бы событие навсегда.
            good_offset = start
            leftover = b""
            for bline in fh:
                if not bline.endswith(b"\n"):
                    leftover = bline
                    break
                good_offset += len(bline)
                seq += 1
                try:
                    d = json.loads(bline.decode("utf-8", errors="ignore"))
                except ValueError:
                    continue
                r = make_row(d, seq)
                if r:
                    rows.append(r)

            # 🔴 Последняя строка без перевода строки может быть ЦЕЛЫМ событием
            # (сессия закончилась, файл больше не растёт). Раньше она не
            # попадала в индекс никогда. Заносим её, но позицию НЕ двигаем:
            # если её потом допишут, перечитаем — uuid первичный ключ, дубля
            # не будет.
            if leftover.strip():
                try:
                    d = json.loads(leftover.decode("utf-8", errors="ignore"))
                    r = make_row(d, seq + 1)
                    if r:
                        rows.append(r)
                        res["reason"] += " (+незавершённая строка)"
                except ValueError:
                    pass

        con.executemany(
            "INSERT OR REPLACE INTO events(uuid,parent_uuid,session,project,role,ts,seq,"
            "text,tool,secrets,sub,chars,src,redactor) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        total = (row[4] if tail else 0) + len(rows)
        con.execute("INSERT OR REPLACE INTO sources(src,mtime,size,events,indexed_at,"
                    "redactor,last_offset,last_seq,head_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                    (path, st.st_mtime, st.st_size, total, time.time(), redact.VERSION,
                     good_offset, seq, head))
    except Exception:
        con.execute("ROLLBACK TO idx_file")
        con.execute("RELEASE idx_file")
        raise
    con.execute("RELEASE idx_file")
    con.commit()

    res["added"] = len(rows)
    res["changed"] = bool(rows or res["deleted"] or not tail)
    return res


def forget_missing(con, verbose=False):
    """Убрать из индекса транскрипты, которых больше нет на диске.

    Раньше удалённый файл жил в `sources` и `events` вечно: его текст
    продолжал находиться поиском (замечание архитектора 05.08).
    """
    gone = [r[0] for r in con.execute("SELECT src FROM sources")
            if not os.path.exists(r[0])]
    for p in gone:
        con.execute("DELETE FROM events WHERE src=?", (p,))
        con.execute("DELETE FROM sources WHERE src=?", (p,))
    if gone:
        con.commit()
        if verbose:
            print(f"забыто исчезнувших транскриптов: {len(gone)}")
    return gone


def _dirty_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS dirty_sessions(
        session TEXT PRIMARY KEY, marked_at REAL NOT NULL)""")


def mark_dirty(con, sessions):
    """Поставить сессии в очередь на пересборку эпизодов.

    🔴 Устойчивая очередь в базе, а не список в памяти. Раньше кто первым
    позвал catalog.build(), тот и получал `changed`; второй вызов (в воркере)
    видел уже пустой список, и эпизоды с FTS для этих сессий не строились
    НИКОГДА (воспроизвёл архитектор 05.08). Передавать список аргументом
    процесса тоже нельзя: упавший воркер унёс бы изменения с собой.
    """
    if not sessions:
        return 0
    _dirty_table(con)
    con.executemany("INSERT OR REPLACE INTO dirty_sessions(session, marked_at) VALUES(?,?)",
                    [(s, time.time()) for s in sessions])
    con.commit()
    return len(sessions)


def take_dirty(con):
    """Что ждёт пересборки. Из очереди НЕ убираем — только после успеха."""
    _dirty_table(con)
    return [r[0] for r in con.execute("SELECT session FROM dirty_sessions")]


def clear_dirty(con, sessions):
    """Снять с очереди то, что реально пересобрано."""
    if not sessions:
        return 0
    _dirty_table(con)
    con.executemany("DELETE FROM dirty_sessions WHERE session=?", [(s,) for s in sessions])
    con.commit()
    return len(sessions)


def enumerate_sources():
    """ЕДИНЫЙ перечень транскриптов: (имя, путь, субагент?).

    🔴 Один список для сборщика и для сверки свежести. Раньше health.behind()
    перебирал только верхнеуровневые .jsonl своим кодом, пропускал субагентов
    и не замечал ИСЧЕЗНУВШИЕ файлы, о которых знает sources, — и уверенно
    докладывал «отставания нет» (воспроизвёл архитектор 05.08).

    Возвращает (список, ok): ok=False — хотя бы один корень не прочитан,
    значит судить о свежести нельзя.
    """
    out, ok = [], True
    dirs = T.project_dirs()
    if not dirs:
        return out, False
    for proj_dir in dirs:
        try:
            names = sorted(os.listdir(proj_dir))
        except OSError:
            ok = False
            continue
        for n in names:
            if n.endswith(".jsonl"):
                out.append((n, os.path.join(proj_dir, n), False))
        for sess in names:
            sd = os.path.join(proj_dir, sess, "subagents")
            if os.path.isdir(sd):
                try:
                    subs = sorted(os.listdir(sd))
                except OSError:
                    ok = False
                    continue
                out += [(f"{sess[:8]}/{n}", os.path.join(sd, n), True)
                        for n in subs if n.endswith(".jsonl")]
    return out, ok


def build(force=False, verbose=True, limit=None, return_changed=False):
    """Индексирует изменившиеся транскрипты.

    return_changed=True → вернуть множество сессий, которые реально обновились:
    по нему пересобираются только их эпизоды, а не все 550.
    """
    con = db()
    total = files = 0
    changed = set()
    t0 = time.time()
    # 🔴 СНАЧАЛА перечисляем, и только потом что-то забываем. Раньше
    # forget_missing() шёл первым, а результат ok перечислителя игнорировался:
    # временно отвалившийся каталог транскриптов принимался за «все файлы
    # удалены», и sources с events обнулялись (воспроизвёл архитектор 05.08).
    # Транскрипты субагентов лежат в <сессия>/subagents/. Это моя рабочая
    # кухня (разведка, параллельные проверки), но её итоги часто не доходят
    # до ответа целиком — искать по ним полезно.
    srcs, roots_ok = enumerate_sources()
    if roots_ok:
        for p in forget_missing(con, verbose=verbose):
            changed.add(os.path.basename(p).replace(".jsonl", ""))
    elif verbose:
        print("ВНИМАНИЕ: часть корней транскриптов не прочитана — "
              "ничего не удаляю, индексирую только доступное")
    by_proj = {}
    for name, path, is_sub in srcs:
        by_proj.setdefault(os.path.basename(os.path.dirname(
            os.path.dirname(path) if is_sub else path)), []).append((name, path, is_sub))
    for project, names in by_proj.items():
        for name, path, is_sub in names:
            try:
                r = index_file(con, path, project, force, sub=is_sub)
            except Exception as e:                      # один битый файл не должен рушить прогон
                print(f"  ОШИБКА {name[:12]}: {type(e).__name__} {e}", file=sys.stderr)
                continue
            files += 1
            total += r["added"]
            # 🔴 Помечаем изменённой по флагу changed, а не по числу добавленных:
            # усечённый или переписанный файл добавляет ноль событий, но его
            # эпизоды обязаны пересобраться (иначе останутся от старого текста).
            if r["changed"]:
                changed.add(name.replace(".jsonl", ""))
                if verbose:
                    print(f"  {project[:28]:30} {name[:12]} +{r['added']} "
                          f"-{r['deleted']} ({r['reason']})", flush=True)
            if limit and files >= limit:
                break
        if limit and files >= limit:
            break
    dt = time.time() - t0
    # Любое изменение немедленно попадает в устойчивую очередь: даже если этот
    # процесс сейчас упадёт, воркер потом всё равно найдёт, что пересобрать.
    if changed:
        mark_dirty(con, changed)
    if verbose:
        print(f"\nфайлов: {files}, событий добавлено: {total}, время: {dt:.1f} с")
    return changed if return_changed else total


def stats():
    con = db()
    q = con.execute("SELECT role, COUNT(*), SUM(chars) FROM events GROUP BY role").fetchall()
    print("события по ролям:")
    for role, n, ch in q:
        print(f"  {role:10} {n:8}  {(ch or 0)/1e6:8.1f} МБ")
    tot = con.execute("SELECT COUNT(*), COUNT(DISTINCT session), COUNT(DISTINCT project) FROM events").fetchone()
    print(f"всего: {tot[0]} событий, {tot[1]} сессий, {tot[2]} проектов")
    sec = con.execute("SELECT COUNT(*) FROM events WHERE secrets != ''").fetchone()[0]
    print(f"событий с вычищенными секретами: {sec}")
    top = con.execute("SELECT secrets, COUNT(*) FROM events WHERE secrets!='' "
                      "GROUP BY secrets ORDER BY 2 DESC LIMIT 8").fetchall()
    for s, n in top:
        print(f"   {s:34} {n}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Каталог событий из транскриптов")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--force", action="store_true", help="переиндексировать всё заново")
    ap.add_argument("--limit", type=int, help="ограничить число файлов (для проверки)")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.build:
        build(force=a.force, limit=a.limit)
    if a.stats or not a.build:
        stats()
