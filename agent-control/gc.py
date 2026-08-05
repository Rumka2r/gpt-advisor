#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Внешний сборщик ресурсов агентов. Версия 2 — переписан после разбора архитектора
(05.08.2026), который нашёл в версии 1 десять дефектов уровня P0.

Модель: аренда определяет владельца → квота ограничивает ущерб → возраст задаёт
порядок → внешний сборщик удаляет. 🔴 Агент за собой не убирает НИКОГДА: он
падает, его убивают, сессия рвётся — обработчик завершения не выполняется.

Машина состояний (настоящая, с фиксацией КАЖДОГО перехода в базу до действий
на диске — иначе падение посреди работы разводит базу и файловую систему):

    ACTIVE → EXPIRED → QUARANTINING → QUARANTINED → PURGEABLE → DELETED

Главные правила, каждое куплено ошибкой:
 · разрушительное действие — только под атомарным захватом в Control Plane;
 · координатор недоступен → не удаляем НИЧЕГО (молчание ≠ «владельца нет»);
 · корень не прочитан → ничего под ним не считаем исчезнувшим и запрещаем --apply;
 · любая проверка, которую не удалось выполнить, значит «не удалять»;
 · режим показа не меняет НИЧЕГО, даже в архиве;
 · минимальная выдержка не обнуляется даже при переполненном диске.

Команды:
    gc.py scan          обойти корни, обновить состояния
    gc.py run           показать, что было бы сделано (ничего не меняет)
    gc.py run --apply   выполнить переходы
    gc.py report        текущее состояние
    gc.py disk          заполнение диска и что оно запрещает
    gc.py recover       разобрать зависшие переходы после падения
"""

import argparse
import errno
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import uuid as uuidlib

ROOT = "/opt/agent-control"
DB = os.path.join(ROOT, "gc.db")
STATE_DIR = os.path.join(ROOT, "state")
LOCKFILE = os.path.join(ROOT, "gc.lock")
QUARANTINE = "/var/agent-quarantine"
STORE = "/srv/agents/store.git"
AGENTS_DIR = "/srv/agents"

CP_API = "http://127.0.0.1:8010"
CP_KEYFILE = os.path.join(ROOT, "api.key")

FLOOR_GB = 15
DISK_MAX_AGE_S = 120
LEVELS = [
    (95, "emergency", "новых исполнителей и сборок не запускать"),
    (90, "critical", "удалять карантин, останавливать неважные задачи"),
    (85, "high", "новые задачи не выдавать"),
    (80, "warn", "чистить просроченные кэши"),
    (0, "ok", ""),
]

# ttl_h    — простой, после которого ресурс считается брошенным
# grace_h  — выдержка в EXPIRED до карантина
# purge_h  — выдержка в карантине до удаления
# min_h    — 🔴 выдержка, которая НЕ сокращается даже при переполнении диска:
#            нехватка места не даёт права потерять неподтверждённую работу
KINDS = {
    "session_cache": dict(ttl_h=72, grace_h=6, purge_h=24, min_h=1),
    "tmp_generic": dict(ttl_h=168, grace_h=12, purge_h=24, min_h=1),
    "worktree": dict(ttl_h=168, grace_h=24, purge_h=72, min_h=6),
}

# 🔴 Пути, для которых аренду взять НЕ У КОГО: в общем /tmp нет владельца,
# и захват выдуманного имени не мешает никому начать пользоваться каталогом
# между проверкой и переносом. Такие ресурсы только показываем.
REPORT_ONLY = ("/tmp",)


def report_only(path):
    z = path.startswith(AGENTS_DIR + os.sep)
    return (not z) and any(path == r or path.startswith(r.rstrip("/") + "/")
                           for r in REPORT_ONLY)


BASE_ROOTS = [
    ("/tmp/claude-1000", "session_cache"),
    ("/tmp", "tmp_generic"),
    ("/home/executor", "worktree"),
    # зоны агентов по возрасту не убираются: их выдаёт и снимает провижининг
]

# 🔴 Отсекаем ТОЧЕЧНО. Раньше здесь стоял «/srv/agents» целиком — и личные
# tmp/cache исполнителей, ради которых зона и заводилась, не сканировались
# никогда: каждый их путь начинался с отсечённого корня.
NEVER = (
    "/tmp/.X11", "/tmp/.ICE", "/tmp/systemd-", "/tmp/snap",
    "/srv/agents/store.git", "/home/executor/agents", "/opt/agent-control",
    QUARANTINE,
)

ALLOWED_ROOTS = ("/tmp", "/home/executor")

DISPOSABLE = ("node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
              ".ruff_cache", "dist", "build", ".vite", ".next", "venv", ".venv",
              "coverage", ".tox", "target", ".turbo", "playwright-report")

RESCUE_KEEP_DAYS = 30
HOLD_FILE = os.path.join(ROOT, "hold.txt")
HOLD_MARKER = ".agent-hold"


# ── Мелочи ──────────────────────────────────────────────────────────────────

def now():
    return int(time.time())


def sh(cmd, timeout=120):
    """(код, вывод). Байты, не текст: в выводе системных команд бывает не-utf8."""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
    except Exception as e:
        return 255, str(e)


def human(n):
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} ПБ"


class Lock:
    """🔴 Один проход сборщика за раз. Два одновременных `run --apply` работали бы
    с одними файлами и разошлись бы в состояниях."""

    def __enter__(self):
        self.fd = os.open(LOCKFILE, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            raise SystemExit("другой проход сборщика уже идёт — выхожу")
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


# ── Control Plane ───────────────────────────────────────────────────────────

def cp(path, **payload):
    """Запрос к координатору. None — недоступен."""
    try:
        key = open(CP_KEYFILE).read().strip()
        req = urllib.request.Request(
            CP_API + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Api-Key": key})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return None


class Claim:
    """Захват ресурса на время работы — с ЖИВЫМ сердцебиением.

    🔴 Простого захвата мало: под ним идут проверки и перенос с таймаутами до
    900 секунд, а срок аренды 90. Аренда истекала бы прямо посреди работы,
    другой процесс получал бы новое поколение, а сборщик продолжал двигать
    каталог. Поэтому здесь фоновое продление и сверка ПОКОЛЕНИЯ; перед каждым
    необратимым шагом надо звать `verify()`, иначе шаг делать нельзя."""

    TICK = 15

    def __init__(self, res_uuid, path=None):
        # 🔴 Для путей внутри зоны арендуем ИМЕННО ЗОНУ: иначе сборщик держал бы
        # выдуманное имя, а исполнитель спокойно продолжал бы пользоваться своим
        # кэшем — конфликта между ними просто не возникало бы.
        if path and path.startswith(AGENTS_DIR + os.sep):
            agent = path[len(AGENTS_DIR) + 1:].split(os.sep)[0]
            self.res = "zone:" + agent
        else:
            self.res = "worktree:" + res_uuid
        self.token = None
        self.fencing = None
        self.error = ""
        self.lost = None
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        r = cp("/gc/claim", resource=self.res, task_id="gc",
               instance_id=str(os.getpid()), pid=os.getpid())
        if r is None:
            self.error = "🔴 координатор недоступен — действие запрещено"
            return self
        if not r.get("ok"):
            self.error = "🔴 " + str(r.get("причина"))
            return self
        self.token = r.get("lease_token")
        self.fencing = (r.get("fencing") or {}).get(self.res)
        self._thread = threading.Thread(target=self._beat, daemon=True)
        self._thread.start()
        return self

    def _beat(self):
        while not self._stop.wait(self.TICK):
            h = cp("/heartbeat", lease_token=self.token, agent_id="gc")
            if h is None:
                self.lost = "координатор пропал во время работы"
                return
            if not h.get("ok"):
                self.lost = str(h.get("причина"))
                return

    def verify(self):
        """Право ещё моё? Звать перед КАЖДЫМ необратимым шагом."""
        if not self.token:
            return False, self.error or "захвата нет"
        if self.lost:
            return False, "🔴 аренда потеряна: " + self.lost
        c = cp("/check", resource=self.res, lease_token=self.token,
               fencing_token=self.fencing)
        if c is None:
            return False, "🔴 координатор недоступен — шаг запрещён"
        if not c.get("allow"):
            return False, "🔴 " + str(c.get("причина"))
        return True, ""

    def __exit__(self, *a):
        self._stop.set()
        if self.token:
            cp("/release", lease_token=self.token)


def holds_now():
    """Удержания: множество идентификаторов ресурсов и путей.
    Возвращает (множество, доступен_ли_координатор). 🔴 Недоступен — второе
    False, и это запрещает любые удаления: неполный список удержаний хуже, чем
    никакого."""
    ok = True
    out = set()
    r = cp("/holds")
    if r is None or not r.get("ok"):
        ok = False
    else:
        for h in r["удержания"]:
            res = h["resource"]
            cls, _, rest = res.partition(":")
            out.add(rest if cls in ("worktree", "zone") else "/" + rest
                    if cls == "path" else res)
    try:
        with open(HOLD_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    out.add(line.rstrip("/"))
    except OSError:
        pass
    return out, ok


def on_hold(path, res_uuid, holds):
    if path.rstrip("/") in holds:
        return "в hold.txt"
    if res_uuid and res_uuid in holds:
        return "удержание в координаторе"
    if os.path.exists(os.path.join(path, HOLD_MARKER)):
        return f"маркер {HOLD_MARKER}"
    return None


# ── Диск ────────────────────────────────────────────────────────────────────

def disk_state():
    st = os.statvfs("/")
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used_pct = 100.0 * (1 - free / total)
    free_gb = free / (1024 ** 3)
    level, note = "ok", ""
    for pct, name, msg in LEVELS:
        if used_pct >= pct:
            level, note = name, msg
            break
    if free_gb < FLOOR_GB and level in ("ok", "warn"):
        level, note = "high", f"свободно меньше {FLOOR_GB} ГБ"
    return dict(used_pct=round(used_pct, 1), free_gb=round(free_gb, 1),
                level=level, note=note,
                allow_new_tasks=level in ("ok", "warn"),
                allow_new_agents=level not in ("critical", "emergency"),
                purge_now=level in ("critical", "emergency"),
                generated_at=now(), max_age_s=DISK_MAX_AGE_S, ts=now())


def write_disk_state(d):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = os.path.join(STATE_DIR, "disk.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(STATE_DIR, "disk.json"))


# ── База ────────────────────────────────────────────────────────────────────

SCHEMA = """
-- 🔴 Первичный ключ — uuid ЭКЗЕМПЛЯРА, а не путь: по одному пути со временем
-- живут разные ресурсы. С путём в ключе новый worktree на месте удалённого
-- наследовал бы состояние DELETED и не замечался бы сборщиком никогда.
CREATE TABLE IF NOT EXISTS resources(
    uuid TEXT PRIMARY KEY, path TEXT, kind TEXT, state TEXT, state_since INTEGER,
    first_seen INTEGER, size INTEGER, idle_h REAL, reason TEXT,
    quarantine_path TEXT, intended_path TEXT, generation INTEGER, live INTEGER);
