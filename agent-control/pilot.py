#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Пилотная пара: прогоняет две независимые задачи через полный цикл координатора.

🔴 Это проверка ИЗМЕРИТЕЛЯ и механики, а НЕ замер производительности агентов.
Работу здесь никто не делает — задачи закрываются заранее известным объектом.
Смысл: убедиться, что показатели вообще извлекаются из базы и что независимые
задачи действительно не конфликтуют.

Настоящий замер начнётся, когда задачи будут выполнять живые исполнители.
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

API = "http://127.0.0.1:8010"
KEY = open("/opt/agent-control/api.key").read().strip()
COMMIT = subprocess.run(["git", "-C", "/srv/agents/store.git", "rev-parse",
                         "refs/heads/main"], capture_output=True,
                        text=True).stdout.strip()
SERIES = sys.argv[1] if len(sys.argv) > 1 else "EXP-A"
PAIR = sys.argv[2] if len(sys.argv) > 2 else "pilot"


def call(path, **p):
    r = urllib.request.Request(API + path, data=json.dumps(p).encode(),
                               headers={"Content-Type": "application/json",
                                        "X-Api-Key": KEY})
    try:
        with urllib.request.urlopen(r, timeout=20) as f:
            return json.loads(f.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def contract(agent, res, to):
    return {"schema_version": 1, "objective": f"пилотная задача {agent}",
            "assignee": agent, "resources": res,
            "outputs": [{"slot": "impl", "kind": "git_commit", "required": True,
                         "checks": ["tests"]}],
            "constraints": {"forbidden_actions": [], "deadline": None},
            "handoff_to": to}


def run_one(agent, task, res, to, work_s):
    """Один исполнитель: захват → работа → продукт → сверка → предложение."""
    inst = str(uuid.uuid4())
    r = call("/task", task_id=task, title=f"{SERIES} {agent}", agent_id=agent,
             state="assigned", contract=contract(agent, res, to))
    if not r.get("ok"):
        return {"ok": False, "шаг": "задача", "ответ": r}
    c = call("/contract", task_id=task)
    g = call("/acquire", agent_id=agent, instance_id=inst, task_id=task,
             resources=res)
    if not g.get("ok"):
        return {"ok": False, "шаг": "аренда", "ответ": g}

    time.sleep(work_s)          # «работа»: здесь её изображает пауза

    p = call("/product/register", task_id=task, agent_id=agent,
             contract_version=c["версия"], contract_sha256=c["отпечаток"],
             output_slot="impl", kind="git_commit",
             locator={"type": "git", "repository": "agent-store", "commit": COMMIT},
             digest=COMMIT, digest_alg="git_sha1", lease_token=g["lease_token"],
             instance_id=inst, fencing=g["fencing"],
             idempotency_key=uuid.uuid4().hex)
    if not p.get("ok"):
        return {"ok": False, "шаг": "продукт", "ответ": p}
    subprocess.run(["python3", "/opt/agent-control/verifier.py",
                    "--product", p["product_id"]], capture_output=True)
    call("/product/check", agent_id=agent, product_id=p["product_id"],
         check_name="tests", status="passed", evidence="пилотный прогон")

    h = call("/handoff/offer", task_id=task, agent_id=agent,
             contract_version=c["версия"], contract_sha256=c["отпечаток"],
             products={"impl": p["product_id"]}, summary="пилот",
             known_issues=[], next_action="принять",
             lease_token=g["lease_token"], instance_id=inst,
             fencing=g["fencing"], idempotency_key=uuid.uuid4().hex)
    return {"ok": h.get("ok"), "шаг": "предложение", "ответ": h,
            "handoff": h.get("handoff_id"), "task": task}


def main():
    stamp = time.strftime("%H%M%S")
    a_res = ["branch:" + f"{SERIES}-a-{stamp}".lower(), f"path:backend/a{stamp}"]
    b_res = ["branch:" + f"{SERIES}-b-{stamp}".lower(), f"path:backend/b{stamp}"]
    ta = f"{SERIES}-{PAIR}-a-{stamp}"
    tb = f"{SERIES}-{PAIR}-b-{stamp}"

    print(f"пара {PAIR}: ресурсы не пересекаются")
    print(f"  {ta}: {a_res}")
    print(f"  {tb}: {b_res}")

    import threading
    out = {}

    def go(agent, task, res, to, work, key):
        out[key] = run_one(agent, task, res, to, work)

    t1 = threading.Thread(target=go, args=("exec1", ta, a_res, "most", 3, "a"))
    t2 = threading.Thread(target=go, args=("exec2", tb, b_res, "most", 4, "b"))
    t0 = time.time()
    t1.start(); t2.start()
    t1.join(); t2.join()
    print(f"\nобе задачи предложены за {time.time() - t0:.1f} с")

    for k, r in out.items():
        print(f"  {k}: {'ок' if r.get('ok') else 'ОШИБКА на шаге ' + r.get('шаг', '?')}")
        if not r.get("ok"):
            print("   ", r.get("ответ"))

    # приёмка обоих — она в основной показатель не входит, считается отдельно
    for k, r in out.items():
        if r.get("handoff"):
            time.sleep(1)
            acc = call("/handoff/accept", agent_id="most", handoff_id=r["handoff"])
            print(f"  приёмка {k}: {'ок' if acc.get('ok') else acc.get('причина')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
