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
COMMIT = "a" * 40


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
                locator={"type": "git", "repository": "agent-store", "commit": COMMIT},
                digest="sha256:" + "b" * 64, lease_token=ctx["lease"],
                instance_id=ctx["inst"], fencing=ctx["fencing"],
                idempotency_key=uuid.uuid4().hex)
    body.update(over)
    return call("/product/register", **body)


print("1. Результат регистрируется по законному пути")
c1 = prepare("ok")
r = reg(c1)
check("продукт зарегистрирован", r.get("ok"), r)
check("состояние — кандидат", r.get("состояние") == "candidate", r)
check("названы требуемые проверки", r.get("требуются_проверки") == ["tests"], r)
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
c9 = prepare("path")
r = reg(c9, locator={"type": "path", "path": "/tmp/результат.txt"}, digest="")
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
check("🔴 успешный повтор подтвердил продукт",
      r.get("состояние_продукта") == "verified", r)
check("это вторая попытка, история сохранена", r.get("попытка") == 2, r)
st = call("/product", product_id=p12)
check("обе попытки в истории", len(st.get("проверки", [])) == 2, st.get("проверки"))

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
        locator={"type": "git", "repository": "agent-store", "ref": "refs/tasks/x"})
check("🔴 ссылка без target_commit отклонена", not r.get("ok"), r)
r = reg(c16, kind="git_ref",
        locator={"type": "git", "repository": "agent-store", "ref": "refs/tasks/x",
                 "target_commit": COMMIT})
check("ссылка с зафиксированным коммитом принята", r.get("ok"), r)

print("")
print("17. Объект хранилища требует версии")
c17 = prepare("obj", kind="object")
r = reg(c17, kind="object",
        locator={"type": "object_storage", "bucket": "b", "key": "k"})
check("🔴 объект без version_id отклонён", not r.get("ok"), r)

print("")
print(f"ИТОГ: {N[1]} из {N[0]}")
sys.exit(0 if N[1] == N[0] else 1)
