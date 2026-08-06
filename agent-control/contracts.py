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
import time

SCHEMA_VERSION = 1

# Состояния задачи и разрешённые переходы. 🔴 Прямого пути в «готово» нет:
# завершает задачу только принятая передача результата.
STATES = ("created", "assigned", "running", "handoff_pending",
          "done", "blocked", "cancelled")
TRANSITIONS = {
    "created": {"assigned", "cancelled"},
    "assigned": {"running", "blocked", "cancelled"},
    "running": {"handoff_pending", "blocked", "cancelled"},
    "handoff_pending": {"done", "running", "blocked"},
    "blocked": {"running", "cancelled"},
    "done": set(),
    "cancelled": set(),
}
WORKABLE = ("assigned", "running")

KINDS = ("git_commit", "git_ref", "object", "report", "dataset", "config")
LOCATOR_TYPES = ("git", "object_storage", "path")


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
            slot = o.get("slot")
            if not isinstance(slot, str) or not slot.strip():
                errs.append("у выхода обязателен непустой slot")
                continue
            slots.append(slot)
            if o.get("kind") not in KINDS:
                errs.append(f"выход {slot}: kind должен быть из {', '.join(KINDS)}")
            if not isinstance(o.get("required", True), bool):
                errs.append(f"выход {slot}: required должен быть да/нет")
            checks = o.get("checks", [])
            if not isinstance(checks, list) or not all(isinstance(c, str) for c in checks):
                errs.append(f"выход {slot}: checks должен быть списком названий проверок")
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
            loc = i.get("locator")
            if not isinstance(loc, dict) or loc.get("type") not in LOCATOR_TYPES:
                errs.append(f"вход {i.get('name')}: locator.type должен быть из "
                            f"{', '.join(LOCATOR_TYPES)}")

    con = b.get("constraints", {})
    if not isinstance(con, dict):
        errs.append("constraints должен быть объектом")
    else:
        fa = con.get("forbidden_actions", [])
        if not isinstance(fa, list):
            errs.append("constraints.forbidden_actions должен быть списком")
        dl = con.get("deadline")
        if dl is not None and not isinstance(dl, int):
            errs.append("constraints.deadline — момент времени числом или пусто")
        b["constraints"] = {"forbidden_actions": list(fa), "deadline": dl}

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
