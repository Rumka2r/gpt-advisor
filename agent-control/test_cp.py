#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверки Control Plane на настоящих сценариях гонки, а не «служба поднялась»."""
import json
import os
import sys
import time
import urllib.request
import uuid

API = "http://127.0.0.1:8010"
KEY = open("/opt/agent-control/api.key").read().strip()
N = [0, 0]


def call(path, **p):
    r = urllib.request.Request(API + path, data=json.dumps(p).encode(),
                               headers={"Content-Type": "application/json", "X-Api-Key": KEY})
    with urllib.request.urlopen(r, timeout=20) as f:
        return json.loads(f.read())


def check(name, cond, detail=""):
    N[0] += 1
    if cond:
        N[1] += 1
        print(f"  ✔ {name}")
    else:
        print(f"  ✘ {name}   {detail}")


def contract_for(agent, resources, handoff_to="most"):
    """Контракт под конкретные ресурсы: теперь аренда без него не выдаётся."""
    return {"schema_version": 1, "objective": "проверка", "assignee": agent,
            "resources": list(resources),
            # слот и проверки — машинные имена: только латиница, как в схеме;
            # у обязательного результата должна быть хотя бы одна проверка
            "outputs": [{"slot": "result", "kind": "report", "required": True,
                         "checks": ["digest_verified"]}],
            "constraints": {"forbidden_actions": [], "deadline": None},
            "handoff_to": handoff_to}


_tasks = set()
# 🔴 База переживает прогоны: с постоянными именами задача из прошлого запуска
# осталась бы в состоянии «в работе», и повторное заведение падало бы.
RUN = uuid.uuid4().hex[:6]


def acq(agent, instance, task_id, resources, state="assigned"):
    """Завести задачу с контрактом (один раз) и взять по ней аренду — законный
    путь. Повторно задачу не пересоздаём: после первого захвата она уже в
    работе, и выставить ей assigned запросом нельзя по замыслу."""
    task_id = f"{task_id}-{RUN}"
    if task_id not in _tasks:
        r = call("/task", task_id=task_id, title="проверка", agent_id=agent,
                 state=state, contract=contract_for(agent, resources))
        if not r.get("ok"):
            return r
        _tasks.add(task_id)
    return call("/acquire", agent_id=agent, instance_id=instance, task_id=task_id,
                resources=list(resources))



def uniq(p):
    return f"{p}:{uuid.uuid4().hex[:8]}"


print("1. Захват всё-или-ничего")
r1, r2, r3 = uniq("branch"), uniq("db:schema"), uniq("port")
a1, a2 = str(uuid.uuid4()), str(uuid.uuid4())
g1 = acq("exec1", a1, "T-1", [r1, r2])
check("первый берёт два ресурса", g1.get("ok"), g1)
check("выдано поколение на каждый", set(g1.get("fencing", {})) == {r1, r2}, g1.get("fencing"))

g2 = acq("exec2", a2, "T-2", [r2, r3])
check("второму отказано при пересечении", not g2.get("ok"), g2)
st = call("/status")
free = all(l["resource"] != r3 for l in st["аренды"])
check("🔴 непересекающийся ресурс НЕ захвачен (всё-или-ничего)", free,
      "r3 оказался занят — частичный захват, это взаимная блокировка")

print("\n2. Тот же владелец берёт повторно")
# повторно берём ТОТ ЖЕ набор: частичный захват теперь запрещён контрактом
g3 = acq("exec1", a1, "T-1", [r1, r2])
check("повторный захват своим же процессом разрешён", g3.get("ok"), g3)

print("\n3. Право проверяется, а не подразумевается")
c = call("/check", resource=r1, lease_token=g3["lease_token"], fencing_token=g3["fencing"][r1])
check("владельцу разрешено", c.get("allow"), c)
c = call("/check", resource=r1, lease_token="чужой-секрет")
check("с чужим секретом запрещено", not c.get("allow"), c)
c = call("/check", resource=r1, lease_token=g3["lease_token"],
         fencing_token=g3["fencing"][r1] - 1)
check("🔴 с устаревшим поколением запрещено", not c.get("allow"),
      "отставший fencing_token пропущен — ожившая копия сможет деплоить")

print("\n4. Сердцебиение продлевает только по верному секрету")
h = call("/heartbeat", lease_token=g3["lease_token"], agent_id="exec1")
check("продление верным секретом", h.get("ok"), h)
h = call("/heartbeat", lease_token="подделка", agent_id="exec1")
check("продление подделкой отклонено", not h.get("ok"), h)

