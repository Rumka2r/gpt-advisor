# Состояние развёрнутой системы

Снимок с сервера. Здесь то, чего НЕТ в коде: что реально запущено, права, тома, числа.

## Службы
    ● agent-cp.service - Control Plane агентов (задачи, присутствие, аренды, журнал событий)
         Loaded: loaded (/etc/systemd/system/agent-cp.service; enabled; vendor preset: enabled)
         Active: active (running) since Wed 2026-08-05 19:03:58 UTC; 2h 58min ago
    agent-gc.timer               active
    agent-diskwatch.timer        active
    plumbingcore-sandbox         active
    plumbingcore-prod            active

## Расписание
    NEXT                        LEFT     LAST                        PASSED    UNIT                  ACTIVATES
    Wed 2026-08-05 22:02:39 UTC 7s left  Wed 2026-08-05 22:02:09 UTC 22s ago   agent-diskwatch.timer agent-diskwatch.service
    Wed 2026-08-05 22:03:10 UTC 37s left Wed 2026-08-05 21:48:10 UTC 14min ago agent-gc.timer        agent-gc.service
    

## Как запускается сборщик (сейчас БЕЗ --apply)
    ExecStart=/usr/bin/python3 /opt/agent-control/gc.py run

## Диски и тома
    Filesystem      Size  Used Avail Use% Mounted on
    /dev/sda1       150G  111G   34G  78% /
    /dev/loop0       15G   51M   15G   1% /srv/agents/exec2

## Зона исполнителя-2
    drwxrwxr-x 10 agent2 agent2 4096 Aug  5 18:59 /srv/agents/exec2
    drwxrwxr-x 13 agent2 agent2 4096 Aug  5 19:13 /srv/agents/exec2/work
    drwxr-x---  8 root   agents 4096 Aug  5 19:18 /srv/agents/store.git
    отдельный пользователь: agent2
    личный репозиторий: 1.2M · архив: 260M
    общие объекты через alternates: /srv/agents/store.git/objects

## Настройки архива (проверяются кодом, но вот факт)
    gc.auto          0
    gc.pruneExpire   never
    gc.reflogExpire  never
    ссылок всего: 9379 · из них спасательных: 1

## Учёт сборщика прямо сейчас
    
    Диск: занято 78.0%, свободно 33.0 ГБ · уровень OK 
      новые задачи: да · новые исполнители: да
      координатор: доступен
    
    состояние     вид                шт       объём
    ACTIVE        worktree           32     15.2 ГБ
    ACTIVE        session_cache      13     11.6 ГБ
    ACTIVE        tmp_generic      4597      2.4 ГБ
    EXPIRED       worktree            2    423.8 МБ
    EXPIRED       tmp_generic       667    220.3 МБ
    
    спасательных ссылок: 1 (хранятся 30 дней от спасения)
    
    🔴 Удерживается проверками (НЕ удалено):
      /home/executor/plumbingcore-integration
         🔴 удержание: в hold.txt
      /home/executor/work-qa01
         🔴 удержание: в hold.txt
      /home/executor/work-uxfix01

## Прогоны проверок
    координатор: ИТОГ: 29 из 29
    сборщик:     ИТОГ: 47 из 47
