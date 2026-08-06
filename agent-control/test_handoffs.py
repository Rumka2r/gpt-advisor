#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Регрессии передачи результата: по одной на каждое требование архитектора."""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

API = "http://127.0.0.1:8010"
KEY = open("/opt/agent-control/api.key").read().strip()
RUN = uuid.uuid4().hex[:6]
N = [0, 0]
COMMIT = subprocess.run(["git", "-C", "/srv/agents/store.git", "rev-parse",
                         "refs/heads/main"], capture_output=True,
                        text=True).stdout.strip()


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


def contract(agent, res, to="exec2"):
    return {"schema_version": 1, "objective": "передача результата",
            "assignee": agent, "resources": [res],
            "outputs": [{"slot": "impl", "kind": "git_commit", "required": True,
                         "checks": ["tests"]}],
            "constraints": {"forbidden_actions": [], "deadline": None},
            "handoff_to": to}


def ready(name, agent="exec1", to="exec2", verify=True):
    """Задача с подтверждённым результатом — готовая к передаче."""
    task = f"H-{name}-{RUN}"
    res = "branch:" + uuid.uuid4().hex[:8]
    inst = str(uuid.uuid4())
    call("/task", task_id=task, title=name, agent_id=agent, state="assigned",
         contract=contract(agent, res, to))
    g = call("/acquire", agent_id=agent, instance_id=inst, task_id=task,
             resources=[res])
    c = call("/contract", task_id=task)
    p = call("/product/register", task_id=task, agent_id=agent,
             contract_version=c["версия"], contract_sha256=c["отпечаток"],
             output_slot="impl", kind="git_commit",
             locator={"type": "git", "repository": "agent-store", "commit": COMMIT},
             digest=COMMIT, digest_alg="git_sha1", lease_token=g["lease_token"],
             instance_id=inst, fencing=g["fencing"],
             idempotency_key=uuid.uuid4().hex)
    ctx = dict(task=task, res=res, inst=inst, lease=g.get("lease_token"),
               fencing=g.get("fencing"), version=c["версия"], sha=c["отпечаток"],
               agent=agent, product=p.get("product_id"), to=to)
    if verify:
        subprocess.run(["python3", "/opt/agent-control/verifier.py",
                        "--product", ctx["product"]], capture_output=True)
        call("/product/check", agent_id=agent, product_id=ctx["product"],
             check_name="tests", status="passed", evidence="прогон")
    return ctx


def offer(ctx, **over):
    body = dict(task_id=ctx["task"], agent_id=ctx["agent"],
                contract_version=ctx["version"], contract_sha256=ctx["sha"],
                products={"impl": ctx["product"]}, summary="сделано",
                known_issues=[], next_action="принять",
                lease_token=ctx["lease"], instance_id=ctx["inst"],
                fencing=ctx["fencing"], idempotency_key=uuid.uuid4().hex)
    body.update(over)
    return call("/handoff/offer", **body)


print("1. Передача по законному пути")
c1 = ready("ok")
st = call("/product", product_id=c1["product"])
check("результат подтверждён", st.get("продукт", {}).get("state") == "verified",
      st.get("продукт"))
r = offer(c1)
check("передача создана", r.get("ok"), r)
check("получатель взят из контракта", r.get("кому") == "exec2", r)
check("🔴 аренды отозваны", r.get("отозвано_аренд"), r)
check("задача ждёт решения", r.get("состояние_задачи") == "handoff_pending", r)
h1 = r.get("handoff_id")

print("")
print("2. После передачи исполнитель не уводит задачу сам")
r = call("/task", task_id=c1["task"], title="ok", agent_id="exec1", state="blocked")
check("🔴 blocked отклонён", not r.get("ok"), r)
r = call("/task", task_id=c1["task"], title="ok", agent_id="exec1", state="assigned")
check("🔴 assigned отклонён", not r.get("ok"), r)
g = call("/acquire", agent_id="exec1", instance_id=c1["inst"], task_id=c1["task"],
         resources=[c1["res"]])
check("новую аренду не выдают", not g.get("ok"), g)

print("")
print("3. Решение принимает только получатель")
# 🔴 Именно СВОИМ ключом: под административным можно действовать от чужого
# имени, и проверка ничего бы не значила.
K1 = open("/opt/agent-control/keys/exec1.key").read().strip()
r = call("/handoff/accept", key=K1, agent_id="exec1", handoff_id=h1)
check("🔴 отправитель принять не может", not r.get("ok"), r)
K2 = open("/opt/agent-control/keys/exec2.key").read().strip()
r = call("/handoff/reject", key=K2, agent_id="exec2", handoff_id=h1,
         reason="проверка прав")
check("получатель своим ключом — может", r.get("ok"), r)

