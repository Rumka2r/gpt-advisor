#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Строгий контракт задачи — шаг 1 порядка архитектора (06.08.2026).

Зачем: раньше `tasks.contract` был произвольным текстом, который никто не
проверял, а `/acquire` брал ресурсы со слов клиента, не сверяя их ни с задачей,
ни с исполнителем. То есть «задача» ничего не обязывала и ничего не доказывала.

🔴 Инварианты:
 · контракт НЕИЗМЕНЯЕМ; правка создаёт новую версию, старая остаётся в истории;
 · версия и отпечаток контракта пишутся в событие получения аренды;
 · запрашиваемые ресурсы обязаны СОВПАДАТЬ с ресурсами активного контракта;
 · аренду получает только назначенный исполнитель и только в рабочем состоянии;
 · произвольный переход задачи в «готово» запрещён — только через передачу.

Живёт в той же базе, что аренды: контракт, продукты и передача обязаны меняться
одной транзакцией, иначе получим «передача записана, а задача не переведена».
"""

import hashlib
import json
import re
import time

import product_policy

SCHEMA_VERSION = 1

# Состояния задачи и разрешённые переходы. 🔴 Прямого пути в «готово» нет:
# завершает задачу только принятая передача результата.
STATES = ("created", "assigned", "running", "handoff_pending",
          "done", "blocked", "cancelled")
TRANSITIONS = {
    "created": {"assigned", "cancelled"},
    "assigned": {"running", "blocked", "cancelled"},
    "running": {"handoff_pending", "blocked", "cancelled"},
    # 🔴 Из ожидания решения — только приём или отказ. Раньше отсюда можно было
    # уйти в blocked и обратно в assigned, то есть отправитель сам отменял бы
    # передачу, минуя получателя.
    "handoff_pending": {"done", "assigned"},
    # 🔴 Из блокировки возвращаемся в assigned, а не сразу в работу: running
    # выставляет только успешный захват аренды. Раньше blocked был тупиком —
    # выйти из него можно было лишь отменой задачи.
    "blocked": {"assigned", "cancelled"},
    "done": set(),
    "cancelled": set(),
}
WORKABLE = ("assigned", "running")

KINDS = ("git_commit", "git_ref", "object", "report", "dataset", "config")
LOCATOR_TYPES = ("git", "object_storage", "path")

# 🔴 Неизменяемые виды расположения. Обычный путь на диске закрыть обязательный
# результат не может: файл на диске завтра будет другим, и доказать, что задача
# произвела именно этот результат, нечем.
IMMUTABLE_LOCATORS = ("git", "object_storage")

# 🔴 Списки разрешённых ключей. Без них опечатка `cheks` тихо проходит как
# отсутствие проверок, и реестр продуктов решит, что контракт их не требовал.
KEYS_CONTRACT = {"schema_version", "objective", "assignee", "resources",
                 "inputs", "outputs", "constraints", "handoff_to"}
KEYS_OUTPUT = {"slot", "kind", "required", "checks"}
KEYS_INPUT = {"name", "locator"}
KEYS_CONSTRAINTS = {"forbidden_actions", "deadline"}

NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def norm_name(x):
    """Имя слота или проверки: без краевых пробелов, в нижнем регистре.
    Иначе `result` и ` result ` — разные слоты, а `tests` и ` tests ` — разные
    проверки, и контракт перестаёт что-либо гарантировать."""
    return " ".join(str(x).split()).strip().lower()


def unknown_keys(obj, allowed, where, errs):
    extra = sorted(set(obj) - allowed)
    if extra:
        errs.append(f"{where}: неизвестные поля {', '.join(extra)} "
                    f"(опечатка? допустимы: {', '.join(sorted(allowed))})")


def now():
    return int(time.time())


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(body):
    """Каноническая запись: отпечаток не должен зависеть от порядка ключей и
    пробелов, иначе одна и та же договорённость даст разные отпечатки."""
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ── Проверка ────────────────────────────────────────────────────────────────

def validate(body, canon_resource=None):
    """Проверить тело контракта. Возвращает (ошибки, нормализованное_тело).

    Пустой список ошибок означает, что контракт пригоден. Нормализация приводит
    имена ресурсов к каноническому виду — тем же, что и в арендах, иначе
    сверка ресурсов при захвате всегда будет ложно расходиться."""
    errs = []
    if not isinstance(body, dict):
        return ["контракт должен быть объектом"], None
    b = dict(body)
    unknown_keys(b, KEYS_CONTRACT, "контракт", errs)

    v = b.get("schema_version")
    if v != SCHEMA_VERSION:
        errs.append(f"schema_version должен быть {SCHEMA_VERSION}, получено {v!r}")

    for field in ("objective", "assignee"):
        if not isinstance(b.get(field), str) or not b[field].strip():
            errs.append(f"поле {field} обязательно и должно быть непустой строкой")

    res = b.get("resources")
    if not isinstance(res, list) or not res:
        errs.append("resources обязателен и должен быть непустым списком")
    else:
        norm = []
        for r in res:
            if not isinstance(r, str):
                errs.append(f"ресурс должен быть строкой: {r!r}")
                continue
            if canon_resource:
                c, err = canon_resource(r)
                if err:
                    errs.append(f"ресурс {r!r}: {err}")
                    continue
                norm.append(c)
            else:
                norm.append(r)
        if len(set(norm)) != len(norm):
            errs.append("в resources есть повторы")
        b["resources"] = sorted(set(norm))

    outs = b.get("outputs")
    if not isinstance(outs, list) or not outs:
        errs.append("outputs обязателен: задача обязана иметь ожидаемый результат")
    else:
        slots = []
        for o in outs:
            if not isinstance(o, dict):
                errs.append(f"выход должен быть объектом: {o!r}")
                continue
            unknown_keys(o, KEYS_OUTPUT, "выход", errs)
            slot = norm_name(o.get("slot", ""))
            if not NAME_RE.match(slot):
                errs.append(f"слот {o.get('slot')!r}: имя должно быть вида "
                            f"{NAME_RE.pattern}")
                continue
            o["slot"] = slot
            slots.append(slot)
            if o.get("kind") not in KINDS:
                errs.append(f"выход {slot}: kind должен быть из {', '.join(KINDS)}")
            req = o.get("required", True)
            if not isinstance(req, bool):
                errs.append(f"выход {slot}: required должен быть да/нет")
                req = True
            o["required"] = bool(req)

            checks = o.get("checks", [])
            if not isinstance(checks, list) or not all(isinstance(x, str) for x in checks):
                errs.append(f"выход {slot}: checks должен быть списком названий проверок")
                continue
            names = [norm_name(x) for x in checks]
            bad = [x for x in names if not NAME_RE.match(x)]
            if bad:
                errs.append(f"выход {slot}: негодные названия проверок: "
                            f"{', '.join(bad) or 'пустые'}")
                continue
            if len(set(names)) != len(names):
                errs.append(f"выход {slot}: названия проверок повторяются")
                continue
            # 🔴 Неизвестную проверку отвергаем СРАЗУ: иначе получится контракт,
            # который невозможно закрыть — такую проверку никто не сможет записать.
            unknown = [x for x in names if not product_policy.known(x)]
            if unknown:
                errs.append(f"выход {slot}: неизвестные проверки {', '.join(unknown)}; "
                            f"допустимые: "
                            f"{', '.join(sorted(product_policy.CHECK_POLICIES))}")
                continue
            # 🔴 Обязательный результат без единой проверки бессмыслен: реестр
            # примет что угодно и сочтёт задачу выполненной.
            # 🔴 У обязательного результата сверка отпечатка обязана быть в
            # списке: без неё «подтверждено» означало бы лишь то, что кто-то
            # нажал кнопку. Добавляем сами, если автор её не указал.
            if o["required"] and "digest_verified" not in names:
                names = ["digest_verified"] + names
            o["checks"] = names
        if len(set(slots)) != len(slots):
            errs.append("слоты выходов повторяются")
        if not any(o.get("required", True) for o in outs if isinstance(o, dict)):
            errs.append("хотя бы один выход обязан быть required")

    ins = b.get("inputs", [])
    if not isinstance(ins, list):
        errs.append("inputs должен быть списком")
    else:
        for i in ins:
            if not isinstance(i, dict) or not isinstance(i.get("name"), str):
                errs.append(f"вход должен быть объектом с именем: {i!r}")
                continue
            unknown_keys(i, KEYS_INPUT, f"вход {i.get('name')}", errs)
            loc = i.get("locator")
            if not isinstance(loc, dict) or loc.get("type") not in LOCATOR_TYPES:
                errs.append(f"вход {i.get('name')}: locator.type должен быть из "
                            f"{', '.join(LOCATOR_TYPES)}")

    con = b.get("constraints", {})
    if not isinstance(con, dict):
        errs.append("constraints должен быть объектом")
    else:
        unknown_keys(con, KEYS_CONSTRAINTS, "constraints", errs)
        fa = con.get("forbidden_actions", [])
        dl = con.get("deadline")
        ok_fa = isinstance(fa, list) and all(isinstance(x, str) for x in fa)
        if not ok_fa:
            errs.append("constraints.forbidden_actions должен быть списком строк")
        if dl is not None and not isinstance(dl, int):
            errs.append("constraints.deadline — момент времени числом или пусто")
        # 🔴 Нормализуем ТОЛЬКО пригодное. Раньше list(fa) выполнялся даже после
        # ошибки, и на не-списке падал с внутренней ошибкой вместо отказа.
        if ok_fa:
            b["constraints"] = {"forbidden_actions": list(fa),
                                "deadline": dl if isinstance(dl, int) else None}

    if not isinstance(b.get("handoff_to"), str) or not b["handoff_to"].strip():
        errs.append("handoff_to обязателен: кому передаётся результат")

    return errs, (None if errs else b)


# ── Хранение ────────────────────────────────────────────────────────────────

SCHEMA = """
-- 🔴 Контракт неизменяем: правка добавляет ВЕРСИЮ, а не переписывает прошлую.
-- Иначе нельзя доказать, по каким условиям работа была принята.
CREATE TABLE IF NOT EXISTS task_contracts(
    task_id TEXT NOT NULL, version INTEGER NOT NULL, schema_version INTEGER NOT NULL,
    body TEXT NOT NULL, body_sha256 TEXT NOT NULL, created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL, active INTEGER NOT NULL,
    PRIMARY KEY(task_id, version));
