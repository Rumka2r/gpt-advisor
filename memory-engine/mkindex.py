# -*- coding: utf-8 -*-
"""Генератор WORK_INDEX.md — карты РАБОЧЕЙ части памяти (shared/).

Личное (private/) сюда не попадает by design: WORK_INDEX уезжает на сервер и служит
картой памяти для executor'а (у него `memory/MEMORY.md` → этот файл).
Запускается из синка; можно руками: python mkindex.py
"""
import os, re, collections

def _store():
    for cand in (os.environ.get("MEM_STORE"),
                 os.path.expanduser("~/.claude/projects/C--Users-andri-work-osha/memory"),
                 "/opt/agent-memory"):
        if cand and os.path.isdir(cand):
            return cand
    return os.getcwd()


STORE = _store()
SHARED = os.path.join(STORE, "shared")
OUT = os.path.join(STORE, "WORK_INDEX.md")

# порядок и заголовки групп; ключ — префикс имени файла
GROUPS = [
    ("plumbingcore", "🏗️ PlumbingCore — продукт и данные",
     ("plumbing", "warehouse", "catalog", "wh_", "billing", "project_", "projects_", "operations",
      "photo", "pplan", "prod_", "sandbox", "deploy", "migration", "alembic", "oauth", "auth",
      "pentest", "straznik", "kraken", "security", "api_", "frontend", "ui_", "ux", "a11y", "calendar")),
    ("infra", "🖥️ Инфраструктура и сервер",
     ("server", "hetzner", "systemd", "nginx", "postgres", "db_", "uvicorn", "gunicorn", "docker",
      "cron", "ssh", "network", "dns", "backup")),
    ("agents", "🤖 Агенты и инструменты",
     ("alex", "hermes", "codex", "claude", "glm", "kimi", "mimo", "openclaw", "agent", "executor",
      "telegram", "gpt", "model", "fable", "usage")),
    ("rules", "📋 Правила и уроки", ("feedback", "lesson", "gotcha", "rule")),
    ("memory", "🧠 Память и непрерывность", ("memory", "continuity", "recall", "semantic", "shared_memory")),
]


def fm(txt):
    m = re.match(r"^---\r?\n(.*?)\r?\n---", txt, re.S)
    if not m:
        return {}
    d = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith(("-", "#")):
            k, v = line.split(":", 1)
            d[k.strip()] = v.strip().strip('"')
    return d


def hook_of(path, name):
    txt = open(path, encoding="utf-8", errors="replace").read()
    d = fm(txt)
    desc = d.get("description", "").strip()
    if not desc:                                   # без frontmatter — первая содержательная строка
        for line in txt.splitlines():
            s = line.strip().lstrip("#* ").strip()
            if len(s) > 20 and not s.startswith(("---", "|", "```")):
                desc = s
                break
    return re.sub(r"\s+", " ", desc)[:150]


def main():
    files = sorted(f for f in os.listdir(SHARED) if f.endswith(".md") and f != "MEMORY.md")
    buckets = collections.OrderedDict((g[0], []) for g in GROUPS)
    buckets["other"] = []
    for f in files:
        name = f[:-3]
        low = name.lower()
        placed = False
        for key, _, prefixes in GROUPS:
            if any(low.startswith(p) or ("_" + p) in low for p in prefixes):
                buckets[key].append((name, hook_of(os.path.join(SHARED, f), name)))
                placed = True
                break
        if not placed:
            buckets["other"].append((name, hook_of(os.path.join(SHARED, f), name)))

    lines = [
        "# 🔧 Рабочая память (общая: ПК + сервер)",
        "",
        "Карта рабочей части стора — то, что синкается с сервером и видит executor.",
        "Личное и секреты сюда не попадают (они в `private/` и на сервер не уезжают).",
        "Правила ведения — `shared/memory_system_standard.md`. Файл генерируется `mkindex.py`, руками не править.",
        "",
        "> executor: ты смотришь на этот файл как на `memory/MEMORY.md`, а сами файлы лежат рядом с ним —",
        "> префикс `shared/` в ссылках относится к раскладке на ПК, у тебя это просто `<имя>.md`.",
        "",
        f"Всего файлов: {len(files)}",
        "",
    ]
    titles = dict((g[0], g[1]) for g in GROUPS)
    titles["other"] = "📦 Прочее"
    for key, items in buckets.items():
        if not items:
            continue
        lines.append(f"## {titles[key]} ({len(items)})")
        for name, hook in sorted(items):
            lines.append(f"- [{name}](shared/{name}.md) — {hook}" if hook else f"- [{name}](shared/{name}.md)")
        lines.append("")
    open(OUT, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(f"WORK_INDEX.md: {len(files)} файлов, групп {sum(1 for v in buckets.values() if v)}")


if __name__ == "__main__":
    main()
