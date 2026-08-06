#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Control Plane агентов — шаг 3 плана архитектора (05.08.2026).

🔴 ГЛАВНЫЙ ИНВАРИАНТ (его формулировка): «память может объяснить состояние, но
никогда не выдаёт право на действие». Здесь живёт ПРАВО: кто прямо сейчас может
трогать ветку, гнать миграцию, деплоить. Поиск по памяти для этого не годится —
он отвечает с задержкой и неполно.

Три разные системы, смешивать опасно:
    Control Plane  — задачи, присутствие, аренды, статусы   ← этот файл
    Event Log      — что агент реально сделал                ← этот же файл, таблица events
    Memory Engine  — поиск по прошлому и канону              ← отдельно, заморожен

Устройство: SQLite в WAL за HTTP-демоном на localhost. 🔴 Файл базы агентам не
отдаётся — только API, иначе гонки и порча базы. Мосты с ПК ходят через SSH-туннель.

Владелец аренды — НЕ номер процесса (прямая поправка архитектора к моей прошлой
ошибке), а связка:
    agent_id      постоянное имя исполнителя
    instance_id   UUID конкретного процесса
    host_id       сервер
    boot_id       загрузка ОС (переживает ли перезагрузку)
    lease_token   секрет конкретной аренды — без него продлить нельзя
    fencing_token растущий номер поколения ресурса
Номер процесса хранится ТОЛЬКО для диагностики.

После истечения аренды поколение ресурса растёт. Ожившему старому процессу
опасные операции запрещены, даже если он считает себя владельцем: его
fencing_token отстал.
"""

import contracts
import http.server
import json
import os
import secrets
import socket
import socketserver
import products
import sqlite3
import sys
import threading
import time
import uuid

# 🔴 Пути и порт берутся из окружения: иначе проверочный запуск перед
# развёртыванием пришлось бы делать поверх боевой базы и боевого порта.
ROOT = os.environ.get("CP_ROOT", "/opt/agent-control")
DB = os.environ.get("CP_DB", os.path.join(ROOT, "cp.db"))
KEYFILE = os.path.join(ROOT, "api.key")
HOST = os.environ.get("CP_HOST", "127.0.0.1")
PORT = int(os.environ.get("CP_PORT", "8010"))

HEARTBEAT_S = 20          # как часто агент обязан отмечаться
TTL_S = 90                # через сколько молчания аренда считается брошенной

# Классы ресурсов, которые архитектор велел сериализовать ВСЕГДА, даже если код
# писался параллельно. Помечаем их, чтобы отказ был понятным.
SERIAL = ("migration:", "deploy:", "merge:", "release:", "prod:")

# 🔴 Имена ресурсов НЕ принимаем как произвольные строки от агента: иначе один
# возьмёт `deploy:sandbox`, другой `sandbox:deploy`, и оба получат разрешение.
# Каталог допустимых имён ведёт сервер.
KNOWN = {
    "merge": {"main"},
    "deploy": {"sandbox", "prod", "staging"},
    "migration": {"head", "prod", "sandbox"},
    "release": {"tag"},
    "prod": {"access"},
    "branch": None,          # None = любое значение, но префикс обязателен
    "db": None,
    "port": None,
    "worktree": None,
    "zone": None,
    "path": None,
    "ci": None,
}


PATHY = ("path", "zone")


def canon(resource):
    """Каноническое имя или ошибка.

    🔴 Регистр и пробелы приводим ТОЛЬКО у именованных ресурсов (`deploy:sandbox`).
    Для путей этого делать нельзя: на Linux пути регистрозависимы, а пробел —
    часть имени файла. `path:Backend/A B.py` и `path:backend/a b.py` — разные файлы.
    """
    s = str(resource).strip()
    if ":" not in s:
        return None, f"имя без класса: {resource!r} (нужно вида deploy:sandbox)"
    cls, rest = s.split(":", 1)
    cls = cls.strip().lower()
    if cls not in KNOWN:
        return None, (f"неизвестный класс ресурса {cls!r}; допустимые: "
                      f"{', '.join(sorted(KNOWN))}")
    if cls in PATHY:
        rest = rest.strip()
        if rest.startswith("/"):
            return None, "путь должен быть относительно корня репозитория"
        parts = []
        for p in rest.split("/"):
            if p in ("", "."):
                continue
            if p == "..":
                return None, "путь с '..' не принимается"
            parts.append(p)
        if not parts:
            return None, "пустой путь"
        return f"{cls}:" + "/".join(parts), None

    rest = " ".join(rest.split()).lower()
    allowed = KNOWN[cls]
    if allowed is not None and rest not in allowed:
        return None, f"для класса {cls} допустимо: {', '.join(sorted(allowed))}"
    return f"{cls}:{rest}", None


def conflicts_with(a, b):
    """Пересечение ресурсов. Для файловых зон точного сравнения строк мало:
    `path:backend/warehouse` и `path:backend/warehouse/models.py` — один и тот же
    участок кода. Сравниваем ПО СОСТАВЛЯЮЩИМ пути, а не по строковому началу:
    иначе `backend/ware` ложно накрыл бы `backend/warehouse`."""
    if a == b:
        return True
    ca, ra = a.split(":", 1)
    cb, rb = b.split(":", 1)
    if ca in PATHY and cb in PATHY:
        pa, pb = ra.split("/"), rb.split("/")
        n = min(len(pa), len(pb))
        return pa[:n] == pb[:n]
    return False


def now():
    return int(time.time())


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return "?"


# ── База ────────────────────────────────────────────────────────────────────

def db():
    con = sqlite3.connect(DB, timeout=30, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
    agent_id TEXT PRIMARY KEY, host_id TEXT, instance_id TEXT, boot_id TEXT,
    pid INTEGER, first_seen INTEGER, last_seen INTEGER, note TEXT);

CREATE TABLE IF NOT EXISTS tasks(
    task_id TEXT PRIMARY KEY, title TEXT, agent_id TEXT, state TEXT,
    created INTEGER, updated INTEGER, contract TEXT);

CREATE TABLE IF NOT EXISTS leases(
    resource TEXT PRIMARY KEY, task_id TEXT, agent_id TEXT, instance_id TEXT,
    host_id TEXT, boot_id TEXT, lease_token TEXT, fencing_token INTEGER,
    acquired INTEGER, expires INTEGER, pid INTEGER);

-- поколение ресурса: растёт при КАЖДОЙ новой аренде и при каждом истечении.
-- Отставший fencing_token — признак того, что владелец больше не владелец.
CREATE TABLE IF NOT EXISTS fencing(resource TEXT PRIMARY KEY, counter INTEGER);

-- 🔴 Удержания живут ЗДЕСЬ, а не файлом рядом со сборщиком: у удержания должны
-- быть автор, причина и срок, иначе список устаревает и о нём забывают.
-- Бессрочное удержание ставит только администратор (Мост).
CREATE TABLE IF NOT EXISTS holds(
    resource TEXT PRIMARY KEY, reason TEXT, created_by TEXT, approved_by TEXT,
    created_at INTEGER, expires_at INTEGER);

CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, agent_id TEXT,
    task_id TEXT, kind TEXT, payload TEXT);

CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ev_task ON events(task_id);
""" + contracts.SCHEMA + products.SCHEMA


