#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ЕДИНСТВЕННЫЙ публичный вход в память. Всё остальное — внутренности.

Зачем. 05.08.2026 карта памяти советовала `recall.py`, который к тому моменту
уже был заменён на `find.py`. Инструмент оказался сломан, выдал пустоту — и
восемь минут ушло на ручные раскопки. Причина не в поиске: команда была
записана строкой сразу в нескольких markdown-файлах и в `now.py`, и они
разъехались с кодом.

Лечение (совет архитектора 05.08): наружу торчит одна команда. Документация
ссылается только на неё и не знает имён внутренних модулей. Если вход
переедет — менять одно место, вот это.

    memoryctl.py search "вопрос"   — искать в истории и памяти
    memoryctl.py doctor            — что сейчас сломано, свеж ли индекс
    memoryctl.py recent            — чем занимались недавно, открытые нити
    memoryctl.py claim ...         — слоты действующих фактов
    memoryctl.py thread ...        — нити работ

Код возврата search: 0 — всё отработало; 2 — память работает частично
(пустая выдача НЕ означает отсутствия); 3 — оба искателя лежат.
"""
import os
import pathlib
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 🔴 Единственный источник правды о том, как звать память. Любой текст,
# который учит пользоваться памятью, обязан брать строку ОТСЮДА, а не
# хранить свою копию (иначе повторится расхождение 05.08).
CMD = 'python ~/.claude/continuity/bin/memoryctl.py'
CMD_SEARCH = f'{CMD} search "вопрос"'
CMD_DOCTOR = f'{CMD} doctor'
CMD_RECENT = f'{CMD} recent'

STATE = pathlib.Path.home() / '.claude' / 'continuity' / 'state'

# Сколько всего секунд отпущено интерактивному поиску. Больше — человек ждёт,
# а он спрашивает, чтобы НЕ ждать. Тяжёлое доделывает фоновый воркер.
SEARCH_BUDGET_S = 10.0


def _spawn_worker():
    """Отдать тяжёлую пересборку фоновому воркеру и НЕ ждать его.

    Ровно то, чего требует бюджет: человек получает ответ по имеющемуся
    индексу сейчас, а эпизоды и FTS догоняются отдельно.
    """
    try:
        import subprocess
        subprocess.Popen(
            [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          'worker.py')],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            | getattr(subprocess, 'DETACHED_PROCESS', 0))
    except Exception:
        pass


def cmd_search(argv):
    import argparse
    import catalog
    import find
    import health as health_mod

    ap = argparse.ArgumentParser(prog='memoryctl search')
    ap.add_argument('query')
    ap.add_argument('--k', type=int, default=6)
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--exclude', help='не искать в этой сессии (обычно — текущей)')
    ap.add_argument('--no-refresh', action='store_true',
                    help='не догонять индекс перед поиском (для замеров)')
    # Разграничение инстансов (06.08): по умолчанию scope=mine — свои сессии,
    # общий канон памяти и legacy (владелец неизвестен, помечаются). Чужие
    # агенты — только явно: --scope all или --agent <имя>, каждая находка
    # приходит с [автор: …].
    ap.add_argument('--scope', choices=['mine', 'all'], default='mine')
    ap.add_argument('--agent', help='явный cross-agent запрос: только сессии этого агента')
    ap.add_argument('--include-legacy', dest='legacy', action='store_true', default=True)
    ap.add_argument('--no-legacy', dest='legacy', action='store_false',
                    help='скрыть сессии с неизвестным владельцем')
    a = ap.parse_args(argv)

    import time as _t0
    t_start = _t0.time()
    h = health_mod.Health()
    con = catalog.db()

    # 🔴 Догоняем ВСЕГДА, без условия по времени. Проход без изменений стоит
    # 0.02 с, а прежнее условие опиралось на возраст последней реплики — мерку,
    # которая врала в обе стороны (разбор архитектора 05.08).
    refreshed_at = None
    if not a.no_refresh:
        try:
            import time as _t
            # 🔴 Интерактивный поиск НИКОГДА не перестраивает эпизоды/FTS/нити
            # синхронно. Раньше эта пересборка шла ДО потока с дедлайном, и
            # зависший embed() держал человека сколько угодно — лимит её не
            # останавливал (воспроизвёл архитектор 05.08: 12 секунд ожидания,
            # код 0 и наглое «этого в истории действительно нет»).
            # Дочитывание каталога дешёвое (сотые доли секунды) — его делаем,
            # а тяжёлое отдаём воркеру и честно помечаем partial.
            # 🔴 Даже дочитывание каталога идёт ПОД дедлайном. На тёплом
            # индексе это сотые доли секунды, но на холодном (или когда файлов
            # прибавилось разом) — минуты, и бюджет обходился стороной, потому
            # что этот вызов стоял ДО потока с лимитом.
            import threading as _th
            _box = {}

            def _refresh():
                try:
                    _box['changed'] = catalog.build(verbose=False, return_changed=True)
                except Exception as e:
                    _box['err'] = e

            _rt = _th.Thread(target=_refresh, daemon=True)
            _rt.start()
            _rt.join(min(3.0, max(0.5, SEARCH_BUDGET_S - (_t.time() - t_start))))
            if _rt.is_alive():
                h.fail('досборка индекса', 'не уложилась в бюджет, отдана воркеру')
                _spawn_worker()
            elif 'err' in _box:
                h.fail('досборка индекса', _box['err'])
            else:
                changed = _box.get('changed') or []
                refreshed_at = _t.time()
                # 🔴 Смотрим не только СВОИ изменения, но и хвост очереди от
                # прошлого неудачного воркера: устойчивая очередь сама по себе
                # не гарантирует, что кто-то её разберёт (замечание архитектора).
                pending = []
                try:
                    pending = catalog.take_dirty(con)
                except Exception:
                    pass
                if pending:
                    h.fail('досборка индекса',
                           f'{len(pending)} сессий ждут воркера, ищу по прежнему индексу')
                    _spawn_worker()
                    print(f'(в очереди {len(pending)} сессий — запустил воркера)',
                          file=sys.stderr)
        except Exception as e:
            h.fail('досборка индекса', e)

    # 🔴 Интерактивно диск НЕ обходим ВООБЩЕ. os.walk сначала перечисляет все
    # 404 тысячи файлов свалки ferguson_probe и только потом видит порог
    # BIG_DIR — это секунды, и они шли МИМО бюджета, потому что вызов стоял до
    # потока с лимитом (архитектор замерил 12 секунд подменой обхода).
    # Отметка устарела → отдаём воркеру и честно помечаем partial.
    if not a.no_refresh:
        try:
            import artifacts
            age = None
            try:
                age = _t0.time() - artifacts.SCAN_STAMP.stat().st_mtime
            except OSError:
                age = None
            if age is None or age > 300:
                h.fail('индекс артефактов',
                       'обход отдан воркеру, показываю прежний снимок')
                _spawn_worker()
        except Exception as e:
            h.fail('индекс артефактов', e)

    # 🔴 Готовность эмбеддера проверяем КОРОТКО (2 с) и до поиска. Иначе запрос
    # к неотвечающему сервису висел до HTTP-таймаута в минуту — а человек ждёт
    # ответа сейчас (замечание архитектора 05.08). Не ответил — работаем
    # словами и честно говорим partial.
    semantic = health_mod.embedder_ready(timeout=2)
    if not semantic:
        h.fail('смысловой поиск', 'эмбеддер не ответил за 2 с')
        # подъём запускаем, но НЕ ждём: он нужен следующему запросу, не этому
        try:
            health_mod.ensure_embedder(wait_s=0)
        except Exception:
            pass

    # 🔴 ЖЁСТКИЙ бюджет на интерактивный поиск. Проверка готовности эмбеддера
    # не спасает: сервис может зависнуть уже ПОСЛЕ неё, а embed() внутри ждёт
    # до 60 с — и восемь минут ожидания теоретически повторились бы при другом
    # отказе (замечание архитектора 05.08). Не успели — отдаём то, что есть,
    # и честно помечаем partial; тяжёлую досборку доделает воркер.
    import threading
    import time as _t
    # Остаток именно ОСТАТОК: прежний max(2.0, …) добавлял две секунды
    # уже сверх исчерпанного бюджета (замечание архитектора 05.08).
    left = max(0.1, SEARCH_BUDGET_S - (_t.time() - t_start))
    box = {}

    def _run():
        # 🔴 Своё соединение: объект SQLite принадлежит потоку, в котором создан,
        # и передача его сюда даёт ProgrammingError на КАЖДОЙ стадии — поиск
        # молча превращается в пустоту (поймал себя на этом сразу после правки).
        try:
            box['hits'] = find.search(a.query, a.k, con=catalog.db(),
                                      exclude_session=a.exclude,
                                      want_semantic=semantic, health=h)
        except Exception as e:
            h.fail('поиск', e)

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(left)
    if th.is_alive():
        h.fail('поиск', f'не уложился в {SEARCH_BUDGET_S:.0f} с')
        hits = []
        # запасной ответ: продукты работы — самое дешёвое и часто самое нужное
        try:
            import artifacts
            import time as _tt
            for r in artifacts.search(a.query, k=4, con=catalog.db()):
                hits.append({
                    'kind': 'артефакт', 'score': 0.9, 'sources': ['файлы'], 'both': False,
                    'title': r['path'],
                    'when': _tt.strftime('%d.%m %H:%M', _tt.localtime(r['mtime'])),
                    'session': '', 'project': r['project'] or '', 'facts': '',
                    'evidence': [], 'outcome': (f'папка, {r["files"]} файлов'
                                                if r['kind'] == 'папка' else 'файл'),
                    'anchor': r['path'], 'frag': None, 'thread': None, 'newer': None,
                })
        except Exception as e:
            h.fail('индекс артефактов', e)
    else:
        hits = box.get('hits', [])

    # Фильтр принадлежности — ПОСЛЕ поиска, сам поиск не переделан.
    dropped = 0
    try:
        import continuity_identity as ident
        hits, dropped = ident.apply_scope(hits, scope=a.scope, agent=a.agent,
                                          include_legacy=a.legacy)
        for hh in hits:
            if hh.get('owner_note'):
                hh['title'] = f"{hh['owner_note']} {hh['title']}"
    except Exception as e:
        h.fail('фильтр принадлежности', e)
    h.watermark(con)
    # Сверку файлов делаем ПОСЛЕ досборки: до неё активная сессия всегда
    # расходится с индексом просто потому, что пишется прямо сейчас, и
    # предупреждение горело бы всегда. Если файл отстал ПОСЛЕ досборки —
    # это уже настоящая проблема, о ней и говорим.
    # Сверка свежести тоже внутри бюджета: она быстрая (stat по файлам), но
    # «внутри бюджета» должно значить ВЕСЬ вызов, без исключений.
    if _t.time() - t_start > SEARCH_BUDGET_S:
        h.fail('сверка свежести', 'бюджет исчерпан, состояние индекса неизвестно')
    elif h.behind(con, since=refreshed_at) is None:
        # Не смогли сверить — значит про свежесть НИЧЕГО не знаем.
        # Молчать об этом нельзя: пустая выдача станет ложным «нет».
        h.fail('сверка свежести', 'состояние индекса неизвестно')
    h.save()

    if a.json:
        import json
        print(json.dumps({**h.as_dict(), 'results': hits}, ensure_ascii=False, indent=1))
    else:
        banner = h.banner(empty=not hits)
        if banner:
            print(banner)
        if dropped:
            print(f'(скрыто {dropped} находок других агентов — '
                  f'смотреть явно: --scope all или --agent <имя>)')
        print(find.render(hits))
    return {'ok': 0, 'partial': 2, 'failed': 3}[h.status]


def cmd_doctor(argv):
    import health as health_mod
    return health_mod.doctor()


def cmd_recent(argv):
    n = int(argv[0]) if argv and argv[0].isdigit() else 40
    th = STATE / 'THREADS.md'
    rc = STATE / 'RECENT.md'
    if th.exists():
        print('=== открытые нити ===')
        lines = [x for x in th.read_text(encoding='utf-8').splitlines() if x.strip()]
        print('\n'.join(lines[:25]))
    if rc.exists():
        print(f'\n=== лента последних событий (хвост {n} строк) ===')
        print('\n'.join(rc.read_text(encoding='utf-8').splitlines()[-n:]))
    # Продукты работы — то, что чаще всего и спрашивают под «что делал вчера».
    try:
        import artifacts
        import time as _t
        rows = artifacts.recent(hours=48, limit=12)
        if rows:
            print('\n=== что появилось и менялось за 48 часов ===')
            for r in rows:
                when = _t.strftime('%d.%m %H:%M', _t.localtime(r['mtime']))
                print(f'{when}  {r["path"]}  ({r["files"]} файлов)')
    except Exception as e:
        print(f'[индекс артефактов недоступен: {type(e).__name__}]')

    # свежесть индекса важна и здесь: «недавнее» может быть ещё не разобрано
    try:
        import catalog
        import health as health_mod
        h = health_mod.Health()
        miss = h.behind(catalog.db())
        if miss:
            print(f'\n⚠ индекс отстал: {len(miss)} транскрипт(ов) ещё не разобраны — '
                  f'самого свежего тут может не быть')
    except Exception:
        pass
    return 0


def delegate(module, argv):
    """Прокинуть во внутренний модуль, не заставляя знать его имя снаружи."""
    sys.argv = [module] + argv
    runpy.run_module(module, run_name='__main__')
    return 0


USAGE = __doc__


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help', 'help'):
        print(USAGE)
        return 0
    cmd, argv = sys.argv[1], sys.argv[2:]
    if cmd == 'search':
        return cmd_search(argv)
    if cmd == 'doctor':
        return cmd_doctor(argv)
    if cmd == 'recent':
        return cmd_recent(argv)
    if cmd == 'claim':
        return delegate('claims', argv)
    if cmd == 'thread':
        return delegate('threads', argv)
    if cmd == 'window':
        return delegate('window', argv)
    print(f'неизвестная команда: {cmd}\n\n{USAGE}', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
