#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Регрессии сборщика: по одной на каждый дефект, который нашёл архитектор.
Всё в отдельной песочнице — настоящие каталоги, настоящий git, но НИ ОДНОГО
пути рабочей системы: модуль переопределяется целиком перед запуском.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time

N = [0, 0]


def check(name, cond, detail=""):
    N[0] += 1
    if cond:
        N[1] += 1
        print(f"  ✔ {name}")
    else:
        print(f"  ✘ {name}   {detail}")


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def load(sandbox):
    """Загрузить сборщик и увести ВСЕ его пути в песочницу."""
    # 🔴 Проверяем ИМЕННО тот файл, что лежит рядом с тестом. Раньше по
    # умолчанию грузилась установленная копия из /opt — и зелёный результат
    # ничего не говорил о коде в репозитории.
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gc.py")
    src = os.environ.get("GC_SRC", here)
    assert os.path.abspath(src) == os.path.abspath(here) or "GC_SRC" in os.environ, \
        "проверяется не соседний gc.py"
    print(f"проверяется файл: {src}")
    spec = importlib.util.spec_from_file_location("gcmod", src)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.ROOT = os.path.join(sandbox, "control")
    os.makedirs(m.ROOT, exist_ok=True)
    m.DB = os.path.join(m.ROOT, "gc.db")
    m.LOCKFILE = os.path.join(m.ROOT, "gc.lock")
    m.STATE_DIR = os.path.join(m.ROOT, "state")
    m.HOLD_FILE = os.path.join(m.ROOT, "hold.txt")
    m.QUARANTINE = os.path.join(sandbox, "quarantine")
    m.STORE = os.path.join(sandbox, "store.git")
    m.AGENTS_DIR = os.path.join(sandbox, "agents")
    m.ALLOWED_ROOTS = (os.path.join(sandbox, "work"),
                       os.path.join(sandbox, "tmp"))
    m.BASE_ROOTS = [(os.path.join(sandbox, "work"), "worktree"),
                    (os.path.join(sandbox, "tmp"), "tmp_generic")]
    m.NEVER = (m.QUARANTINE, m.STORE)
    # песочница физически лежит в /tmp, но правило «общий /tmp только показываем»
    # к ней не относится: иначе тесты проверяли бы отказ, а не работу
    m.REPORT_ONLY = ("/несуществующий-корень",)
    # координатор в тестах подменяем: настоящий трогать нельзя
    m._cp_up = True
    m._holds = set()
    m._fencing_ok = True

    def fake_cp(path, **p):
        if not m._cp_up:
            return None
        if path == "/gc/claim":
            return {"ok": True, "lease_token": "t",
                    "fencing": {p.get("resource"): 1}}
        if path == "/holds":
            return {"ok": True,
                    "удержания": [{"resource": "worktree:" + h} for h in m._holds]}
        if path == "/check":
            return ({"allow": True} if m._fencing_ok
                    else {"allow": False, "причина": "поколение устарело"})
        if path == "/heartbeat":
            return {"ok": True} if m._fencing_ok else {"ok": False,
                                                       "причина": "аренда истекла"}
        return {"ok": True}

    m.cp = fake_cp
    return m


def make_repo(sandbox, name, dirty=False):
    """Настоящий bare-архив + личный репозиторий исполнителя поверх него через
    alternates + рабочая копия. Так же, как на сервере: архив только для чтения,
    пишет исполнитель к себе. Раньше тест использовал один репозиторий и в роли
    архива, и в роли личного — и не воспроизводил главного разделения."""
    store = os.path.join(sandbox, "store.git")
    if not os.path.exists(store):
        sh("git", "init", "--bare", "-q", store)
        seed = os.path.join(sandbox, "seed")
        sh("git", "init", "-q", "-b", "main", seed)
        open(os.path.join(seed, "f.txt"), "w").write("исходный\n")
        sh("git", "add", ".", cwd=seed)
        sh("git", "-c", "user.email=t@t", "-c", "user.name=t",
           "commit", "-qm", "первый", cwd=seed)
        sh("git", "push", "-q", store, "main", cwd=seed)
        sh("git", "config", "gc.auto", "0", cwd=store)
        sh("git", "config", "gc.pruneExpire", "never", cwd=store)
    wt = os.path.join(sandbox, "work", name)
    # ветка должна быть уникальной: копию с тем же именем создаём повторно,
    # когда проверяем занятость пути
    br = "b/" + name
    n = 1
    while sh("git", "-C", store, "rev-parse", "--verify", "--quiet", br).returncode == 0:
        n += 1
        br = f"b/{name}-{n}"
    r = sh("git", "-C", store, "worktree", "add", "-q", "-b", br, wt, "main")
    if r.returncode != 0:
        raise RuntimeError("не удалось создать копию: " + r.stderr[:200])
    if dirty:
        open(os.path.join(wt, "мусор.txt"), "w").write("не закоммичено\n")
    return wt