def init():
    os.makedirs(ROOT, exist_ok=True)
    con = db()
    con.executescript(SCHEMA)
    products.migrate(con)
    con.close()
    if not os.path.exists(KEYFILE):
        with open(KEYFILE, "w") as f:
            f.write(secrets.token_urlsafe(32))
        os.chmod(KEYFILE, 0o640)


KEYDIR = os.path.join(ROOT, "keys")


def api_key():
    with open(KEYFILE) as f:
        return f.read().strip()


def identify(key):
    """🔴 Кто пришёл — решает СЕРВЕР по ключу, а не клиент своим полем agent_id.
    Иначе журнал ничего не доказывает: любой агент подписался бы чужим именем.
    Возвращает (agent_id, admin) или (None, False)."""
    if not key:
        return None, False
    try:
        for name in os.listdir(KEYDIR):
            if not name.endswith(".key"):
                continue
            with open(os.path.join(KEYDIR, name)) as f:
                if secrets.compare_digest(f.read().strip(), key):
                    who = name[:-4]
                    return who, who in ("admin", "most")
    except OSError:
        pass
    # Общий ключ оставлен только для перехода: им нельзя быть агентом.
    try:
        if secrets.compare_digest(api_key(), key):
            return "legacy", True
    except OSError:
        pass
    return None, False


# ── Логика ──────────────────────────────────────────────────────────────────

def bump(con, resource):
    """Следующее поколение ресурса."""
    row = con.execute("SELECT counter FROM fencing WHERE resource=?", (resource,)).fetchone()
    n = (row[0] if row else 0) + 1
    con.execute("INSERT INTO fencing VALUES(?,?) ON CONFLICT(resource) "
                "DO UPDATE SET counter=?", (resource, n, n))
    return n


