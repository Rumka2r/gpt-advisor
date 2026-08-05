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


def uniq(p):
    return f"{p}:{uuid.uuid4().hex[:8]}"


print("1. Захват всё-или-ничего")
r1, r2, r3 = uniq("branch"), uniq("db:schema"), uniq("port")
a1, a2 = str(uuid.uuid4()), str(uuid.uuid4())
g1 = call("/acquire", agent_id="exec1", instance_id=a1, task_id="T-1", resources=[r1, r2])
check("первый берёт два ресурса", g1.get("ok"), g1)
check("выдано поколение на каждый", set(g1.get("fencing", {})) == {r1, r2}, g1.get("fencing"))

g2 = call("/acquire", agent_id="exec2", instance_id=a2, task_id="T-2", resources=[r2, r3])
check("второму отказано при пересечении", not g2.get("ok"), g2)
st = call("/status")
free = all(l["resource"] != r3 for l in st["аренды"])
check("🔴 непересекающийся ресурс НЕ захвачен (всё-или-ничего)", free,
      "r3 оказался занят — частичный захват, это взаимная блокировка")

print("\n2. Тот же владелец берёт повторно")
g3 = call("/acquire", agent_id="exec1", instance_id=a1, task_id="T-1", resources=[r1])
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
after = call("/acquire", agent_id="exec2", instance_id=a2, task_id="T-2", resources=[r1])
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
gz = call("/acquire", agent_id="exec1", instance_id=a1, task_id="T-3", resources=[r4])
con = sqlite3.connect("/opt/agent-control/cp.db")
con.execute("UPDATE leases SET expires=? WHERE resource=?", (int(time.time()) - 5, r4))
con.commit(); con.close()
g = call("/acquire", agent_id="exec2", instance_id=a2, task_id="T-4", resources=[r4])
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
    c = call("/acquire", agent_id="exec1", instance_id=a1, task_id="T-9",
             resources=[bad_name])
    check(f"отвергнуто: {bad_name}", not c.get("ok"), c)

print("")
print("10. Вложенные пути конфликтуют, регистр не создаёт двойника")
z = uuid.uuid4().hex[:8]
p1 = call("/acquire", agent_id="exec1", instance_id=a1, task_id="T-10",
          resources=[f"path:backend/{z}"])
check("зона взята", p1.get("ok"), p1)
p2 = call("/acquire", agent_id="exec2", instance_id=a2, task_id="T-11",
          resources=[f"path:backend/{z}/models.py"])
check("файл внутри занятой зоны недоступен другому", not p2.get("ok"), p2)
p3 = call("/acquire", agent_id="exec2", instance_id=a2, task_id="T-12",
          resources=[f"  PATH:backend/{z}  "])
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
w = call_as(k2, "/acquire", agent_id="exec2", instance_id="i", resources=[res])
check("своим именем можно", w.get("ok"), w)
if w.get("ok"):
    call("/release", lease_token=w["lease_token"])

print("")
print(f"ИТОГ: {N[1]} из {N[0]}")
sys.exit(0 if N[1] == N[0] else 1)
