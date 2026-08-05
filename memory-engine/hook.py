# -*- coding: utf-8 -*-
"""Хуки слоя непрерывности. Никогда не падает наружу: любая ошибка → тихий выход 0.

  hook.py start   ← SessionStart: инжектит сводку + догоняет несуммаризированные сессии
  hook.py end     ← SessionEnd:   суммаризирует текущую сессию в фоне
"""
import os, sys, json, subprocess, tempfile

BIN = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BIN)
PY = sys.executable
LOG = os.path.join(ROOT, "state", "worker.log")

DETACH = 0
if os.name == "nt":
    DETACH = getattr(subprocess, "DETACHED_PROCESS", 8) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def spawn(args):
    """Фоновая задача, не держит родителя."""
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        log = open(LOG, "a", encoding="utf-8", errors="replace")
        subprocess.Popen([PY] + args, cwd=BIN, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, creationflags=DETACH, close_fds=True)
    except Exception:
        pass


# 🔴 Управления локом здесь БОЛЬШЕ НЕТ. Раньше хук писал в лок свой PID и лишь
# потом запускал воркера — тот видел «живого владельца» (сам хук!) и выходил, а
# свежий лок оставался лежать. Следующие хуки целый час вообще не запускали
# воркера. Плюс проверка была неатомарной: два хука проходили её одновременно.
# Единственный владелец лока — worker.py, он берёт его атомарно сам
# (замечание архитектора 05.08). Здесь только запуск.


def start(data):
    sid = data.get("session_id") or ""
    spawn([os.path.join(BIN, "worker.py"), "--skip-session", sid])
    try:
        out = subprocess.run([PY, os.path.join(BIN, "now.py"), "hook"],
                             capture_output=True, text=True, timeout=25,
                             encoding="utf-8", errors="replace")
        sys.stdout.write(out.stdout or "{}")
    except Exception:
        sys.stdout.write("{}")


def end(data):
    path = data.get("transcript_path")
    args = [os.path.join(BIN, "worker.py")]
    if path:
        args += ["--current", path]
    spawn(args)
    sys.stdout.write("{}")


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    try:
        (start if cmd == "start" else end)(data)
    except Exception:
        sys.stdout.write("{}")
    sys.exit(0)


if __name__ == "__main__":
    main()