def sweep(con):
    """Снять протухшие аренды. Поколение растёт — ожившему владельцу уже нельзя."""
    t = now()
    rows = con.execute("SELECT resource, agent_id, task_id FROM leases WHERE expires < ?",
                       (t,)).fetchall()
    for resource, agent_id, task_id in rows:
        con.execute("DELETE FROM leases WHERE resource=?", (resource,))
        bump(con, resource)
        log(con, agent_id, task_id, "lease_expired",
            {"resource": resource, "молчание_с": TTL_S})
    return len(rows)


def log(con, agent_id, task_id, kind, payload):
    con.execute("INSERT INTO events(ts,agent_id,task_id,kind,payload) VALUES(?,?,?,?,?)",
                (now(), agent_id, task_id, kind, json.dumps(payload, ensure_ascii=False)))


DISK_STATE = os.path.join(ROOT, "state", "disk.json")


def disk_gate(kind):
    """Проверка места ПЕРЕД выдачей работы. 🔴 Протухшее состояние — это запрет,
    а не «наверное всё хорошо»: молчание монитора ничего не гарантирует."""
    try:
        with open(DISK_STATE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception as e:
        return False, f"состояние диска недоступно ({e}) — новая работа запрещена"
    age = now() - int(s.get("generated_at", 0))
    if age > int(s.get("max_age_s", 120)):
        return False, (f"состояние диска протухло ({age} с назад) — "
                       f"монитор не работает, новая работа запрещена")
    if kind == "task" and not s.get("allow_new_tasks", False):
        return False, (f"диск занят на {s.get('used_pct')}% "
                       f"(свободно {s.get('free_gb')} ГБ): {s.get('note')}")
    if kind == "agent" and not s.get("allow_new_agents", False):
        return False, f"диск занят на {s.get('used_pct')}%: новых исполнителей нельзя"
    return True, ""


def acquire(con, d):
    """🔴 Все ресурсы задачи берутся ОДНОЙ транзакцией. Не вышло взять один —
    не берём ни одного. Иначе двое возьмут по половине и встанут насмерть."""
    names, bad = [], []
    for r in d["resources"]:
        c, err = canon(r)
        (names.append(c) if c else bad.append(err))
    if bad:
        return {"ok": False, "причина": "имена ресурсов не приняты", "ошибки": bad}
    resources = sorted(set(names))      # порядок один для всех — от взаимных блокировок
    agent_id, instance_id = d["agent_id"], d["instance_id"]
    task_id = d.get("task_id")
    t = now()

    # 🔴 Служебный путь у сборщика отдельный (/gc/claim). Здесь исключений нет:
    # иначе контракт обходился бы простым представлением именем gc.
    if not task_id:
        return {"ok": False, "причина": "аренда без задачи не выдаётся"}

    ver = digest = None
    con.execute("BEGIN IMMEDIATE")
    try:
        # 🔴 Проверка контракта — ВНУТРИ той же транзакции, что и вставка аренды.
        # Снаружи между проверкой и выдачей успевала появиться новая версия
        # контракта, и аренда выдавалась по условиям уже недействующей.
        err, ver, digest = contracts.check_acquire(con, task_id, agent_id, resources)
        if err:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": err} if isinstance(err, str) else \
                   {"ok": False, **err}
        sweep(con)
        busy = []
        # 🔴 Удержание сравниваем ПО ПЕРЕСЕЧЕНИЮ, а не по точному совпадению имени:
        # удержание на `path:backend/warehouse` обязано закрывать и файл внутри
        # него. Иначе зона под расследованием защищена только целиком.
        active_holds = con.execute(
            "SELECT resource, reason FROM holds WHERE expires_at=0 OR expires_at>?",
            (t,)).fetchall()
        for r in resources:
            hit = next(((hr, hres) for hr, hres in active_holds
                        if conflicts_with(r, hr)), None)
            if hit:
                con.execute("ROLLBACK")
                return {"ok": False,
                        "причина": f"ресурс {r} закрыт удержанием {hit[0]}: {hit[1]}"}
        held = con.execute("SELECT resource, agent_id, instance_id, expires "
                           "FROM leases").fetchall()
        for r in resources:
            for hr, ha, hi, he in held:
                if conflicts_with(r, hr) and not (ha == agent_id and hi == instance_id):
                    busy.append({"resource": r, "конфликт_с": hr, "занят": ha,
                                 "освободится_через_с": max(0, he - t)})
        if busy:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "ресурсы заняты", "занятые": busy}

        token = secrets.token_urlsafe(24)
        fencing = {}
        for r in resources:
            n = bump(con, r)
            fencing[r] = n
            con.execute("INSERT INTO leases VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(resource) DO UPDATE SET task_id=excluded.task_id,"
                        "agent_id=excluded.agent_id, instance_id=excluded.instance_id,"
                        "lease_token=excluded.lease_token, fencing_token=excluded.fencing_token,"
                        "acquired=excluded.acquired, expires=excluded.expires",
                        (r, task_id, agent_id, instance_id, d.get("host_id", ""),
                         d.get("boot_id", ""), token, n, t, t + TTL_S, d.get("pid", 0)))
        # 🔴 Состояние «в работе» выставляет УСПЕШНЫЙ захват, а не клиент: только
        # так момент начала работы совпадает с моментом, когда условия
        # зафиксированы.
        con.execute("UPDATE tasks SET state='running', updated=? "
                    "WHERE task_id=? AND state='assigned'", (t, task_id))
        log(con, agent_id, task_id, "lease_acquired",
            {"resources": resources, "fencing": fencing,
             "версия_контракта": ver, "отпечаток_контракта": digest})
        con.execute("COMMIT")
        return {"ok": True, "lease_token": token, "fencing": fencing,
                "expires": t + TTL_S, "heartbeat_s": HEARTBEAT_S}
    except Exception as e:
        con.execute("ROLLBACK")
        return {"ok": False, "причина": f"сбой: {e}"}


