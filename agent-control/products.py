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
import re
import time
import uuid as uuidlib

import product_policy

STATES = ("candidate", "verified", "rejected", "superseded")

# 🔴 Подтверждать можно только то, что мы умеем ПРОВЕРИТЬ. Сверка объектного
# хранилища сейчас не читает конкретную версию объекта — значит доказывает не
# то. До настоящей потоковой сверки такой продукт остаётся кандидатом навсегда:
# лучше честно недоступная возможность, чем проверка не той версии.
IMMUTABLE = ("git",)
VERIFIABLE = ("git",)

# 🔴 Псевдонимы хранилищ ведёт СЕРВЕР. Произвольный путь или имя корзины от
# клиента означали бы, что «доказательство» указывает куда угодно.
GIT_REPOSITORIES = {
    "agent-store": "/srv/agents/store.git",
}
OBJECT_STORES = {
    "agent-archive": {"bucket": "plumbingcore-prod-immutable-backups",
                      "prefix": "agent-history/"},
}

# Какой вид результата в каком хранилище допустим.
KIND_LOCATORS = {
    "git_commit": ("git",),
    "git_ref": ("git",),
    "object": ("git", "object_storage", "path"),
    "report": ("git", "object_storage", "path"),
    "dataset": ("git", "object_storage", "path"),
    "config": ("git", "object_storage", "path"),
}

# Отпечаток: для git это сам идентификатор объекта, для хранилища — SHA-256.
DIGEST_ALGS = {"git": "git_sha1", "object_storage": "sha256"}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")

# 🔴 Проверка, без которой подтверждение бессмысленно: она доказывает, что
# результат действительно существует и совпадает с заявленным отпечатком.
DIGEST_CHECK = "digest_verified"

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_products(
    product_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL, contract_version INTEGER NOT NULL,
    contract_sha256 TEXT NOT NULL, output_slot TEXT NOT NULL, kind TEXT NOT NULL,
    locator_type TEXT NOT NULL, locator TEXT NOT NULL,
    digest_alg TEXT NOT NULL, digest TEXT NOT NULL, size INTEGER,
    producer_agent TEXT NOT NULL, producer_instance TEXT NOT NULL,
    fencing TEXT NOT NULL, state TEXT NOT NULL, supersedes TEXT,
    idempotency_key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at INTEGER NOT NULL, registered_at INTEGER NOT NULL,
    -- 🔴 Ключ повтора действует В ПРЕДЕЛАХ ЗАДАЧИ: иначе тот же ключ в другой
    -- задаче возвращал бы чужой продукт с признаком успеха.
    UNIQUE(task_id, producer_agent, idempotency_key),
    -- 🔴 Связность держит база, а не только прикладной код: продукт без своей
    -- версии контракта или ссылающийся на несуществующую замену — это молчаливо
    -- испорченный учёт, который выяснится в самый неподходящий момент.
    FOREIGN KEY(task_id, contract_version)
        REFERENCES task_contracts(task_id, version) ON DELETE RESTRICT,
    FOREIGN KEY(supersedes)
        REFERENCES work_products(product_id) ON DELETE RESTRICT);

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
    UNIQUE(product_id, check_name, attempt),
    FOREIGN KEY(product_id)
        REFERENCES work_products(product_id) ON DELETE RESTRICT);
