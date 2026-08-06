#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Передача результата — шаг 3 порядка архитектора (06.08.2026).

Зачем: до сих пор задачу нельзя было завершить вовсе — и правильно. Завершает
её только ПРИНЯТАЯ передача: отправитель предъявляет конкретные подтверждённые
результаты, получатель решает.

🔴 Инварианты:
 · передавать может только назначенный исполнитель, и только пока задача в работе;
 · получатель берётся ИЗ КОНТРАКТА, а не из запроса;
 · каждый обязательный слот закрыт конкретным подтверждённым продуктом той же
   версии и того же отпечатка контракта;
 · успешное предложение ОТЗЫВАЕТ аренды и поднимает поколения — иначе после
   передачи исполнитель продолжит менять то, что уже предъявлено как итог;
 · отказ возвращает задачу в assigned, а не в работу: аренд больше нет, и
   вернуться в работу можно только новым захватом;
 · фактический субъект запроса хранится ОТДЕЛЬНО от исполнителя: администратор
   может действовать от чужого имени, но это должно быть видно.
"""

import hashlib
import json
import time
import uuid as uuidlib

STATUSES = ("offered", "accepted", "rejected")

SCHEMA = """
CREATE TABLE IF NOT EXISTS handoffs(
    handoff_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL, contract_version INTEGER NOT NULL,
    contract_sha256 TEXT NOT NULL,
    from_agent TEXT NOT NULL, to_agent TEXT NOT NULL,
    offered_by TEXT NOT NULL, decided_by TEXT,
    status TEXT NOT NULL, summary TEXT NOT NULL, known_issues TEXT NOT NULL,
    next_action TEXT NOT NULL, rejection_reason TEXT,
    idempotency_key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
    created_at INTEGER NOT NULL, decided_at INTEGER,
    UNIQUE(task_id, from_agent, idempotency_key),
    FOREIGN KEY(task_id, contract_version)
        REFERENCES task_contracts(task_id, version) ON DELETE RESTRICT);

-- 🔴 Открытая передача у задачи ровно одна: иначе «кто решает» становится
-- вопросом без ответа, а два получателя могут принять разные наборы.
CREATE UNIQUE INDEX IF NOT EXISTS one_open_handoff
    ON handoffs(task_id) WHERE status='offered';

CREATE TABLE IF NOT EXISTS handoff_products(
    handoff_id TEXT NOT NULL, output_slot TEXT NOT NULL,
    product_id TEXT NOT NULL, required INTEGER NOT NULL,
    PRIMARY KEY(handoff_id, output_slot),
    FOREIGN KEY(handoff_id) REFERENCES handoffs(handoff_id) ON DELETE RESTRICT,
    FOREIGN KEY(product_id) REFERENCES work_products(product_id) ON DELETE RESTRICT);