def heartbeat(con, d):
    """Продлить можно ТОЛЬКО с верным секретом аренды."""
    token = d.get("lease_token", "")
    t = now()
    con.execute("BEGIN IMMEDIATE")
    try:
        sweep(con)
        rows = con.execute("SELECT resource FROM leases WHERE lease_token=?",
                           (token,)).fetchall()
        if not rows:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "аренда не найдена или уже истекла — "
                                            "получить заново, старые действия запрещены"}
        con.execute("UPDATE leases SET expires=? WHERE lease_token=?", (t + TTL_S, token))
        con.execute("UPDATE agents SET last_seen=? WHERE agent_id=?",
                    (t, d.get("agent_id", "")))
        con.execute("COMMIT")
        return {"ok": True, "продлено": [r[0] for r in rows], "expires": t + TTL_S}
    except Exception as e:
        con.execute("ROLLBACK")
        return {"ok": False, "причина": str(e)}


def check(con, d):
    """«Можно ли мне сейчас это делать?» — спрашивать ПЕРЕД каждой опасной
    операцией, а не один раз в начале. Между началом задачи и деплоем аренда
    могла протухнуть."""
    r0, token, fencing = d["resource"], d.get("lease_token", ""), d.get("fencing_token")
    r, err = canon(r0)
    if err:
        return {"allow": False, "причина": err}
    sweep(con)
    # 🔴 Удержание, поставленное УЖЕ ПОСЛЕ выдачи аренды, обязано останавливать
    # работу. Раньше здесь смотрели только на аренду и поколение: сборщик успевал
    # взять захват, перечитать удержания (пусто), уйти в долгое спасение — и за
    # это время поставленный hold не мешал ему ничего.
    t = now()
    for hres, hreason in con.execute(
            "SELECT resource, reason FROM holds WHERE expires_at=0 OR expires_at>?",
            (t,)):
        if conflicts_with(r, hres):
            return {"allow": False,
                    "причина": f"ресурс закрыт удержанием {hres}: {hreason}"}
    row = con.execute("SELECT lease_token, fencing_token, agent_id, expires FROM leases "
                      "WHERE resource=?", (r,)).fetchone()
    if not row:
        return {"allow": False, "причина": "аренды нет — ресурс свободен, но право не выдано"}
    if row[0] != token:
        return {"allow": False, "причина": f"ресурсом владеет {row[2]}"}
    if fencing is not None and int(fencing) != row[1]:
        return {"allow": False, "причина": f"поколение устарело: у тебя {fencing}, "
                                           f"текущее {row[1]} — аренда была потеряна"}
    return {"allow": True, "осталось_с": row[3] - now(),
            "сериализуемый": r.startswith(SERIAL)}


def release(con, d):
    token = d.get("lease_token", "")
    con.execute("BEGIN IMMEDIATE")
    try:
        rows = con.execute("SELECT resource, agent_id, task_id FROM leases "
                           "WHERE lease_token=?", (token,)).fetchall()
        for r, a, tk in rows:
            con.execute("DELETE FROM leases WHERE resource=?", (r,))
            bump(con, r)
            log(con, a, tk, "lease_released", {"resource": r})
        con.execute("COMMIT")
        return {"ok": True, "освобождено": [r[0] for r in rows]}
    except Exception as e:
        con.execute("ROLLBACK")
        return {"ok": False, "причина": str(e)}


