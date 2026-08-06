#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Реестр продуктов работы — шаг 2 порядка архитектора (06.08.2026).

Зачем: без него «задача выполнена» — это слова. Реестр доказывает, что задача
произвела конкретный НЕИЗМЕНЯЕМЫЙ результат, и что он прошёл именно те проверки,
которые требовал контракт.

🔴 Это НЕ поисковый индекс файлов (`memory-engine/artifacts.py`). Тот обходит
диск, ключом берёт путь и забывает исчезнувшее — он отвечает на вопрос «что
недавно появилось». Здесь — авторитетная запись: что именно и кем произведено.

Инварианты:
 · продукт неизменяем; исправление создаёт новый, прошлый становится superseded;
 · обычный путь на диске может быть кандидатом, но НЕ закрывает обязательный
   слот: завтра по этому пути будет другое содержимое;
 · проверки только дописываются: перезапись стёрла бы историю повторных прогонов;
 · сверка отпечатка идёт БЕЗ транзакции — сетевой таймаут не должен держать
   блокировку базы, от которой зависят аренды и сердцебиение всех агентов;
 · регистрация продукта НЕ завершает задачу и не отпускает аренды.
"""

import hashlib
import json
import time
import uuid as uuidlib

import product_policy

STATES = ("candidate", "verified", "rejected", "superseded")
IMMUTABLE = ("git", "object_storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_products(
    product_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL, contract_version INTEGER NOT NULL,
    contract_sha256 TEXT NOT NULL, output_slot TEXT NOT NULL, kind TEXT NOT NULL,
    locator_type TEXT NOT NULL, locator TEXT NOT NULL,
    digest_alg TEXT NOT NULL, digest TEXT NOT NULL, size INTEGER,
    producer_agent TEXT NOT NULL, producer_instance TEXT NOT NULL,
    fencing TEXT NOT NULL, state TEXT NOT NULL, supersedes TEXT,
    idempotency_key TEXT NOT NULL, metadata TEXT NOT NULL,
    created_at INTEGER NOT NULL, registered_at INTEGER NOT NULL,
    UNIQUE(producer_agent, idempotency_key));

-- 🔴 В одном слоте одной версии контракта живой продукт ровно один: иначе
-- «какой результат считается итогом» становится вопросом без ответа.
CREATE UNIQUE INDEX IF NOT EXISTS wp_current_slot
    ON work_products(task_id, contract_version, output_slot)
    WHERE state IN ('candidate', 'verified');

-- Проверки ТОЛЬКО дописываются: попытка с номером, история сохраняется целиком.
CREATE TABLE IF NOT EXISTS product_checks(
    check_id TEXT PRIMARY KEY, product_id TEXT NOT NULL, check_name TEXT NOT NULL,
    attempt INTEGER NOT NULL, status TEXT NOT NULL, checker_agent TEXT NOT NULL,
    checker_instance TEXT, evidence TEXT NOT NULL, evidence_digest TEXT,
    checked_at INTEGER NOT NULL,
    UNIQUE(product_id, check_name, attempt));
"""


def now():
    return int(time.time())


def new_id(prefix):
    return f"{prefix}-{uuidlib.uuid4().hex[:16]}"


# ── Проверка расположения ───────────────────────────────────────────────────

