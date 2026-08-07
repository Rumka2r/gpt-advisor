# -*- coding: utf-8 -*-
"""Launcher — единственное место, где НАЗНАЧАЕТСЯ идентичность агента.

    python agent_launch.py <agent_id> [--cwd DIR] -- <команда запуска Claude...>

Порядок (вердикт архитектора 06.08.2026): читаем agents.yaml, создаём новый
instance_id, регистрируем запуск в registry.sqlite — и только после этого
запускаем Claude с переменными окружения:

    AGENT_ID=osha-999          кто владеет памятью (долговечный)
    AGENT_PROFILE=osha         какой профиль
    AGENT_INSTANCE_ID=<uuid>   этот конкретный запуск
    AGENT_GENERATION=N         номер запуска этого агента

Стартовые скрипты (start-999.ps1, *-hold.sh, glm.cmd) сами ID не придумывают —
они говорят launcher'у «запусти такого-то». Хуки continuity читают env и
проверяют по реестру, но не создают.

Реестр недоступен (диск, блокировка) → агент всё равно запускается, но с
пометкой в stderr: continuity обязана работать и без реестра, просто сессии
останутся неразмеченными.
"""

import os
import signal
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    argv = sys.argv[1:]
    if "--" not in argv or not argv or argv[0].startswith("-"):
        print(__doc__, file=sys.stderr)
        return 2
    sep = argv.index("--")
    head, cmd = argv[:sep], argv[sep + 1:]
    agent_id = head[0]
    cwd = None
    if "--cwd" in head:
        cwd = head[head.index("--cwd") + 1]
    if not cmd:
        print("нет команды после --", file=sys.stderr)
        return 2

    env = dict(os.environ)
    instance_id = None
    try:
        import continuity_identity as ident
        agents = ident.load_agents()
        if agent_id not in agents:
            print(f"🔴 агент {agent_id!r} не описан в {ident.AGENTS_YAML} — не запускаю.\n"
                  f"Известные: {', '.join(sorted(agents))}", file=sys.stderr)
            return 3
        instance_id, gen = ident.register_instance(agent_id, pid=os.getpid())
        env[ident.ENV_AGENT] = agent_id
        env[ident.ENV_PROFILE] = agents[agent_id].get("profile") or ""
        env[ident.ENV_INSTANCE] = instance_id
        env[ident.ENV_GENERATION] = str(gen)
        print(f"[identity] {agent_id} · запуск №{gen} · instance {instance_id[:8]}",
              file=sys.stderr)
    except Exception as e:
        # Реестр лежит — работаем без идентичности, но НЕ молча.
        print(f"[identity] ⚠️ реестр недоступен ({type(e).__name__}: {e}) — "
              f"запускаю без назначенной идентичности", file=sys.stderr)

    # Ctrl+C уходит ребёнку; сам launcher переживает его, чтобы честно
    # закрыть запись о запуске.
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:
        pass

    rc = 1
    try:
        rc = subprocess.call(cmd, env=env, cwd=cwd)
    finally:
        if instance_id:
            try:
                import continuity_identity as ident
                ident.end_instance(instance_id)
            except Exception:
                pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