print("")
print("4. Повтор предложения")
c4 = ready("idem")
k = uuid.uuid4().hex
a = offer(c4, idempotency_key=k)
check("первое предложение принято", a.get("ok"), a)
b = offer(c4, idempotency_key=k)
check("🔴 повтор вернул ту же передачу", b.get("handoff_id") == a.get("handoff_id"),
      (a, b))
check("помечен как повтор", b.get("повтор"), b)
c = offer(c4, idempotency_key=k, summary="другое описание")
check("🔴 тот же ключ с другим содержимым — отказ", not c.get("ok"), c)

print("")
print("5. Второй открытой передачи у задачи не бывает")
r = offer(c4)
check("🔴 вторая передача отклонена", not r.get("ok"), r)

print("")
print("6. Неподтверждённый результат не передаётся")
c6 = ready("cand", verify=False)
r = offer(c6)
check("🔴 кандидат отклонён", not r.get("ok"), r)
check("причина названа", "подтверждён" in str(r.get("причина", "")), r)

print("")
print("7. Не закрытый обязательный слот отклоняется")
c7 = ready("noslot")
r = offer(c7, products={})
check("🔴 пустой набор отклонён", not r.get("ok"), r)
r = offer(c7, products={"выдуманный": c7["product"]})
check("🔴 несуществующий слот отклонён", not r.get("ok"), r)

print("")
print("8. Чужой продукт не прикрепляется")
c8 = ready("alien")
r = offer(c7, products={"impl": c8["product"]})
check("🔴 продукт другой задачи отклонён", not r.get("ok"), r)

print("")
print("9. Без живой аренды передать нельзя")
c9 = ready("nolease")
call("/release", lease_token=c9["lease"])
r = offer(c9)
check("🔴 без аренды отклонено", not r.get("ok"), r)

print("")
print("10. Отказ возвращает задачу в assigned")
c10 = ready("reject")
r = offer(c10)
h10 = r.get("handoff_id")
r = call("/handoff/reject", agent_id="exec2", handoff_id=h10)
check("🔴 отказ без причины не принимается", not r.get("ok"), r)
r = call("/handoff/reject", agent_id="exec2", handoff_id=h10,
         reason="нужен ещё один прогон")
check("отказ принят", r.get("ok"), r)
check("🔴 задача вернулась в assigned", r.get("состояние_задачи") == "assigned", r)
g = call("/acquire", agent_id="exec1", instance_id=c10["inst"],
         task_id=c10["task"], resources=[c10["res"]])
check("🔴 после отказа аренда снова доступна", g.get("ok"), g)
tasks = {t["task_id"]: t["state"] for t in call("/status")["задачи"]}
check("и задача снова в работе", tasks.get(c10["task"]) == "running", tasks.get(c10["task"]))

print("")
print("11. Поздняя неудачная проверка ломает приём")
c11 = ready("late")
r = offer(c11)
h11 = r.get("handoff_id")
call("/product/check", agent_id="exec1", product_id=c11["product"],
     check_name="tests", status="failed", evidence="сломалось после передачи")
st = call("/product", product_id=c11["product"])
check("продукт вернулся в кандидаты",
      st.get("продукт", {}).get("state") == "candidate", st.get("продукт"))
r = call("/handoff/accept", agent_id="exec2", handoff_id=h11)
check("🔴 приём отклонён: результат больше не подтверждён", not r.get("ok"), r)

print("")
print("12. Приём завершает задачу")
c12 = ready("accept")
r = offer(c12)
h12 = r.get("handoff_id")
r = call("/handoff/accept", agent_id="exec2", handoff_id=h12)
check("приём выполнен", r.get("ok"), r)
check("🔴 задача завершена", r.get("состояние_задачи") == "done", r)
# 🔴 Повтор ТОГО ЖЕ решения — успех: ответ мог потеряться, и повторный запрос
# обязан вернуть тот же исход, а не «ошибку» уже завершённой задачи.
r = call("/handoff/accept", agent_id="exec2", handoff_id=h12)
check("🔴 повтор того же решения — успех", r.get("ok") and r.get("повтор"), r)
r = call("/handoff/reject", agent_id="exec2", handoff_id=h12, reason="передумал")
check("🔴 противоположное решение отклонено", not r.get("ok"), r)
g = call("/acquire", agent_id="exec1", instance_id=c12["inst"],
         task_id=c12["task"], resources=[c12["res"]])
check("🔴 после завершения аренду не выдают", not g.get("ok"), g)
p = call("/product/register", task_id=c12["task"], agent_id="exec1",
         contract_version=c12["version"], contract_sha256=c12["sha"],
         output_slot="impl", kind="git_commit",
         locator={"type": "git", "repository": "agent-store", "commit": COMMIT},
         digest=COMMIT, digest_alg="git_sha1", lease_token=c12["lease"],
         instance_id=c12["inst"], fencing=c12["fencing"],
         idempotency_key=uuid.uuid4().hex)
