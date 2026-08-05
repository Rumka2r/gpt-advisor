#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Честный статус памяти: что сейчас НЕ работает и насколько свеж индекс.

Зачем. 05.08.2026 сервис эмбеддингов лежал, поиск молча откатился в запасной
режим и вернул пустоту. Пустота неотличима от честного «такого нет», и агент
ушёл искать руками на восемь минут. Правило, о котором договорились с
архитектором:

    «ничего не найдено» — настоящее отсутствие ТОЛЬКО при status=ok
    и свежем индексе. Во всех остальных случаях выдача обязана кричать.

Три состояния:
  ok      — все стадии отработали;
  partial — часть стадий отвалилась, выдача неполная;
  failed  — оба искателя (смысл и слова) лежат, выдача бессмысленна.

Стадии, помеченные CRITICAL, — это сами искатели. Остальное (выделение ответа,
слоты фактов) ухудшает выдачу, но не делает её ложной.
"""
import calendar
import json
import os
import pathlib
import sqlite3
import time

STATE = pathlib.Path.home() / '.claude' / 'continuity' / 'state'
HEALTH_FILE = STATE / 'health.json'

CRITICAL = {'смысловой поиск', 'точный поиск'}

# Порт эмбеддера настраивается переменной окружения — иначе «порт открыт, но
# сервис не отвечает» невозможно проверить честно, только подменой функций.
EMB_PORT = int(os.environ.get('MEM_EMB_PORT', '8899'))

# Насколько отставание индекса считаем незаметным. Больше — предупреждаем:
# свежая работа в выдачу ещё не попала, и пустота может быть об этом.
LAG_WARN_MIN = 20


class Health:
    """Собирает отказы за один прогон поиска."""

    def __init__(self):
        self.down = {}          # стадия -> тип исключения
        self.behind_files = None
        self.index_as_of = None
        self.lag_min = None

    def fail(self, stage, exc):
        self.down[stage] = type(exc).__name__ if isinstance(exc, BaseException) else str(exc)

    @property
    def status(self):
        if not self.down:
            return 'ok'
        if CRITICAL <= set(self.down):      # лежат ОБА искателя
            return 'failed'
        return 'partial'

    def watermark(self, con):
        """До какого момента история вообще попала в индекс."""
        try:
            row = con.execute("SELECT MAX(ts) FROM events").fetchone()
            self.index_as_of = row[0] if row else None
            if self.index_as_of:
                # 🔴 Отметки событий в UTC («…Z»), а time.mktime считает местным
                # временем — отставание выходило отрицательным (поймано 05.08).
                t = time.strptime(self.index_as_of[:19], '%Y-%m-%dT%H:%M:%S')
                epoch = calendar.timegm(t) if 'Z' in self.index_as_of else time.mktime(t)
                self.lag_min = int((time.time() - epoch) / 60)
        except Exception:
            pass
        return self.index_as_of

    def behind(self, con, since=None):
        """Сколько транскриптов на диске РАСХОДЯТСЯ с индексом.

        🔴 Это и есть честное отставание. Прежняя мерка — `MAX(ts)` из событий —
        врала в обе стороны (поймал архитектор 05.08): если Рувим два часа
        молчал, индекс объявлялся устаревшим, хотя разобрано всё; а если один
        файл проиндексирован, а другой изменён и пропущен — показывалось
        отставание ноль. Сравниваем размер и время каждого файла с тем, что
        записано в `sources`.
        """
        try:
            import catalog
            known = {r[0]: (r[1], r[2]) for r in
                     con.execute("SELECT src, mtime, size FROM sources")}
            # 🔴 Тот же перечислитель, что и у сборщика: свой обход пропускал
            # субагентов и не видел ИСЧЕЗНУВШИЕ файлы, о которых знает sources.
            srcs, roots_ok = catalog.enumerate_sources()
            if not roots_ok:
                # корень не прочитан — про свежесть НИЧЕГО не знаем
                self.behind_files = None
                return None
            on_disk = set()
            miss = []
            for _name, p, _sub in srcs:
                on_disk.add(p)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                # Файл, который дописали УЖЕ ПОСЛЕ досборки, не отставание,
                # а живая запись прямо сейчас — иначе предупреждение горит
                # всегда, пока идёт разговор.
                if since and st.st_mtime >= since:
                    continue
                row = known.get(p)
                if row is None or row[0] != st.st_mtime or row[1] != st.st_size:
                    miss.append(p)
            # 🔴 Исчезнувшие: индекс помнит файл, которого на диске уже нет.
            # Его события и эпизоды ещё находятся поиском — это тоже отставание.
            miss += [p for p in known if p not in on_disk]
            self.behind_files = miss
            return miss
        except Exception:
            self.behind_files = None
            return None

    def stale(self):
        """Индекс отстал, если хоть один транскрипт разошёлся с записью о нём."""
        if getattr(self, 'behind_files', None):
            return True
        # Пока сверку файлов не делали, судить не по чему — но и врать не будем.
        return False

    def banner(self, empty=False):
        """Первая строка выдачи. Пустая, когда всё в порядке и индекс свеж."""
        out = []
        if self.status != 'ok':
            out.append(f'🔴 ПАМЯТЬ РАБОТАЕТ ЧАСТИЧНО ({self.status}). '
                       f'Не отработало: {", ".join(sorted(self.down))}.')
            out.append('   Пустая или бедная выдача НЕ означает, что данных нет — '
                       'проверь другим способом, прежде чем говорить «не найдено».')
            if 'смысловой поиск' in self.down:
                out.append('   Поднять эмбеддер: '
                           r'Start-Process C:\Users\andri\.hermes\profiles\alex\emb\venv'
                           r'\Scripts\python.exe -ArgumentList ...\emb_server.py')
        if self.stale():
            n = len(self.behind_files or [])
            out.append(f'⚠ индекс отстал: {n} транскрипт(ов) на диске не совпадают '
                       f'с записями в индексе. Самой свежей работы в нём ещё нет.')
        # 🔴 «Этого действительно нет» — самое сильное утверждение, которое
        # система может сделать. Разрешено ТОЛЬКО когда всё известно точно:
        # статус ok, сверка ВЫПОЛНЕНА и расхождений ноль. Раньше несделанная
        # сверка (behind() вернул None) считалась «отставания нет», и баннер
        # уверенно врал (воспроизвёл архитектор 05.08).
        known = getattr(self, 'behind_files', None) is not None
        if empty:
            if self.status == 'ok' and known and not self.behind_files:
                out.append('(индекс свеж и все стадии отработали — '
                           'значит этого в истории действительно нет)')
            elif not known:
                out.append('⚠ состояние индекса НЕИЗВЕСТНО (сверка не выполнена) — '
                           'пустая выдача ничего не доказывает.')
        return '\n'.join(out)

    def as_dict(self):
        return {'status': self.status,
                'unavailable': sorted(self.down),
                'reasons': self.down,
                # last_event_at — просто «последнее событие в корпусе».
                # Отставанием его называть нельзя, см. behind().
                'last_event_at': self.index_as_of,
                'behind_files': len(getattr(self, 'behind_files', None) or []),
                'behind_known': getattr(self, 'behind_files', None) is not None}

    def save(self):
        """Пишем на диск, чтобы хук мог предупредить один раз за сессию."""
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            d = self.as_dict()
            d['checked_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
            HEALTH_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                                   encoding='utf-8')
        except Exception:
            pass    # здоровье не должно ронять того, чьё здоровье меряет


def last() -> dict:
    try:
        return json.loads(HEALTH_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {'status': 'unknown'}


def embedder_alive(timeout=1.0) -> bool:
    """Порт открыт. Быстрая проверка, но НЕ доказательство готовности."""
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(('127.0.0.1', EMB_PORT)) == 0
    finally:
        s.close()


def embedder_ready(timeout=20.0) -> bool:
    """Сервис реально ОТВЕЧАЕТ вектором.

    🔴 Открытый порт — не готовность: BGE-M3 грузится на GPU около минуты, и всё
    это время сокет уже слушает, а /embed ещё падает по таймауту (замечание
    архитектора 05.08). Проверяем настоящим запросом.
    """
    if not embedder_alive():
        return False
    import json as _j
    import urllib.request
    try:
        req = urllib.request.Request(
            f'http://127.0.0.1:{EMB_PORT}/embed',
            data=_j.dumps({'texts': ['проверка готовности']}).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = _j.loads(r.read())
        v = d[0] if isinstance(d, list) else (d.get('vectors') or d.get('embeddings') or [None])[0]
        return bool(v) and len(v) > 100
    except Exception:
        return False


def ensure_embedder(wait_s=0):
    """Поднять эмбеддер, если он лежит. Возвращает True, если сервис доступен.

    05.08 сервис жил только руками: автозапуска не было, и после перезагрузки
    смысловой поиск молча умирал. Теперь есть задача планировщика
    `MemoryEmbedServer` (вход в систему) и этот страховочный подъём — на случай,
    если процесс упал среди дня.
    """
    # 🔴 wait_s=0 обязано означать «НЕ ЖДАТЬ». Раньше функция всё равно
    # тратила до 3 с на проверку готовности и до 5 с в конце — то есть
    # «без ожидания» стоило почти десять секунд (замер архитектора 05.08).
    # 🔴 И второй процесс не запускаем, если порт уже открыт: там просто
    # грузится модель, а второй экземпляр займёт GPU и подерётся за порт.
    port_open = embedder_alive(timeout=0.4)
    if port_open:
        if wait_s <= 0:
            # порт есть, готовности пока нет — это «грузится», а не «лежит»
            return embedder_ready(timeout=1)
    else:
        emb = pathlib.Path.home() / '.hermes' / 'profiles' / 'alex' / 'emb'
        try:
            import subprocess
            subprocess.Popen(
                [str(emb / 'venv' / 'Scripts' / 'python.exe'), str(emb / 'emb_server.py')],
                cwd=str(emb), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                | getattr(subprocess, 'DETACHED_PROCESS', 0))
        except Exception:
            return False
        if wait_s <= 0:
            return False        # запустили и сразу вернулись, как и просили
    # ждём готовности уже существующего или только что запущенного процесса
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if embedder_ready(timeout=5):
            return True
        time.sleep(3)
    return embedder_ready(timeout=5)


def doctor(verbose=True):
    """Быстрая проверка всех опор памяти. Ненулевой код — что-то лежит."""
    import catalog
    bad = []
    print(f'— эмбеддер 127.0.0.1:{EMB_PORT}:', end=' ')
    if embedder_ready():
        print('отвечает вектором')
    elif embedder_alive():
        print('порт открыт, но НЕ ОТВЕЧАЕТ (грузит модель?)'); bad.append('эмбеддер не готов')
    else:
        print('ЛЕЖИТ'); bad.append('эмбеддер')

    con = catalog.db()
    h = Health()
    h.watermark(con)
    # 🔴 doctor СТРОГО read-only. Раньше он звал catalog.build() и выбрасывал
    # список изменившихся сессий: каталог обновлялся, а эпизоды и FTS — нет.
    # Следующий вызов получал пустой changed и уже ничего не перестраивал —
    # то есть сама диагностика создавала ровно ту дыру свежести, которую мы
    # чиним (воспроизвёл архитектор 05.08). Диагност не лечит.
    # Файлы, дописанные в последние 60 с, — идущие разговоры, не отставание.
    miss = h.behind(con, since=time.time() - 60)
    print(f'— последнее событие в корпусе: {h.index_as_of}')
    if miss is None:
        print('— сверка транскриптов: НЕ УДАЛАСЬ')
        bad.append('сверка транскриптов не удалась')
    else:
        print(f'— транскриптов расходится с индексом: {len(miss)}')
        for p in miss[:3]:
            print(f'    {p}')
        if miss:
            bad.append(f'индекс отстал на {len(miss)} файл(ов)')

    # 🔴 Проверяем все три вектора эпизода: раньше смотрели только vec_goal,
    # и doctor рапортовал «всё в порядке», пока воркер видел дыру по
    # vec_detail (замечание архитектора 05.08).
    for table, col, name in (
            ('episodes', 'vec_goal IS NULL OR vec_outcome IS NULL OR vec_detail IS NULL',
             'эпизоды'),
            ('mem_docs', 'vec IS NULL', 'куски памяти')):
        try:
            n = con.execute(f'SELECT COUNT(*) FROM {table} WHERE {col}').fetchone()[0]
            total = con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            print(f'— {name} без вектора: {n} из {total}')
            if n:
                bad.append(f'{name}: {n} без вектора')
        except Exception as e:
            print(f'— {name}: не проверить ({type(e).__name__})')

    # 🔴 Сироты проверяются ЗДЕСЬ. В отчёте 05.08 я написал «сирот 0», а doctor
    # их не считал — цифру я взял отдельным запросом. Утверждение должно
    # приходить оттуда, куда я на него ссылаюсь (замечание архитектора).
    orphans = {
        'ep_fact': "SELECT COUNT(*) FROM ep_fact WHERE episode_id NOT IN (SELECT id FROM episodes)",
        'ep_uuid': "SELECT COUNT(*) FROM ep_uuid WHERE episode_id NOT IN (SELECT id FROM episodes)",
        'thread_episode': "SELECT COUNT(*) FROM thread_episode "
                          "WHERE episode_id NOT IN (SELECT id FROM episodes)",
        'эпизоды без событий': "SELECT COUNT(*) FROM episodes "
                               "WHERE session NOT IN (SELECT DISTINCT session FROM events)",
    }
    for name, sql in orphans.items():
        try:
            n = con.execute(sql).fetchone()[0]
            print(f'— сироты {name}: {n}')
            if n:
                bad.append(f'сироты {name}: {n}')
        except Exception as e:
            print(f'— сироты {name}: не проверить ({type(e).__name__})')
            bad.append(f'сироты {name} не проверены')

    print('ИТОГ:', 'всё в порядке' if not bad else 'ПРОБЛЕМЫ — ' + '; '.join(bad))
    return 1 if bad else 0


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(doctor())
