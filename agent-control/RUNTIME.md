# Состояние развёрнутой системы

## Реестр ожидаемых зон
    /srv/agents/exec2
    проверка зоны: (True, '')

## Права зоны
    drwxr-xr-x 11 root   agent2 4096 Aug  5 22:45 /srv/agents/exec2
    drwx------  2 root   root   4096 Aug  5 22:45 /srv/agents/exec2/.quarantine
    drwxrwxr-x 13 agent2 agent2 4096 Aug  5 22:52 /srv/agents/exec2/work

## Границы исполнителя (код возврата)
    переименовать карантин код 1 (нужен НЕ 0)
    удалить карантин    код 1 (нужен НЕ 0)
    писать в корень зоны код 1 (нужен НЕ 0)
    писать в свою работу код 0 (нужен 0)
    писать в архив         код 128 (нужен НЕ 0)

## Прогоны
    координатор: ИТОГ: 35 из 35
    сборщик:     ИТОГ: 71 из 71

## Службы и диск
    agent-cp                   active
    agent-gc.timer             active
    agent-diskwatch.timer      active
    plumbingcore-sandbox       active
    plumbingcore-prod          active
    /dev/sda1       150G  112G   32G  78% /

## Запуск сборщика (без --apply)
    ExecStart=/usr/bin/python3 /opt/agent-control/gc.py run
