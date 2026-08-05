#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Клиент Control Plane. Этим агенты и я пользуемся; базу напрямую никто не трогает.

    ctl.py status                                  кто в сети, что занято
    ctl.py events [N]                              что реально происходило
    ctl.py task T-1 "название" --agent exec2       завести задачу
    ctl.py acquire T-1 --agent exec2 -r branch:x -r db:schema:exec2
    ctl.py run T-1 --agent exec2 -r deploy:sandbox -- ./deploy.sh
        взять ресурсы → держать сердцебиение в фоне → выполнить →
        освободить. 🔴 Перед самой командой ещё раз спрашивает право:
        между захватом и запуском аренда могла протухнуть.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

API = os.environ.get("CP_API", "http://127.0.0.1:8010")
# Классы необратимых операций: при недоступном координаторе они запрещены,
# а изолированная работа в своём worktree может продолжаться (fail-closed
# только там, где ошибка необратима).
SERIAL = ("migration:", "deploy:", "merge:", "release:", "prod:")
KEYFILE = "/opt/agent-control/api.key"
HOST_ID = os.uname().nodename


def key():
    with open(os.environ.get("CP_KEYFILE", KEYFILE)) as f:
        return f.read().strip()


SOCKET = os.environ.get("CP_SOCKET", "/opt/agent-control/cp.sock")


def call_socket(path, payload):
    """Через Unix-сокет: сервер узнаёт агента по UID, ключ не нужен вовсе."""
    body = json.dumps(payload).encode("utf-8")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(20)
    try:
        s.connect(SOCKET)
        eol = "\r\n"
        head = (f"POST {path} HTTP/1.1{eol}"
                f"Host: cp{eol}"
                f"Content-Type: application/json{eol}"
                f"Content-Length: {len(body)}{eol}"
                f"Connection: close{eol}{eol}")
        s.sendall(head.encode() + body)
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        _, _, rest = buf.partition(b"\r\n\r\n")
        return json.loads(rest)
    finally:
        s.close()


