#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Регрессии реестра продуктов: по одной на каждое требование архитектора."""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

API = "http://127.0.0.1:8010"
KEY = open("/opt/agent-control/api.key").read().strip()
RUN = uuid.uuid4().hex[:6]
N = [0, 0]
# 🔴 Настоящий коммит из архива: без него системная сверка отпечатка провалится,
# и проверить подтверждение было бы нечем. Поддельный используем отдельно.
import subprocess
COMMIT = subprocess.run(["git", "-C", "/srv/agents/store.git", "rev-parse",
                         "refs/heads/main"], capture_output=True,
                        text=True).stdout.strip() or "a" * 40
FAKE = "f" * 40


def call(path, key=KEY, **p):
    r = urllib.request.Request(API + path, data=json.dumps(p).encode(),
                               headers={"Content-Type": "application/json",
                                        "X-Api-Key": key})
    try:
        with urllib.request.urlopen(r, timeout=20) as f:
            return json.loads(f.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def check(name, cond, detail=""):
    N[0] += 1
    if cond:
        N[1] += 1
        print(f"  ✔ {name}")
    else:
        print(f"  ✘ {name}   {detail}")


def contract(agent, res, checks=("tests",), kind="git_commit", required=True):
    return {"schema_version": 1, "objective": "проверка реестра", "assignee": agent,
            "resources": [res],
            "outputs": [{"slot": "impl", "kind": kind, "required": required,
                         "checks": list(checks)}],
            "constraints": {"forbidden_actions": [], "deadline": None},
            "handoff_to": "most"}


def prepare(name, agent="exec1", checks=("tests",), kind="git_commit"):
    """Задача с контрактом + аренда: законный путь до регистрации результата."""
    task = f"T-{name}-{RUN}"
    res = "branch:" + uuid.uuid4().hex[:8]
    inst = str(uuid.uuid4())
    r = call("/task", task_id=task, title=name, agent_id=agent, state="assigned",
             contract=contract(agent, res, checks, kind))
    assert r.get("ok"), r
    g = call("/acquire", agent_id=agent, instance_id=inst, task_id=task,
             resources=[res])
    assert g.get("ok"), g
    c = call("/contract", task_id=task)
    return dict(task=task, res=res, inst=inst, lease=g["lease_token"],
                fencing=g["fencing"], version=c["версия"], sha=c["отпечаток"],
                agent=agent)


def reg(ctx, **over):
    body = dict(task_id=ctx["task"], agent_id=ctx["agent"],
                contract_version=ctx["version"], contract_sha256=ctx["sha"],
                output_slot="impl", kind="git_commit",
                locator={"type": "git", "repository": "agent-store",
                         "commit": COMMIT},
                digest=COMMIT, digest_alg="git_sha1", lease_token=ctx["lease"],
                instance_id=ctx["inst"], fencing=ctx["fencing"],
                idempotency_key=uuid.uuid4().hex)
    body.update(over)
    return call("/product/register", **body)


print("1. Результат регистрируется по законному пути")
c1 = prepare("ok")
r = reg(c1)
check("продукт зарегистрирован", r.get("ok"), r)
check("состояние — кандидат", r.get("состояние") == "candidate", r)
# 🔴 Сверка отпечатка добавляется в контракт сама: без неё подтверждать нечего
check("названы требуемые проверки",
      r.get("требуются_проверки") == ["digest_verified", "tests"], r)
pid = r.get("product_id")

print("")
print("2. Чужой агент результат не регистрирует")
r = reg(c1, agent_id="exec2")
check("🔴 чужой агент отклонён", not r.get("ok"), r)

print("")
print("3. Без действующей аренды результат не принимается")
c3 = prepare("nolease")
call("/release", lease_token=c3["lease"])
r = reg(c3)
check("🔴 без аренды отклонено", not r.get("ok"), r)
check("причина названа", "аренд" in str(r.get("причина", "")), r)

print("")
print("4. Аренда, отозванная удержанием, не подходит")
c4 = prepare("hold")
call("/hold", resource=c4["res"], reason="расследование",
     expires_at=int(time.time()) + 3600)
r = reg(c4)
check("🔴 после удержания отклонено", not r.get("ok"), r)
call("/unhold", resource=c4["res"])

print("")
print("5. Старая версия или неверный отпечаток контракта отклоняются")
r = reg(c1, contract_version=c1["version"] + 5)
check("🔴 чужая версия отклонена", not r.get("ok"), r)
r = reg(c1, contract_sha256="c" * 64)
check("🔴 неверный отпечаток отклонён", not r.get("ok"), r)

print("")
print("6. Несуществующий слот и неверный вид отклоняются")
r = reg(c1, output_slot="выдуманный")
check("🔴 нет такого слота", not r.get("ok"), r)
r = reg(c1, kind="report")
check("🔴 вид не совпадает с контрактом", not r.get("ok"), r)

print("")
print("7. Повтор с тем же ключом не создаёт дубль")
c7 = prepare("idem")
key = uuid.uuid4().hex
a = reg(c7, idempotency_key=key)
b = reg(c7, idempotency_key=key)
check("первый принят", a.get("ok"), a)
check("🔴 повтор вернул тот же продукт", b.get("product_id") == a.get("product_id"),
      (a.get("product_id"), b.get("product_id")))
check("повтор помечен как повтор", b.get("повтор"), b)

print("")
print("8. Второй результат в слот требует явной замены")
r = reg(c7)
check("🔴 без supersedes отклонено", not r.get("ok"), r)
check("текущий продукт назван", r.get("текущий") == a.get("product_id"), r)
r = reg(c7, supersedes=a.get("product_id"))
check("с указанием замены принято", r.get("ok"), r)
if r.get("ok"):
    old = call("/product", product_id=a["product_id"])
    check("прошлый стал заменённым",
          old.get("продукт", {}).get("state") == "superseded", old.get("продукт"))

print("")
print("9. Обычный путь не становится подтверждённым")
c9 = prepare("path", kind="report")
r = reg(c9, kind="report", locator={"type": "path", "path": "/tmp/результат.txt"},
        digest="", digest_alg="")
check("путь принят как кандидат", r.get("ok"), r)
check("помечен как изменяемый", r.get("неизменяемый") is False, r)
if r.get("ok"):
    p9 = r["product_id"]
    call("/product/check", agent_id="exec1", product_id=p9, check_name="tests",
         status="passed", evidence="журнал прогона")
    st = call("/product", product_id=p9)
    check("🔴 остался кандидатом даже после проверок",
          st.get("продукт", {}).get("state") == "candidate", st.get("продукт"))

print("")
print("10. Системную проверку производитель не записывает")
c10 = prepare("sys", checks=("digest_verified",))
r = reg(c10)
p10 = r.get("product_id")
r = call("/product/check", agent_id="exec1", product_id=p10,
         check_name="digest_verified", status="passed", evidence="сам себе")
check("🔴 digest_verified исполнителем отклонён", not r.get("ok"), r)
check("причина названа", "координатор" in str(r.get("причина", "")), r)

print("")
print("11. Независимую проверку автор результата не подтверждает")
c11 = prepare("audit", checks=("audit",))
r = reg(c11)
p11 = r.get("product_id")
r = call("/product/check", agent_id="exec1", product_id=p11, check_name="audit",
         status="passed", evidence="сам себя проверил")
check("🔴 автор не может подтвердить аудит", not r.get("ok"), r)
r = call("/product/check", agent_id="exec2", product_id=p11, check_name="audit",
         status="passed", evidence="проверил другой агент")
check("другой агент — может", r.get("ok"), r)

print("")
print("12. Неуспешная проверка слот не закрывает")
c12 = prepare("failed")
r = reg(c12)
p12 = r.get("product_id")
r = call("/product/check", agent_id="exec1", product_id=p12, check_name="tests",
         status="failed", evidence="упало три теста")
check("неуспех записан", r.get("ok"), r)
check("🔴 продукт НЕ подтверждён", r.get("состояние_продукта") != "verified", r)
r = call("/product/check", agent_id="exec1", product_id=p12, check_name="tests",
         status="passed", evidence="после починки прошло")
check("🔴 без сверки отпечатка продукт НЕ подтверждён",
      r.get("состояние_продукта") == "candidate", r)
subprocess.run(["python3", "/opt/agent-control/verifier.py", "--product", p12],
               capture_output=True, text=True)
st12 = call("/product", product_id=p12)
check("🔴 после сверки подтверждён",
      st12.get("продукт", {}).get("state") == "verified", st12.get("продукт"))
check("это вторая попытка, история сохранена", r.get("попытка") == 2, r)
st = call("/product", product_id=p12)
tests_only = [c for c in st.get("проверки", []) if c["проверка"] == "tests"]
check("обе попытки проверки в истории", len(tests_only) == 2, tests_only)

print("")
print("13. Подтверждение продукта задачу НЕ завершает")
tasks = call("/status")["задачи"]
t12 = next((t for t in tasks if t["task_id"] == c12["task"]), None)
check("🔴 задача осталась в работе", t12 and t12["state"] == "running", t12)

print("")
print("14. Проверка, которой нет в контракте, не принимается")
r = call("/product/check", agent_id="exec1", product_id=p12, check_name="audit",
         status="passed", evidence="лишняя")
check("🔴 лишняя проверка отклонена", not r.get("ok"), r)

print("")
print("15. Контракт с неизвестной проверкой не заводится")
r = call("/task", task_id=f"T-badcheck-{RUN}", title="плохой", agent_id="exec1",
         contract=contract("exec1", "branch:x", checks=("выдуманная",)))
check("🔴 неизвестная проверка отклонена заранее", not r.get("ok"), r)

print("")
print("16. Ссылка git требует зафиксированного коммита")
c16 = prepare("ref", kind="git_ref")
r = reg(c16, kind="git_ref",
        locator={"type": "git", "repository": "agent-store", "ref": "refs/tasks/x"},
        digest=COMMIT)
check("🔴 ссылка без target_commit отклонена", not r.get("ok"), r)
r = reg(c16, kind="git_ref",
        locator={"type": "git", "repository": "agent-store", "ref": "refs/tasks/x",
                 "target_commit": COMMIT}, digest=COMMIT)
check("ссылка с зафиксированным коммитом принята", r.get("ok"), r)

print("")
print("17. Объект хранилища требует версии")
c17 = prepare("obj", kind="object")
r = reg(c17, kind="object",
        locator={"type": "object_storage", "bucket": "agent-archive", "key": "k"},
        digest="d" * 64, digest_alg="sha256")
check("🔴 объект без version_id отклонён", not r.get("ok"), r)

print("")
print("18. Псевдонимы хранилищ и совместимость вида с адресом")
c18 = prepare("alias")
r = reg(c18, locator={"type": "git", "repository": "/srv/agents/store.git",
                      "commit": COMMIT})
check("🔴 произвольный путь вместо псевдонима отклонён", not r.get("ok"), r)
r = reg(c18, locator={"type": "object_storage", "bucket": "agent-archive",
                      "key": "k", "version_id": "v"}, digest="d" * 64,
        digest_alg="sha256")
check("🔴 git_commit в объектном хранилище отклонён", not r.get("ok"), r)
r = reg(c18, digest=FAKE)
check("🔴 отпечаток, не совпадающий с адресом, отклонён", not r.get("ok"), r)

print("")
print("19. Системная сверка отпечатка: настоящий объект")
c19 = prepare("verify")
r = reg(c19)
p19 = r.get("product_id")
check("продукт зарегистрирован", r.get("ok"), r)
out = subprocess.run(["python3", "/opt/agent-control/verifier.py",
                      "--product", p19], capture_output=True, text=True).stdout
check("🔴 сверка прошла", "passed" in out, out.strip()[:200])
st = call("/product", product_id=p19)
checks19 = {c["проверка"]: c["статус"] for c in st.get("проверки", [])}
check("digest_verified записана системой", checks19.get("digest_verified") == "passed",
      checks19)
call("/product/check", agent_id="exec1", product_id=p19, check_name="tests",
     status="passed", evidence="прогон")
st = call("/product", product_id=p19)
check("🔴 продукт подтверждён после сверки и проверок",
      st.get("продукт", {}).get("state") == "verified", st.get("продукт"))

print("")
print("20. Системная сверка: поддельный объект")
c20 = prepare("fake")
r = reg(c20, locator={"type": "git", "repository": "agent-store", "commit": FAKE},
        digest=FAKE)
p20 = r.get("product_id")
check("поддельный принят как кандидат", r.get("ok"), r)
out = subprocess.run(["python3", "/opt/agent-control/verifier.py",
                      "--product", p20], capture_output=True, text=True).stdout
check("🔴 сверка провалилась", "failed" in out, out.strip()[:200])
call("/product/check", agent_id="exec1", product_id=p20, check_name="tests",
     status="passed", evidence="прогон")
st = call("/product", product_id=p20)
check("🔴 поддельный НЕ подтверждён даже при пройденных проверках",
      st.get("продукт", {}).get("state") == "candidate", st.get("продукт"))

print("")
print("21. Поздняя неудача снимает подтверждение")
r = call("/product/check", agent_id="exec1", product_id=p19, check_name="tests",
         status="failed", evidence="сломалось позже")
check("🔴 продукт вернулся в кандидаты", r.get("состояние_продукта") == "candidate", r)
r = call("/product/check", agent_id="exec1", product_id=p19, check_name="tests",
         status="passed", evidence="починили")
check("после починки снова подтверждён", r.get("состояние_продукта") == "verified", r)

print("")
print("22. Протухшая аренда и неполный набор не годятся")
c22 = prepare("stale")
import sqlite3 as _s
con = _s.connect("/opt/agent-control/cp.db")
con.execute("UPDATE leases SET expires=? WHERE task_id=?",
            (int(time.time()) - 5, c22["task"]))
con.commit(); con.close()
r = reg(c22)
check("🔴 протухшая аренда отклонена", not r.get("ok"), r)

print("")
print("23. Поколение аренды обязательно")
c23 = prepare("fence")
r = reg(c23, fencing={})
check("🔴 без поколений отклонено", not r.get("ok"), r)
r = reg(c23, fencing={k: v + 1 for k, v in c23["fencing"].items()})
check("🔴 устаревшее поколение отклонено", not r.get("ok"), r)

print("")
print("24. Ключ повтора не пересекает задачи и содержимое")
c24a = prepare("idemA")
c24b = prepare("idemB")
k = uuid.uuid4().hex
a24 = reg(c24a, idempotency_key=k)
check("первый принят", a24.get("ok"), a24)
b24 = reg(c24b, idempotency_key=k)
check("🔴 тот же ключ в другой задаче не вернул чужой продукт",
      b24.get("product_id") != a24.get("product_id"), (a24, b24))
c24c = reg(c24a, idempotency_key=k, output_slot="impl", digest=FAKE,
           locator={"type": "git", "repository": "agent-store", "commit": FAKE})
check("🔴 тот же ключ с другим содержимым — отказ", not c24c.get("ok"), c24c)

print("")
print("25. Непроверяемый тип не копит неудачные попытки")
c25 = prepare("nostore", kind="report")
r = reg(c25, kind="report",
        locator={"type": "object_storage", "bucket": "agent-archive",
                 "key": "agent-history/tasks/x.json", "version_id": "v1"},
        digest="e" * 64, digest_alg="sha256")
check("объектный продукт зарегистрирован", r.get("ok"), r)
p25 = r.get("product_id")
for _ in range(2):
    subprocess.run(["python3", "/opt/agent-control/verifier.py"],
                   capture_output=True, text=True)
st = call("/product", product_id=p25)
digest_tries = [c for c in st.get("проверки", []) if c["проверка"] == "digest_verified"]
check("🔴 два прохода не создали ни одной попытки", not digest_tries, digest_tries)
out = subprocess.run(["python3", "/opt/agent-control/verifier.py", "--product", p25],
                     capture_output=True, text=True)
check("ручной запуск честно отказывает", out.returncode == 2, out.stdout.strip()[:120])
check("продукт остался кандидатом",
      call("/product", product_id=p25).get("продукт", {}).get("state") == "candidate")

print("")
print("26. Ключ вне отведённого пространства отклоняется")
r = reg(c25, kind="report",
        locator={"type": "object_storage", "bucket": "agent-archive",
                 "key": "db/prod-dump.sql", "version_id": "v1"},
        digest="e" * 64, digest_alg="sha256")
check("🔴 чужое пространство корзины отклонено", not r.get("ok"), r)

print("")
print("27. Файл, выданный за коммит, не проходит сверку")
blob = subprocess.run(["git", "-C", "/srv/agents/store.git", "rev-parse",
                       COMMIT + ":.gitignore"], capture_output=True,
                      text=True).stdout.strip()
if not blob:
    blob = subprocess.run(
        ["bash", "-c", f"git -C /srv/agents/store.git ls-tree {COMMIT} | "
                       f"awk '$2==\"blob\"{{print $3; exit}}'"],
        capture_output=True, text=True).stdout.strip()
if blob:
    c27 = prepare("blob")
    r = reg(c27, locator={"type": "git", "repository": "agent-store",
                          "commit": blob}, digest=blob)
    if r.get("ok"):
        out = subprocess.run(["python3", "/opt/agent-control/verifier.py",
                              "--product", r["product_id"]],
                             capture_output=True, text=True).stdout
        check("🔴 файл, выданный за коммит, отклонён", "failed" in out,
              out.strip()[:160])
        st = call("/product", product_id=r["product_id"])
        check("остался кандидатом",
              st.get("продукт", {}).get("state") == "candidate", st.get("продукт"))
    else:
        check("файл, выданный за коммит, отклонён", False, r)
else:
    check("файл, выданный за коммит, отклонён", False, "не нашёл blob в архиве")

print("")
print("28. Внешние ключи действительно в схеме и работают")
import sqlite3 as _sq
con28 = _sq.connect("/opt/agent-control/cp.db")
fk_p = list(con28.execute("PRAGMA foreign_key_list(work_products)"))
fk_c = list(con28.execute("PRAGMA foreign_key_list(product_checks)"))
# составной ключ даёт по строке на КАЖДЫЙ столбец — считаем сами ключи
check("🔴 два внешних ключа у продуктов", len({r[0] for r in fk_p}) == 2, fk_p)
check("🔴 один внешний ключ у проверок", len({r[0] for r in fk_c}) == 1, fk_c)
con28.execute("PRAGMA foreign_keys=ON")
try:
    con28.execute("INSERT INTO product_checks VALUES(?,?,?,?,?,?,?,?,?,?)",
                  ("chk-сирота", "нет-такого-продукта", "tests", 1, "passed",
                   "exec1", None, "", None, int(time.time())))
    con28.commit()
    check("🔴 вставка сироты отклонена", False, "сирота прошла")
except _sq.IntegrityError as e:
    check("🔴 вставка сироты отклонена", True)
con28.close()

print("")
print("29. Ключ повтора после смены контракта не возвращает старый продукт")
c29 = prepare("verchange")
k29 = uuid.uuid4().hex
a29 = reg(c29, idempotency_key=k29)
check("продукт версии 1 создан", a29.get("ok"), a29)
call("/release", lease_token=c29["lease"])
call("/task", task_id=c29["task"], title="verchange", agent_id="exec1",
     state="blocked")
call("/task", task_id=c29["task"], title="verchange", agent_id="exec1",
     state="assigned",
     contract=dict(contract("exec1", c29["res"]), objective="изменённая цель"))
c2 = call("/contract", task_id=c29["task"])
g = call("/acquire", agent_id="exec1", instance_id=c29["inst"],
         task_id=c29["task"], resources=[c29["res"]])
if g.get("ok") and c2.get("версия", 1) > 1:
    b29 = call("/product/register", task_id=c29["task"], agent_id="exec1",
               contract_version=c2["версия"], contract_sha256=c2["отпечаток"],
               output_slot="impl", kind="git_commit",
               locator={"type": "git", "repository": "agent-store",
                        "commit": COMMIT},
               digest=COMMIT, digest_alg="git_sha1", lease_token=g["lease_token"],
               instance_id=c29["inst"], fencing=g["fencing"],
               idempotency_key=k29)
    # 🔴 Правильный ответ — именно ОТКАЗ: тот же ключ после смены условий
    # больше не повтор. Возврат старого продукта был бы подлогом.
    check("🔴 после смены контракта тот же ключ даёт отказ",
          not b29.get("ok") and "другим содержимым" in str(b29.get("причина", "")),
          b29)
else:
    check("после смены контракта тот же ключ не вернул старый продукт", False,
          (g, c2))

print("")
print(f"ИТОГ: {N[1]} из {N[0]}")
sys.exit(0 if N[1] == N[0] else 1)