def status(con, d):
    sweep(con)
    t = now()
    agents = [dict(agent_id=a, host=h, instance=i[:8] if i else "", pid=p,
                   молчит_с=t - (ls or t))
              for a, h, i, p, ls in con.execute(
                  "SELECT agent_id, host_id, instance_id, pid, last_seen FROM agents")]
    leases = [dict(resource=r, agent=a, task=tk, поколение=f, осталось_с=e - t)
              for r, a, tk, f, e in con.execute(
                  "SELECT resource, agent_id, task_id, fencing_token, expires FROM leases "
                  "ORDER BY resource")]
    tasks = [dict(task_id=ti, title=ttl, agent=a, state=s)
             for ti, ttl, a, s in con.execute(
                 "SELECT task_id, title, agent_id, state FROM tasks "
                 "WHERE state NOT IN ('done','cancelled') ORDER BY created")]
    return {"ok": True, "агенты": agents, "аренды": leases, "задачи": tasks,
            "heartbeat_s": HEARTBEAT_S, "ttl_s": TTL_S}


def register(con, d):
    t = now()
    con.execute("INSERT INTO agents VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(agent_id) DO UPDATE "
                "SET host_id=excluded.host_id, instance_id=excluded.instance_id,"
                "boot_id=excluded.boot_id, pid=excluded.pid, last_seen=excluded.last_seen",
                (d["agent_id"], d.get("host_id", ""), d.get("instance_id", ""),
                 d.get("boot_id", ""), d.get("pid", 0), t, t, d.get("note", "")))
    log(con, d["agent_id"], None, "agent_online", {"instance": d.get("instance_id", "")})
    return {"ok": True, "heartbeat_s": HEARTBEAT_S, "ttl_s": TTL_S}


def revoke_task_leases(con, task_id, why):
    """🔴 Снять все аренды задачи и поднять поколения. Без этого блокировка и
    отмена задачи ничего не значат: проверка права смотрит только на аренду,
    удержание и поколение, а состояние задачи в ней не участвует — и процесс
    продолжает работать над отменённой задачей."""
    revoked = []
    for res, agent in con.execute("SELECT resource, agent_id FROM leases "
                                  "WHERE task_id=?", (task_id,)).fetchall():
        con.execute("DELETE FROM leases WHERE resource=?", (res,))
        bump(con, res)
        revoked.append(res)
        log(con, agent, task_id, "task_lease_revoked", {"resource": res, "причина": why})
    return revoked