def check_locator(kind, loc):
    """Годен ли адрес результата. Возвращает (ошибка_или_None, тип, канон)."""
    if not isinstance(loc, dict):
        return "locator должен быть объектом", None, None
    t = loc.get("type")
    if t not in ("git", "object_storage", "path"):
        return f"locator.type должен быть git, object_storage или path", None, None

    if t == "git":
        repo = loc.get("repository")
        if not isinstance(repo, str) or not repo.strip():
            return ("для git обязателен repository — серверный псевдоним хранилища, "
                    "а не произвольный путь клиента"), None, None
        if kind == "git_ref":
            # 🔴 Ссылка изменяема сама по себе: сегодня она указывает на один
            # коммит, завтра на другой. Поэтому личностью продукта считается
            # ЗАФИКСИРОВАННЫЙ коммит, а ссылка — только адрес.
            if not loc.get("ref"):
                return "для git_ref обязателен ref", None, None
            tgt = loc.get("target_commit")
            if not isinstance(tgt, str) or len(tgt) != 40:
                return ("для git_ref обязателен target_commit — полный "
                        "идентификатор коммита"), None, None
            canon = {"type": "git", "repository": repo, "ref": loc["ref"],
                     "target_commit": tgt}
        else:
            c = loc.get("commit")
            if not isinstance(c, str) or len(c) != 40:
                return "для git обязателен полный commit (40 знаков)", None, None
            canon = {"type": "git", "repository": repo, "commit": c}
            if loc.get("path"):
                canon["path"] = loc["path"]
        return None, t, canon

    if t == "object_storage":
        for f in ("bucket", "key", "version_id"):
            if not isinstance(loc.get(f), str) or not loc[f].strip():
                return (f"для object_storage обязателен {f}"
                        + (" — без версии объект изменяем" if f == "version_id" else ""),
                        None, None)
        return None, t, {"type": t, "bucket": loc["bucket"], "key": loc["key"],
                         "version_id": loc["version_id"]}

    p = loc.get("path")
    if not isinstance(p, str) or not p.strip():
        return "для path обязателен path", None, None
    return None, t, {"type": "path", "path": p}


# ── Регистрация ─────────────────────────────────────────────────────────────