"""


def now():
    return int(time.time())


def new_id():
    return "ho-" + uuidlib.uuid4().hex[:16]


def _req_sha(d):
    return hashlib.sha256(json.dumps(
        {k: d.get(k) for k in ("task_id", "contract_version", "contract_sha256",
                               "products", "summary", "known_issues",
                               "next_action")},
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def offer(con, d, contracts_mod, products_mod, revoke_leases):
    """Предложить результат к приёмке. Одна транзакция целиком."""
    task_id = d.get("task_id")
    agent = d.get("agent_id", "")
    actor = d.get("_actor", agent)
    idem = d.get("idempotency_key")
    mapping = d.get("products")
    if not task_id or not idem:
        return {"ok": False, "причина": "нужны task_id и idempotency_key"}
    if not isinstance(mapping, dict) or not mapping:
        return {"ok": False,
                "причина": "нужен явный набор продуктов по слотам: какой именно "
                           "результат передаётся"}
    if not str(d.get("summary", "")).strip():
        return {"ok": False, "причина": "нужно краткое описание сделанного"}

    req_sha = _req_sha(d)
    con.execute("BEGIN IMMEDIATE")
    try:
        # 🔴 Идемпотентность проверяется ПЕРВОЙ: точный повтор обязан вернуть
        # прежнюю передачу даже после того, как задача уже ушла в ожидание.
        row = con.execute("SELECT handoff_id, status, request_sha256 FROM handoffs "
                          "WHERE task_id=? AND from_agent=? AND idempotency_key=?",
                          (task_id, agent, idem)).fetchone()
        if row:
            con.execute("ROLLBACK")
            if row[2] != req_sha:
                return {"ok": False,
                        "причина": "тот же ключ повтора с другим содержимым",
                        "handoff_id": row[0]}
            return {"ok": True, "handoff_id": row[0], "статус": row[1],
                    "повтор": True}

        trow = con.execute("SELECT state, agent_id FROM tasks WHERE task_id=?",
                           (task_id,)).fetchone()
        if not trow:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": f"задачи {task_id} нет"}
        if trow[0] != "running":
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": f"задача в состоянии {trow[0]}: передать результат "
                               f"можно только из работы"}
        if trow[1] != agent:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": f"задача назначена на {trow[1]}"}
        if actor != agent and not d.get("_admin"):
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "передать может только сам исполнитель"}

        act = contracts_mod.active(con, task_id)
        if not act:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "у задачи нет действующего контракта"}
        ver, body, sha = act
        if d.get("contract_version") != ver or d.get("contract_sha256") != sha:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "версия или отпечаток контракта не "
                                            "совпадают с действующими",
                    "действующая_версия": ver, "действующий_отпечаток": sha}

        # Аренда: живая, целиком по контракту, тем же процессом и поколением.
        t_now = now()
        leases = con.execute("SELECT resource, lease_token, instance_id, "
                             "fencing_token FROM leases WHERE task_id=? AND "
                             "agent_id=? AND expires > ?",
                             (task_id, agent, t_now)).fetchall()
        want_res = sorted(set(body.get("resources", [])))
        if sorted({l[0] for l in leases}) != want_res:
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": "аренда не покрывает все ресурсы контракта или "
                               "уже истекла",
                    "есть": sorted({l[0] for l in leases}), "нужно": want_res}
        if any(l[1] != d.get("lease_token") for l in leases):
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "секрет аренды не совпадает"}
        if any(l[2] != d.get("instance_id") for l in leases):
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "аренда принадлежит другому процессу"}
        fencing = {l[0]: l[3] for l in leases}
        given = d.get("fencing")
        if not isinstance(given, dict) or not given:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "нужно прислать поколения аренды",
                    "текущее": fencing}
        try:
            if {k: int(v) for k, v in given.items()} != fencing:
                raise ValueError
        except (TypeError, ValueError):
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "поколение аренды устарело",
                    "текущее": fencing}

        # 🔴 Получателя берём ИЗ КОНТРАКТА: если бы его называл отправитель, он
        # мог бы передать результат кому угодно, в том числе себе.
        to_agent = body.get("handoff_to")

        outputs = {o["slot"]: o for o in body.get("outputs", [])}
        required = [s for s, o in outputs.items() if o.get("required", True)]
        missing = [s for s in required if s not in mapping]
        if missing:
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": "не закрыты обязательные результаты",
                    "не_хватает": missing}
        unknown = [s for s in mapping if s not in outputs]
        if unknown:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "в контракте нет таких слотов",
                    "лишние": unknown}

        attach = []
        for slot, pid in mapping.items():
            prow = con.execute(
                "SELECT task_id, contract_version, contract_sha256, output_slot, "
                "state, locator_type FROM work_products WHERE product_id=?",
                (pid,)).fetchone()
            if not prow:
                con.execute("ROLLBACK")
                return {"ok": False, "причина": f"продукта {pid} нет"}
            p_task, p_ver, p_sha, p_slot, p_state, p_ltype = prow
            if p_task != task_id or p_ver != ver or p_sha != sha:
                con.execute("ROLLBACK")
                return {"ok": False,
                        "причина": f"продукт {pid} относится к другой задаче или "
                                   f"версии контракта"}
            if p_slot != slot:
                con.execute("ROLLBACK")
                return {"ok": False,
                        "причина": f"продукт {pid} лежит в слоте {p_slot}, а не {slot}"}
            if p_state != "verified":
                con.execute("ROLLBACK")
                return {"ok": False,
                        "причина": f"продукт {pid} в состоянии {p_state}: передавать "
                                   f"можно только подтверждённый результат"}
            if p_ltype not in products_mod.VERIFIABLE:
                con.execute("ROLLBACK")
                return {"ok": False,
                        "причина": f"результат в {p_ltype} нельзя предъявить как "
                                   f"итог: его сверка не поддерживается"}
            attach.append((slot, pid, 1 if outputs[slot].get("required", True) else 0))

        hid = new_id()
        con.execute("INSERT INTO handoffs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (hid, task_id, ver, sha, agent, to_agent, actor, None,
                     "offered", d.get("summary", ""),
                     json.dumps(d.get("known_issues", []), ensure_ascii=False),
                     str(d.get("next_action", "")), None, idem, req_sha, now(), None))
        for slot, pid, req in attach:
            con.execute("INSERT INTO handoff_products VALUES(?,?,?,?)",
                        (hid, slot, pid, req))

        # 🔴 Аренды отзываются здесь же: предъявленный итог не должен меняться
        # после того, как его отдали на приёмку.
        revoked = revoke_leases(con, task_id, "результат передан на приёмку")
        con.execute("UPDATE tasks SET state='handoff_pending', updated=? "
                    "WHERE task_id=?", (now(), task_id))
        con.execute("COMMIT")
        return {"ok": True, "handoff_id": hid, "статус": "offered",
                "кому": to_agent, "отозвано_аренд": len(revoked),
                "состояние_задачи": "handoff_pending"}
    except Exception as e:
        con.execute("ROLLBACK")
        return {"ok": False, "причина": f"сбой: {e}"}


def decide(con, d, contracts_mod, accept):
    """Принять или отклонить переданный результат."""
    hid = d.get("handoff_id")
    actor = d.get("_actor", d.get("agent_id", ""))
    reason = str(d.get("reason", "")).strip()
    if not accept and not reason:
        return {"ok": False, "причина": "при отказе нужна причина"}

    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT task_id, contract_version, contract_sha256, "
                          "to_agent, status FROM handoffs WHERE handoff_id=?",
                          (hid,)).fetchone()
        if not row:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": f"передачи {hid} нет"}
        task_id, ver, sha, to_agent, status = row
        if status != "offered":
            con.execute("ROLLBACK")
            return {"ok": False, "причина": f"решение уже принято: {status}"}
        if actor != to_agent and not d.get("_admin"):
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": f"решение принимает {to_agent}, а не {actor}"}

        trow = con.execute("SELECT state FROM tasks WHERE task_id=?",
                           (task_id,)).fetchone()
        if not trow or trow[0] != "handoff_pending":
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": f"задача в состоянии {trow[0] if trow else '—'}, "
                               f"а не в ожидании решения"}

        if accept:
            act = contracts_mod.active(con, task_id)
            if not act or act[0] != ver or act[2] != sha:
                con.execute("ROLLBACK")
                return {"ok": False,
                        "причина": "контракт изменился с момента передачи — "
                                   "принимать нечего"}
            body = act[1]
            required = [o["slot"] for o in body.get("outputs", [])
                        if o.get("required", True)]
            attached = {r[0]: r[1] for r in con.execute(
                "SELECT output_slot, product_id FROM handoff_products "
                "WHERE handoff_id=?", (hid,))}
            missing = [s for s in required if s not in attached]
            if missing:
                con.execute("ROLLBACK")
                return {"ok": False, "причина": "не закрыты обязательные результаты",
                        "не_хватает": missing}
            # 🔴 Проверяем продукты ЗАНОВО: между предложением и приёмкой поздняя
            # неудачная проверка могла снять подтверждение.
            for slot, pid in attached.items():
                prow = con.execute("SELECT state, contract_version, contract_sha256 "
                                   "FROM work_products WHERE product_id=?",
                                   (pid,)).fetchone()
                if not prow or prow[0] != "verified":
                    con.execute("ROLLBACK")
                    return {"ok": False,
                            "причина": f"продукт {pid} в слоте {slot} больше не "
                                       f"подтверждён ({prow[0] if prow else '—'})"}
                if prow[1] != ver or prow[2] != sha:
                    con.execute("ROLLBACK")
                    return {"ok": False,
                            "причина": f"продукт {pid} относится к другой версии "
                                       f"контракта"}
            con.execute("UPDATE handoffs SET status='accepted', decided_by=?, "
                        "decided_at=? WHERE handoff_id=?", (actor, now(), hid))
            con.execute("UPDATE tasks SET state='done', updated=? WHERE task_id=?",
                        (now(), task_id))
            con.execute("COMMIT")
            return {"ok": True, "статус": "accepted", "состояние_задачи": "done",
                    "решил": actor}

        con.execute("UPDATE handoffs SET status='rejected', decided_by=?, "
                    "decided_at=?, rejection_reason=? WHERE handoff_id=?",
                    (actor, now(), reason, hid))
        # 🔴 Возврат именно в assigned, а не в работу: аренды были отозваны при
        # предложении, и вернуться к работе можно только новым захватом.
        con.execute("UPDATE tasks SET state='assigned', updated=? WHERE task_id=?",
                    (now(), task_id))
        con.execute("COMMIT")
        return {"ok": True, "статус": "rejected", "состояние_задачи": "assigned",
                "решил": actor, "причина_отказа": reason}
    except Exception as e:
        con.execute("ROLLBACK")
        return {"ok": False, "причина": f"сбой: {e}"}


def show(con, d):
    hid = d.get("handoff_id")
    if hid:
        row = con.execute("SELECT * FROM handoffs WHERE handoff_id=?", (hid,)).fetchone()
        if not row:
            return {"ok": False, "причина": "нет такой передачи"}
        cols = [c[1] for c in con.execute("PRAGMA table_info(handoffs)")]
        h = dict(zip(cols, row))
        h["products"] = {r[0]: r[1] for r in con.execute(
            "SELECT output_slot, product_id FROM handoff_products WHERE handoff_id=?",
            (hid,))}
        return {"ok": True, "передача": h}
    rows = con.execute("SELECT handoff_id, status, from_agent, to_agent, created_at "
                       "FROM handoffs WHERE task_id=? ORDER BY created_at",
                       (d.get("task_id"),)).fetchall()
    return {"ok": True, "передачи": [
        dict(zip(("handoff_id", "статус", "от", "кому", "когда"), r)) for r in rows]}