print("\n5. Освобождение и поколение")
before = call("/check", resource=r1, lease_token=g3["lease_token"])
call("/release", lease_token=g3["lease_token"])
# у exec2 своя задача со своим контрактом ровно на этот ресурс
after = acq("exec2", a2, "T-2b", [r1])
check("после освобождения ресурс достаётся другому", after.get("ok"), after)
check("🔴 поколение выросло у нового владельца",
      after["fencing"][r1] > g3["fencing"][r1],
      f"было {g3['fencing'][r1]}, стало {after['fencing'][r1]}")
old = call("/check", resource=r1, lease_token=g3["lease_token"],
           fencing_token=g3["fencing"][r1])
check("прежний владелец больше не имеет права", not old.get("allow"), old)
call("/release", lease_token=after["lease_token"])

print("\n6. Смерть владельца: аренда переходит после TTL")
# TTL 90 с ждать не будем — подменяем срок в базе напрямую, имитируя молчание
import sqlite3
r4 = uniq("branch")   # класс с произвольным значением: каталог имён допускает
gz = acq("exec1", a1, "T-3", [r4])
con = sqlite3.connect("/opt/agent-control/cp.db")
con.execute("UPDATE leases SET expires=? WHERE resource=?", (int(time.time()) - 5, r4))
con.commit(); con.close()
g = acq("exec2", a2, "T-4", [r4])
check("после молчания ресурс достаётся живому", g.get("ok"), g)
check("🔴 поколение выросло при истечении",
      g["fencing"][r4] > gz["fencing"][r4],
      f"было {gz['fencing'][r4]}, стало {g['fencing'][r4]}")
z = call("/heartbeat", lease_token=gz["lease_token"], agent_id="exec1")
check("оживший старый процесс не может продлить", not z.get("ok"), z)
z = call("/check", resource=r4, lease_token=gz["lease_token"],
         fencing_token=gz["fencing"][r4])
check("🔴 оживший старый процесс не получает право на деплой", not z.get("allow"), z)
call("/release", lease_token=g["lease_token"])

print("\n7. Журнал событий пишется")
ev = call("/events", limit=200)["события"]
kinds = {e["kind"] for e in ev}
check("захват записан", "lease_acquired" in kinds, kinds)
check("истечение записано", "lease_expired" in kinds, kinds)
check("освобождение записано", "lease_released" in kinds, kinds)

print("\n8. Без ключа доступа нет")
try:
    r = urllib.request.Request(API + "/status", data=b"{}",
                               headers={"Content-Type": "application/json"})
    urllib.request.urlopen(r, timeout=10)
    check("запрос без ключа отклонён", False, "пропустили без ключа")
except urllib.error.HTTPError as e:
    check("запрос без ключа отклонён", e.code == 403, e.code)

print("")
print("9. Каталог имён ресурсов: выдуманные имена не принимаются")
for bad_name in ["sandbox:deploy", "deploy:выдуманное", "простаястрока"]:
    c = acq("exec1", a1, "T-9", [bad_name])
    check(f"отвергнуто: {bad_name}", not c.get("ok"), c)

print("")
print("10. Вложенные пути конфликтуют, регистр не создаёт двойника")
z = uuid.uuid4().hex[:8]
p1 = acq("exec1", a1, "T-10", [f"path:backend/{z}"])
check("зона взята", p1.get("ok"), p1)
p2 = acq("exec2", a2, "T-11", [f"path:backend/{z}/models.py"])
check("файл внутри занятой зоны недоступен другому", not p2.get("ok"), p2)
p3 = acq("exec2", a2, "T-12", [f"  PATH:backend/{z}  "])
check("регистр и пробелы ведут к тому же ресурсу", not p3.get("ok"), p3)
call("/release", lease_token=p1["lease_token"])

print("")
print("11. Персональные ключи: имя агента определяет сервер по ключу")


