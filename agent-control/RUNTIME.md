# Состояние развёрнутой системы

Снимок с сервера: то, чего нет в коде.

## Координатор — RC1, заморожен
    коммит a81e1dc, метка agent-control-rc1
    модули: cp.py=92f49359 contracts.py=79403537 products.py=110293b5 handoffs.py=4f0db8f2 verifier.py=b0e94156 
    порт 8010 + сокет по UID; снимок базы /var/agent-backup/rc1/cp.db (онлайн, целостность ok)

## Проверки
    test_cp          ИТОГ: 90 из 90
    test_products    ИТОГ: 67 из 67
    test_gc          ИТОГ: 79 из 79
    test_handoffs    ИТОГ: 58 из 58

## Граница эксперимента
    {
     "run_id": "RC1-20260806T043935Z",
     "начало_события_id": 2032,
     "начало_метка_времени": 1785991175,
     "коммит": "a81e1dc",
     "метка": "agent-control-rc1",
     "серии": {
      "EXP-A": "независимая работа",
      "EXP-B": "контролируемый конфликт",
      "EXP-C": "отказ, исправление, приём"
     },
     "снимок_базы": "/var/agent-backup/rc1/cp.db"
    }
## Исполнитель-2
    служба: active
    порт 8002, ответ /api/health: 200
    база plumbingcore_exec2, схема whrequq01, таблиц 223
    том: /dev/loop0       15G  754M   14G   6% /srv/agents/exec2
    срез agent-exec2.slice, пользователь agent2

## Границы исполнителя-2 (код возврата)
    читать окружение песочницы код 1
    читать чужую копию код 2
    писать в архив истории код 128
    тронуть свой карантин код 1

## Службы и диск
    agent-cp                   active
    agent-gc.timer             active
    agent-diskwatch.timer      active
    plumbingcore-prod          active
    plumbingcore-sandbox       active
    plumbingcore-exec2         active
    /dev/sda1       150G  113G   32G  79% /

## Сборщик
    ExecStart=/usr/bin/python3 /opt/agent-control/gc.py run --apply --scope zones --skip-rescue-cleanup