def task_create(con, d):
    """Завести или изменить задачу. 🔴 Контракт обязателен и проверяется схемой.

    🔴 ВСЁ решение — внутри одной транзакции: чтение владельца и состояния,
    проверка перехода, проверка контракта, отзыв аренд и запись. Снаружи два
    одновременных запроса читали одно состояние и оба считали свой переход
    разрешённым — так `running → cancelled` и `running → blocked` давали в итоге
    запрещённое `cancelled → blocked`.
    """
    task_id = d["task_id"]
    state = d.get("state", "created")

    # Служебные состояния клиент не выставляет НИКОГДА:
    #   running          ставит успешный захват аренды;
    #   handoff_pending  ставит создание передачи результата;
    #   done             ставит приём передачи.
    SERVICE_STATES = ("running", "handoff_pending", "done")
    if state in SERVICE_STATES:
        return {"ok": False,
                "причина": f"состояние {state} выставляется системой, а не запросом: "
                           f"running — захватом аренды, handoff_pending — созданием "
                           f"передачи, done — её приёмом"}

    body = d.get("contract")
    if body is not None and not d.get("_admin"):
        return {"ok": False, "причина": "контракт задачи создаёт и меняет только Мост"}

    t = now()
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT state, agent_id FROM tasks WHERE task_id=?",
                          (task_id,)).fetchone()
        cur_state = row[0] if row else "created"
        owner = row[1] if row else None

        if not d.get("_admin"):
            if owner and owner != d.get("agent_id"):
                con.execute("ROLLBACK")
                return {"ok": False, "причина": f"задача назначена на {owner}"}
            if state not in ("blocked", "assigned", "cancelled"):
                con.execute("ROLLBACK")
                return {"ok": False,
                        "причина": "исполнитель может выставить только blocked, "
                                   "assigned или cancelled"}

        ok, why = contracts.can_transition(cur_state, state)
        if not ok:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": why}

        if state in ("created", "assigned"):
            ok, why = disk_gate("task")
            if not ok:
                con.execute("ROLLBACK")
                return {"ok": False, "причина": why}

        if body is None and not con.execute(
                "SELECT 1 FROM task_contracts WHERE task_id=? AND active=1",
                (task_id,)).fetchone():
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": "у задачи должен быть контракт: цель, исполнитель, "
                               "ресурсы, ожидаемые результаты и кому передавать"}

        if body is not None:
            allowed, why2 = contracts.may_change(con, task_id)
            if not allowed:
                con.execute("ROLLBACK")
                return {"ok": False, "причина": why2}
            ver, errs = contracts.put(con, task_id, body, d.get("agent_id", ""),
                                      canon_resource=canon)
            if errs:
                con.execute("ROLLBACK")
                return {"ok": False, "причина": "контракт не принят", "ошибки": errs}

        revoked = []
        if state in ("blocked", "cancelled"):
            revoked = revoke_task_leases(con, task_id, f"задача переведена в {state}")

        act = contracts.active(con, task_id)
        assignee = (act[1].get("assignee") if act else d.get("agent_id", ""))
        con.execute(
            "INSERT INTO tasks VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE "
            "SET title=excluded.title, agent_id=excluded.agent_id, "
            "state=excluded.state, updated=excluded.updated",
            (task_id, d.get("title", ""), assignee, state, t, t,
             json.dumps({"contract_version": act[0] if act else None},
                        ensure_ascii=False)))
        log(con, d.get("agent_id"), task_id, "task_" + state,
            {"title": d.get("title", ""), "версия_контракта": act[0] if act else None,
             "отпечаток": (act[2][:16] if act else None),
             "отозвано_аренд": len(revoked)})
        con.execute("COMMIT")
        return {"ok": True, "версия_контракта": act[0] if act else None,
                "отпечаток_контракта": act[2] if act else None,
                "состояние": state, "отозванные_аренды": revoked}
    except Exception as e:
        con.execute("ROLLBACK")
        return {"ok": False, "причина": f"сбой: {e}"}


def contract_show(con, d):
    act = contracts.active(con, d["task_id"])
    hist = [dict(version=v, sha=sha[:16], created_by=by, created_at=at, active=bool(a))
            for v, sha, by, at, a in con.execute(
                "SELECT version, body_sha256, created_by, created_at, active "
                "FROM task_contracts WHERE task_id=? ORDER BY version", (d["task_id"],))]
    if not act:
        return {"ok": False, "причина": "у задачи нет действующего контракта",
                "версии": hist}
    return {"ok": True, "версия": act[0], "контракт": act[1], "отпечаток": act[2],
            "версии": hist}


def event(con, d):
    log(con, d.get("agent_id"), d.get("task_id"), d.get("kind", "note"),
        d.get("payload", {}))
    return {"ok": True}


def events(con, d):
    n = int(d.get("limit", 50))
    rows = con.execute("SELECT ts, agent_id, task_id, kind, payload FROM events "
                       "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return {"ok": True, "события": [
        dict(ts=r[0], agent=r[1], task=r[2], kind=r[3],
             payload=json.loads(r[4] or "{}")) for r in rows]}


def hold_add(con, d):
    """Поставить удержание. Бессрочное — только администратору.

    🔴 ВСЯ функция — одна транзакция, включая проверку чужого удержания.
    Раньше проверка шла ДО начала транзакции, и между чтением и записью
    администратор успевал поставить бессрочное удержание, а агент — перезаписать
    его своим временным. Защита существовала только на бумаге.
    """
    r, err = canon(d["resource"])
    if err:
        return {"ok": False, "причина": err}
    exp = d.get("expires_at")
    if not exp and not d.get("_admin"):
        return {"ok": False, "причина": "бессрочное удержание ставит только Мост; "
                                        "укажи expires_at"}
    con.execute("BEGIN IMMEDIATE")
    try:
        prev = con.execute("SELECT approved_by FROM holds WHERE resource=?",
                           (r,)).fetchone()
        if prev and prev[0] and not d.get("_admin"):
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": "удержание поставлено Мостом — менять может только он"}

        # Удержание отзывает конфликтующие аренды и поднимает поколение: иначе
        # тот, кто уже держит ресурс, продолжит работу как ни в чём не бывало.
        revoked = []
        for res, agent in con.execute("SELECT resource, agent_id FROM leases").fetchall():
            if conflicts_with(r, res):
                con.execute("DELETE FROM leases WHERE resource=?", (res,))
                bump(con, res)
                revoked.append({"resource": res, "был": agent})
                log(con, agent, None, "lease_revoked_by_hold",
                    {"resource": res, "удержание": r})

        con.execute("INSERT INTO holds VALUES(?,?,?,?,?,?) ON CONFLICT(resource) "
                    "DO UPDATE SET reason=excluded.reason, "
                    "expires_at=excluded.expires_at, approved_by=excluded.approved_by, "
                    "created_by=excluded.created_by, created_at=excluded.created_at",
                    (r, d.get("reason", ""), d.get("agent_id", ""),
                     d.get("agent_id", "") if d.get("_admin") else "",
                     now(), int(exp or 0)))
        log(con, d.get("agent_id"), None, "hold_set",
            {"resource": r, "причина": d.get("reason", ""), "до": exp,
             "отозвано_аренд": len(revoked)})
        con.execute("COMMIT")
        return {"ok": True, "resource": r, "отозванные_аренды": revoked}
    except Exception as e:
        con.execute("ROLLBACK")
        return {"ok": False, "причина": f"сбой: {e}"}


