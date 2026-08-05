#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Происхождение записей памяти — проставляется КОДОМ, не моделью.

Урок Экзегета (см. [[memory-systems-comparison]]): агент рапортовал «записал,
проверил чтением — лежит на месте», а файл был пуст. Поэтому обязательные поля
берутся из события записи, а не из моих слов: когда, в какой сессии, какой
файл, какая контрольная сумма содержимого.

Работает на хуке `PostToolUse` (Write/Edit) и трогает только файлы памяти.
Ничего не переписывает по смыслу: добавляет одну служебную строку в конец
файла и кладёт машинные поля рядом, в `<имя>.prov.json` — чтобы .md оставался
читаемым, а контекст не засорялся.

Модуль намеренно «мягкий»: любая ошибка не должна ломать саму запись памяти,
поэтому наружу не падает никогда.
"""

import hashlib
import json
import os
import re
import sys
import time

STORE = os.path.expanduser("~/.claude/projects/C--Users-andri-work-osha/memory")
MARK = "<!-- происхождение:"
LINE_RX = re.compile(r"^<!-- происхождение:.*?-->\s*$", re.M)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def stamp(path, session_id=None, tool=None):
    """Проставить происхождение. Возвращает True, если файл изменён."""
    path = os.path.abspath(path)
    if not path.lower().endswith(".md"):
        return False
    if os.path.commonpath([path, os.path.abspath(STORE)]) != os.path.abspath(STORE):
        return False        # не файл памяти — не трогаем
    try:
        raw = open(path, encoding="utf-8").read()
    except OSError:
        return False

    body = LINE_RX.sub("", raw).rstrip()      # прежний штамп не копим
    digest = _sha(body)
    now = time.strftime("%Y-%m-%d %H:%M")
    sid = (session_id or "")[:8]

    line = f"{MARK} записано {now}"
    if sid:
        line += f" · сессия {sid}"
    if tool:
        line += f" · через {tool}"
    line += f" · sha {digest} -->"

    new = body + "\n\n" + line + "\n"
    if new == raw:
        return False
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
    except OSError:
        return False

    meta = {
        "file": os.path.relpath(path, STORE).replace("\\", "/"),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "session_id": session_id or None,
        "tool": tool,
        "content_sha256_16": digest,
        "chars": len(body),
        "stamper_version": 1,
    }
    try:
        with open(path[:-3] + ".prov.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return True


def main():
    """Режим хука: читает событие PostToolUse со stdin, всегда выходит с 0."""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    try:
        inp = data.get("tool_input") or {}
        path = inp.get("file_path") or inp.get("notebook_path")
        if path:
            stamp(path, data.get("session_id"), data.get("tool_name"))
    except Exception:
        pass
    print("{}")
    sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--file":
        ok = stamp(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None, "ручной вызов")
        print("проставлено" if ok else "пропущено (не файл памяти или без изменений)")
    else:
        main()