def call_as(k, path, **p):
    r = urllib.request.Request(API + path, data=json.dumps(p).encode(),
                               headers={"Content-Type": "application/json",
                                        "X-Api-Key": k})
    try:
        with urllib.request.urlopen(r, timeout=20) as f:
            return json.loads(f.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


k2 = open("/opt/agent-control/keys/exec2.key").read().strip()
res = "branch:" + uuid.uuid4().hex[:8]
w = call_as(k2, "/acquire", agent_id="exec1", instance_id="i", resources=[res])
check("ключом exec2 нельзя действовать как exec1", not w.get("ok"), w)
# законный путь: сначала задача с контрактом, затем аренда своим ключом
# 🔴 Контракт создаёт Мост, а не исполнитель — это и проверяется ниже отдельно
tk = "T-key-" + RUN
call("/task", task_id=tk, title="проверка ключа", agent_id="exec2",
     state="assigned", contract=contract_for("exec2", [res]))
own = call_as(k2, "/task", task_id=tk + "-чужой", title="своя задача",
              agent_id="exec2", state="assigned", contract=contract_for("exec2", [res]))
check("🔴 исполнитель не может создать контракт", not own.get("ok"), own)
w = call_as(k2, "/acquire", agent_id="exec2", instance_id="i", task_id=tk,
            resources=[res])
check("своим именем можно", w.get("ok"), w)
if w.get("ok"):
    call("/release", lease_token=w["lease_token"])

print("")
print("12. Позднее удержание останавливает уже начатую работу")
res12 = "branch:" + uuid.uuid4().hex[:8]
g12 = acq("exec1", a1, "T-hold", [res12])
check("аренда взята", g12.get("ok"), g12)
c = call("/check", resource=res12, lease_token=g12["lease_token"],
         fencing_token=g12["fencing"][res12])
check("до удержания право есть", c.get("allow"), c)
h = call("/hold", resource=res12, reason="расследование",
         expires_at=int(time.time()) + 3600)
check("удержание поставлено", h.get("ok"), h)
check("удержание отозвало аренду", h.get("отозванные_аренды"), h)
c = call("/check", resource=res12, lease_token=g12["lease_token"],
         fencing_token=g12["fencing"][res12])
check("после удержания право отобрано", not c.get("allow"), c)
call("/unhold", resource=res12)

print("")
print("13. Удержание закрывает и вложенные пути")
z13 = uuid.uuid4().hex[:8]
call("/hold", resource=f"path:backend/{z13}", reason="зона под расследованием",
     expires_at=int(time.time()) + 3600)
r13 = acq("exec2", a2, "T-13", [f"path:backend/{z13}/models.py"])
check("файл внутри удержанной зоны не выдан", not r13.get("ok"), r13)
call("/unhold", resource=f"path:backend/{z13}")

print("")
print("14. Задача без контракта не заводится")
r = call("/task", task_id="T-14", title="без контракта", agent_id="exec1")
check("🔴 задача без контракта отклонена", not r.get("ok"), r)

print("")
print("15. Контракт проверяется схемой")
bad = [
    ({"schema_version": 9}, "чужая версия схемы"),
    ({"schema_version": 1, "objective": "", "assignee": "exec1",
      "resources": ["branch:x"], "outputs": [{"slot": "s", "kind": "report"}],
      "handoff_to": "most"}, "пустая цель"),
    ({"schema_version": 1, "objective": "ц", "assignee": "exec1",
      "resources": [], "outputs": [{"slot": "s", "kind": "report"}],
      "handoff_to": "most"}, "пустые ресурсы"),
    ({"schema_version": 1, "objective": "ц", "assignee": "exec1",
      "resources": ["branch:x"], "outputs": [], "handoff_to": "most"},
     "нет ожидаемого результата"),
    ({"schema_version": 1, "objective": "ц", "assignee": "exec1",
      "resources": ["branch:x"],
      "outputs": [{"slot": "s", "kind": "выдуманный", "required": True}],
      "handoff_to": "most"}, "неизвестный вид результата"),
    ({"schema_version": 1, "objective": "ц", "assignee": "exec1",
      "resources": ["выдуманное:имя"],
      "outputs": [{"slot": "s", "kind": "report", "required": True}],
      "handoff_to": "most"}, "ресурс вне каталога имён"),
]
for body, why in bad:
    r = call("/task", task_id="T-15-" + why[:6], title="проба", agent_id="exec1",
             contract=body)
    check(f"отклонено: {why}", not r.get("ok"), r)

print("")
print("16. Ресурсы аренды обязаны совпадать с контрактом")
r16 = "branch:" + uuid.uuid4().hex[:8]
t16 = "T-16-" + RUN          # база переживает прогоны — имя должно быть своё
lишний = "branch:" + uuid.uuid4().hex[:8]
call("/task", task_id=t16, title="проба", agent_id="exec1", state="assigned",
     contract=contract_for("exec1", [r16]))
g = call("/acquire", agent_id="exec1", instance_id=a1, task_id=t16,
         resources=[r16, lишний])
check("🔴 лишний ресурс не выдан", not g.get("ok"), g)
g = call("/acquire", agent_id="exec1", instance_id=a1, task_id=t16, resources=[])
check("пустой набор не выдан", not g.get("ok"), g)
g = call("/acquire", agent_id="exec1", instance_id=a1, task_id=t16, resources=[r16])
check("ровно те ресурсы — выдано", g.get("ok"), g)
if g.get("ok"):
    call("/release", lease_token=g["lease_token"])

print("")
print("17. Аренду берёт только назначенный исполнитель")
g = call("/acquire", agent_id="exec2", instance_id=a2, task_id=t16, resources=[r16])
check("🔴 чужую задачу взять нельзя", not g.get("ok"), g)

print("")
print("18. Контракт неизменяем: правка создаёт версию (пока работа не началась)")
rv = "branch:" + uuid.uuid4().hex[:8]
tv = "T-ver-" + RUN
call("/task", task_id=tv, title="версии", agent_id="exec1", state="assigned",
     contract=contract_for("exec1", [rv]))
c1 = call("/contract", task_id=tv)
check("контракт читается", c1.get("ok"), c1)
call("/task", task_id=tv, title="версии", agent_id="exec1", state="assigned",
     contract=dict(contract_for("exec1", [rv]), objective="другая цель"))
c2 = call("/contract", task_id=tv)
check("🔴 появилась новая версия", c2.get("версия") == c1.get("версия") + 1,
      (c1.get("версия"), c2.get("версия")))
check("прошлая версия сохранена", len(c2.get("версии", [])) >= 2, c2.get("версии"))
check("отпечаток изменился", c2.get("отпечаток") != c1.get("отпечаток"))
call("/task", task_id=tv, title="версии", agent_id="exec1", state="assigned",
     contract=dict(contract_for("exec1", [rv]), objective="другая цель"))
c3 = call("/contract", task_id=tv)
check("тот же контракт новой версии не плодит", c3.get("версия") == c2.get("версия"),
      (c2.get("версия"), c3.get("версия")))

print("")
print("19. Под действующей арендой контракт не меняется")
g19 = call("/acquire", agent_id="exec1", instance_id=a1, task_id=tv, resources=[rv])
check("аренда взята", g19.get("ok"), g19)
before = call("/contract", task_id=tv)
r = call("/task", task_id=tv, title="версии", agent_id="exec1", state="assigned",
         contract=dict(contract_for("exec1", [rv]), objective="третья цель"))
check("🔴 правка под арендой отклонена", not r.get("ok"), r)
after = call("/contract", task_id=tv)
check("действующая версия не изменилась",
      after.get("версия") == before.get("версия"), (before.get("версия"),
                                                    after.get("версия")))
c = call("/check", resource=rv, lease_token=g19["lease_token"],
         fencing_token=g19["fencing"][rv])
check("аренда осталась действительной", c.get("allow"), c)
check("🔴 задача переведена в работу захватом",
      any(t["task_id"] == tv and t["state"] == "running"
          for t in call("/status")["задачи"]),
      [t for t in call("/status")["задачи"] if t["task_id"] == tv])
call("/release", lease_token=g19["lease_token"])

print("")
print("20. Служебные состояния запросом не выставляются")
for st in ("running", "handoff_pending", "done"):
    r = call("/task", task_id=tv, title="версии", agent_id="exec1", state=st)
    check(f"🔴 состояние {st} запросом не выставить", not r.get("ok"), r)
    check("причина названа", "выставляется системой" in str(r.get("причина", "")), r)

print("")
print("21. Блокировка и отмена отзывают действующую аренду")
for конец in ("blocked", "cancelled"):
    rr = "branch:" + uuid.uuid4().hex[:8]
    tt = f"T-{конец}-" + RUN
    call("/task", task_id=tt, title="проба", agent_id="exec1", state="assigned",
         contract=contract_for("exec1", [rr]))
    g = call("/acquire", agent_id="exec1", instance_id=a1, task_id=tt, resources=[rr])
    check(f"[{конец}] аренда взята", g.get("ok"), g)
    c = call("/check", resource=rr, lease_token=g["lease_token"],
             fencing_token=g["fencing"][rr])
    check(f"[{конец}] право есть", c.get("allow"), c)
    r = call("/task", task_id=tt, title="проба", agent_id="exec1", state=конец)
    check(f"[{конец}] переход выполнен", r.get("ok"), r)
    check(f"🔴 [{конец}] аренда отозвана", r.get("отозванные_аренды"), r)
    c = call("/check", resource=rr, lease_token=g["lease_token"],
             fencing_token=g["fencing"][rr])
    check(f"🔴 [{конец}] право отобрано", not c.get("allow"), c)
    h = call("/heartbeat", lease_token=g["lease_token"], agent_id="exec1")
    check(f"[{конец}] продление отклонено", not h.get("ok"), h)
    g2 = call("/acquire", agent_id="exec1", instance_id=a1, task_id=tt, resources=[rr])
    if конец == "blocked":
        check("[blocked] в блокировке аренда не выдаётся", not g2.get("ok"), g2)
        r = call("/task", task_id=tt, title="проба", agent_id="exec1", state="assigned")
        check("🔴 из блокировки можно вернуться в работу", r.get("ok"), r)
        g3 = call("/acquire", agent_id="exec1", instance_id=a1, task_id=tt,
                  resources=[rr])
        check("после возврата аренда снова выдаётся", g3.get("ok"), g3)
        if g3.get("ok"):
            check("поколение выросло после отзыва",
                  g3["fencing"][rr] > g["fencing"][rr],
                  (g["fencing"][rr], g3["fencing"][rr]))
            call("/release", lease_token=g3["lease_token"])
    else:
        check("[cancelled] отменённая задача аренду не получает", not g2.get("ok"), g2)

print("")
print("22. Опечатка в контракте не проходит молча")
bad = dict(contract_for("exec1", ["branch:x"]))
bad["outputs"] = [{"slot": "impl", "kind": "report", "required": True,
                   "cheks": ["tests"]}]
r = call("/task", task_id="T-typo-" + RUN, title="опечатка", agent_id="exec1",
         contract=bad)
check("🔴 опечатка cheks отклонена", not r.get("ok"), r)
check("названа как неизвестное поле", "неизвестные поля" in str(r.get("ошибки", "")), r)

bad2 = dict(contract_for("exec1", ["branch:x"]))
bad2["выдуманное"] = 1
r = call("/task", task_id="T-typo2-" + RUN, title="опечатка", agent_id="exec1",
         contract=bad2)
check("лишнее поле контракта отклонено", not r.get("ok"), r)

print("")
print("23. Обязательный результат без проверок не принимается")
bad3 = dict(contract_for("exec1", ["branch:x"]))
bad3["outputs"] = [{"slot": "impl", "kind": "report", "required": True, "checks": []}]
r = call("/task", task_id="T-nocheck-" + RUN, title="без проверок", agent_id="exec1",
         contract=bad3)
check("🔴 обязательный результат без проверок отклонён", not r.get("ok"), r)

print("")
print("24. Имена слотов и проверок нормализуются")
ok4 = dict(contract_for("exec1", ["branch:" + uuid.uuid4().hex[:8]]))
ok4["outputs"] = [{"slot": "  Result  ", "kind": "report", "required": True,
                   "checks": ["  Tests ", "digest_verified"]}]
t24 = "T-norm-" + RUN
r = call("/task", task_id=t24, title="нормализация", agent_id="exec1", contract=ok4)
check("контракт принят", r.get("ok"), r)
c24 = call("/contract", task_id=t24)
if c24.get("ok"):
    out = c24["контракт"]["outputs"][0]
    check("🔴 слот приведён к машинному виду", out["slot"] == "result", out)
    check("проверки приведены", out["checks"] == ["tests", "digest_verified"], out)

print("")
print("25. Негодные ограничения не роняют сервер")
bad5 = dict(contract_for("exec1", ["branch:x"]))
bad5["constraints"] = {"forbidden_actions": "нельзя всё", "deadline": None}
r = call("/task", task_id="T-con-" + RUN, title="ограничения", agent_id="exec1",
         contract=bad5)
check("🔴 не-список отклонён понятной ошибкой", not r.get("ok"), r)
check("это отказ, а не сбой", "сбой" not in str(r.get("причина", "")), r)

print("")
print(f"ИТОГ: {N[1]} из {N[0]}")
sys.exit(0 if N[1] == N[0] else 1)