"""


def migrate(con):
    """Привести таблицы к текущей схеме.

    🔴 `CREATE TABLE IF NOT EXISTS` существующую таблицу НЕ меняет: после
    добавления полей и внешних ключей старая база молча оставалась бы прежней.
    Перестраиваем ОБЕ таблицы одной транзакцией с переносом записей, а затем
    проверяем связность. Нашлась сирота — откат целиком: работать на испорченном
    учёте хуже, чем не запуститься.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(work_products)")}
    if not cols:
        return False
    # 🔴 Смотрим фактические внешние ключи ОБЕИХ таблиц, а не текст одной:
    # частично перенесённая база (ключи есть у продуктов, но нет у проверок)
    # иначе считалась бы готовой.
    # составной ключ возвращает по строке на столбец — считаем именно ключи
    fk_products = len({r[0] for r in con.execute(
        "PRAGMA foreign_key_list(work_products)")})
    fk_checks = len({r[0] for r in con.execute(
        "PRAGMA foreign_key_list(product_checks)")})
    need = ("request_sha256" not in cols) or fk_products < 2 or fk_checks < 1
    if not need:
        return False

    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("ALTER TABLE work_products RENAME TO work_products_old")
        con.execute("ALTER TABLE product_checks RENAME TO product_checks_old")
        con.execute("DROP INDEX IF EXISTS wp_current_slot")
        # 🔴 executescript НЕЛЬЗЯ: он неявно закрывает открытую транзакцию, и
        # последующий откат падает с «нет активной транзакции» — то есть при
        # ошибке база осталась бы наполовину перестроенной. Выполняем по одному.
        for stmt in [x.strip() for x in SCHEMA.split(";") if x.strip()]:
            con.execute(stmt)
        keep = [c for c in cols if c != "request_sha256"]
        names = ", ".join(keep)
        con.execute(f"INSERT INTO work_products ({names}, request_sha256) "
                    f"SELECT {names}, '' FROM work_products_old")
        ccols = [r[1] for r in con.execute("PRAGMA table_info(product_checks_old)")]
        cn = ", ".join(ccols)
        con.execute(f"INSERT INTO product_checks ({cn}) SELECT {cn} FROM product_checks_old")

        orphans = list(con.execute("PRAGMA foreign_key_check"))
        if orphans:
            con.execute("ROLLBACK")
            for o in orphans[:10]:
                print(f"🔴 сирота: таблица {o[0]}, строка {o[1]}, ссылается на {o[2]}")
            raise SystemExit(f"🔴 перенос отменён: связность нарушена "
                             f"({len(orphans)} записей). Разбираться вручную.")
        con.execute("DROP TABLE work_products_old")
        con.execute("DROP TABLE product_checks_old")
        con.execute("COMMIT")
    except SystemExit:
        raise
    except Exception as e:
        con.execute("ROLLBACK")
        raise SystemExit(f"🔴 перенос схемы не удался: {e}")
    finally:
        con.execute("PRAGMA foreign_keys=ON")
    print("таблицы продуктов перестроены с внешними ключами, записи сохранены")
    return True


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
        return "locator.type должен быть git, object_storage или path", None, None
    allowed = KIND_LOCATORS.get(kind)
    if not allowed:
        return f"неизвестный вид результата {kind!r}", None, None
    if t not in allowed:
        # 🔴 Раньше git_ref можно было зарегистрировать в объектном хранилище:
        # вид и адрес не сверялись вовсе.
        return (f"вид {kind} не может лежать в {t}; допустимо: "
                f"{', '.join(allowed)}"), None, None

    if t == "git":
        repo = loc.get("repository")
        if repo not in GIT_REPOSITORIES:
            return (f"репозиторий {repo!r} не значится в серверном каталоге; "
                    f"допустимые: {', '.join(sorted(GIT_REPOSITORIES))}"), None, None
        if kind == "git_ref":
            # Ссылка изменяема: сегодня указывает на один коммит, завтра на
            # другой. Личность продукта — зафиксированный коммит, ссылка — адрес.
            if not loc.get("ref"):
                return "для git_ref обязателен ref", None, None
            tgt = str(loc.get("target_commit", "")).lower()
            if not HEX40.match(tgt):
                return ("для git_ref обязателен target_commit — полный "
                        "идентификатор коммита"), None, None
            canon = {"type": "git", "repository": repo, "ref": loc["ref"],
                     "target_commit": tgt}
        else:
            c = str(loc.get("commit", "")).lower()
            if not HEX40.match(c):
                return "для git обязателен полный commit (40 знаков)", None, None
            canon = {"type": "git", "repository": repo, "commit": c}
            if loc.get("path"):
                canon["path"] = loc["path"]
        return None, t, canon

    if t == "object_storage":
        alias = loc.get("bucket")
        store = OBJECT_STORES.get(alias)
        if store is None:
            return (f"хранилище {alias!r} не значится в серверном каталоге; "
                    f"допустимые: {', '.join(sorted(OBJECT_STORES))}"), None, None
        for f in ("key", "version_id"):
            if not isinstance(loc.get(f), str) or not loc[f].strip():
                return (f"для object_storage обязателен {f}"
                        + (" — без версии объект изменяем" if f == "version_id" else ""),
                        None, None)
        # 🔴 Ключ обязан лежать в отведённом пространстве: иначе продукт мог бы
        # ссылаться на что угодно в общей корзине, включая резервные копии баз.
        prefix = store.get("prefix", "")
        if prefix and not loc["key"].startswith(prefix):
            return (f"ключ должен начинаться с {prefix} — за пределами этого "
                    f"пространства продукты не регистрируются"), None, None
        return None, t, {"type": t, "bucket": alias, "key": loc["key"],
                         "version_id": loc["version_id"]}

    pth = loc.get("path")
    if not isinstance(pth, str) or not pth.strip():
        return "для path обязателен path", None, None
    return None, t, {"type": "path", "path": pth}


