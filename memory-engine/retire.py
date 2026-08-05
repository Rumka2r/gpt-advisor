#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Карантин памяти: убрать запись из обращения, но не потерять.

Заимствовано у Экзегета (`retire_legacy.py`). Там это понадобилось, когда в
памяти нашлась прямая ложь («библиотека пуста» при 35 книгах): удалить —
значит потерять след, оставить — значит продолжать врать.

Правило: из памяти ничего не удаляется насовсем. Запись уезжает в
`_retired/`, рядом пишется манифест — что, откуда, когда, почему и с какой
контрольной суммой. Возврат возможен одной командой.

Карантин выведен ЗА пределы `memory/`, поэтому в поиск и в индекс он больше
не попадает: `memdocs.py` обходит только сам стор.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import time

STORE = os.path.expanduser("~/.claude/projects/C--Users-andri-work-osha/memory")
RETIRED = os.path.expanduser("~/.claude/projects/C--Users-andri-work-osha/_retired")
MANIFEST = os.path.join(RETIRED, "manifest.jsonl")


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def retire(rel_path, reason, session_id=None):
    """rel_path — путь относительно memory/ (например shared/old_note.md)."""
    src = os.path.join(STORE, rel_path)
    if not os.path.exists(src):
        return None, f"нет файла: {rel_path}"
    if not reason or len(reason.strip()) < 10:
        return None, "нужна причина: одной фразой, почему запись выводится из обращения"

    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(RETIRED, stamp + "_" + rel_path.replace("/", "_").replace("\\", "_"))
    os.makedirs(RETIRED, exist_ok=True)
    digest = _sha(src)
    shutil.move(src, dst)

    # спутниковые файлы происхождения уводим следом, чтобы не осиротели
    for extra in (src[:-3] + ".prov.json",):
        if os.path.exists(extra):
            shutil.move(extra, dst[:-3] + ".prov.json")

    rec = {
        "retired_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "original": rel_path.replace("\\", "/"),
        "stored_as": os.path.basename(dst),
        "reason": reason.strip(),
        "sha256": digest,
        "session_id": session_id,
    }
    with open(MANIFEST, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return dst, None


def restore(stored_as):
    """Вернуть запись в обращение."""
    src = os.path.join(RETIRED, stored_as)
    if not os.path.exists(src):
        return None, f"нет в карантине: {stored_as}"
    rel = None
    if os.path.exists(MANIFEST):
        for line in open(MANIFEST, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("stored_as") == stored_as:
                rel = r["original"]
    if not rel:
        return None, "в манифесте нет записи об этом файле — куда возвращать, неизвестно"
    dst = os.path.join(STORE, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    with open(MANIFEST, "a", encoding="utf-8") as f:
        f.write(json.dumps({"restored_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "original": rel, "stored_as": stored_as},
                           ensure_ascii=False) + "\n")
    return dst, None


def listing(limit=30):
    if not os.path.exists(MANIFEST):
        return []
    out = []
    for line in open(MANIFEST, encoding="utf-8"):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out[-limit:]


def main():
    ap = argparse.ArgumentParser(description="Карантин памяти (вместо удаления)")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("out", help="вывести запись из обращения")
    p.add_argument("path", help="путь относительно memory/, напр. shared/foo.md")
    p.add_argument("reason", help="почему — одной фразой, обязательно")
    p.add_argument("--session")

    p = sub.add_parser("back", help="вернуть запись")
    p.add_argument("stored_as")

    sub.add_parser("list", help="что в карантине")

    a = ap.parse_args()
    if a.cmd == "out":
        dst, err = retire(a.path, a.reason, a.session)
        print(err if err else f"выведено в карантин: {os.path.basename(dst)}")
        sys.exit(1 if err else 0)
    elif a.cmd == "back":
        dst, err = restore(a.stored_as)
        print(err if err else f"возвращено: {dst}")
        sys.exit(1 if err else 0)
    elif a.cmd == "list":
        rows = listing()
        if not rows:
            print("карантин пуст")
        for r in rows:
            if "retired_at" in r:
                print(f"  {r['retired_at'][:16]}  {r['original']}\n"
                      f"      причина: {r['reason']}\n      файл: {r['stored_as']}")
            else:
                print(f"  {r['restored_at'][:16]}  ВОЗВРАЩЕНО {r['original']}")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
