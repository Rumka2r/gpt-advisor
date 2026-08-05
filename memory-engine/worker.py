# -*- coding: utf-8 -*-
"""Фоновый работник: суммаризирует сессии, пересобирает ленту, обновляет индекс поиска.
Запускается хуками отдельным процессом. Снимает лок в конце.
"""
import os, sys, argparse, traceback, datetime, time

BIN = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BIN)
sys.path.insert(0, BIN)
LOCK = os.path.join(ROOT, "state", ".worker.lock")


def log(msg):
    try:
        os.makedirs(os.path.join(ROOT, "state"), exist_ok=True)
        with open(os.path.join(ROOT, "state", "worker.log"), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def acquire_lock():
    """Захватить лок атомарно и вернуть свой токен (или None, если занято).

    🔴 Раньше лок ставил ТОЛЬКО хук, а worker в конце удалял файл независимо от
    того, владел ли им. Поиск, запускающий воркер напрямую, лок вообще не
    создавал — и мог снести чужой, после чего хуки поднимали третий экземпляр,
    и несколько процессов перестраивали одну SQLite-базу наперегонки
    (нашёл архитектор 05.08).

    Теперь владение проверяется по содержимому: O_CREAT|O_EXCL + свой PID,
    удаляем файл только если внутри наш. Мёртвого владельца вытесняем.
    """
    token = f"{os.getpid()}"
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    for attempt in (1, 2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(token)
            return token
        except FileExistsError:
            if attempt == 2:
                return None
            # владелец жив? если процесса нет или лок совсем старый — вытесняем
            try:
                owner = open(LOCK, encoding="utf-8").read().strip()
                age = time.time() - os.path.getmtime(LOCK)
            except OSError:
                return None
            alive = False
            if owner.isdigit():
                if int(owner) == os.getpid():
                    return None         # это мы сами и есть — лок уже наш
                try:
                    import subprocess
                    # 🔴 Без text=True: tasklist пишет в кодировке консоли
                    # (cp866), и разбор падал UnicodeDecodeError — проверка
                    # живости молча ломалась (поймано своим же тестом 05.08).
                    out = subprocess.run(["tasklist", "/FI", f"PID eq {owner}"],
                                         capture_output=True, timeout=10)
                    alive = owner.encode() in (out.stdout or b"")
                except Exception:
                    alive = True        # не смогли проверить — считаем живым
            # 🔴 Живого владельца НЕ вытесняем никогда, даже если лок висит
            # сутки: он может честно работать долго, а два воркера на одной базе
            # хуже задержки (замечание архитектора 05.08). Возраст — довод
            # только когда достоверно известно, что процесса уже нет.
            if alive:
                return None
            log(f"вытесняю лок мёртвого владельца {owner!r} (возраст {age:.0f} с)")
            try:
                os.remove(LOCK)
            except OSError:
                return None
    return None


def release_lock(token):
    """Снять лок, только если он НАШ."""
    try:
        if open(LOCK, encoding="utf-8").read().strip() == token:
            os.remove(LOCK)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--current", default=None, help="транскрипт завершившейся сессии")
    ap.add_argument("--skip-session", default=None, help="id активной сессии (не трогать)")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--limit", type=int, default=6)
    a = ap.parse_args()
    token = acquire_lock()
    if token is None:
        log("другой воркер уже работает — выхожу")
        return
    try:
        import summarize, now
        if a.current and os.path.exists(a.current):
            project = os.path.basename(os.path.dirname(a.current))
            try:
                d = summarize.summarize_one(a.current, project)
                log(f"current summarized -> {d}")
            except Exception as e:
                log(f"current FAILED {e!r}")
        # Эмбеддер — опора всего смыслового слоя. Если лежит, поднимаем сами:
        # 05.08 он умер незаметно, и поиск полдня работал вслепую.
        try:
            import health as _h
            # 🔴 Проверяем ГОТОВНОСТЬ, а не порт, и ждём 180 с: реальная
            # загрузка BGE-M3 занимала 155 с, прежних 90 не хватало.
            if not _h.embedder_ready(timeout=3):
                log("эмбеддер не отвечает — поднимаю")
                _h.ensure_embedder(wait_s=180)
                log(f"эмбеддер после подъёма: "
                    f"{'отвечает' if _h.embedder_ready(timeout=5) else 'НЕ ПОДНЯЛСЯ'}")
        except Exception as e:
            log(f"проверка эмбеддера пропущена: {e!r}")

        made = summarize.catchup(days=a.days, limit=a.limit, skip_session=a.skip_session)
        log(f"catchup: {len(made)}")
        n = now.build_recent()
        log(f"recent rebuilt: {n} sessions")
        # Старый индекс recall.sqlite больше не обслуживается: он держал вторую
        # конкурирующую систему поиска, из-за чего карта памяти советовала
        # сломанный вход (разбор 05.08). Единственный вход — memoryctl.

        # Исторический индекс: каталог → эпизоды → точный поиск.
        # Эпизоды пересобираем только для изменившихся сессий: полная
        # пересборка занимает больше двух минут и в фоновом проходе не нужна.
        try:
            import catalog, episodes, fts, threads, memdocs, entities, transcript as T
            changed = catalog.build(verbose=False, return_changed=True)
            con = catalog.db()
            # 🔴 Берём ОЧЕРЕДЬ, а не только свой результат: изменения мог уже
            # «съесть» интерактивный поиск, вызвав catalog.build() раньше нас —
            # тогда наш список пуст, а эпизоды так и не построены
            # (воспроизвёл архитектор 05.08).
            queued = set(catalog.take_dirty(con)) | set(changed)
            log(f"catalog: обновлено сессий {len(changed)}, в очереди {len(queued)}")
            n = 0
            if queued:
                episodes.ensure_tables(con)
                n = episodes.build(sessions=sorted(queued), verbose=False)

            # 🔴 Порядок важен: память индексируется ДО точного поиска.
            # Было наоборот, и новые файлы памяти попадали в mem_docs, но не в
            # mem_fts — до какого-нибудь будущего прохода, где изменится
            # транскрипт (нашёл GPT-архитектор 05.08).
            m = memdocs.build(con, verbose=False)
            if m == -1:
                # стор недоступен: индекс сохранён, но доверять ему нельзя
                log("ВНИМАНИЕ: стор памяти недоступен — индекс НЕ обновлялся")
                m = 0
            elif m:
                log(f"память: обновлено {m} кусков")

            # 🔴 Сирот убираем ДО построения нитей: иначе нити считаются по
            # эпизодам, часть которых сейчас исчезнет, и их показатели остаются
            # неверными (замечание архитектора 05.08).
            n_ep = n_hang = 0
            try:
                n_ep, n_hang = episodes.prune_orphans(con, verbose=False)
                if n_ep or n_hang:
                    log(f"сироты убраны: эпизодов {n_ep}, висячих ссылок {n_hang}")
            except Exception as e:
                log(f"чистка сирот пропущена: {e!r}")

            # 🔴 И после чистки сирот тоже: если prune что-то удалил при
            # changed=0 и m=0, старые записи FTS и показатели нитей остались бы
            # (замечание архитектора 05.08).
            if queued or m or n_ep or n_hang:
                fts.build(con, verbose=False)
                # Нити пересобираются целиком: это доли секунды, зато они не
                # разъезжаются с эпизодами после переиндексации сессии.
                t = threads.build(con, verbose=False)
                log(f"эпизоды: {n} по {len(queued)} сессиям; FTS обновлён; нитей {t}")
                # Снимаем с очереди ТОЛЬКО после успешных FTS и нитей.
                catalog.clear_dirty(con, sorted(queued))

            # Теневое извлечение фактов: кандидаты копятся сами, но в
            # действующие слоты ничего не попадает без моего подтверждения.
            try:
                import shadow
                n_cand = shadow.scan(catalog.db(), verbose=False,
                                     exclude_session=a.skip_session)
                if n_cand:
                    log(f"кандидатов в слоты: +{n_cand} (ждут проверки: shadow.py --report)")
            except Exception as e:
                log(f"теневое извлечение пропущено: {e!r}")

            # Продукты работы: какие файлы и папки появились. Разговоры
            # отвечают «что обсуждали», это — «что сделано и где лежит».
            # Без него вопрос «что ты вчера делал» не имел ответа (05.08).
            try:
                import artifacts
                res = artifacts.scan(con=catalog.db(), verbose=False)
                log(f"артефакты: {res['rows']} (статус {res['status']}, "
                    f"корней прочитано {res['read']}/{res['roots']})")
                if res["status"] == "failed_roots":
                    log("ВНИМАНИЕ: рабочие корни не прочитаны — "
                        "индекс артефактов мог устареть")
            except Exception as e:
                log(f"индекс артефактов пропущен: {e!r}")

            # Реестр сущностей: имена одной вещи + типизированные связи.
            # Полный граф отвергнут по замеру (0.2% запросов), это лёгкая замена.
            try:
                entities.seed(catalog.db(), verbose=False)
            except Exception as e:
                log(f"реестр сущностей пропущен: {e!r}")

            # Самопроверка: молчаливая деградация опаснее падения. Если эпизоды
            # остались без векторов (лежал эмбеддер) или каталог отстал от
            # транскриптов — это должно быть видно в логе, а не выясняться
            # через неделю по плохому поиску.
            con = catalog.db()
            # 🔴 Считаем дыры по ВСЕМ трём векторам эпизода и отдельно по кускам
            # памяти: раньше ремонт запускался только при пустом vec_goal, и если
            # терялись векторы одной лишь памяти, чинить никто не приходил.
            novec = con.execute(
                "SELECT COUNT(*) FROM episodes WHERE vec_goal IS NULL "
                "OR vec_outcome IS NULL OR vec_detail IS NULL").fetchone()[0]
            try:
                novec += con.execute(
                    "SELECT COUNT(*) FROM mem_docs WHERE vec IS NULL").fetchone()[0]
            except Exception:
                pass
            total_ep = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            files_seen = con.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            files_on_disk = sum(
                1 for d in T.project_dirs() for f in os.listdir(d) if f.endswith(".jsonl"))
            if novec:
                log(f"ВНИМАНИЕ: {novec} из {total_ep} эпизодов без векторов "
                    f"(эмбеддер лежал?) — поиск по ним не работает")
                # 🔴 Раньше здесь была только жалоба в лог, и дыра оставалась
                # НАВСЕГДА: файл с тех пор не менялся, значит сборщик считал его
                # обработанным и вектор не строил больше никогда (нашёл
                # GPT-архитектор 05.08; на тот момент так потерялись 85 эпизодов
                # и 339 кусков памяти). Теперь чиним, как только сервис ожил.
                try:
                    import repair_vectors
                    e_fix, m_fix = repair_vectors.repair(verbose=False)
                    if e_fix or m_fix:
                        log(f"векторы достроены: эпизодов {e_fix}, кусков памяти {m_fix}")
                        fts.build(con, verbose=False)
                except Exception as e:
                    log(f"ремонт векторов пропущен: {e!r}")
            if files_on_disk > files_seen:
                log(f"ВНИМАНИЕ: транскриптов на диске {files_on_disk}, "
                    f"в каталоге {files_seen} — часть не проиндексирована")
        except Exception as e:
            log(f"исторический индекс пропущен: {e!r}")
    except Exception:
        log("WORKER CRASH\n" + traceback.format_exc()[:2000])
    finally:
        release_lock(token)


if __name__ == "__main__":
    main()