CREATE INDEX IF NOT EXISTS tc_active ON task_contracts(task_id, active);
-- 🔴 Действующая версия ровно одна: без этого две активные версии сделали бы
-- ответ на вопрос «по каким условиям работа принята» неоднозначным.
CREATE UNIQUE INDEX IF NOT EXISTS tc_one_active
    ON task_contracts(task_id) WHERE active=1;
"""


def put(con, task_id, body, created_by, canon_resource=None):
    """Добавить версию контракта. Вызывать ВНУТРИ уже открытой транзакции."""
    errs, norm = validate(body, canon_resource)
    if errs:
        return None, errs
    text = canonical(norm)
    digest = sha256(text)
    row = con.execute("SELECT version, body_sha256 FROM task_contracts "
                      "WHERE task_id=? ORDER BY version DESC LIMIT 1",
                      (task_id,)).fetchone()
    if row and row[1] == digest:
        return row[0], []          # тот же контракт — новой версии не плодим
    ver = (row[0] + 1) if row else 1
    con.execute("UPDATE task_contracts SET active=0 WHERE task_id=?", (task_id,))
    con.execute("INSERT INTO task_contracts VALUES(?,?,?,?,?,?,?,1)",
                (task_id, ver, SCHEMA_VERSION, text, digest, created_by, now()))
    return ver, []


def active(con, task_id):
    """Действующая версия: (version, тело, отпечаток) или None."""
    row = con.execute("SELECT version, body, body_sha256 FROM task_contracts "
                      "WHERE task_id=? AND active=1", (task_id,)).fetchone()
    if not row:
        return None
    return row[0], json.loads(row[1]), row[2]


def can_transition(frm, to):
    if to not in STATES:
        return False, f"неизвестное состояние {to!r}"
    if frm == to:
        return True, ""
    allowed = TRANSITIONS.get(frm, set())
    if to not in allowed:
        return False, (f"переход {frm} → {to} запрещён; из {frm} можно: "
                       f"{', '.join(sorted(allowed)) or 'никуда'}")
    return True, ""


def has_active_lease(con, task_id):
    row = con.execute("SELECT COUNT(*) FROM leases WHERE task_id=?",
                      (task_id,)).fetchone()
    return bool(row and row[0])


def may_change(con, task_id):
    """🔴 Менять условия под работающим исполнителем нельзя: уже выданная аренда
    о новой версии не узнает никогда — ни сердцебиение, ни проверка права её не
    сверяют. Поэтому правка допустима только до начала работы и только пока нет
    ни одной действующей аренды."""
    row = con.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        return True, ""
    state = row[0]
    if state not in ("created", "assigned", "blocked"):
        return False, (f"задача в состоянии {state}: менять контракт можно только "
                       f"в created, assigned или blocked")
    if has_active_lease(con, task_id):
        return False, "у задачи есть действующая аренда — контракт не меняется"
    return True, ""


def check_acquire(con, task_id, agent_id, resources):
    """🔴 Главная проверка при захвате: задача существует, в рабочем состоянии,
    исполнитель тот самый, а набор ресурсов ТОЧНО совпадает с контрактом.

    Возвращает (ошибка_или_None, версия, отпечаток)."""
    row = con.execute("SELECT state, agent_id FROM tasks WHERE task_id=?",
                      (task_id,)).fetchone()
    if not row:
        return f"задачи {task_id} нет — аренда под неизвестную задачу не выдаётся", None, None
    state, assignee = row
    if state not in WORKABLE:
        return (f"задача в состоянии {state}: аренду можно брать только в "
                f"{' или '.join(WORKABLE)}"), None, None
    if assignee and assignee != agent_id:
        return f"задача назначена на {assignee}, а просит {agent_id}", None, None
    act = active(con, task_id)
    if not act:
        return "у задачи нет действующего контракта", None, None
    ver, body, digest = act
    if body.get("assignee") != agent_id:
        return f"контракт назначен на {body.get('assignee')}, а просит {agent_id}", None, None
    want = sorted(set(body.get("resources", [])))
    got = sorted(set(resources))
    if want != got:
        lacking = [r for r in got if r not in want]
        extra = [r for r in want if r not in got]
        return ({"причина": "запрошенные ресурсы не совпадают с контрактом",
                 "нет_в_контракте": lacking, "не_запрошены": extra}, None, None)
    return None, ver, digest