def check_digest(ltype, alg, digest, loc):
    """Отпечаток по правилам своего хранилища."""
    want_alg = DIGEST_ALGS.get(ltype)
    if want_alg is None:
        return None            # обычный путь: отпечаток не обязателен
    if alg != want_alg:
        return f"для {ltype} digest_alg должен быть {want_alg}, прислан {alg!r}"
    d = str(digest or "").lower()
    if ltype == "git":
        if not HEX40.match(d):
            return "для git отпечаток — идентификатор объекта (40 знаков)"
        # 🔴 Отпечаток обязан совпадать с адресом: иначе продукт «доказывает»
        # один объект, а лежит по другому.
        own = loc.get("target_commit") or loc.get("commit")
        if not loc.get("path") and d != own:
            return "отпечаток не совпадает с коммитом в адресе"
    elif not HEX64.match(d):
        return "для объектного хранилища отпечаток — SHA-256 (64 знака)"
    return None


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

    # Отпечаток запроса: по нему отличаем настоящий повтор от чужого содержимого
    # под тем же ключом.
    # 🔴 Версия контракта входит в отпечаток запроса: тот же ключ после смены
    # условий — это уже другой запрос, а не повтор прежнего.
    req_sha = hashlib.sha256(json.dumps(
        {k: d.get(k) for k in ("task_id", "contract_version", "contract_sha256",
                               "output_slot", "kind", "locator", "digest",
                               "digest_alg", "size", "supersedes", "metadata")},
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    con.execute("BEGIN IMMEDIATE")
    try:
        # Повтор с тем же ключом возвращает уже созданный продукт, а не дубль.
        row = con.execute("SELECT product_id, state, request_sha256 FROM work_products "
                          "WHERE task_id=? AND producer_agent=? AND idempotency_key=?",
                          (task_id, agent, idem)).fetchone()
        if row:
            con.execute("ROLLBACK")
            # 🔴 Повтор — это ТОТ ЖЕ запрос. Тот же ключ с другим слотом, адресом
            # или отпечатком — не повтор, а ошибка вызывающего.
            if row[2] != req_sha:
                return {"ok": False,
                        "причина": "тот же ключ повтора с другим содержимым запроса",
                        "product_id": row[0]}
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
        # 🔴 Срок аренды читаем ЗДЕСЬ, а не полагаемся на фонового уборщика:
        # между истечением и его проходом есть окно, в котором протухший секрет
        # проходил бы как действующий.
        t_now = now()
        leases = con.execute("SELECT resource, lease_token, instance_id, "
                             "fencing_token, expires FROM leases "
                             "WHERE task_id=? AND agent_id=? AND expires > ?",
                             (task_id, agent, t_now)).fetchall()
        if not leases:
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": "нет действующей аренды задачи — результат "
                               "регистрирует только тот, кто прямо сейчас работает"}
        # 🔴 Набор аренд обязан покрывать ВСЕ ресурсы контракта: удержание могло
        # отобрать одну из них, а по остатку регистрация проходила бы как ни в
        # чём не бывало.
        have = sorted({l[0] for l in leases})
        want = sorted(set(body.get("resources", [])))
        if have != want:
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": "аренда покрывает не все ресурсы контракта",
                    "есть": have, "нужно": want}
        token = d.get("lease_token")
        inst = d.get("instance_id")
        if any(l[1] != token for l in leases):
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "секрет аренды не совпадает"}
        if any(l[2] != inst for l in leases):
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "аренда принадлежит другому процессу"}
        fencing = {l[0]: l[3] for l in leases}
        want_f = d.get("fencing")
        # 🔴 Поколение обязательно: раньше отсутствие поля просто пропускало
        # сравнение, и устаревшая аренда проходила молча.
        if not isinstance(want_f, dict) or not want_f:
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": "нужно прислать поколения аренды по всем ресурсам",
                    "текущее": fencing}
        try:
            given = {k: int(v) for k, v in want_f.items()}
        except (TypeError, ValueError):
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "поколения должны быть числами"}
        if given != fencing:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": "поколение аренды устарело",
                    "текущее": fencing}

        err, ltype, loc = check_locator(d.get("kind"), d.get("locator"))
        if err:
            con.execute("ROLLBACK")
            return {"ok": False, "причина": err}

        digest = str(d.get("digest") or "").lower()
        alg = d.get("digest_alg") or DIGEST_ALGS.get(ltype, "")
        if ltype in IMMUTABLE:
            err = check_digest(ltype, alg, digest, loc)
            if err:
                con.execute("ROLLBACK")
                return {"ok": False, "причина": err}

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
        con.execute("INSERT INTO work_products "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, task_id, ver, sha, slot, d.get("kind"), ltype,
                     json.dumps(loc, ensure_ascii=False, sort_keys=True),
                     alg, digest or "", d.get("size"), agent, inst or "",
                     json.dumps(fencing, sort_keys=True), "candidate", sup, idem,
                     req_sha, json.dumps(d.get("metadata", {}), ensure_ascii=False),
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

        # 🔴 Результат, принятый в составе передачи, ЗАПЕЧАТАН. Иначе поздняя
        # неудачная проверка сняла бы подтверждение уже после приёмки, и
        # завершённая задача перестала бы удовлетворять собственному основанию.
        # Найденный позже дефект — это новая задача, а не переписывание истории.
        sealed = con.execute(
            "SELECT h.handoff_id FROM handoff_products hp "
            "JOIN handoffs h ON h.handoff_id = hp.handoff_id "
            "WHERE hp.product_id=? AND h.status='accepted' LIMIT 1",
            (pid,)).fetchone()
        if sealed:
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": f"результат принят в составе передачи {sealed[0]} и "
                               f"запечатан: заводи отдельную задачу, историю "
                               f"приёмки не переписываем"}

        act = contracts_mod.active(con, task_id)
        if not act or act[0] != ver or act[2] != sha:
            con.execute("ROLLBACK")
            return {"ok": False,
                    "причина": "контракт продукта больше не действующий — "
                               "проверять нечего"}
        out = next((o for o in act[1].get("outputs", []) if o.get("slot") == slot), None)
        required = list(out.get("checks", [])) if out else []
        # digest_verified требуется всегда, даже если контракт её не перечислил
        if name not in required and name != DIGEST_CHECK:
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
        new_state = "verified" if verified else (
            "candidate" if state in ("candidate", "verified") else state)
        if new_state != state:
            con.execute("UPDATE work_products SET state=? WHERE product_id=?",
                        (new_state, pid))
        con.execute("COMMIT")
        return {"ok": True, "попытка": attempt, "состояние_продукта": new_state,
                "почему": why2}
    except Exception as e:
        con.execute("ROLLBACK")
        return {"ok": False, "причина": f"сбой: {e}"}


