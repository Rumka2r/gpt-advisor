#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Системный проверяющий отпечатков — завершает реестр продуктов.

Зачем: без него «подтверждено» означало бы лишь то, что кто-то нажал кнопку.
Проверяющий идёт к настоящему хранилищу и убеждается, что объект существует и
совпадает с заявленным отпечатком.

🔴 Порядок работы жёсткий:
    короткая транзакция создала кандидата
    → сверка БЕЗ транзакции (сеть и диск)
    → короткая транзакция записывает результат
Иначе сетевой таймаут держал бы блокировку базы, от которой зависят аренды и
сердцебиение ВСЕХ агентов.

🔴 Свидетельство составляет сам проверяющий, а не тот, кого проверяют.
Запускается от root, ходит в координатор служебным путём.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import products                       # noqa: E402

ROOT = os.environ.get("CP_ROOT", "/opt/agent-control")
DB = os.environ.get("CP_DB", os.path.join(ROOT, "cp.db"))
API = os.environ.get("CP_API", "http://127.0.0.1:8010")


def now():
    return int(time.time())


def sh(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
    except Exception as e:
        return 255, str(e)


# ── Сверка по видам хранилищ ────────────────────────────────────────────────

def verify_git(loc, digest):
    """Существует ли объект и совпадает ли отпечаток. Возвращает
    (статус, свидетельство)."""
    repo = products.GIT_REPOSITORIES.get(loc.get("repository"))
    if not repo:
        return "error", {"причина": "репозиторий не значится в каталоге"}
    if not os.path.isdir(repo):
        return "error", {"причина": f"хранилище {repo} недоступно"}

    target = loc.get("target_commit") or loc.get("commit")
    if loc.get("ref"):
        # 🔴 Ссылку разрешаем ТОЛЬКО чтобы убедиться: она и правда указывает на
        # зафиксированный коммит. Личность продукта — коммит, не ссылка.
        code, out = sh(["git", "-C", repo, "rev-parse", "--verify", "--quiet",
                        loc["ref"]])
        if code != 0:
            return "failed", {"причина": f"ссылки {loc['ref']} в хранилище нет"}
        if out.strip() != target:
            return "failed", {"причина": "ссылка указывает на другой коммит",
                              "ожидался": target, "фактически": out.strip()}

    obj = target
    if loc.get("path"):
        code, out = sh(["git", "-C", repo, "rev-parse",
                        f"{target}:{loc['path']}"])
        if code != 0:
            return "failed", {"причина": f"пути {loc['path']} в коммите нет"}
        obj = out.strip()

    # 🔴 `^{object}` подтверждает существование ЛЮБОГО объекта: файл с 40-значным
    # идентификатором прошёл бы как коммит. Требуем именно тот тип, который
    # заявлен видом результата.
    base = loc.get("target_commit") or loc.get("commit")
    code, out = sh(["git", "-C", repo, "cat-file", "-e", base + "^{commit}"])
    if code != 0:
        return "failed", {"причина": "это не коммит", "объект": base}
    code, out = sh(["git", "-C", repo, "cat-file", "-e", obj + "^{object}"])
    if code != 0:
        return "failed", {"причина": "объекта нет в хранилище", "объект": obj}

    if obj != digest:
        return "failed", {"причина": "отпечаток не совпадает",
                          "ожидался": digest, "фактически": obj}
    code, kind = sh(["git", "-C", repo, "cat-file", "-t", obj])
    return "passed", {"источник": loc.get("repository"), "объект": obj,
                      "тип_объекта": kind.strip(), "ожидался": digest,
                      "фактически": obj}


def verify_object_storage(loc, digest):
    # 🔴 Пока не читаем КОНКРЕТНУЮ версию объекта — не подтверждаем ничего.
    # Прежняя проверка смотрела на текущий объект по ключу, то есть доказывала
    # не то, что записано в продукте.
    return "error", {"причина": "сверка объектного хранилища не реализована: "
                                "нужна потоковая проверка указанной версии; "
                                "до этого такие продукты остаются кандидатами"}


def _verify_object_storage_disabled(loc, digest):
    store = products.OBJECT_STORES.get(loc.get("bucket"))
    if not store:
        return "error", {"причина": "хранилище не значится в каталоге"}
    remote = os.environ.get("RCLONE_REMOTE", "hetzner-s3")
    path = f"{remote}:{store['bucket']}/{loc['key']}"
    code, out = sh(["rclone", "lsjson", "--hash", path], timeout=300)
    if code != 0:
        # Недоступность хранилища — это ошибка проверки, а не провал продукта:
        # разница важна, иначе временный сбой сети отбраковывал бы работу.
        return "error", {"причина": "хранилище недоступно", "вывод": out[:200]}
    try:
        items = json.loads(out)
    except ValueError:
        return "error", {"причина": "не удалось разобрать ответ хранилища"}
    if not items:
        return "failed", {"причина": "объекта нет"}
    got = (items[0].get("Hashes") or {}).get("sha256", "").lower()
    if not got:
        return "error", {"причина": "хранилище не сообщило SHA-256; нужна "
                                    "потоковая сверка, она пока не сделана"}
    if got != digest:
        return "failed", {"причина": "отпечаток не совпадает",
                          "ожидался": digest, "фактически": got}
    return "passed", {"источник": loc.get("bucket"), "ключ": loc["key"],
                      "версия": loc.get("version_id"), "ожидался": digest,
                      "фактически": got}


# ── Запись результата ───────────────────────────────────────────────────────

def record(con, product_id, status, evidence):
    """Записать итог сверки служебным путём: обычному агенту эта проверка
    недоступна по правилам каталога."""
    import contracts
    evidence = dict(evidence)
    evidence["verified_at"] = now()
    return products.record_check(
        con, {"product_id": product_id, "check_name": products.DIGEST_CHECK,
              "status": status, "agent_id": "system", "evidence": evidence},
        contracts, system=True)


def pending(con):
    """Кандидаты, у которых сверка ещё не прошла успешно.

    🔴 Берём ТОЛЬКО те виды хранилищ, которые умеем проверять. Иначе каждый
    запуск снова находил бы объектных кандидатов и дописывал им очередную
    неудачную попытку — бесконечный рост таблицы проверок и постоянный шум.
    """
    allowed = tuple(products.VERIFIABLE)
    marks = ",".join("?" for _ in allowed)
    return con.execute(f"""
        SELECT p.product_id, p.locator_type, p.locator, p.digest
        FROM work_products p
        WHERE p.state = 'candidate'
          AND p.locator_type IN ({marks})
          AND NOT EXISTS (
            SELECT 1 FROM product_checks c
            WHERE c.product_id = p.product_id AND c.check_name = ?
              AND c.status = 'passed')
        ORDER BY p.registered_at
    """, (*allowed, products.DIGEST_CHECK)).fetchall()


def verify_one(product_id, ltype, locator, digest):
    """Сама сверка — БЕЗ открытой транзакции."""
    loc = json.loads(locator)
    if ltype == "git":
        return verify_git(loc, digest)
    if ltype == "object_storage":
        return verify_object_storage(loc, digest)
    return "error", {"причина": f"нечего сверять для {ltype}"}


def main():
    import sqlite3
    ap = argparse.ArgumentParser(description="системная сверка отпечатков")
    ap.add_argument("--once", action="store_true", help="один проход и выйти")
    ap.add_argument("--product", help="сверить один конкретный продукт")
    a = ap.parse_args()

    con = sqlite3.connect(DB, timeout=30, isolation_level=None)
    con.execute("PRAGMA busy_timeout=30000")
    # 🔴 Внешние ключи включаются НА КАЖДОМ соединении: иначе проверяющий писал
    # бы мимо ограничений, которые соблюдает координатор.
    con.execute("PRAGMA foreign_keys=ON")

    if a.product:
        row = con.execute("SELECT product_id, locator_type, locator, digest "
                          "FROM work_products WHERE product_id=?",
                          (a.product,)).fetchone()
        if row and row[1] not in products.VERIFIABLE:
            # Отказ БЕЗ записи проверки: иначе ручной запуск тоже засорял бы
            # историю неудачными попытками.
            print(f"тип {row[1]} пока не поддерживает системную сверку")
            return 2
        rows = [row] if row else []
    else:
        rows = pending(con)

    if not rows:
        print("нечего сверять")
        return 0

    for pid, ltype, locator, digest in rows:
        # 🔴 Сверка идёт вне транзакции: соединение с базой на это время
        # свободно, аренды и сердцебиение остальных агентов не ждут.
        status, evidence = verify_one(pid, ltype, locator, digest)
        res = record(con, pid, status, evidence)
        mark = {"passed": "✔", "failed": "✘", "error": "?"}.get(status, "?")
        print(f"  {mark} {pid}: {status} — {evidence.get('причина', 'совпало')}")
        if not res.get("ok"):
            print(f"     🔴 запись не прошла: {res.get('причина')}")
        else:
            print(f"     продукт: {res.get('состояние_продукта')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
