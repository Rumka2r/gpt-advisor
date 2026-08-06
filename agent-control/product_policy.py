# -*- coding: utf-8 -*-
"""Кто вправе подтверждать проверки результата.

🔴 Одних названий «tests» и «audit» мало: без правил производитель сам отметит
любую проверку пройденной, и «проверено» перестанет что-либо означать.

Каталог общий для контракта и реестра: контракт заранее отклоняет проверку,
которой не существует, иначе получится задача, которую невозможно завершить —
её нельзя ни выполнить, ни закрыть.
"""

# system      — записывает только сам координатор по итогам сверки;
# producer    — может записать исполнитель, но обязан приложить свидетельство;
# independent — только агент, ОТЛИЧНЫЙ от производителя результата.
CHECK_POLICIES = {
    "digest_verified": "system",
    "tests": "producer",
    "smoke": "producer",
    "lint": "producer",
    "typecheck": "producer",
    "audit": "independent",
    "review": "independent",
    "migration_check": "independent",
}

STATUSES = ("passed", "failed", "error")


def known(name):
    return name in CHECK_POLICIES


def policy(name):
    return CHECK_POLICIES.get(name)


def may_record(name, checker_agent, producer_agent, system=False):
    """Вправе ли этот агент записать результат проверки.
    Возвращает (можно, причина_отказа)."""
    pol = CHECK_POLICIES.get(name)
    if pol is None:
        return False, f"проверка {name!r} неизвестна; допустимые: " \
                      f"{', '.join(sorted(CHECK_POLICIES))}"
    if pol == "system":
        if not system:
            return False, (f"проверку {name} записывает только сам координатор "
                           f"по итогам сверки")
        return True, ""
    if pol == "independent" and checker_agent == producer_agent:
        return False, (f"проверку {name} не может подтвердить тот, кто произвёл "
                       f"результат — нужен другой агент")
    return True, ""