def _verify(con, pid, required, ltype, digest, state):
    """Подтверждён ли продукт.

    🔴 Считается ПО ПОСЛЕДНИМ попыткам, и результат применяется в обе стороны:
    поздняя неудача снимает подтверждение обратно в кандидаты. Иначе продукт,
    у которого последний прогон провалился, оставался бы «проверенным».
    """
    if state in ("superseded", "rejected"):
        return False, f"продукт в состоянии {state}"
    if ltype not in IMMUTABLE:
        # Путь на диске не может закрыть обязательный слот: содержимое по нему
        # завтра будет другим, и доказать происхождение нечем.
        return False, ("обычный путь остаётся кандидатом: подтвердить можно только "
                       "неизменяемый адрес")
    if not digest:
        return False, "нет отпечатка"
    # 🔴 Сверка отпечатка обязательна ВСЕГДА, независимо от того, какие проверки
    # перечислил автор контракта: без неё «подтверждено» означало бы лишь то,
    # что кто-то нажал кнопку.
    names = list(dict.fromkeys(list(required) + [DIGEST_CHECK]))
    for name in names:
        row = con.execute("SELECT status FROM product_checks WHERE product_id=? AND "
                          "check_name=? ORDER BY attempt DESC LIMIT 1",
                          (pid, name)).fetchone()
        if not row:
            return False, f"проверка {name} ещё не выполнялась"
        if row[0] != "passed":
            return False, f"последняя попытка проверки {name}: {row[0]}"
    return True, "все обязательные проверки пройдены, отпечаток сверен"


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
