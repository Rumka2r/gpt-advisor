#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Индекс ПРОДУКТОВ работы: какие файлы и папки появились, когда, в каком деле.

Зачем. 05.08.2026 Рувим спросил «ты подготовил последнюю версию большого набора
под 0.6?». Ответ лежал в `jig/p06/big_06_rev/` с меткой времени накануне 21:32.
Поиск не нашёл его ни разу — потому что индексируются РАЗГОВОРЫ (реплики,
эпизоды, файлы памяти), а продуктов работы не знает никто. Вопрос «что ты вчера
делал» — в первую очередь про продукты, а не про реплики.

Решение (архитектор 05.08): отдельный индекс артефактов по разрешённым рабочим
корням, а не по всему диску.

Как не захлебнуться. В `work/osha/ferguson_probe` лежит 404 тысячи файлов
скрапа. Правило: папка с числом файлов больше BIG_DIR попадает в индекс ОДНОЙ
записью («папка, 404715 файлов, последнее изменение …»), а её содержимое не
разворачивается. Профили браузеров, кэши, venv и установленные программы
пропускаются целиком.
"""
import os
import pathlib
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog  # noqa: E402

ROOTS = [os.path.expanduser("~/work")]

# Каталоги, которые не несут продуктов работы: профили браузеров, кэши,
# зависимости, установленные программы, наши же покадровые съёмки печати.
SKIP_DIRS = {
    ".git", ".hg", "node_modules", "venv", ".venv", "__pycache__", "frames",
    "Cache", "cache", "Code Cache", "GPUCache", "Crashpad", "Default",
    ".playwright-mcp", "profile-browser", "BambuStudio-110", ".pytest_cache",
    "dist", "build", ".next", "site-packages", "Temp", "tmp",
}
SKIP_PREFIX = ("chrome-", "chrome_", ".playwright")
SKIP_EXT = {".pyc", ".pyo", ".tmp", ".lock", ".pyd", ".dll", ".so", ".class"}

BIG_DIR = 400        # больше файлов — папку не разворачиваем
SCAN_STAMP = pathlib.Path.home() / '.claude' / 'continuity' / 'state' / '.artifacts_scan'
MAX_DEPTH = 6


def db(con=None):
    con = con or catalog.db()
    con.execute("""CREATE TABLE IF NOT EXISTS artifact(
        path     TEXT PRIMARY KEY,
        parent   TEXT,
        name     TEXT NOT NULL,
        project  TEXT,              -- верхняя папка внутри work/: osha, bridge…
        kind     TEXT NOT NULL,     -- 'файл' | 'папка'
        ext      TEXT,
        size     INTEGER,
        files    INTEGER,           -- для папки: сколько внутри
        mtime    REAL NOT NULL,
        seen_at  REAL NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_art_mtime ON artifact(mtime DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_art_name ON artifact(name)")
    con.commit()
    return con


def _base(path):
    """Имя последнего элемента пути НЕЗАВИСИМО от разделителя.

    🔴 В базе лежат windows-пути, а разбирал их os.path — на Linux
    `os.path.basename(r"W:")` возвращает всю строку целиком, и проверка
    имени папки не работала (архитектор поймал на своём прогоне 05.08).
    """
    if not path:
        return ""
    return re.split(r"[\\/]", str(path).rstrip("\\/"))[-1]


def _project(path):
    parts = os.path.normpath(path).split(os.sep)
    if "work" in parts:
        i = parts.index("work")
        if i + 1 < len(parts):
            return parts[i + 1]
    return ""


def scan(roots=None, con=None, verbose=True):
    """Сверка с файловой системой. Возвращает (записей, пропущено больших папок)."""
    con = db(con)
    now = time.time()
    rows, big = [], 0
    read_ok = 0            # сколько корней удалось реально прочитать
    for root in (roots or ROOTS):
        if not os.path.isdir(root):
            continue
        base_depth = os.path.normpath(root).count(os.sep)
        seen_any = False
        for cur, dirs, files in os.walk(root):
            seen_any = True
            if os.path.normpath(cur).count(os.sep) - base_depth >= MAX_DEPTH:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs
                       if d not in SKIP_DIRS and not d.startswith(SKIP_PREFIX)
                       and not d.startswith(".")]
            files = [f for f in files if os.path.splitext(f)[1].lower() not in SKIP_EXT
                     and not f.startswith(".")]

            # сама папка — тоже продукт работы: «появилась папка big_06_rev»
            try:
                st = os.stat(cur)
            except OSError:
                continue
            newest = st.st_mtime
            for f in files[:BIG_DIR]:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(cur, f)))
                except OSError:
                    pass
            rows.append((cur, os.path.dirname(cur), os.path.basename(cur),
                         _project(cur), "папка", "", None, len(files), newest, now))

            if len(files) > BIG_DIR:
                # свалка вроде скрапа на 404 тысячи файлов — одной записью
                big += 1
                dirs[:] = []
                continue
            for f in files:
                p = os.path.join(cur, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                rows.append((p, cur, f, _project(p), "файл",
                             os.path.splitext(f)[1].lower(), st.st_size, None,
                             st.st_mtime, now))
        if seen_any:
            read_ok += 1

    # 🔴 Если НИ ОДИН корень не прочитался (диск отвалился, папку унесли),
    # старый индекс НЕ трогаем. Раньше пустой обход считал все файлы
    # исчезнувшими и стирал индекс целиком (воспроизвёл архитектор 05.08).
    if not read_ok:
        # 🔴 Молчать нельзя: старый индекс сохранён, но он МОГ устареть, а
        # выдача покажет его как актуальный. Возвращаем признак сбоя, чтобы
        # вызывающий поднял partial и не ставил отметку успешного обхода
        # (замечание архитектора 05.08).
        if verbose:
            print("артефакты: ни один рабочий корень не прочитан — "
                  "индекс сохранён, статус failed_roots")
        return {"status": "failed_roots", "rows": 0, "big": 0,
                "roots": len(roots or ROOTS), "read": 0}

    con.executemany("""INSERT INTO artifact(path,parent,name,project,kind,ext,size,files,mtime,seen_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET
          mtime=excluded.mtime, size=excluded.size, files=excluded.files,
          seen_at=excluded.seen_at""", rows)
    # исчезнувшее убираем: индекс должен отражать то, что есть сейчас
    gone = con.execute("DELETE FROM artifact WHERE seen_at < ?", (now - 1,)).rowcount
    con.commit()
    status = "ok" if read_ok == len(roots or ROOTS) else "partial"
    # 🔴 Отметку успешного обхода ставит САМ обход, а не только scan_if_old:
    # воркер зовёт scan() напрямую, отметка не обновлялась, и поиск считал
    # снимок вечно устаревшим — постоянный partial на ровном месте.
    try:
        SCAN_STAMP.parent.mkdir(parents=True, exist_ok=True)
        SCAN_STAMP.write_text(time.strftime('%Y-%m-%dT%H:%M:%S'), encoding='utf-8')
    except OSError:
        pass
    if verbose:
        print(f"артефактов: {len(rows)} · больших папок свёрнуто: {big} · "
              f"удалено исчезнувших: {gone} · статус {status}")
    return {"status": status, "rows": len(rows), "big": big,
            "roots": len(roots or ROOTS), "read": read_ok}


def scan_if_old(max_age_s=120, con=None, roots=None):
    """Обойти файлы, только если прошлый обход старше max_age_s.

    Своя отметка времени, а не общий водяной знак индекса: обход ~/work стоит
    секунды, и привязывать его к свежести транскриптов неправильно
    (замечание архитектора 05.08).
    """
    try:
        age = time.time() - SCAN_STAMP.stat().st_mtime
        if age < max_age_s:
            return 0
    except OSError:
        pass
    res = scan(roots=roots, con=con, verbose=False)
    # Отметка успешного обхода — только если корни реально прочитаны, иначе
    # следующий вызов решит, что всё свежо, и сбой останется незамеченным.
    if res["status"] != "failed_roots":
        try:
            SCAN_STAMP.parent.mkdir(parents=True, exist_ok=True)
            SCAN_STAMP.write_text(time.strftime('%Y-%m-%dT%H:%M:%S'), encoding='utf-8')
        except OSError:
            pass
    return res


def recent(hours=48, limit=25, project=None, con=None):
    """Что появилось или менялось за последние N часов — по папкам."""
    con = db(con)
    con.row_factory = sqlite3.Row
    since = time.time() - hours * 3600
    q = ("SELECT * FROM artifact WHERE kind='папка' AND mtime > ? "
         + ("AND project=? " if project else "")
         + "ORDER BY mtime DESC LIMIT ?")
    args = (since, project, limit) if project else (since, limit)
    return con.execute(q, args).fetchall()


def search(query, k=5, con=None, hours=None):
    """Поиск по именам файлов и папок + свежесть.

    Слова запроса ищем в пути. Совпадение по имени папки весит больше, чем по
    глубоко зарытому файлу: спрашивают обычно про «набор», а не про plate_1.
    """
    import re
    con = db(con)
    con.row_factory = sqlite3.Row
    words = [w for w in re.findall(r"\w{3,}", query.lower(), re.U)]
    # Папки называются латиницей («big_06_rev», «nabor»), а спрашивают
    # по-русски. Без транслитерации «большой набор» не встречает ни одной папки.
    try:
        import translit
        words += list(translit.expand(query))
    except Exception:
        pass
    # Русско-английские пары, которыми реально названы рабочие папки.
    # 🔴 Слова «сопло» здесь НЕТ намеренно: раньше оно означало «06», и запрос
    # «набор под сопло 0.4» поднимал big_06_rev (воспроизвёл архитектор 05.08).
    # Общее слово не должно означать конкретный размер.
    SYN = {"большой": "big", "малый": "small", "маленький": "small",
           "набор": "nabor", "кондуктор": "jig",
           "новый": "new", "старый": "old", "тест": "test", "печать": "print"}
    for ru, en in SYN.items():
        if any(w.startswith(ru[:5]) for w in words):
            words.append(en)
    # Размер сопла привязываем только к ЯВНО названной цифре: «0.6», «0,6»,
    # «06», «шестёрка» → в именах папок он записан как «06».
    low = query.lower()
    for num, forms in (("04", ("0.4", "0,4", " 04", "четвёрк", "четверк")),
                       ("06", ("0.6", "0,6", " 06", "шестёрк", "шестерк")),
                       ("08", ("0.8", "0,8", " 08", "восьмёрк", "восьмерк")),
                       ("02", ("0.2", "0,2", " 02", "двойк"))):
        if any(f in low for f in forms):
            words.append(num)
    words = list(dict.fromkeys(words))
    if not words:
        return []
    since = (time.time() - hours * 3600) if hours else 0
    rows = con.execute(
        "SELECT * FROM artifact WHERE mtime > ? ORDER BY mtime DESC LIMIT 40000",
        (since,)).fetchall()
    # 🔴 Размер сопла ищем в ИМЕНИ папки, а не во всём пути: обе папки лежат
    # внутри `p06`, поэтому запрос про 0.6 поднимал и big_04_mesh_rev
    # (воспроизвёл архитектор 05.08). Для файла именем считаем его
    # непосредственную родительскую папку — именно она названа по набору.
    nozzles = {w for w in words if w in ("02", "04", "06", "08")}
    plain = [w for w in words if w not in nozzles]

    now = time.time()
    out = []
    for r in rows:
        low = r["path"].lower()
        own = (r["name"] if r["kind"] == "папка"
               else _base(r["parent"] or "")).lower()
        if nozzles and not any(n in own for n in nozzles):
            continue          # спросили конкретное сопло — чужие наборы не нужны
        hit = sum(1 for w in plain if w in low) + sum(1 for n in nozzles if n in own)
        if not hit:
            continue
        age_d = (now - r["mtime"]) / 86400
        # свежесть решает: вопрос почти всегда про последнее состояние дела
        score = hit + (2.0 if age_d < 1 else 1.0 if age_d < 3 else 0.0)
        if r["kind"] == "папка":
            score += 0.5
        out.append((score, r))
    out.sort(key=lambda x: (-x[0], -x[1]["mtime"]))
    return [r for _, r in out[:k]]


def main():
    import argparse
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Индекс продуктов работы")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    r = sub.add_parser("recent"); r.add_argument("--hours", type=int, default=48)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--k", type=int, default=5)
    a = ap.parse_args()

    if a.cmd == "scan":
        scan()
    elif a.cmd == "recent":
        for x in recent(hours=a.hours):
            when = time.strftime("%d.%m %H:%M", time.localtime(x["mtime"]))
            print(f'{when}  {x["path"]}  ({x["files"]} файлов)')
    else:
        for x in search(a.query, k=a.k):
            when = time.strftime("%d.%m %H:%M", time.localtime(x["mtime"]))
            extra = f'{x["files"]} файлов' if x["kind"] == "папка" else f'{x["size"]} б'
            print(f'{when}  [{x["kind"]}] {x["path"]}  ({extra})')


if __name__ == "__main__":
    main()