def register(con, d, contracts_mod, canon_resource):
    """POST /product/register — короткая транзакция, никаких сетевых обращений.

    Возвращает словарь ответа. Задачу НЕ завершает и аренды НЕ отпускает: это
    следующий слой (передача результата)."""
    agent = d.get("agent_id", "")
    task_id = d.get("task_id")
    idem = d.get("idempotency_key")
    if not task_id or not idem:
        return {"ok": False, "причина": "нужны task_id и idempotency_key"}

    con.execute("BEGIN IMMEDIATE")
    try:
        # Повтор с тем же ключом возвращает уже созданный продукт, а не дубль.
        row = con.execute("SELECT product_id, state FROM work_products "
                          "WHERE producer_agent=? AND idempotency_key=?",
                          (agent, idem)).fetchone()
        if row:
            con.execute("ROLLBACK")
            return {"ok": True, "product_id": row[0], "состояние": row[1],
                    "повтор": True}

        trow = con.execute("SELECT state, agent_id FROM tasks WHERE task_id=?",
                           (task_id,)).fetchone()
        if not trow:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": f"задачи {task_id} нет"}
        if trow[0] != "running":
            con.execute("ROLLBACK")
            return {"ok": False, "причина": f"задача в состоянии {trow[0]}: "
                                            f"результат регистрируется только в работе"}
        if trow[1] != agent:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": f"задача назначена на {trow[1]}"}

        act = contracts_mod.active(con, task_id)
        if not act:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "у задачи нет действующего контракта"}
        ver, body, sha = act
        if d.get("contract_version") != ver or d.get("contract_sha256") != sha:
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": "версия или отпечаток контракта не совпадают с "
                               "действующими",
                    "действующая_версия": ver, "действующий_отпечаток": sha}

        slot = str(d.get("output_slot", "")).strip().lower()
        out = next((o for o in body.get("outputs", []) if o.get("slot") == slot), None)
        if not out:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": f"в контракте нет слота {slot!r}",
                    "слоты": [o.get("slot") for o in body.get("outputs", [])]}
        if d.get("kind") != out.get("kind"):
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": f"вид результата не совпадает: контракт требует "
                               f"{out.get('kind')}, прислан {d.get('kind')}"}

        # 🔴 Аренда проверяется ПРЯМО ЗДЕСЬ, а не отдельным запросом: между
        # ответом «право есть» и вставкой было бы окно, в котором право
        # отбирают удержанием или блокировкой задачи.
        leases = con.execute("SELECT resource, lease_token, instance_id, fencing_token "
                             "FROM leases WHERE task_id=? AND agent_id=?",
                             (task_id, agent)).fetchall()
        if not leases:
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": "нет действующей аренды задачи — результат "
                               "регистрирует только тот, кто прямо сейчас работает"}
        token = d.get("lease_token")
        inst = d.get("instance_id")
        if any(l[1] != token for l in leases):
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "секрет аренды не совпадает"}
        if any(l[2] != inst for l in leases):
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "аренда принадлежит другому процессу"}
        fencing = {l[0]: l[3] for l in leases}
        want_f = d.get("fencing") or {}
        if want_f and {k: int(v) for k, v in want_f.items()} != fencing:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "поколение аренды устарело",
                    "текущее": fencing}

        err, ltype, loc = check_locator(d.get("kind"), d.get("locator"))
        if err:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": err}

        digest = d.get("digest")
        alg = d.get("digest_alg", "sha256")
        if ltype in IMMUTABLE and (not isinstance(digest, str) or not digest.strip()):
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": "для неизменяемого результата обязателен отпечаток"}

        sup = d.get("supersedes")
        cur = con.execute("SELECT product_id FROM work_products WHERE task_id=? AND "
                          "contract_version=? AND output_slot=? AND "
                          "state IN ('candidate','verified')",
                          (task_id, ver, slot)).fetchone()
        if cur and sup != cur[0]:
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": f"в слоте {slot} уже есть продукт {cur[0]}; чтобы "
                               f"заменить его, укажи supersedes",
                    "текущий": cur[0]}
        if sup:
            if not cur or cur[0] != sup:
                con.execute("ROLLBACK")
                return {"ok": False, "причина": "указанный supersedes не является "
                                                "текущим продуктом слота"}
            con.execute("UPDATE work_products SET state='superseded' WHERE product_id=?",
                        (sup,))

        pid = new_id("wp")
        con.execute("INSERT INTO work_products VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, task_id, ver, sha, slot, d.get("kind"), ltype,
                     json.dumps(loc, ensure_ascii=False, sort_keys=True),
                     alg, digest or "", d.get("size"), agent, inst or "",
                     json.dumps(fencing, sort_keys=True), "candidate", sup, idem,
                     json.dumps(d.get("metadata", {}), ensure_ascii=False),
                     int(d.get("created_at") or now()), now()))
        con.execute("COMMIT")
        return {"ok": True, "product_id": pid, "состояние": "candidate",
                "требуются_проверки": out.get("checks", []),
                "неизменяемый": ltype in IMMUTABLE}
    except Exception as e:
        con.execute("ROLLBACK")
        return {"ok": False, "причина": f"сбой: {e}"}


# ── Проверки ────────────────────────────────────────────────────────────────