def call(path, **payload):
    if os.path.exists(SOCKET):
        try:
            return call_socket(path, payload)
        except (OSError, ValueError):
            pass          # сокета нет или он занят — идём по ключу
    req = urllib.request.Request(
        API + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Api-Key": key()})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def quiet(path, **payload):
    """Вызов, который не обязан удаться: журналирование и освобождение аренды.
    Возвращает ответ или None. Нужен, потому что падение координатора не должно
    превращать законченную работу в трассировку."""
    try:
        return call(path, **payload)
    except Exception:
        return None


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return "?"


def out(o):
    print(json.dumps(o, ensure_ascii=False, indent=1))


def cmd_status(a):
    s = call("/status")
    print(f"Агенты (сердцебиение {s['heartbeat_s']} с, TTL {s['ttl_s']} с):")
    for x in s["агенты"] or []:
        mark = "🟢" if x["молчит_с"] < s["ttl_s"] else "⚪"
        print(f"  {mark} {x['agent_id']:<10} {x['host']:<18} молчит {x['молчит_с']} с")
    print("\nАренды:" if s["аренды"] else "\nАренд нет — все ресурсы свободны")
    for x in s["аренды"]:
        print(f"  {x['resource']:<34} {x['agent']:<8} задача {x['task'] or '—':<8} "
              f"поколение {x['поколение']}, осталось {x['осталось_с']} с")
    if s["задачи"]:
        print("\nЗадачи:")
        for x in s["задачи"]:
            print(f"  {x['task_id']:<8} {x['state']:<10} {x['agent'] or '—':<8} {x['title']}")


def cmd_events(a):
    for e in reversed(call("/events", limit=a.n)["события"]):
        ts = time.strftime("%d.%m %H:%M:%S", time.localtime(e["ts"]))
        p = json.dumps(e["payload"], ensure_ascii=False)
        print(f"{ts}  {e['agent'] or '—':<8} {e['kind']:<16} {p[:110]}")


def cmd_task(a):
    out(call("/task", task_id=a.task_id, title=a.title, agent_id=a.agent,
             state=a.state, contract={"resources": a.resource or []}))


def cmd_acquire(a):
    inst = str(uuid.uuid4())
    call("/register", agent_id=a.agent, host_id=HOST_ID, instance_id=inst,
         boot_id=boot_id(), pid=os.getpid())
    out(call("/acquire", agent_id=a.agent, instance_id=inst, task_id=a.task_id,
             resources=a.resource, host_id=HOST_ID, boot_id=boot_id(), pid=os.getpid()))


def cmd_release(a):
    out(call("/release", lease_token=a.token))


def cmd_run(a):
    inst = str(uuid.uuid4())
    try:
        call("/register", agent_id=a.agent, host_id=HOST_ID, instance_id=inst,
             boot_id=boot_id(), pid=os.getpid())
        got = call("/acquire", agent_id=a.agent, instance_id=inst, task_id=a.task_id,
                   resources=a.resource, host_id=HOST_ID, boot_id=boot_id(),
                   pid=os.getpid())
    except Exception as e:
        # 🔴 Координатор недоступен → новые аренды запрещены, и точка. Отказ
        # должен быть понятным сообщением, а не трассировкой: агент по коду 75
        # обязан отложить работу, а не решить, что сломался он сам.
        print(f"ОТКАЗ: координатор недоступен ({e}). Новые аренды не выдаются — "
              f"работа отложена.", file=sys.stderr)
        return 75
    if not got.get("ok"):
        print("ОТКАЗ:", got.get("причина"))
        for b in got.get("занятые", []):
            print(f"  {b['resource']} занят {b['занят']}, "
                  f"освободится через {b['освободится_через_с']} с")
        return 75          # EX_TEMPFAIL — «попробуй позже», не ошибка задачи

    token, fencing = got["lease_token"], got["fencing"]
    stop = threading.Event()
    proc = {"p": None}
    lost = {"why": None}

    def kill_all(why):
        """🔴 Потеря аренды посреди работы обязана ОСТАНОВИТЬ работу, а не просто
        записаться в журнал. Иначе поколение живёт в базе, а ресурс продолжает
        портить живой процесс — это и есть дыра, на которую указал архитектор.
        Бьём по всей группе процессов: у деплоя есть потомки."""
        lost["why"] = why
        print(f"🔴 аренда потеряна: {why} — останавливаю работу", file=sys.stderr)
        p = proc["p"]
        if not p or p.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except OSError:
            pass
        for _ in range(50):                 # 5 секунд на достойное завершение
            if p.poll() is not None:
                return
            time.sleep(0.1)
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except OSError:
            pass

    # 🔴 Два режима. Обычная задача: такт 20 с, окно потери права до 20 с —
    # для изолированной песочницы терпимо. Критическая операция (миграция, merge,
    # деплой, релиз, прод): такт 3 с, и достаточно ДВУХ неудач подряд, потому что
    # минута неавторизованной работы на общем ресурсе — это много.
    critical = any(r.startswith(SERIAL) for r in a.resource)
    tick = 3 if critical else got["heartbeat_s"]
    max_misses = 2 if critical else 3
    if critical:
        print(f"критическая операция: проверка права каждые {tick} с", file=sys.stderr)

    def beat():
        misses = 0
        while not stop.wait(tick):
            try:
                r = call("/heartbeat", lease_token=token, agent_id=a.agent)
                if not r.get("ok"):
                    return kill_all(r.get("причина", "аренда не продлена"))
                misses = 0
                # Сердцебиения мало: аренду могли перехватить и выдать заново.
                # Сверяем ПОКОЛЕНИЕ каждого ресурса — оно растёт при любой смене.
                for res in a.resource:
                    c = call("/check", resource=res, lease_token=token,
                             fencing_token=fencing[res])
                    if not c.get("allow"):
                        return kill_all(f"{res}: {c.get('причина')}")
            except Exception as e:
                misses += 1
                # Координатор недоступен: для необратимых операций это ЗАПРЕТ.
                # Молчание координатора не равно «право у меня осталось».
                if misses >= max_misses and critical:
                    return kill_all(f"координатор недоступен ({e}), "
                                    f"а операция необратимая")
                print("сердцебиение не прошло:", e, file=sys.stderr)

    code = 1
    try:
        # Повторная проверка права ПЕРЕД самой командой: между захватом и
        # запуском аренда могла истечь, а поколение — уйти вперёд.
        for r in a.resource:
            c = call("/check", resource=r, lease_token=token, fencing_token=fencing[r])
            if not c.get("allow"):
                print(f"ОТКАЗ перед запуском ({r}):", c.get("причина"))
                return 75
        call("/event", agent_id=a.agent, task_id=a.task_id, kind="run_start",
             payload={"cmd": a.cmd, "resources": a.resource})
        threading.Thread(target=beat, daemon=True).start()
        # Своя группа процессов, чтобы убить команду вместе со всеми потомками.
        proc["p"] = subprocess.Popen(a.cmd, start_new_session=True)
        code = proc["p"].wait()
        if lost["why"]:
            code = 75
        quiet("/event", agent_id=a.agent, task_id=a.task_id,
              kind="run_aborted" if lost["why"] else "run_done",
              payload={"code": code, "причина": lost["why"]})
    finally:
        stop.set()
        # 🔴 Освобождение не должно падать с трассировкой, если координатор лежит:
        # работа уже закончена, а аренда всё равно истечёт по TTL. Иначе агент
        # получает мусор вместо кода возврата и не понимает, что произошло.
        if not quiet("/release", lease_token=token):
            print("координатор недоступен: аренда освободится сама по TTL",
                  file=sys.stderr)
    return code


def cmd_check(a):
    out(call("/check", resource=a.resource, lease_token=a.token,
             fencing_token=a.fencing))


def main():
    ap = argparse.ArgumentParser(description="клиент Control Plane агентов")
    sub = ap.add_subparsers(dest="c", required=True)

    sub.add_parser("status").set_defaults(f=cmd_status)
    p = sub.add_parser("events"); p.add_argument("n", nargs="?", type=int, default=40)
    p.set_defaults(f=cmd_events)

    p = sub.add_parser("task")
    p.add_argument("task_id"); p.add_argument("title")
    p.add_argument("--agent", default=""); p.add_argument("--state", default="open")
    p.add_argument("-r", "--resource", action="append")
    p.set_defaults(f=cmd_task)

    p = sub.add_parser("acquire")
    p.add_argument("task_id"); p.add_argument("--agent", required=True)
    p.add_argument("-r", "--resource", action="append", required=True)
    p.set_defaults(f=cmd_acquire)

    p = sub.add_parser("release"); p.add_argument("token"); p.set_defaults(f=cmd_release)

    p = sub.add_parser("check")
    p.add_argument("resource"); p.add_argument("--token", required=True)
    p.add_argument("--fencing", type=int)
    p.set_defaults(f=cmd_check)

    p = sub.add_parser("run")
    p.add_argument("task_id"); p.add_argument("--agent", required=True)
    p.add_argument("-r", "--resource", action="append", required=True)
    p.set_defaults(f=cmd_run)

    # 🔴 Команду отделяем от опций САМИ, до argparse: с nargs=REMAINDER остаток
    # съедал --agent и -r, и запуск падал на «не хватает аргументов».
    argv = sys.argv[1:]
    cmd = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]

    a = ap.parse_args(argv)
    a.cmd = cmd
    if a.c == "run" and not cmd:
        ap.error("после -- должна идти команда: ctl.py run T-1 --agent X -r res -- ./скрипт")
    return a.f(a) or 0


if __name__ == "__main__":
    sys.exit(main())
