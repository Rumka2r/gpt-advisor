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


def bind_identity(sid, transcript):
    """Write-once привязка сессии к агенту. Возвращает текст предупреждения или "".

    Конфликт (resume чужой сессии) — жёсткая ошибка по вердикту архитектора
    06.08: привязку НЕ перезаписываем, а в контекст уходит громкое требование
    остановиться. Отсутствие идентичности (запуск мимо launcher) — тихая
    строка-предупреждение: работа продолжается, но сессия неразмечена.
    """
    try:
        sys.path.insert(0, BIN)
        import continuity_identity as ident
        status = ident.bind_session(sid, transcript)
        if status.startswith("conflict:"):
            owner = status.split(":", 1)[1]
            me = ident.current_agent_id() or "?"
            return ("## 🔴 ЧУЖАЯ СЕССИЯ\n"
                    f"Сессия {sid[:8]} принадлежит агенту **{owner}**, а этот процесс — "
                    f"**{me}**. Resume чужой сессии запрещён (разграничение инстансов, "
                    "вердикт 06.08.2026). Привязка НЕ изменена. Останови работу в этой "
                    "сессии и продолжай в своей (запуск через свой launcher).\n\n")
        if status == "anonymous":
            return ("⚠️ Идентичность не назначена (запуск мимо launcher): сессия останется "
                    "неразмеченной (legacy-unowned), «вспомни, что ты делал» её не увидит. "
                    "Правильный запуск: cco/999/000/glm или start-скрипты.\n\n")
    except Exception:
        pass
    return ""


def start(data):
    sid = data.get("session_id") or ""
    warn = bind_identity(sid, data.get("transcript_path"))
    spawn([os.path.join(BIN, "worker.py"), "--skip-session", sid])
    try:
        out = subprocess.run([PY, os.path.join(BIN, "now.py"), "hook"],
                             capture_output=True, text=True, timeout=25,
                             encoding="utf-8", errors="replace")
        payload = out.stdout or "{}"
    except Exception:
        payload = "{}"
    if warn:
        # Предупреждение обязано дойти даже при пустой сводке.
        try:
            d = json.loads(payload) if payload.strip() else {}
            h = d.setdefault("hookSpecificOutput", {"hookEventName": "SessionStart"})
            h["additionalContext"] = warn + (h.get("additionalContext") or "")
            payload = json.dumps(d, ensure_ascii=False)
        except Exception:
            pass
    sys.stdout.write(payload)


def end(data):
    try:
        sys.path.insert(0, BIN)
        import continuity_identity as ident
        ident.end_session(data.get("session_id") or "")
    except Exception:
        pass
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