def record_check(con, d, contracts_mod, system=False):
    """POST /product/check — дописать попытку проверки и, если все обязательные
    проверки пройдены, перевести продукт в «проверен»."""
    pid = d.get("product_id")
    name = " ".join(str(d.get("check_name", "")).split()).strip().lower()
    status = d.get("status")
    checker = d.get("agent_id", "")
    if status not in product_policy.STATUSES:
        return {"ok": False, "причина": f"статус должен быть из "
                                        f"{', '.join(product_policy.STATUSES)}"}
    con.execute("BEGIN IMMEDIATE")
    try:
        p = con.execute("SELECT task_id, contract_version, contract_sha256, "
                        "output_slot, producer_agent, state, locator_type, digest "
                        "FROM work_products WHERE product_id=?", (pid,)).fetchone()
        if not p:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": f"продукта {pid} нет"}
        task_id, ver, sha, slot, producer, state, ltype, digest = p
        if state in ("superseded", "rejected"):
            con.execute("ROLLBACK")
            return {"ok": False, "причина": f"продукт в состоянии {state}"}

        act = contracts_mod.active(con, task_id)
        if not act or act[0] != ver or act[2] != sha:
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": "контракт продукта больше не действующий — "
                               "проверять нечего"}
        out = next((o for o in act[1].get("outputs", []) if o.get("slot") == slot), None)
        required = list(out.get("checks", [])) if out else []
        if name not in required:
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": f"контракт не требует проверки {name!r} для слота "
                               f"{slot}", "требуются": required}

        may, why = product_policy.may_record(name, checker, producer, system=system)
        if not may:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": why}

        evidence = d.get("evidence")
        if product_policy.policy(name) == "producer" and not str(evidence or "").strip():
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": f"проверку {name} записывает сам исполнитель, поэтому "
                               f"свидетельство обязательно"}

        row = con.execute("SELECT MAX(attempt) FROM product_checks "
                          "WHERE product_id=? AND check_name=?", (pid, name)).fetchone()
        attempt = (row[0] or 0) + 1
        ev = json.dumps(evidence, ensure_ascii=False) if evidence is not None else ""
        con.execute("INSERT INTO product_checks VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (new_id("chk"), pid, name, attempt, status, checker,
                     d.get("instance_id"), ev,
                     hashlib.sha256(ev.encode("utf-8")).hexdigest() if ev else None,
                     now()))

        verified, why2 = _verify(con, pid, required, ltype, digest, state)
        if verified:
            con.execute("UPDATE work_products SET state='verified' WHERE product_id=?",
                        (pid,))
        con.execute("COMMIT")
        return {"ok": True, "попытка": attempt, "состояние_продукта":
                ("verified" if verified else state), "почему": why2}
    except Exception as e:
        con.execute("ROLLBACK")
        return {"ok": False, "причина": f"сбой: {e}"}


def _verify(con, pid, required, ltype, digest, state):
    """Проверен ли продукт: последняя попытка КАЖДОЙ обязательной проверки должна
    быть успешной, адрес — неизменяемым, отпечаток — на месте."""
    if state != "candidate":
        return False, f"продукт в состоянии {state}"
    if ltype not in IMMUTABLE:
        # 🔴 Путь на диске не может закрыть обязательный слот: содержимое по нему
        # завтра будет другим, и доказать происхождение нечем.
        return False, ("обычный путь остаётся кандидатом: подтвердить можно только "
                       "неизменяемый адрес")
    if not digest:
        return False, "нет отпечатка"
    for name in required:
        row = con.execute("SELECT status FROM product_checks WHERE product_id=? AND "
                          "check_name=? ORDER BY attempt DESC LIMIT 1",
                          (pid, name)).fetchone()
        if not row:
            return False, f"проверка {name} ещё не выполнялась"
        if row[0] != "passed":
            return False, f"последняя попытка проверки {name}: {row[0]}"
    return True, "все обязательные проверки пройдены"


def show(con, d):
    pid = d.get("product_id")
    if pid:
        row = con.execute("SELECT * FROM work_products WHERE product_id=?",
                          (pid,)).fetchone()
        if not row:
            return {"ok": False, "причина": "нет такого продукта"}
        cols = [c[1] for c in con.execute("PRAGMA table_info(work_products)")]
        prod = dict(zip(cols, row))
        prod["locator"] = json.loads(prod["locator"])
        checks = [dict(zip(("проверка", "попытка", "статус", "кто", "когда"), r))
                  for r in con.execute(
                      "SELECT check_name, attempt, status, checker_agent, checked_at "
                      "FROM product_checks WHERE product_id=? ORDER BY check_name, attempt",
                      (pid,))]
        return {"ok": True, "продукт": prod, "проверки": checks}
    rows = con.execute("SELECT product_id, output_slot, kind, state, producer_agent "
                       "FROM work_products WHERE task_id=? ORDER BY registered_at",
                       (d.get("task_id"),)).fetchall()
    return {"ok": True, "продукты": [
        dict(zip(("product_id", "слот", "вид", "состояние", "кто"), r)) for r in rows]}