CREATE INDEX IF NOT EXISTS res_live ON resources(path, live);

-- Время СПАСЕНИЯ ссылки. В самом git его нет: дата ссылки — это дата коммита,
-- и сегодня спасённый двухмесячный коммит выглядел бы двухмесячной ссылкой.
CREATE TABLE IF NOT EXISTS rescue(
    ref TEXT PRIMARY KEY, res_uuid TEXT, sha TEXT, rescued_at INTEGER);

CREATE TABLE IF NOT EXISTS events(
    ts INTEGER, uuid TEXT, path TEXT, frm TEXT, too TEXT, reason TEXT);
"""


def db():
    os.makedirs(ROOT, exist_ok=True)
    # 🔴 CREATE TABLE IF NOT EXISTS существующую таблицу НЕ меняет: база прошлой
    # версии осталась бы без uuid/live/generation, и код падал бы на ровном месте.
    # Несовместимую базу уводим в сторону и начинаем чистую.
    if os.path.exists(DB):
        try:
            probe = sqlite3.connect(DB, timeout=30)
            cols = {r[1] for r in probe.execute("PRAGMA table_info(resources)")}
            probe.close()
            if cols and not {"uuid", "live", "generation"} <= cols:
                # 🔴 Сначала слить WAL, иначе незаписанные изменения старой базы
                # пропадут; и унести sidecar-файлы вместе с ней, иначе они
                # останутся рядом с новой и будут считаться её журналом.
                # 🔴 Слияние журнала может вернуть «занято» БЕЗ исключения —
                # результат надо читать. По собственному правилу «не удалось
                # проверить — не трогаем» здесь прекращаем работу, а не
                # продолжаем с непонятной базой.
                try:
                    fix = sqlite3.connect(DB, timeout=30)
                    busy, _, _ = fix.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    fix.close()
                except sqlite3.Error as e:
                    raise SystemExit(f"🔴 старую базу не удалось прочитать ({e}) — "
                                     f"разберись вручную, ничего не трогаю")
                if busy:
                    raise SystemExit("🔴 старую базу держит другой процесс "
                                     "(журнал не слит) — ничего не трогаю")
                aside = DB + ".old-" + time.strftime("%Y%m%dT%H%M%S")
                os.replace(DB, aside)
                for suf in ("-wal", "-shm"):
                    if os.path.exists(DB + suf):
                        os.replace(DB + suf, aside + suf)
                print(f"старая база несовместима, отложена: {aside}")
                # 🔴 Журнал появился снова — значит старую базу кто-то ещё
                # держит открытой. Это не «мелочь»: рядом с новой базой окажется
                # чужой журнал, и она будет читаться неверно.
                stray = [DB + s for s in ("-wal", "-shm") if os.path.exists(DB + s)]
                if stray:
                    raise SystemExit(
                        "🔴 журнал старой базы воссоздан — её кто-то держит: "
                        + ", ".join(stray) + ". Ничего не трогаю.")
        except sqlite3.Error:
            pass
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
    return con


def to_state(con, uuid, new, reason, **fields):
    """Перевод состояния с НЕМЕДЛЕННОЙ фиксацией: следующий шаг на диске должен
    начинаться уже после того, как намерение записано."""
    row = con.execute("SELECT state, path FROM resources WHERE uuid=?", (uuid,)).fetchone()
    old = row[0] if row else "—"
    # 🔴 Состояние не изменилось — время перехода НЕ трогаем и событие не пишем.
    # Иначе обход каждые 15 минут заново ставил бы EXPIRED и обнулял его возраст:
    # ресурс не дозрел бы до карантина никогда, сборщик работал бы вхолостую.
    if old == new:
        sets, vals = ["reason=?"], [reason]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(v)
        vals.append(uuid)
        con.execute(f"UPDATE resources SET {', '.join(sets)} WHERE uuid=?", vals)
        con.commit()
        return False
    sets = ["state=?", "state_since=?", "reason=?"]
    vals = [new, now(), reason]
    for k, v in fields.items():
        sets.append(f"{k}=?")
        vals.append(v)
    vals.append(uuid)
    con.execute(f"UPDATE resources SET {', '.join(sets)} WHERE uuid=?", vals)
    con.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
                (now(), uuid, row[1] if row else "", old, new, reason))
    con.commit()


# ── Обход ───────────────────────────────────────────────────────────────────

ZONES_FILE = os.path.join(ROOT, "zones.txt")


def expected_zones():
    """Зоны, которые ДОЛЖНЫ существовать — из отдельного реестра, а не из того,
    что сейчас видно на диске.

    🔴 Иначе отвал тома зоны выглядит как «зоны больше нет»: точка монтирования
    остаётся, а `agent.env` вместе с содержимым исчезает, и обход считает себя
    полным. Реестр ведёт провижининг, сборщик его только читает.

    🔴 Файл обязателен. Его отсутствие или нечитаемость — НЕ «зон нет», а
    неизвестное состояние: возвращаем (список, False), и это запрещает apply.
    """
    out = []
    try:
        with open(ZONES_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    out.append(line)
    except OSError as e:
        print(f"🔴 реестр зон недоступен ({e}) — состояние неизвестно")
        return out, False
    if not out:
        # 🔴 Пустой реестр — не «зон нет», а не заполненная настройка. Считать
        # его нормой значит разрешить работу там, где состав зон неизвестен.
        print("🔴 реестр зон пуст — состав зон неизвестен")
        return out, False
    return out, True


def zone_ok(zone):
    """Зона на месте и это её собственный том? Возвращает (да, пояснение)."""
    if not os.path.isdir(zone):
        return False, "каталог зоны отсутствует"
    if not os.path.exists(os.path.join(zone, "agent.env")):
        return False, "нет agent.env — похоже, том отвалился"
    for sub in ("tmp", "cache", "work"):
        if not os.path.isdir(os.path.join(zone, sub)):
            return False, f"нет каталога {sub}"
    try:
        # у зоны свой том: устройство её каталога обязано отличаться от корня
        if os.stat(zone).st_dev == os.stat(AGENTS_DIR).st_dev:
            return False, "зона не на своём томе — том не смонтирован"
    except OSError as e:
        return False, f"не удалось проверить устройство: {e}"
    return True, ""


def roots():
    """Корни обхода. Возвращает (список, всё_ли_прочитано).

    🔴 Ошибку чтения каталога зон НЕЛЬЗЯ проглатывать: раньше при недоступном
    /srv/agents возвращались обычные корни и обход считался полным, хотя зоны
    не проверялись вообще. А если том зоны отвалился, её ресурсы ещё и выглядят
    исчезнувшими."""
    out = list(BASE_ROOTS)
    ok = True
    # Сначала — ожидаемые зоны из реестра: их отсутствие означает поломку,
    # а не «зоны больше нет».
    zones, registry_ok = expected_zones()
    if not registry_ok:
        ok = False
    known = {z.rstrip("/") for z in zones}
    for zone in zones:
        good, why = zone_ok(zone)
        if not good:
            print(f"🔴 ожидаемая зона {zone} не в порядке: {why}")
            ok = False
    if not os.path.isdir(AGENTS_DIR):
        return out, False
    try:
        names = sorted(os.listdir(AGENTS_DIR))
    except OSError:
        return out, False
    for name in names:
        zone = os.path.join(AGENTS_DIR, name)
        try:
            if not os.path.exists(os.path.join(zone, "agent.env")):
                continue
            # 🔴 Зона на диске есть, а в реестре её нет — это ошибка настройки,
            # а не «ещё одна зона». Работать при неизвестном составе нельзя.
            if zone.rstrip("/") not in known:
                print(f"🔴 зона {zone} не значится в реестре")
                ok = False
            for sub in ("tmp", "cache"):
                p = os.path.join(zone, sub)
                if os.path.isdir(p):
                    out.append((p, "tmp_generic"))
        except OSError:
            return out, False
    return out, ok


def roots_invariant_ok(rs):
    """Один путь — ровно один класс срока жизни."""
    kinds = {}
    for path, kind in rs:
        p = path.rstrip("/")
        if p in kinds and kinds[p] != kind:
            return False, f"{p} объявлен и как {kinds[p]}, и как {kind}"
        kinds[p] = kind
    declared = set(kinds)
    seen = {}
    for root, kind in rs:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            child = os.path.join(root, name).rstrip("/")
            if child in declared:
                continue
            if child in seen and seen[child] != kind:
                return False, f"{child} попадает под два разных срока"
            seen[child] = kind
    return True, f"учтено {len(seen)} путей"


def held_paths():
    """Пути, занятые живыми процессами: рабочий каталог, дескрипторы, отображения."""
    held = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        base = "/proc/" + pid
        try:
            held.add(os.readlink(base + "/cwd"))
        except OSError:
            pass
        try:
            for fd in os.listdir(base + "/fd"):
                try:
                    held.add(os.readlink(base + "/fd/" + fd))
                except OSError:
                    pass
        except OSError:
            pass
        try:
            with open(base + "/maps", "rb") as f:
                for line in f:
                    parts = line.split(b" ", 5)
                    if len(parts) == 6:
                        p = parts[5].strip().decode("utf-8", "replace")
                        if p.startswith("/"):
                            held.add(p)
        except OSError:
            pass
    return held


def is_held(path, held):
    pref = path.rstrip("/") + "/"
    return any(h == path or h.startswith(pref) for h in held)


def cgroup_busy(zone_path):
    agent = os.path.basename(zone_path.rstrip("/"))
    base = f"/sys/fs/cgroup/agent.slice/agent-{agent}.slice"
    if not os.path.isdir(base):
        return False
    for dirpath, _, _ in os.walk(base):
        try:
            with open(os.path.join(dirpath, "cgroup.procs")) as fh:
                if fh.read().strip():
                    return True
        except OSError:
            pass
    return False


def idle_hours(path):
    """Часы простоя. None — узнать не удалось (тогда ресурс не трогаем)."""
    try:
        own = (now() - os.lstat(path).st_mtime) / 3600.0
    except OSError:
        return None
    if not os.path.isdir(path) or os.path.islink(path):
        return own
    for hours in (1, 6, 24, 72, 168, 336, 720):
        code, out = sh(["find", path, "-name", ".git", "-prune", "-o",
                        "-mmin", f"-{hours * 60}", "-print", "-quit"], timeout=180)
        if code != 0:
            return None          # ошибка обхода ≠ «ничего свежего нет»
        if out.strip():
            return min(own, float(hours))
    return max(own, 720.0)


def dir_size(path):
    code, out = sh(["du", "-sb", "--one-file-system", path], timeout=300)
    if code != 0:
        return None
    try:
        return int(out.split("\t", 1)[0])
    except (ValueError, IndexError):
        return None


def scan(con):
    """Возвращает {'roots_ok': bool, 'failed': [...], 'read': n}."""
    all_roots, zones_ok = roots()
    ok_inv, why_inv = roots_invariant_ok(all_roots)
    if not ok_inv:
        return dict(roots_ok=False, failed=["инвариант: " + why_inv], read=0)

    held = held_paths()
    holds, holds_ok = holds_now()
    declared = {r[0].rstrip("/") for r in all_roots}
    failed, read_roots, seen_paths = [], [], set()
    if not zones_ok:
        failed.append(f"{AGENTS_DIR}: каталог зон не прочитан")

    for root, kind in all_roots:
        try:
            names = sorted(os.listdir(root))
        except OSError as e:
            failed.append(f"{root}: {e.strerror}")
            continue
        read_roots.append(root)
        for name in names:
            path = os.path.join(root, name)
            if any(path.startswith(n) for n in NEVER) or path.rstrip("/") in declared:
                continue
            if path in seen_paths:
                continue
            seen_paths.add(path)

            if kind == "worktree":
                # В домашнем каталоге трогаем ТОЛЬКО рабочие копии репозитория:
                # ключи, настройки и задания сборщику не принадлежат.
                if not os.path.isdir(path) or not os.path.exists(os.path.join(path, ".git")):
                    continue

            idle = idle_hours(path)
            # 🔴 Размер обычного файла берём через lstat: раньше здесь стоял
            # только обход каталога, и любой ФАЙЛ в /tmp получал size=None,
            # после чего молча выпадал из учёта — то есть не убирался никогда.
            if os.path.isdir(path) and not os.path.islink(path):
                size = dir_size(path)
            else:
                try:
                    size = os.lstat(path).st_size
                except OSError:
                    size = None
            if idle is None or size is None:
                continue          # не смогли измерить — не считаем ничего

            # 🔴 Владелец исходного пути — только экземпляр, который на нём и
            # лежит. Карантинный остаётся live ради своей уборки, но путь больше
            # не занимает: иначе новый worktree на старом месте не был бы замечен.
            row = con.execute(
                "SELECT uuid, state FROM resources WHERE path=? AND live=1 "
                "AND state IN ('ACTIVE','EXPIRED','QUARANTINING')", (path,)).fetchone()
            if row:
                uid, state = row
                con.execute("UPDATE resources SET kind=?, size=?, idle_h=? WHERE uuid=?",
                            (kind, size, idle, uid))
            else:
                uid, state = uuidlib.uuid4().hex, "ACTIVE"
                gen = con.execute("SELECT COUNT(*) FROM resources WHERE path=?",
                                  (path,)).fetchone()[0] + 1
                con.execute("INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (uid, path, kind, "ACTIVE", now(), now(), size, idle,
                             "", None, None, gen, 1))
            con.commit()

            if state not in ("ACTIVE", "EXPIRED"):
                continue

            pol = KINDS[kind]
            zone = None
            if path.startswith(AGENTS_DIR + os.sep):
                zone = os.path.join(AGENTS_DIR,
                                    path[len(AGENTS_DIR) + 1:].split(os.sep)[0])
            hold = on_hold(path, uid, holds)
            if not holds_ok:
                to_state(con, uid, "ACTIVE", "🔴 список удержаний неполон — не трогаю")
            elif zone and cgroup_busy(zone):
                to_state(con, uid, "ACTIVE", "группа исполнителя не пуста")
            elif hold:
                to_state(con, uid, "ACTIVE", f"🔴 удержание: {hold}")
            elif is_held(path, held):
                to_state(con, uid, "ACTIVE", "внутри живые процессы")
            elif idle < pol["ttl_h"]:
                to_state(con, uid, "ACTIVE", f"простой {idle:.0f} ч < {pol['ttl_h']} ч")
            else:
                to_state(con, uid, "EXPIRED", f"простой {idle:.0f} ч, процессов нет")

    # 🔴 Исчезнувшим считаем ТОЛЬКО то, что лежало под успешно прочитанным корнем.
    # Недоступный корень раньше означал «все ресурсы под ним удалены».
    for uid, path, state, qpath in con.execute(
            "SELECT uuid, path, state, quarantine_path FROM resources WHERE live=1"):
        under_read = any(path.startswith(r.rstrip("/") + "/") for r in read_roots)
        if state in ("QUARANTINING", "QUARANTINED", "PURGEABLE"):
            # у карантина настоящий путь — второй; исходного и не должно быть
            if qpath and os.path.exists(qpath):
                continue
            if not qpath:
                continue
            # 🔴 Отсутствие карантинного пути НЕ означает, что его удалили:
            # у зоны исполнителя отдельный том, и при его отвале разом «исчезли
            # бы» все её ресурсы. Пока корень карантина недоступен — не решаем.
            if not os.path.isdir(os.path.dirname(qpath)):
                continue
            to_state(con, uid, "DELETED", "карантинный каталог исчез", live=0)
            continue
        if state in ("ACTIVE", "EXPIRED") and under_read and not os.path.exists(path):
            to_state(con, uid, "DELETED", "исчез сам", live=0)

    return dict(roots_ok=not failed, failed=failed, read=len(read_roots))


# ── Проверки рабочей копии ──────────────────────────────────────────────────

def safe_location(path):
    real = os.path.realpath(path)
    if real != os.path.abspath(path) or os.path.islink(path):
        return False, "путь не канонический или является ссылкой"
    zone_roots, _ = roots()          # roots() теперь возвращает (список, полнота)
    allowed = list(ALLOWED_ROOTS) + [z[0] for z in zone_roots
                                     if z[0].startswith(AGENTS_DIR + "/")]
    if not any(real == r or real.startswith(r.rstrip("/") + "/") for r in allowed):
        return False, f"вне разрешённых корней: {real}"
    code, out = sh(["findmnt", "-rn", "-o", "TARGET", "-R", real], timeout=60)
    if code not in (0, 1):            # 1 = ничего не найдено, это норма
        return False, "не удалось проверить точки монтирования"
    if [m for m in out.split() if m and m != real]:
        return False, "внутри точка монтирования"
    if os.path.ismount(real):
        return False, "сам является точкой монтирования"
    return True, ""


def validate_worktree(path):
    """🔴 ТОЛЬКО ЧТЕНИЕ. Ничего не создаёт и не меняет — иначе режим показа
    правил бы архив: раньше он создавал настоящие спасательные ссылки каждые
    15 минут. Любая непройденная проверка означает «не удалять»."""
    ok, why = safe_location(path)
    if not ok:
        return False, why
    if not os.path.exists(os.path.join(path, ".git")):
        return False, "не рабочая копия"

    code, out = sh(["git", "-C", path, "status", "--porcelain"])
    if code != 0:
        return False, "git status не отработал"
    if out.strip():
        return False, f"незакоммиченные файлы: {len(out.strip().splitlines())}"

    code, out = sh(["git", "-C", path, "submodule", "status", "--recursive"], timeout=300)
    if code != 0:
        return False, "не удалось проверить подмодули"
    if [l for l in out.splitlines() if l[:1] in ("+", "U", "-")]:
        return False, "подмодули не в порядке"

    code, out = sh(["find", path, "-mindepth", "2", "-name", ".git",
                    "-not", "-path", f"{path}/.git/*", "-print", "-quit"], timeout=300)
    if code != 0:
        return False, "не удалось проверить вложенные репозитории"
    if out.strip():
        return False, "внутри вложенный репозиторий"

    code, out = sh(["git", "-C", path, "ls-files", ":(attr:filter=lfs)"], timeout=300)
    if code != 0:
        return False, "не удалось проверить LFS"
    if out.strip():
        return False, (f"{len(out.strip().splitlines())} файлов LFS — "
                       f"спасательная ссылка их содержимое не сохраняет")

    code, out = sh(["git", "-C", path, "ls-files", "--others", "--ignored",
                    "--exclude-standard", "--directory"], timeout=300)
    if code != 0:
        return False, "не удалось перечислить игнорируемые файлы"
    # 🔴 Сравниваем ЦЕЛЫЕ составляющие пути. По концу строки `customer-dist`
    # и `important-prebuild` считались одноразовыми `dist` и `build` — и копия
    # с ними была бы удалена как «ничего ценного».
    unknown = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = [x for x in line.rstrip("/").split("/") if x]
        if not any(x in DISPOSABLE for x in parts):
            unknown.append(line)
    if unknown:
        return False, f"игнорируемые файлы неизвестного назначения: {unknown[0]}"

    code, head = sh(["git", "-C", path, "rev-parse", "HEAD"])
    if code != 0:
        return False, "не удалось получить HEAD"
    return True, head.strip()


def store_sane():
    """Архив пригоден как последнее хранилище? Проверяем ПРОГРАММНО, а не
    полагаемся на то, что кто-то настроил сервер правильно."""
    alt = os.path.join(STORE, "objects", "info", "alternates")
    if os.path.exists(alt):
        return False, "архив сам ссылается наружу через alternates"
    for key, want in (("gc.auto", "0"), ("gc.pruneExpire", "never")):
        code, out = sh(["git", "-C", STORE, "config", "--get", key])
        if code != 0 or out.strip() != want:
            return False, f"в архиве не выставлено {key}={want}"
    return True, ""


def rescue(con, path, res_uuid, head):
    """🔴 Пишет в архив. Вызывается ТОЛЬКО под захватом и только при --apply."""
    ok, why = store_sane()
    if not ok:
        return None, why
    ref = (f"refs/rescue/gc/{res_uuid}/"
           f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}")
    code, out = sh(["git", "-C", STORE, "fetch", "--no-tags", path, f"+HEAD:{ref}"],
                   timeout=900)
    if code != 0:
        return None, f"спасение не удалось: {out.strip()[:120]}"
    code, have = sh(["git", "-C", STORE, "rev-parse", "--verify", "--quiet", ref])
    if code != 0 or have.strip() != head:
        return None, "ссылка не совпала с HEAD"
    code, listing = sh(["git", "-C", STORE, "rev-list", "--objects",
                        "--no-object-names", head], timeout=900)
    if code != 0:
        return None, "обход объектов архива не прошёл"
    code, present = sh(["git", "-C", STORE, "cat-file", "--batch-check=%(objectname)",
                        "--batch-all-objects", "--unordered"], timeout=900)
    if code != 0:
        return None, "не удалось перечислить объекты архива"
    have_objs = set(present.split())
    missing = [o for o in listing.split() if o and o not in have_objs]
    if missing:
        return None, f"в архиве не хватает {len(missing)} объектов"
    con.execute("INSERT OR REPLACE INTO rescue VALUES(?,?,?,?)",
                (ref, res_uuid, head, now()))
    con.commit()
    return ref, ""


def parent_repo(path):
    """Каталог, из которого управляют этой рабочей копией.

    🔴 Для ГОЛОГО репозитория (наш случай: общий архив и личные репозитории
    исполнителей) это сам общий каталог, а не его родитель. Раньше здесь всегда
    бралcя `dirname`, и `git worktree move` падал с «not a git repository» —
    то есть карантин не работал вовсе. Поймано регрессией."""
    code, out = sh(["git", "-C", path, "rev-parse", "--git-common-dir"])
    if code != 0:
        return None
    common = out.strip()
    if not common:
        return None
    if not os.path.isabs(common):
        common = os.path.join(path, common)
    common = os.path.realpath(common)
    if os.path.basename(common) == ".git":
        return os.path.dirname(common) or None      # обычный репозиторий
    return common                                    # голый репозиторий


def quarantine_root(path):
    """🔴 Карантин обязан лежать на ТОЙ ЖЕ файловой системе. У исполнителя своя
    (отдельный том с квотой), и перенос в общий карантин упёрся бы в EXDEV —
    то есть уборка зоны не работала бы вовсе."""
    if path.startswith(AGENTS_DIR + os.sep):
        zone = os.path.join(AGENTS_DIR, path[len(AGENTS_DIR) + 1:].split(os.sep)[0])
        return os.path.join(zone, ".quarantine")
    return QUARANTINE


def qpath_for(res_uuid, path=None):
    """Имя в карантине — по неизменяемому идентификатору. Раньше имя строилось
    из пути с заменой разделителей, и `/tmp/a_b` с `/tmp/a/b` давали одно имя:
    второй ресурс затирал карантин первого."""
    return os.path.join(quarantine_root(path or ""), res_uuid)


# ── Переходы ────────────────────────────────────────────────────────────────

def recheck_free(path, zone_ok=True):
    """Повторная проверка занятости — уже ПОД захватом. Снимок процессов,
    сделанный до захвата, к моменту переноса успевает устареть."""
    held = held_paths()
    if is_held(path, held):
        return False, "внутрь вошёл живой процесс"
    if path.startswith(AGENTS_DIR + os.sep):
        zone = os.path.join(AGENTS_DIR, path[len(AGENTS_DIR) + 1:].split(os.sep)[0])
        if cgroup_busy(zone):
            return False, "группа исполнителя не пуста"
    return True, ""


def do_quarantine(con, uid, path, kind, head):
    """EXPIRED → QUARANTINING (фиксация) → перенос → QUARANTINED (фиксация)."""
    dst = qpath_for(uid, path)
    if os.path.exists(dst):
        return False, "карантинное имя занято — не трогаю чужое"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    to_state(con, uid, "QUARANTINING", "намерение записано", intended_path=dst)

    if kind == "worktree":
        parent = parent_repo(path)
        if not parent or not os.path.isdir(parent):
            to_state(con, uid, "ACTIVE", "🔴 родительский репозиторий не найден")
            return False, "родительский репозиторий не найден"
        code, out = sh(["git", "-C", parent, "worktree", "move", path, dst], timeout=900)
        if code != 0:
            to_state(con, uid, "ACTIVE", f"git отказал в переносе: {out.strip()[:80]}")
            return False, "git отказал в переносе"
    else:
        # 🔴 Обычный каталог тоже обязан пройти проверку расположения: ссылка
        # или точка монтирования внутри уводит перенос куда угодно.
        ok, why = safe_location(path)
        if not ok:
            to_state(con, uid, "ACTIVE", "🔴 " + why)
            return False, why
        try:
            os.rename(path, dst)
        except OSError as e:
            to_state(con, uid, "ACTIVE", f"перенос не удался: {e}")
            return False, str(e)

    to_state(con, uid, "QUARANTINED", "перенесён в карантин", quarantine_path=dst)
    return True, dst


def do_purge(con, uid, kind, qpath):
    """QUARANTINED → PURGEABLE (фиксация) → удаление → DELETED (фиксация)."""
    to_state(con, uid, "PURGEABLE", "намерение удалить записано")
    if kind == "worktree":
        parent = parent_repo(qpath)
        if not parent or not os.path.isdir(parent):
            # 🔴 rmtree здесь запрещён: это настоящая рабочая копия с
            # повреждённой учётной записью — случай для расследования.
            to_state(con, uid, "QUARANTINED",
                     "🔴 родитель не найден, удалять отказываюсь", quarantine_path=qpath)
            return False, "родитель не найден"
        code, out = sh(["git", "-C", parent, "worktree", "remove", qpath], timeout=900)
        if code != 0:
            to_state(con, uid, "QUARANTINED", f"🔴 git отказал: {out.strip()[:80]}",
                     quarantine_path=qpath)
            return False, "git отказал"
    else:
        try:
            if os.path.islink(qpath) or os.path.isfile(qpath):
                os.unlink(qpath)
            elif os.path.isdir(qpath):
                shutil.rmtree(qpath)
        except OSError as e:
            to_state(con, uid, "QUARANTINED", f"удаление не удалось: {e}",
                     quarantine_path=qpath)
            return False, str(e)
    if os.path.exists(qpath):
        to_state(con, uid, "QUARANTINED", "🔴 после удаления путь на месте",
                 quarantine_path=qpath)
        return False, "путь остался"
    to_state(con, uid, "DELETED", "удалён из карантина", live=0)
    return True, ""


def in_scope(uid, path, kind, sel):
    """Попадает ли ресурс под ограничения запуска.

    🔴 Без этого «сначала только зоны» технически невозможно: один ручной
    запуск ради двух пробных ресурсов перенёс бы и удалил всё созревшее.
    """
    if sel.get("only_uuid") and uid not in sel["only_uuid"]:
        return False
    scope = sel.get("scope", "all")
    in_zone = path.startswith(AGENTS_DIR + os.sep)
    if scope == "zones":
        return in_zone and kind != "worktree"
    if scope == "worktrees":
        return kind == "worktree"
    return True


def recover(con, sel=None):
    """При точечном запуске разбирает ТОЛЬКО указанные ресурсы: иначе ручная
    команда ради одного canary-каталога чинила бы состояния всех остальных."""
    sel = sel or {}
    only = sel.get("only_uuid") or None
    # 🔴 Область тоже обязательна: запуск таймера с областью zones не должен
    # чинить состояния рабочих копий, которые в эту область не входят.
    scope = sel.get("scope", "all")
    """Разобрать зависшие переходы после падения: файловая система и база могли
    разойтись ровно между фиксацией намерения и действием."""
    fixed = []
    for uid, path, kind, ip, qp in con.execute(
            "SELECT uuid, path, kind, intended_path, quarantine_path FROM resources "
            "WHERE state='QUARANTINING'").fetchall():
        if not in_scope(uid, path, kind,
                        {"only_uuid": only or set(), "scope": scope}):
            continue
        # 🔴 Недоступный карантин НЕ значит «перенесено и удалено». Пока корень
        # карантина не читается, решение не принимаем вообще.
        qroot = os.path.dirname(ip) if ip else quarantine_root(path)
        if not os.path.isdir(qroot):
            fixed.append((uid, path, "🔴 карантин недоступен — решение отложено"))
            continue
        here, there = os.path.exists(path), bool(ip) and os.path.exists(ip)
        if here and there:
            # 🔴 Есть оба — это не «выбрать карантин», а повод разобраться:
            # одно из двух может быть чужим или недокопированным.
            to_state(con, uid, "QUARANTINING",
                     "🔴 существуют оба пути — разобрать вручную")
            fixed.append((uid, path, "🔴 существуют оба пути"))
        elif there:
            to_state(con, uid, "QUARANTINED", "восстановлено: перенос состоялся",
                     quarantine_path=ip)
            fixed.append((uid, path, "перенос состоялся"))
        elif here:
            to_state(con, uid, "ACTIVE", "восстановлено: перенос не состоялся")
            fixed.append((uid, path, "перенос не состоялся"))
        else:
            # 🔴 Ни одного пути. В ACTIVE переводить НЕЛЬЗЯ: на этом месте мог
            # появиться уже другой ресурс, и старая строка склеилась бы с ним.
            to_state(con, uid, "DELETED", "🔴 оба пути исчезли — экземпляр закрыт",
                     live=0)
            fixed.append((uid, path, "🔴 оба пути исчезли — закрыт"))
    for uid, path, kind, qp in con.execute(
            "SELECT uuid, path, kind, quarantine_path FROM resources "
            "WHERE state='PURGEABLE'").fetchall():
        if not in_scope(uid, path, kind,
                        {"only_uuid": only or set(), "scope": scope}):
            continue
        qroot = os.path.dirname(qp) if qp else quarantine_root(path)
        if not os.path.isdir(qroot):
            fixed.append((uid, path, "🔴 карантин недоступен — решение отложено"))
            continue
        if qp and os.path.exists(qp):
            to_state(con, uid, "QUARANTINED", "восстановлено: удаление не состоялось",
                     quarantine_path=qp)
            fixed.append((uid, path, "удаление не состоялось"))
        else:
            to_state(con, uid, "DELETED", "восстановлено: удаление состоялось", live=0)
            fixed.append((uid, path, "удаление состоялось"))
    return fixed


def advance(con, disk, apply_, scan_res, sel=None):
    sel = sel or {}
    acted = []
    holds, holds_ok = holds_now()
    if apply_ and not holds_ok:
        return [("отказ", "—", "🔴 координатор недоступен: удержания неизвестны", 0)]
    if apply_ and not scan_res["roots_ok"]:
        return [("отказ", "—",
                 f"🔴 корни не прочитаны: {'; '.join(scan_res['failed'])}", 0)]

    speed = 0.5 if disk["purge_now"] else 1.0

    for uid, path, kind, since, size in con.execute(
            "SELECT uuid, path, kind, state_since, size FROM resources "
            "WHERE state='EXPIRED' AND live=1").fetchall():
        if not in_scope(uid, path, kind, sel):
            continue
        pol = KINDS[kind]
        need = max(pol["grace_h"] * speed, pol["min_h"]) * 3600
        if now() - since < need:
            continue
        hold = on_hold(path, uid, holds)
        if hold:
            to_state(con, uid, "ACTIVE", f"🔴 удержание: {hold}")
            continue
        if apply_ and report_only(path):
            to_state(con, uid, "EXPIRED",
                     "🔴 общий /tmp: владельца нет, аренду взять не у кого — "
                     "только показ")
            acted.append(("только показ", path, "общий /tmp не убираю", size))
            continue
        if not apply_:
            ok, why = (validate_worktree(path) if kind == "worktree" else (True, ""))
            acted.append(("показ", path,
                          "в карантин" if ok else f"НЕ трогаю: {why}", size))
            continue

        with Claim(uid, path) as cl:
            if not cl.token:
                to_state(con, uid, "ACTIVE", cl.error)
                continue
            fresh, ok_h = holds_now()          # удержание могло появиться уже
            if not ok_h:                        # после первого снимка
                to_state(con, uid, "ACTIVE", "🔴 удержания недоступны")
                continue
            h2 = on_hold(path, uid, fresh)
            if h2:
                to_state(con, uid, "ACTIVE", f"🔴 удержание: {h2}")
                continue
            free, why = recheck_free(path)
            if not free:
                to_state(con, uid, "ACTIVE", "🔴 " + why)
                continue
            head = ""
            if kind == "worktree":
                ok, res = validate_worktree(path)
                if not ok:
                    to_state(con, uid, "ACTIVE", "🔴 " + res)
                    continue
                ok, why = cl.verify()          # проверки долгие — право могло уйти
                if not ok:
                    to_state(con, uid, "ACTIVE", why)
                    continue
                head = res
                ref, err = rescue(con, path, uid, head)
                if not ref:
                    to_state(con, uid, "ACTIVE", "🔴 " + err)
                    continue
            ok, why = cl.verify()              # последний рубеж перед переносом
            if not ok:
                to_state(con, uid, "ACTIVE", why)
                continue
            ok, res = do_quarantine(con, uid, path, kind, head)
            acted.append(("в карантин" if ok else "не вышло", path, res, size))

    for uid, path, kind, since, size, qp in con.execute(
            "SELECT uuid, path, kind, state_since, size, quarantine_path FROM resources "
            "WHERE state='QUARANTINED' AND live=1").fetchall():
        if not in_scope(uid, path, kind, sel):
            continue
        pol = KINDS[kind]
        need = max(pol["purge_h"] * speed, pol["min_h"]) * 3600
        if now() - since < need:
            continue
        hold = on_hold(qp or path, uid, holds)
        if hold:
            to_state(con, uid, "QUARANTINED", f"🔴 удержание: {hold}",
                     quarantine_path=qp)
            continue
        # 🔴 Тот же страж, что и перед карантином. Строка в состоянии
        # QUARANTINED могла появиться от прошлой версии, восстановления или
        # вручную — и её исходный путь может лежать в общем /tmp, где владельца
        # нет. Без этой проверки такой ресурс удалялся бы, минуя запрет.
        if apply_ and report_only(path):
            to_state(con, uid, "QUARANTINED",
                     "🔴 исходный путь в общем /tmp — только показ",
                     quarantine_path=qp)
            acted.append(("только показ", qp or path, "общий /tmp не удаляю", size))
            continue
        if not apply_:
            acted.append(("показ", qp or path, "удалить из карантина", size))
            continue
        # 🔴 Захват нужен и перед окончательным удалением, а не только перед
        # карантином: за время выдержки ресурс мог понадобиться снова.
        with Claim(uid, qp or path) as cl:
            if not cl.token:
                to_state(con, uid, "QUARANTINED", cl.error, quarantine_path=qp)
                continue
            # В карантине тоже мог кто-то оказаться: смотрим ещё раз.
            fresh, ok_h = holds_now()
            if not ok_h:
                to_state(con, uid, "QUARANTINED", "🔴 удержания недоступны",
                         quarantine_path=qp)
                continue
            h2 = on_hold(qp or path, uid, fresh)
            if h2:
                to_state(con, uid, "QUARANTINED", f"🔴 удержание: {h2}",
                         quarantine_path=qp)
                continue
            free, why = recheck_free(qp or path)
            if not free:
                to_state(con, uid, "QUARANTINED", "🔴 " + why, quarantine_path=qp)
                continue
            ok, why = cl.verify()
            if not ok:
                to_state(con, uid, "QUARANTINED", why, quarantine_path=qp)
                continue
            ok, res = do_purge(con, uid, kind, qp)
            acted.append(("удалено" if ok else "оставлено", qp or path, res, size))

    return acted


def rescue_reconcile(con):
    """🔴 Сверка архива с базой. Падение между `git fetch` и записью строки
    оставляет ссылку в git навсегда: под тридцатидневную уборку она не попадёт,
    потому что о ней никто не знает. Такие ссылки НЕ удаляем автоматически —
    заводим с неизвестной датой и показываем для разбора."""
    code, out = sh(["git", "-C", STORE, "for-each-ref", "--format=%(refname)",
                    "refs/rescue"], timeout=300)
    if code != 0:
        return []
    known = {r[0] for r in con.execute("SELECT ref FROM rescue")}
    orphans = []
    for ref in out.split():
        if ref in known:
            continue
        code, sha = sh(["git", "-C", STORE, "rev-parse", ref])
        con.execute("INSERT OR REPLACE INTO rescue VALUES(?,?,?,?)",
                    (ref, "", sha.strip() if code == 0 else "", 0))
        orphans.append(ref)
    if orphans:
        con.commit()
    return orphans


def rescue_cleanup(con, apply_, sel=None):
    sel = sel or {}
    if sel.get("skip_rescue"):
        return [("пропущено", "refs/rescue", "чистка ссылок отключена флагом", 0)]
    """Спасательные ссылки живут 30 дней ОТ ДАТЫ СПАСЕНИЯ (она в базе: у git
    даты создания ссылки нет, а дата коммита к спасению отношения не имеет)."""
    holds, holds_ok = holds_now()
    if not holds_ok:
        return [("отказ", "—", "🔴 координатор недоступен — ссылки не трогаю", 0)]
    limit = now() - RESCUE_KEEP_DAYS * 86400
    gone = []
    for ref, res_uuid, at in con.execute(
            "SELECT ref, res_uuid, rescued_at FROM rescue "
            "WHERE rescued_at > 0 AND rescued_at < ?", (limit,)).fetchall():
        if sel.get("only_uuid") and res_uuid not in sel["only_uuid"]:
            continue
        if res_uuid in holds or ref in holds:
            continue
        # 🔴 Ссылка — последняя копия работы. Удалять её можно, только когда сам
        # ресурс ДЕЙСТВИТЕЛЬНО удалён: если копия зависла в карантине из-за
        # отказа удаления, ссылка обязана её пережить.
        row = con.execute("SELECT state FROM resources WHERE uuid=?",
                          (res_uuid,)).fetchone()
        # 🔴 Нет строки ресурса — тем более не удаляем: это значит, что учёт
        # потерян (частично восстановленная база), а ссылка может быть последней
        # копией работы. Раньше здесь было `row and ...`, то есть отсутствие
        # строки РАЗРЕШАЛО удаление.
        if not row or row[0] != "DELETED":
            continue
        days = int((now() - at) / 86400)
        if not apply_:
            gone.append(("спасённое", ref, f"хранилось {days} дней", 0))
            continue
        # 🔴 Захват и ПОВТОРНАЯ проверка удержаний прямо перед удалением:
        # удержание могло появиться уже после первого снимка.
        with Claim(res_uuid or ref) as cl:
            if not cl.token:
                continue
            fresh, ok_h = holds_now()
            if not ok_h or res_uuid in fresh or ref in fresh:
                continue
            ok, _ = cl.verify()
            if not ok:
                continue
            code, _ = sh(["git", "-C", STORE, "update-ref", "-d", ref], timeout=120)
            if code != 0:
                continue
            con.execute("DELETE FROM rescue WHERE ref=?", (ref,))
            con.commit()
        gone.append(("спасённое", ref, f"хранилось {days} дней", 0))
    return gone


# ── Вывод ───────────────────────────────────────────────────────────────────

def cmd_report(con, scan_res=None):
    d = disk_state()
    print(f"Диск: занято {d['used_pct']}%, свободно {d['free_gb']} ГБ · "
          f"уровень {d['level'].upper()} {d['note']}")
    print(f"  новые задачи: {'да' if d['allow_new_tasks'] else 'НЕТ'} · "
          f"новые исполнители: {'да' if d['allow_new_agents'] else 'НЕТ'}")
    _, holds_ok = holds_now()
    print(f"  координатор: {'доступен' if holds_ok else '🔴 НЕДОСТУПЕН — удаление запрещено'}")
    if scan_res and not scan_res["roots_ok"]:
        print(f"  🔴 корни не прочитаны: {'; '.join(scan_res['failed'])}")
    print()
    rows = con.execute("SELECT state, kind, COUNT(*), SUM(size) FROM resources "
                       "WHERE live=1 GROUP BY state, kind ORDER BY SUM(size) DESC")
    print(f"{'состояние':<14}{'вид':<16}{'шт':>5}{'объём':>12}")
    for state, kind, n, sz in rows:
        print(f"{state:<14}{kind:<16}{n:>5}{human(sz):>12}")
    n = con.execute("SELECT COUNT(*) FROM rescue").fetchone()[0]
    print(f"\nспасательных ссылок: {n} (хранятся {RESCUE_KEEP_DAYS} дней от спасения)")
    rows = con.execute("SELECT path, reason FROM resources WHERE reason LIKE '🔴%' "
                       "AND live=1 LIMIT 10").fetchall()
    if rows:
        print("\n🔴 Удерживается проверками (НЕ удалено):")
        for path, reason in rows:
            print(f"  {path}\n     {reason}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan", "run", "report", "disk", "recover"])
    ap.add_argument("--apply", action="store_true", help="реально выполнять переходы")
    ap.add_argument("--only-uuid", action="append", default=[],
                    help="только эти ресурсы (можно повторять) — для canary")
    ap.add_argument("--scope", choices=["zones", "worktrees", "all"], default="all",
                    help="zones = только tmp/cache зон исполнителей")
    ap.add_argument("--skip-rescue-cleanup", action="store_true",
                    help="не трогать спасательные ссылки в этом запуске")
    a = ap.parse_args()
    sel = {"only_uuid": set(a.only_uuid), "scope": a.scope,
           "skip_rescue": a.skip_rescue_cleanup}

    d = disk_state()
    write_disk_state(d)
    if a.cmd == "disk":
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0

    with Lock():
        con = db()
        fixed = recover(con, sel)
        if fixed:
            print(f"восстановлено зависших переходов: {len(fixed)}")
            for uid, path, what in fixed:
                print(f"  {path} — {what}")
        if a.cmd == "recover":
            return 0

        scan_res = None
        if a.cmd in ("scan", "run"):
            scan_res = scan(con)
            if not scan_res["roots_ok"]:
                print(f"🔴 корни прочитаны не полностью: {'; '.join(scan_res['failed'])}")
        if a.cmd == "run":
            orphans = rescue_reconcile(con)
            if orphans:
                print(f"🔴 спасательных ссылок без записи в базе: {len(orphans)} — "
                      f"автоматически НЕ удаляются, разобрать вручную")
                for r in orphans[:5]:
                    print("   ", r)
            if a.apply and (sel["only_uuid"] or a.scope != "all"):
                print(f"область: scope={a.scope}"
                      + (f", только {len(sel['only_uuid'])} ресурсов"
                         if sel["only_uuid"] else ""))
            acted = advance(con, d, a.apply, scan_res, sel)
            acted += rescue_cleanup(con, a.apply, sel)
            # честно: показ обновляет учёт и состояние диска, но ничего
            # не переносит и не удаляет
            head = ("СДЕЛАНО" if a.apply else
                    "ПОКАЗ (учёт обновляется; ничего не переносится и не удаляется)")
            print(f"{head}: {len(acted)}")
            for what, path, why, size in acted[:40]:
                print(f"  {what:<12} {human(size):>9}  {path}\n               {why}")
        if a.cmd in ("report", "run"):
            print()
            cmd_report(con, scan_res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