def backdate(m, con, uid, hours):
    """Состарить момент перехода: выдержка считается от него, а не от файлов."""
    con.execute("UPDATE resources SET state_since=? WHERE uuid=?",
                (int(time.time()) - int(hours * 3600), uid))
    con.commit()


def age(path, hours):
    t = time.time() - hours * 3600
    for dirpath, dirs, files in os.walk(path):
        for n in files + dirs:
            p = os.path.join(dirpath, n)
            try:
                os.utime(p, (t, t), follow_symlinks=False)
            except OSError:
                pass
    os.utime(path, (t, t))


def main():
    sandbox = tempfile.mkdtemp(prefix="gc-test-")
    try:
        os.makedirs(os.path.join(sandbox, "work"))
        os.makedirs(os.path.join(sandbox, "tmp"))
        m = load(sandbox)
        # 🔴 Глобальный конфиг не трогаем: тест не имеет права навсегда
        # разрешать «доверять любому репозиторию» на машине. Настройка живёт
        # только внутри песочницы.
        os.environ["GIT_CONFIG_GLOBAL"] = os.path.join(sandbox, "gitconfig")
        sh("git", "config", "--global", "--add", "safe.directory", "*")

        print("1. Режим показа ничего не меняет")
        wt = make_repo(sandbox, "старая")
        age(wt, 400)
        con = m.db()
        m.scan(con)
        refs_before = sh("git", "-C", m.STORE, "for-each-ref").stdout
        d = m.disk_state()
        m.advance(con, d, False, dict(roots_ok=True, failed=[], read=2))
        refs_after = sh("git", "-C", m.STORE, "for-each-ref").stdout
        check("🔴 показ не создаёт спасательных ссылок", refs_before == refs_after,
              "архив изменился в режиме показа")
        check("показ не переносит файлы", os.path.exists(wt))

        print("\n2. Грязная копия не удаляется")
        dirty = make_repo(sandbox, "грязная", dirty=True)
        ok, why = m.validate_worktree(dirty)
        check("копия с незакоммиченным отклонена", not ok, why)

        print("\n3. Карантин не превращается в DELETED на следующем обходе")
        con = m.db()
        m.scan(con)
        uid = con.execute("SELECT uuid FROM resources WHERE path=?", (wt,)).fetchone()[0]
        m.to_state(con, uid, "EXPIRED", "для проверки")
        backdate(m, con, uid, 100)
        ok, res = m.advance(con, m.disk_state(), True,
                            dict(roots_ok=True, failed=[], read=2)), None
        st = con.execute("SELECT state, quarantine_path FROM resources WHERE uuid=?",
                         (uid,)).fetchone()
        check("копия ушла в карантин", st[0] == "QUARANTINED", st)
        check("карантинный путь по идентификатору",
              bool(st[1]) and st[1].endswith(uid), st[1])
        m.scan(con)
        st2 = con.execute("SELECT state FROM resources WHERE uuid=?", (uid,)).fetchone()
        check("🔴 после обхода состояние сохранено", st2[0] == "QUARANTINED", st2)

        print("\n4. Время спасения берётся из базы, а не из даты коммита")
        row = con.execute("SELECT ref, rescued_at FROM rescue").fetchone()
        check("спасение записано в базу", row is not None)
        if row:
            check("время спасения — сейчас, а не дата коммита",
                  abs(row[1] - int(time.time())) < 120, row)

        print("\n5. Недоступный корень не значит «всё исчезло»")
        con2 = m.db()
        saved = m.BASE_ROOTS
        m.BASE_ROOTS = [(os.path.join(sandbox, "нет-такого"), "worktree")] + saved
        res = m.scan(con2)
        check("обход честно сообщает о непрочитанном корне", not res["roots_ok"], res)
        alive = con2.execute("SELECT COUNT(*) FROM resources WHERE live=1").fetchone()[0]
        check("ресурсы не помечены исчезнувшими", alive > 0, alive)
        out = m.advance(con2, m.disk_state(), True, res)
        check("🔴 при непрочитанном корне удаление запрещено",
              any("корни не прочитаны" in str(x[2]) for x in out), out)
        m.BASE_ROOTS = saved

        print("\n6. Недоступный координатор запрещает всё")
        m._cp_up = False
        out = m.advance(con2, m.disk_state(), True, dict(roots_ok=True, failed=[], read=2))
        check("🔴 без координатора удаление запрещено",
              any("координатор недоступен" in str(x[2]) for x in out), out)
        out = m.rescue_cleanup(con2, True)
        check("🔴 без координатора спасённое не чистится",
              any("координатор недоступен" in str(x[2]) for x in out), out)
        m._cp_up = True

        print("\n7. Удержание останавливает удаление")
        wt2 = make_repo(sandbox, "удержанная")
        age(wt2, 400)
        con3 = m.db()
        m.scan(con3)
        uid2 = con3.execute("SELECT uuid FROM resources WHERE path=?", (wt2,)).fetchone()[0]
        m.to_state(con3, uid2, "EXPIRED", "для проверки")
        backdate(m, con3, uid2, 100)
        m._holds = {uid2}
        m.advance(con3, m.disk_state(), True, dict(roots_ok=True, failed=[], read=2))
        st = con3.execute("SELECT state, reason FROM resources WHERE uuid=?",
                          (uid2,)).fetchone()
        check("удержанная копия осталась активной", st[0] == "ACTIVE", st)
        check("причина названа", "удержание" in (st[1] or ""), st)
        m._holds = set()

        print("\n8. Восстановление после падения посреди переноса")
        con4 = m.db()
        uid3 = con4.execute("SELECT uuid FROM resources WHERE path=?", (wt2,)).fetchone()[0]
        m.to_state(con4, uid3, "QUARANTINING", "имитация падения",
                   intended_path=m.qpath_for(uid3))
        fixed = m.recover(con4)
        st = con4.execute("SELECT state FROM resources WHERE uuid=?", (uid3,)).fetchone()
        check("🔴 зависший перенос разобран", st[0] == "ACTIVE", st)
        check("восстановление названо", any("не состоялся" in f[2] for f in fixed), fixed)

        print("\n9. Повторное использование пути заводит новый экземпляр")
        con5 = m.db()
        p = os.path.join(sandbox, "tmp", "повтор")
        os.makedirs(p, exist_ok=True)
        open(os.path.join(p, "a"), "w").write("x")
        age(p, 400)
        m.scan(con5)
        u1 = con5.execute("SELECT uuid FROM resources WHERE path=? AND live=1",
                          (p,)).fetchone()[0]
        m.to_state(con5, u1, "DELETED", "имитация", live=0)
        shutil.rmtree(p)
        os.makedirs(p)
        open(os.path.join(p, "b"), "w").write("y")
        age(p, 400)
        m.scan(con5)
        u2 = con5.execute("SELECT uuid FROM resources WHERE path=? AND live=1",
                          (p,)).fetchone()
        check("🔴 новый ресурс по тому же пути замечен", u2 is not None and u2[0] != u1,
              f"{u1} → {u2}")

        print("\n10. Обычный файл удаляется, а не rmtree")
        con6 = m.db()
        f = os.path.join(sandbox, "tmp", "файл.log")
        open(f, "w").write("мусор")
        age(f, 400)
        m.scan(con6)
        uf = con6.execute("SELECT uuid FROM resources WHERE path=? AND live=1",
                          (f,)).fetchone()
        check("файл попал под учёт", uf is not None)
        if uf:
            m.to_state(con6, uf[0], "EXPIRED", "для проверки")
            backdate(m, con6, uf[0], 100)
            m.advance(con6, m.disk_state(), True, dict(roots_ok=True, failed=[], read=2))
            st = con6.execute("SELECT state, quarantine_path FROM resources WHERE uuid=?",
                              (uf[0],)).fetchone()
            check("файл перенесён в карантин", st[0] == "QUARANTINED", st)
            backdate(m, con6, uf[0], 1000)
            m.advance(con6, m.disk_state(), True, dict(roots_ok=True, failed=[], read=2))
            st = con6.execute("SELECT state FROM resources WHERE uuid=?",
                              (uf[0],)).fetchone()
            check("файл удалён корректно", st[0] == "DELETED", st)

        print("\n11. Минимальная выдержка не обнуляется при переполнении диска")
        con7 = m.db()
        wt3 = make_repo(sandbox, "срочная")
        age(wt3, 400)
        m.scan(con7)
        u = con7.execute("SELECT uuid FROM resources WHERE path=?", (wt3,)).fetchone()[0]
        m.to_state(con7, u, "EXPIRED", "только что просрочена")
        d = m.disk_state(); d["purge_now"] = True; d["level"] = "emergency"
        m.advance(con7, d, True, dict(roots_ok=True, failed=[], read=2))
        st = con7.execute("SELECT state FROM resources WHERE uuid=?", (u,)).fetchone()
        check("🔴 при 95% свежая просрочка НЕ удаляется мгновенно",
              st[0] == "EXPIRED", st)

        print("\n12. Архив с alternates не годится для спасения")
        alt = os.path.join(m.STORE, "objects", "info", "alternates")
        os.makedirs(os.path.dirname(alt), exist_ok=True)
        open(alt, "w").write("/куда-то\n")
        ok, why = m.store_sane()
        check("🔴 архив со ссылкой наружу отвергнут", not ok, why)
        os.unlink(alt)
        ok, why = m.store_sane()
        check("нормальный архив принят", ok, why)

        print("")
        print("13. Повторный обход не сбрасывает возраст просрочки")
        con8 = m.db()
        wt4 = make_repo(sandbox, "возраст")
        age(wt4, 400)
        m.scan(con8)
        u4 = con8.execute("SELECT uuid FROM resources WHERE path=?", (wt4,)).fetchone()[0]
        m.to_state(con8, u4, "EXPIRED", "просрочена")
        backdate(m, con8, u4, 50)
        was = con8.execute("SELECT state_since FROM resources WHERE uuid=?", (u4,)).fetchone()[0]
        m.scan(con8)
        became = con8.execute("SELECT state_since FROM resources WHERE uuid=?", (u4,)).fetchone()[0]
        check("🔴 возраст просрочки не обнулился обходом", was == became,
              f"{was} -> {became}: ресурс не дозреет до карантина никогда")

        print("")
        print("14. Потеря поколения во время проверки останавливает шаг")
        con9 = m.db()
        wt5 = make_repo(sandbox, "потеря")
        age(wt5, 400)
        m.scan(con9)
        u5 = con9.execute("SELECT uuid FROM resources WHERE path=?", (wt5,)).fetchone()[0]
        m.to_state(con9, u5, "EXPIRED", "просрочена")
        backdate(m, con9, u5, 100)
        m._fencing_ok = False
        m.advance(con9, m.disk_state(), True, dict(roots_ok=True, failed=[], read=2))
        st = con9.execute("SELECT state, reason FROM resources WHERE uuid=?", (u5,)).fetchone()
        check("🔴 при потере права перенос не выполнен", st[0] == "ACTIVE", st)
        check("копия на месте", os.path.exists(wt5))
        m._fencing_ok = True

        print("")
        print("15. Пока старая копия в карантине, путь свободен для новой")
        con10 = m.db()
        # после проверки 14 копия вернулась в ACTIVE — снова просрочим её
        m.to_state(con10, u5, "EXPIRED", "просрочена повторно")
        backdate(m, con10, u5, 100)
        m.advance(con10, m.disk_state(), True, dict(roots_ok=True, failed=[], read=2))
        st = con10.execute("SELECT state FROM resources WHERE uuid=?", (u5,)).fetchone()
        if st[0] == "QUARANTINED":
            make_repo(sandbox, "потеря")
            m.scan(con10)
            rows = con10.execute("SELECT uuid, state FROM resources WHERE path=? AND live=1",
                                 (wt5,)).fetchall()
            fresh = [r for r in rows if r[1] in ("ACTIVE", "EXPIRED")]
            check("🔴 новая копия на том же пути замечена", len(fresh) == 1, rows)
        else:
            check("новая копия на том же пути замечена", False, f"не ушла в карантин: {st}")

        print("")
        print("16. Живой процесс в карантине запрещает удаление")
        con11 = m.db()
        row = con11.execute("SELECT uuid, quarantine_path FROM resources "
                            "WHERE state='QUARANTINED' AND live=1").fetchone()
        if row:
            uq, qp = row
            backdate(m, con11, uq, 10000)
            real_held = m.held_paths
            m.held_paths = lambda: {os.path.join(qp, "чтото")}
            m.advance(con11, m.disk_state(), True, dict(roots_ok=True, failed=[], read=2))
            st = con11.execute("SELECT state, reason FROM resources WHERE uuid=?", (uq,)).fetchone()
            m.held_paths = real_held
            check("🔴 живой процесс в карантине остановил удаление",
                  st[0] == "QUARANTINED", st)
            check("путь на месте", os.path.exists(qp))
        else:
            check("живой процесс в карантине остановил удаление", False, "нет карантина")

        print("")
        print("17. Удержание, появившееся позже, спасает ссылку")
        con12 = m.db()
        con12.execute("INSERT OR REPLACE INTO rescue VALUES(?,?,?,?)",
                      ("refs/rescue/gc/держим/старая", "держим", "0" * 40,
                       int(time.time()) - 40 * 86400))
        con12.commit()
        m._holds = {"держим"}
        gone = m.rescue_cleanup(con12, True)
        check("🔴 удержанная ссылка не удалена",
              not any("держим" in str(g[1]) for g in gone), gone)
        m._holds = set()

        print("")
        print("18. Осиротевшая ссылка находится и не удаляется молча")
        con13 = m.db()
        sh("git", "-C", m.STORE, "update-ref", "refs/rescue/gc/сирота/1", "main")
        orph = m.rescue_reconcile(con13)
        check("🔴 сирота найдена", any("сирота" in o for o in orph), orph)
        row = con13.execute("SELECT rescued_at FROM rescue WHERE ref LIKE ?",
                            ("%сирота%",)).fetchone()
        check("у сироты дата неизвестна", row is not None and row[0] == 0, row)
        gone = m.rescue_cleanup(con13, True)
        check("сирота не удалена автоматически",
              not any("сирота" in str(g[1]) for g in gone), gone)

        print("")
        print("19. Личные tmp и cache исполнителя вообще сканируются")
        zone = os.path.join(m.AGENTS_DIR, "exec9")
        os.makedirs(os.path.join(zone, "tmp"), exist_ok=True)
        os.makedirs(os.path.join(zone, "cache"), exist_ok=True)
        open(os.path.join(zone, "agent.env"), "w").write("AGENT_ID=exec9\n")
        junk = os.path.join(zone, "tmp", "хлам")
        os.makedirs(junk, exist_ok=True)
        open(os.path.join(junk, "f"), "w").write("x")
        age(junk, 400)
        rs = [r[0] for r in m.roots()[0]]
        check("\U0001f534 корни зоны попали в обход",
              os.path.join(zone, "tmp") in rs, rs)
        con14 = m.db()
        m.scan(con14)
        row = con14.execute("SELECT uuid, state FROM resources WHERE path=? AND live=1",
                            (junk,)).fetchone()
        check("\U0001f534 мусор в зоне замечен", row is not None,
              "зона отсекалась целиком — её мусор не убирался никогда")

        print("")
        print("20. Карантин зоны лежит внутри зоны, а не на чужой файловой системе")
        qr = m.quarantine_root(junk)
        check("\U0001f534 карантин зоны внутри зоны", qr.startswith(zone), qr)
        check("карантин остального — общий",
              m.quarantine_root("/tmp/что-то") == m.QUARANTINE)

        print("")
        print("21. Ресурс зоны арендуется как зона, а не выдуманным именем")
        c1 = m.Claim("uid-1", junk)
        check("\U0001f534 для пути в зоне арендуется zone:exec9",
              c1.res == "zone:exec9", c1.res)
        c2 = m.Claim("uid-2", os.path.join(sandbox, "work", "чужое"))
        check("для копии — worktree:<uuid>", c2.res == "worktree:uid-2", c2.res)

        print("")
        print("22. Восстановление: оба пути существуют — не выбираем молча")
        con15 = m.db()
        wt6 = make_repo(sandbox, "оба")
        m.scan(con15)
        u6 = con15.execute("SELECT uuid FROM resources WHERE path=? AND live=1",
                           (wt6,)).fetchone()[0]
        ip = m.qpath_for(u6, wt6)
        os.makedirs(ip, exist_ok=True)
        m.to_state(con15, u6, "QUARANTINING", "имитация", intended_path=ip)
        fixed = m.recover(con15)
        st = con15.execute("SELECT state, reason FROM resources WHERE uuid=?",
                           (u6,)).fetchone()
        check("\U0001f534 остаётся в QUARANTINING для разбора", st[0] == "QUARANTINING", st)
        check("причина названа", "оба пути" in (st[1] or ""), st)
        shutil.rmtree(ip, ignore_errors=True)

        print("")
        print("23. Восстановление: карантин недоступен — решение откладывается")
        con16 = m.db()
        saved_q = m.QUARANTINE
        m.QUARANTINE = os.path.join(sandbox, "нет-карантина")
        m.to_state(con16, u6, "QUARANTINING", "имитация",
                   intended_path=os.path.join(m.QUARANTINE, u6))
        fixed = m.recover(con16)
        st = con16.execute("SELECT state FROM resources WHERE uuid=?", (u6,)).fetchone()
        check("\U0001f534 состояние не изменено при недоступном карантине",
              st[0] == "QUARANTINING", st)
        check("решение названо отложенным",
              any("отложено" in f[2] for f in fixed), fixed)
        m.QUARANTINE = saved_q

        print("")
        print("24. Ссылка живёт, пока ресурс не удалён на самом деле")
        con17 = m.db()
        uid_live = con17.execute("SELECT uuid FROM resources WHERE state!='DELETED' "
                                 "AND live=1 LIMIT 1").fetchone()[0]
        con17.execute("INSERT OR REPLACE INTO rescue VALUES(?,?,?,?)",
                      ("refs/rescue/gc/живой/1", uid_live, "0" * 40,
                       int(time.time()) - 40 * 86400))
        con17.commit()
        gone = m.rescue_cleanup(con17, True)
        check("\U0001f534 ссылка не удалена, пока копия не удалена",
              not any("живой" in str(g[1]) for g in gone), gone)

        print("")
        print("25. Несовместимая старая база отводится в сторону, а не ломает работу")
        old_db_path = os.path.join(m.ROOT, "старая.db")
        import sqlite3 as _s
        c = _s.connect(old_db_path)
        c.execute("CREATE TABLE resources(path TEXT PRIMARY KEY, kind TEXT)")
        c.commit(); c.close()
        saved_db = m.DB
        m.DB = old_db_path
        con18 = m.db()
        cols = {r[1] for r in con18.execute("PRAGMA table_info(resources)")}
        check("\U0001f534 новая база создана с нужными колонками",
              {"uuid", "live", "generation"} <= cols, cols)
        aside = [f for f in os.listdir(m.ROOT) if f.startswith("старая.db.old-")]
        check("старая база сохранена рядом", bool(aside), os.listdir(m.ROOT))
        m.DB = saved_db

        print("")
        print("26. Недоступный карантин не закрывает ресурсы")
        con20 = m.db()
        row = con20.execute("SELECT uuid, quarantine_path FROM resources "
                            "WHERE state='QUARANTINED' AND live=1").fetchone()
        if row:
            uq, qp = row
            saved = m.QUARANTINE
            os.rename(m.QUARANTINE, m.QUARANTINE + "-унесён")
            m.scan(con20)
            st = con20.execute("SELECT state FROM resources WHERE uuid=?", (uq,)).fetchone()
            os.rename(m.QUARANTINE + "-унесён", saved)
            check("\U0001f534 при недоступном карантине состояние сохранено",
                  st[0] == "QUARANTINED", st)
        else:
            check("при недоступном карантине состояние сохранено", False, "нет карантина")

        print("")
        print("27. Недоступный каталог зон делает обход неполным")
        saved_agents = m.AGENTS_DIR
        m.AGENTS_DIR = os.path.join(sandbox, "нет-зон")
        rs, ok = m.roots()
        check("\U0001f534 недоступные зоны видны как непрочитанные", ok is False, ok)
        con21 = m.db()
        res = m.scan(con21)
        check("обход честно неполон", not res["roots_ok"], res)
        out = m.advance(con21, m.disk_state(), True, res)
        check("\U0001f534 удаление при этом запрещено",
              any("корни не прочитаны" in str(x[2]) for x in out), out)
        m.AGENTS_DIR = saved_agents

        print("")
        print("28. Похожие имена не считаются одноразовыми")
        wt7 = make_repo(sandbox, "похожие")
        for name in ("customer-dist", "important-prebuild"):
            open(os.path.join(wt7, name), "w").write("ценное\n")
        open(os.path.join(wt7, ".gitignore"), "w").write("customer-dist\nimportant-prebuild\n")
        sh("git", "-C", wt7, "add", ".gitignore")
        sh("git", "-C", wt7, "-c", "user.email=t@t", "-c", "user.name=t",
           "commit", "-qm", "правила игнорирования")
        ok, why = m.validate_worktree(wt7)
        check("\U0001f534 customer-dist и important-prebuild удерживают копию",
              not ok and "неизвестного назначения" in why, why)

        print("")
        print("29. Общий /tmp только показывается, но не убирается")
        # в песочнице правило подменено (она сама лежит в /tmp) — проверяем
        # настоящее значение, а не подменённое
        real_ro, m.REPORT_ONLY = m.REPORT_ONLY, ("/tmp",)
        check("\U0001f534 /tmp помечен как только показ", m.report_only("/tmp/что-то"))
        m.REPORT_ONLY = real_ro
        check("зона исполнителя убирается", not m.report_only(
            os.path.join(m.AGENTS_DIR, "exec9", "tmp", "х")))
        con22 = m.db()
        f2 = os.path.join(sandbox, "tmp", "показать.log")
        open(f2, "w").write("x")
        age(f2, 400)
        saved_ro = m.REPORT_ONLY
        m.REPORT_ONLY = (os.path.join(sandbox, "tmp"),)
        m.scan(con22)
        u7 = con22.execute("SELECT uuid FROM resources WHERE path=? AND live=1",
                           (f2,)).fetchone()[0]
        m.to_state(con22, u7, "EXPIRED", "просрочен")
        backdate(m, con22, u7, 100)
        m.advance(con22, m.disk_state(), True, dict(roots_ok=True, failed=[], read=2))
        check("\U0001f534 файл в общем /tmp не перенесён", os.path.exists(f2))
        m.REPORT_ONLY = saved_ro

        print("")
        print("30. Миграция базы с незаписанным журналом")
        import sqlite3 as _s3
        old = os.path.join(m.ROOT, "старая2.db")
        c = _s3.connect(old)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE resources(path TEXT PRIMARY KEY, kind TEXT)")
        c.execute("INSERT INTO resources VALUES('/x','tmp')")
        c.commit()
        check("журнал существует до миграции", os.path.exists(old + "-wal"))
        c.close()
        # 🔴 Честная имитация аварии: снимаем копию базы ВМЕСТЕ с живым журналом,
        # пока владелец её ещё держит. Так выглядит база после падения процесса.
        # (Затирать журнал мусором нельзя — это уничтожает данные самим тестом,
        # и проверка тогда доказывает не то.)
        live = os.path.join(m.ROOT, "живая.db")
        c2 = _s3.connect(live)
        c2.execute("PRAGMA journal_mode=WAL")
        c2.execute("CREATE TABLE resources(path TEXT PRIMARY KEY, kind TEXT)")
        c2.execute("INSERT INTO resources VALUES('/x','tmp')")
        c2.commit()
        c2.execute("INSERT INTO resources VALUES('/y','tmp')")
        c2.commit()
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(live + suf):
                shutil.copy2(live + suf, old + suf)
        c2.close()
        check("аварийный журнал скопирован", os.path.exists(old + "-wal"))
        saved_db = m.DB
        m.DB = old
        con23 = m.db()
        aside = sorted(f for f in os.listdir(m.ROOT) if f.startswith("старая2.db.old-"))
        check("\U0001f534 старая база отложена", bool(aside), os.listdir(m.ROOT))
        # 🔴 Главное — не «куда уехал журнал», а что данные СТАРОЙ базы целы:
        # перед переносом журнал вливается в неё, поэтому рядом с новой базой
        # чужого журнала не остаётся, а записи сохраняются в отложенной копии.
        rows = []
        if aside:
            chk = _s3.connect(os.path.join(m.ROOT, aside[0]))
            rows = [r[0] for r in chk.execute("SELECT path FROM resources")]
            chk.close()
        check("\U0001f534 данные старой базы сохранены при переносе",
              sorted(rows) == ["/x", "/y"], rows)
        left = [f for f in os.listdir(m.ROOT) if f in ("старая2.db-wal", "старая2.db-shm")]
        if left:
            # новая база в режиме WAL заводит СВОЙ журнал — он должен быть пустым
            # от старого содержимого
            got = open(os.path.join(m.ROOT, left[0]), "rb").read(60)
            check("оставшийся журнал принадлежит новой базе",
                  "аварийного".encode() not in got, got[:30])
        con23.close()
        m.DB = saved_db

        print("")
        print("31. Общий /tmp защищён и на стадии удаления, не только карантина")
        con24 = m.db()
        f3 = os.path.join(sandbox, "tmp", "старый-карантин.log")
        open(f3, "w").write("x")
        age(f3, 400)
        m.scan(con24)
        u8 = con24.execute("SELECT uuid FROM resources WHERE path=? AND live=1",
                           (f3,)).fetchone()[0]
        m.to_state(con24, u8, "EXPIRED", "просрочен")
        backdate(m, con24, u8, 100)
        m.advance(con24, m.disk_state(), True, dict(roots_ok=True, failed=[], read=2))
        st = con24.execute("SELECT state, quarantine_path FROM resources WHERE uuid=?",
                           (u8,)).fetchone()
        if st[0] == "QUARANTINED":
            # теперь объявляем этот путь общим /tmp задним числом — так выглядит
            # строка, оставшаяся от прошлой версии или восстановления
            saved_ro2, m.REPORT_ONLY = m.REPORT_ONLY, (os.path.join(sandbox, "tmp"),)
            backdate(m, con24, u8, 10000)
            m.advance(con24, m.disk_state(), True, dict(roots_ok=True, failed=[], read=2))
            st2 = con24.execute("SELECT state FROM resources WHERE uuid=?",
                                (u8,)).fetchone()
            check("🔴 карантинная строка из общего /tmp не удаляется",
                  st2[0] == "QUARANTINED", st2)
            check("файл в карантине цел", os.path.exists(st[1]))
            m.REPORT_ONLY = saved_ro2
        else:
            check("карантинная строка из общего /tmp не удаляется", False, st)

        print("")
        print("32. Исчезновение тома одной зоны делает обход неполным")
        zone2 = os.path.join(m.AGENTS_DIR, "exec9")
        os.makedirs(os.path.join(zone2, "work"), exist_ok=True)   # зона полна
        m.ZONES_FILE = os.path.join(m.ROOT, "zones.txt")
        open(m.ZONES_FILE, "w", encoding="utf-8").write(zone2 + chr(10))
        rs, ok = m.roots()
        check("🔴 зона без своего тома признана неисправной", ok is False,
              "том зоны отвалился, а обход считает себя полным")
        good, why = m.zone_ok(zone2)
        check("причина названа — зона не на своём томе",
              not good and "том" in why, why)
        # а если зону убрать совсем — тоже неполный обход, а не «зоны нет»
        os.rename(os.path.join(zone2, "agent.env"), os.path.join(zone2, "agent.env.off"))
        rs, ok2 = m.roots()
        check("🔴 исчезнувший agent.env не значит «зоны больше нет»",
              ok2 is False, ok2)
        os.rename(os.path.join(zone2, "agent.env.off"), os.path.join(zone2, "agent.env"))
        open(m.ZONES_FILE, "w", encoding="utf-8").write("")

        print("")
        print("33. Личный репозиторий поверх архива через alternates")
        personal = os.path.join(sandbox, "личный.git")
        sh("git", "init", "--bare", "-q", personal)
        os.makedirs(os.path.join(personal, "objects", "info"), exist_ok=True)
        open(os.path.join(personal, "objects", "info", "alternates"), "w").write(
            os.path.join(m.STORE, "objects") + chr(10))
        sh("git", "-C", personal, "fetch", "-q", m.STORE, "+refs/heads/main:refs/heads/main")
        wt8 = os.path.join(sandbox, "work", "личная-копия")
        r = sh("git", "-C", personal, "worktree", "add", "-q", "-b", "личн", wt8, "main")
        check("копия создана из личного репозитория", r.returncode == 0, r.stderr[:120])
        got = sh("git", "-C", wt8, "log", "--oneline", "-1")
        check("общая история читается через alternates", got.returncode == 0, got.stderr[:120])
        parent = m.parent_repo(wt8)
        check("🔴 родителем считается ЛИЧНЫЙ репозиторий, а не архив",
              parent == os.path.realpath(personal), f"{parent} против {personal}")
        age(wt8, 400)
        con25 = m.db()
        m.scan(con25)
        u9 = con25.execute("SELECT uuid FROM resources WHERE path=? AND live=1",
                           (wt8,)).fetchone()[0]
        m.to_state(con25, u9, "EXPIRED", "просрочена")
        backdate(m, con25, u9, 100)
        m.advance(con25, m.disk_state(), True, dict(roots_ok=True, failed=[], read=2))
        st = con25.execute("SELECT state, quarantine_path FROM resources WHERE uuid=?",
                           (u9,)).fetchone()
        check("🔴 копия из личного репозитория уходит в карантин",
              st[0] == "QUARANTINED", st)
        refs = sh("git", "-C", m.STORE, "for-each-ref", "--format=%(refname)",
                  "refs/rescue").stdout
        check("🔴 спасательная ссылка создана в АРХИВЕ, не в личном",
              "refs/rescue" in refs, refs[:80])
        own = sh("git", "-C", personal, "for-each-ref", "--format=%(refname)",
                 "refs/rescue").stdout
        check("в личном репозитории спасательных ссылок нет", not own.strip(), own[:80])

        print(f"\nИТОГ: {N[1]} из {N[0]}")
        return 0 if N[1] == N[0] else 1
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