def hold_del(con, d):
    r, err = canon(d["resource"])
    if err:
        return {"ok": False, "причина": err}
    row = con.execute("SELECT approved_by FROM holds WHERE resource=?", (r,)).fetchone()
    if row and row[0] and not d.get("_admin"):
        return {"ok": False, "причина": "удержание поставлено Мостом — снять может только он"}
    con.execute("DELETE FROM holds WHERE resource=?", (r,))
    log(con, d.get("agent_id"), None, "hold_cleared", {"resource": r})
    return {"ok": True}


def holds(con, d):
    t = now()
    rows = con.execute("SELECT resource, reason, created_by, approved_by, created_at, "
                       "expires_at FROM holds").fetchall()
    out = []
    for r, reason, by, appr, at, exp in rows:
        if exp and exp < t:
            con.execute("DELETE FROM holds WHERE resource=?", (r,))
            log(con, None, None, "hold_expired", {"resource": r})
            continue
        out.append(dict(resource=r, reason=reason, created_by=by, approved_by=appr,
                        created_at=at, expires_at=exp,
                        бессрочно=not exp))
    return {"ok": True, "удержания": out}


def gc_claim(con, d):
    """🔴 Атомарный переход для сборщика. Спросить «аренды нет» недостаточно:
    сразу после ответа ресурс может занять другой процесс. Поэтому сборщик сам
    БЕРЁТ исключительную аренду на копию и только потом трогает файлы."""
    r, err = canon(d["resource"])
    if err:
        return {"ok": False, "причина": err}
    t = now()
    con.execute("BEGIN IMMEDIATE")
    try:
        sweep(con)
        held = con.execute("SELECT resource FROM holds WHERE expires_at=0 OR "
                           "expires_at>?", (t,)).fetchall()
        hit = next((h[0] for h in held if conflicts_with(r, h[0])), None)
        if hit:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": f"ресурс закрыт удержанием {hit}"}
        for hr, ha, he in con.execute("SELECT resource, agent_id, expires FROM leases"):
            if conflicts_with(r, hr):
                con.execute("ROLLBACK")
                return {"ok": False, "причина": f"занят: {hr} держит {ha}",
                        "освободится_через_с": max(0, he - t)}
        token = secrets.token_urlsafe(24)
        n = bump(con, r)
        con.execute("INSERT INTO leases VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (r, d.get("task_id", "gc"), "gc", d.get("instance_id", "gc"),
                     d.get("host_id", ""), d.get("boot_id", ""), token, n, t,
                     t + TTL_S, d.get("pid", 0)))
        log(con, "gc", None, "gc_claimed", {"resource": r})
        con.execute("COMMIT")
        return {"ok": True, "lease_token": token, "fencing": {r: n},
                "expires": t + TTL_S, "heartbeat_s": HEARTBEAT_S}
    except Exception as e:
        con.execute("ROLLBACK")
        return {"ok": False, "причина": str(e)}


def product_register(con, d):
    return products.register(con, d, contracts, canon)


def product_check(con, d):
    # 🔴 Признак «системная проверка» никогда не приходит от клиента: иначе
    # производитель сам поставил бы себе digest_verified.
    return products.record_check(con, d, contracts, system=False)


def product_show(con, d):
    return products.show(con, d)


