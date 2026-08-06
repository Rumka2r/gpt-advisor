#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Живая проверка координатора: он должен ОТВЕЧАТЬ, а не просто существовать.

🔴 `systemctl is-active` доказывает лишь, что процесс не завершился. Пустой или
сломанный координатор может висеть и не обслуживать никого — а развёртывание
считало бы это успехом.

Код возврата 0 — отвечает, иначе — нет.
"""
import json
import os
import sys
import urllib.request

ROOT = os.environ.get("CP_ROOT", "/opt/agent-control")
API = os.environ.get("CP_API", "http://127.0.0.1:8010")


def main():
    try:
        key = open(os.path.join(ROOT, "api.key")).read().strip()
        req = urllib.request.Request(
            API + "/status", data=b"{}",
            headers={"Content-Type": "application/json", "X-Api-Key": key})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"координатор не отвечает: {e}")
        return 1
    if not data.get("ok"):
        print("координатор ответил отказом:", data)
        return 1
    print(f"координатор отвечает: агентов {len(data.get('агенты', []))}, "
          f"аренд {len(data.get('аренды', []))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
