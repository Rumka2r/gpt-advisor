# -*- coding: utf-8 -*-
"""Проверка целостности стора памяти — общий инструмент всех агентов Рувима.

  python memcheck.py            — сводка + примеры
  python memcheck.py --full     — все нарушения, без обрезки
  python memcheck.py --fix-links — только показать битые [[ссылки]] (правит человек/агент)

Стандарт, который проверяем: shared/memory_system_standard.md
Ничего не меняет на диске: это линтер, а не автоправка.
"""
import os, re, sys, json, argparse

def _store():
    """Один линтер на два узла: на ПК — полный стор, на сервере — рабочая копия executor'а."""
    for cand in (os.environ.get("MEM_STORE"),
                 os.path.expanduser("~/.claude/projects/C--Users-andri-work-osha/memory"),
                 "/opt/agent-memory"):
        if cand and os.path.isdir(cand):
            return cand
    return os.getcwd()


STORE = _store()
# карты, из которых файл считается достижимым: индекс, хабы и сгенерированный
# WORK_INDEX (карта рабочей части, по ней живёт executor)
HUB_GLOB = ("MEMORY.md", "WORK_INDEX.md")
SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SIZE_SOFT = 1500          # ориентир из стандарта, мягкий
SECRETS = [
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "API-ключ sk-…"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "токен GitHub"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS key"),
    (re.compile(r"^-----BEGIN [A-Z ]*PRIVATE KEY", re.M), "приватный ключ"),
    (re.compile(r"\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b"), "SSN-паттерн"),
]
TYPES = {"user", "feedback", "project", "reference"}
LINK = re.compile(r"\[\[([^\]]+)\]\]")


def read(p):
    try:
        return open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def frontmatter(txt):
    m = re.match(r"^---\r?\n(.*?)\r?\n---", txt, re.S)
    if not m:
        return None
    fm = {}
    for line in m.group(1).splitlines():
        if re.match(r"^\s*[-#]", line) or ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip().strip('"')
    return fm


def scan():
    facts, hubs = [], []
    for root, dirs, files in os.walk(STORE):
        dirs[:] = [d for d in dirs if d not in (".git", "generated", "sanitized")]
        for n in files:
            if not n.endswith(".md"):
                continue
            p = os.path.join(root, n)
            rel = os.path.relpath(p, STORE).replace("\\", "/")
            (hubs if (rel.startswith("hub_") or rel in HUB_GLOB) else facts).append((rel, p))
    return facts, hubs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    a = ap.parse_args()
    limit = 10 ** 6 if a.full else 8

    facts, hubs = scan()
    hub_text = "\n".join(read(p) for _, p in hubs)
    names = {os.path.basename(rel)[:-3] for rel, _ in facts}
    names |= {os.path.basename(rel)[:-3].replace("_", "-") for rel, _ in facts}
    # ссылаются и на слаг из frontmatter, и на имя файла — валидны оба
    for rel, p in facts:
        nm = (frontmatter(read(p)) or {}).get("name", "")
        if nm:
            names |= {nm, nm.replace("_", "-")}
    # зашифрованные файлы — законные цели ссылок ([[vault]], [[personal-identity]])
    for sub in ("private", "shared"):
        d = os.path.join(STORE, sub)
        for n in os.listdir(d) if os.path.isdir(d) else []:
            if n.endswith(".dpapi"):
                stem = n.split(".")[0]
                names |= {stem, stem.replace("_", "-")}

    problems = {"нет frontmatter": [], "name не слаг": [], "плохой type": [],
                "не достижим из карты/хабов": [], "битые [[ссылки]]": [],
                "крупнее ориентира": [], "🔴 секрет открытым текстом": []}

    for rel, p in facts:
        txt = read(p)
        base = os.path.basename(rel)[:-3]
        fm = frontmatter(txt)
        if fm is None:
            problems["нет frontmatter"].append(rel)
        else:
            # слаг может исторически расходиться с именем файла — это не беда;
            # беда, когда в name предложение с пробелами: по нему не сошлёшься [[…]]
            nm = fm.get("name", "")
            if nm and not SLUG.match(nm):
                problems["name не слаг"].append(f"{rel} (name: {nm[:50]})")
            t = fm.get("type", "")
            if t and t not in TYPES:
                problems["плохой type"].append(f"{rel} (type: {t})")
        if base not in hub_text and rel not in hub_text:
            problems["не достижим из карты/хабов"].append(rel)
        for ln in set(LINK.findall(txt)):
            key = ln.strip()
            if key not in names and key.replace("-", "_") not in names:
                problems["битые [[ссылки]]"].append(f"{rel} → [[{key}]]")
        if len(txt) > SIZE_SOFT * 3:
            problems["крупнее ориентира"].append(f"{rel} ({len(txt)} симв.)")
        for rx, what in SECRETS:
            if rx.search(txt):
                problems["🔴 секрет открытым текстом"].append(f"{rel}: {what}")

    print(f"стор: {STORE}")
    print(f"файлов-фактов: {len(facts)} · хабов и карт: {len(hubs)}\n")
    bad = 0
    for k, v in problems.items():
        if not v:
            print(f"✓ {k}: 0")
            continue
        bad += len(v)
        print(f"✗ {k}: {len(v)}")
        for x in v[:limit]:
            print(f"    {x}")
        if len(v) > limit:
            print(f"    … и ещё {len(v) - limit} (полностью: --full)")
    print(f"\nвсего замечаний: {bad}")
    # секреты — единственное, что делает выход ненулевым: остальное это гигиена, не авария
    sys.exit(1 if problems["🔴 секрет открытым текстом"] else 0)


if __name__ == "__main__":
    main()