ROUTES = {"/register": register, "/task": task_create, "/acquire": acquire,
          "/contract": contract_show,
          "/product/register": product_register, "/product/check": product_check,
          "/product": product_show,
          "/heartbeat": heartbeat, "/release": release, "/check": check,
          "/status": status, "/event": event, "/events": events,
          "/hold": hold_add, "/unhold": hold_del, "/holds": holds,
          "/gc/claim": gc_claim}


# ── HTTP ────────────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if hasattr(self, "_peer"):
            # Локальный агент через Unix-сокет: личность даёт ядро.
            pid, uid, gid = self._peer()
            who, admin = uid_to_agent(uid)
            if not who:
                return self._send(403, {"ok": False,
                                        "причина": f"пользователь uid={uid} не сопоставлен"})
        else:
            who, admin = identify(self.headers.get("X-Api-Key", ""))
            if not who:
                return self._send(403, {"ok": False, "причина": "ключ не признан"})
        route = ROUTES.get(self.path.split("?")[0])
        if not route:
            return self._send(404, {"ok": False, "причина": "нет такого метода"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"ok": False, "причина": f"тело не разобрано: {e}"})
        # Имя, присланное клиентом, перебивается именем из ключа. Мост (админ)
        # может действовать от чужого имени — ему это по роли положено.
        via = "UID" if hasattr(self, "_peer") else "ключу"
        if not admin:
            if d.get("agent_id") and d["agent_id"] != who:
                return self._send(403, {"ok": False,
                                        "причина": f"по {via} это {who}, "
                                                   f"а в запросе {d['agent_id']}"})
            d["agent_id"] = who
        elif not d.get("agent_id"):
            # 🔴 Мост тоже должен подписываться: иначе удержание, поставленное
            # администратором, не помечается как его и снять сможет любой.
            d["agent_id"] = who
        d["_admin"] = admin
        con = db()
        try:
            self._send(200, route(con, d))
        except KeyError as e:
            self._send(400, {"ok": False, "причина": f"не хватает поля {e}"})
        except Exception as e:
            self._send(500, {"ok": False, "причина": str(e)})
        finally:
            con.close()

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, {"ok": True, "ttl_s": TTL_S})
        self._send(404, {"ok": False})

    def log_message(self, *a):
        pass          # свой журнал ведём в events, а не в stderr


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ── Unix-сокет: личность по UID, а не по ключу ──────────────────────────────
# 🔴 Надёжнее файлов с ключами: ядро само сообщает, какой пользователь на том
# конце. Представиться другим невозможно в принципе — ключ нечего красть.
SOCKET = os.environ.get("CP_SOCKET", os.path.join(ROOT, "cp.sock"))
MAPFILE = os.path.join(ROOT, "agents.map")


def uid_to_agent(uid):
    """Отображение системного пользователя в имя исполнителя."""
    try:
        import pwd
        user = pwd.getpwuid(uid).pw_name
    except Exception:
        return None, False
    if uid == 0:
        return "admin", True
    try:
        with open(MAPFILE, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].split()
                if len(line) >= 2 and line[0] == user:
                    return line[1], len(line) > 2 and line[2] == "admin"
    except OSError:
        pass
    return user, False


class UnixHandler(Handler):
    def _peer(self):
        import struct
        creds = self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                           struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", creds)
        return pid, uid, gid

    def address_string(self):
        return "unix"


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    server_name = "cp"
    server_port = 0


def sweeper():
    """Отдельный поток: снимать протухшее, даже когда запросов нет. Иначе
    брошенный ресурс держится до следующего чужого обращения."""
    while True:
        time.sleep(HEARTBEAT_S)
        try:
            con = db()
            con.execute("BEGIN IMMEDIATE")
            n = sweep(con)
            con.execute("COMMIT")
            con.close()
        except Exception:
            pass


def main():
    init()
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        print("готово:", DB)
        return 0
    threading.Thread(target=sweeper, daemon=True).start()

    # Unix-сокет для локальных агентов (личность по UID) и TCP для Моста с ПК
    # через SSH-туннель (там UID узнать неоткуда, остаётся ключ).
    if os.path.exists(SOCKET):
        os.unlink(SOCKET)
    us = UnixServer(SOCKET, UnixHandler)
    os.chmod(SOCKET, 0o666)          # доступ решает UID, а не права на файл
    threading.Thread(target=us.serve_forever, daemon=True).start()

    print(f"Control Plane: сокет {SOCKET} (по UID) + http://{HOST}:{PORT} (по ключу) · "
          f"heartbeat {HEARTBEAT_S} с · TTL {TTL_S} с", flush=True)
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