check("🔴 после завершения продукт не зарегистрировать", not p.get("ok"), p)

print("")
print("13. Фактический субъект отличается от исполнителя")
h = call("/handoff", handoff_id=h12)
rec = h.get("передача", {})
check("от кого — исполнитель", rec.get("from_agent") == "exec1", rec)
check("кому — из контракта", rec.get("to_agent") == "exec2", rec)
check("🔴 предложил — фактический субъект", rec.get("offered_by") == "legacy",
      rec.get("offered_by"))
check("решил — фактический субъект", rec.get("decided_by") == "legacy",
      rec.get("decided_by"))
check("история не переписана", rec.get("status") == "accepted", rec)

print("")
print("14. Принятый результат запечатан")
st = call("/product", product_id=c12["product"])
check("результат подтверждён", st.get("продукт", {}).get("state") == "verified",
      st.get("продукт"))
r = call("/product/check", agent_id="exec1", product_id=c12["product"],
         check_name="tests", status="failed", evidence="нашли дефект позже")
check("🔴 новая попытка отклонена", not r.get("ok"), r)
check("причина названа", "запечатан" in str(r.get("причина", "")), r)
st = call("/product", product_id=c12["product"])
check("🔴 результат остался подтверждённым",
      st.get("продукт", {}).get("state") == "verified", st.get("продукт"))
h = call("/handoff", handoff_id=h12)
check("передача осталась принятой",
      h.get("передача", {}).get("status") == "accepted", h.get("передача"))
tasks = {t["task_id"]: t["state"] for t in call("/status")["задачи"]}
check("задача осталась завершённой", c12["task"] not in tasks or
      tasks[c12["task"]] == "done", tasks.get(c12["task"]))

print("")
print("15. Повтор отказа с той же причиной — успех, с другой — отказ")
c15 = ready("idemrej")
r = offer(c15)
h15 = r.get("handoff_id")
r = call("/handoff/reject", agent_id="exec2", handoff_id=h15, reason="нужен прогон")
check("отказ принят", r.get("ok"), r)
r = call("/handoff/reject", agent_id="exec2", handoff_id=h15, reason="нужен прогон")
check("🔴 повтор с той же причиной — успех", r.get("ok") and r.get("повтор"), r)
r = call("/handoff/reject", agent_id="exec2", handoff_id=h15, reason="другая причина")
check("🔴 другая причина — отказ", not r.get("ok"), r)

print("")
print("16. Самопередача запрещена контрактом")
bad = contract("exec1", "branch:" + uuid.uuid4().hex[:8], to="exec1")
r = call("/task", task_id=f"H-self-{RUN}", title="самому себе", agent_id="exec1",
         contract=bad)
check("🔴 передача самому себе отклонена", not r.get("ok"), r)
bad2 = contract("exec1", "branch:" + uuid.uuid4().hex[:8], to="выдуманный")
r = call("/task", task_id=f"H-unknown-{RUN}", title="неизвестному", agent_id="exec1",
         contract=bad2)
check("🔴 неизвестный получатель отклонён", not r.get("ok"), r)

print("")
print("17. Конфликт аренды попадает в журнал")
res17 = "branch:" + uuid.uuid4().hex[:8]
t17a, t17b = f"H-c1-{RUN}", f"H-c2-{RUN}"
call("/task", task_id=t17a, title="первый", agent_id="exec1", state="assigned",
     contract=contract("exec1", res17))
call("/task", task_id=t17b, title="второй", agent_id="exec2", state="assigned",
     contract=contract("exec2", res17, to="exec1"))
g = call("/acquire", agent_id="exec1", instance_id=str(uuid.uuid4()), task_id=t17a,
         resources=[res17])
check("первый взял ресурс", g.get("ok"), g)
r = call("/acquire", agent_id="exec2", instance_id=str(uuid.uuid4()), task_id=t17b,
         resources=[res17])
check("второму отказано", not r.get("ok"), r)
ev = call("/events", limit=60)["события"]
conflicts = [e for e in ev if e["kind"] == "lease_conflict"
             and e["payload"].get("requested") == res17]
check("🔴 конфликт записан событием", conflicts, [e["kind"] for e in ev[:5]])
if conflicts:
    p17 = conflicts[0]["payload"]
    check("виден держатель", p17.get("held_by") == "exec1", p17)
    check("видно, сколько ждать", isinstance(p17.get("expires_in_s"), int), p17)
call("/release", lease_token=g["lease_token"])

print("")
print("18. События передачи пригодны для измерений")
ev = call("/events", limit=200)["события"]
kinds = {e["kind"] for e in ev}
for k in ("handoff_offered", "handoff_accepted", "handoff_rejected"):
    check(f"событие {k} пишется", k in kinds, sorted(kinds))

print("")
print(f"ИТОГ: {N[1]} из {N[0]}")
sys.exit(0 if N[1] == N[0] else 1)
