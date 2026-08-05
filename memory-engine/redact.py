#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Редактор секретов для слоя непрерывности.

Единственная точка, через которую текст транскрипта попадает в индекс или в
контекст модели. Работает в двух местах (оба обязательны):
  1) перед индексацией  — в dense/FTS уходит только очищенный текст;
  2) при дочитывании    — read_window() гонит сырой транскрипт через redact()
                          ещё раз, потому что на диске лежит оригинал.

Секрет не удаляется молча, а заменяется типизированной меткой:
    export OPENAI_API_KEY=<SECRET:API_KEY#8f32a1>
Смысл фразы сохраняется, значение — нет.

Идентификатор в метке — HMAC, а не обычный хеш: короткие значения (SSN,
PIN) перебираются по словарю за секунды, и голый sha256 их бы не защитил.
Одинаковый секрет даёт одинаковый идентификатор, поэтому по индексу видно
«это тот же ключ, что и в июле», не раскрывая самого ключа.

VERSION поднимается при любой правке правил: по нему видно, какие записи
проиндексированы старым редактором и подлежат переиндексации.
"""

import hashlib
import hmac
import os
import re

VERSION = 1

_KEY_PATH = os.path.expanduser("~/.claude/continuity/state/.redact_key")


def _key():
    """Локальная соль для HMAC. Создаётся один раз, из репозитория исключена."""
    try:
        with open(_KEY_PATH, "rb") as f:
            k = f.read().strip()
            if k:
                return k
    except OSError:
        pass
    k = os.urandom(32).hex().encode()
    os.makedirs(os.path.dirname(_KEY_PATH), exist_ok=True)
    with open(_KEY_PATH, "wb") as f:
        f.write(k)
    try:  # на Windows права выставляем best-effort
        os.chmod(_KEY_PATH, 0o600)
    except OSError:
        pass
    return k


def _tag(kind, value):
    ident = hmac.new(_key(), value.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:6]
    return f"<SECRET:{kind}#{ident}>"


# --- детекторы -------------------------------------------------------------
# Порядок важен: сначала длинные и специфичные, иначе общий детектор съест
# кусок ключа и оставит хвост.

RULES = [
    ("PRIVATE_KEY", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.S)),
    ("JWT",          re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("OPENAI_KEY",   re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("GITHUB_TOKEN", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("AWS_KEY",      re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("SLACK_TOKEN",  re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("GOOGLE_KEY",   re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("TELEGRAM_BOT", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("ANTHROPIC_KEY", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("CONNECTION_STRING", re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:@/]+:[^\s@]+@\S+")),
    ("SSN",          re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
    ("CARD",         re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
    # присваивание секрета по имени переменной: KEY=..., "password": "..."
    ("ASSIGNED", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL)[A-Z0-9_]*)"
        r"\s*[:=]\s*[\"']?([^\s\"',;]{8,})[\"']?")),
]

# Строка .env-дампа — их вырезаем блоком, а не по одной.
_ENVLINE = re.compile(r"(?m)^\s*(?:export\s+)?[A-Z][A-Z0-9_]{2,}\s*=\s*\S+\s*$")

_LUHN_STRIP = re.compile(r"[ -]")

# Значения, которые выглядят как присваивание секрета, но им не являются:
# плейсхолдеры в шаблонах и слова-описания типа поля.
_PLACEHOLDER = re.compile(r"^[<`$%{\[(*\"']|^\.\.\.|^x{3,}$|^\*{3,}$", re.I)
_NOT_SECRET_WORDS = {
    "optional", "required", "none", "null", "true", "false", "empty", "unset",
    "sha256", "sha512", "md5", "bcrypt", "argon2", "hash", "hashed", "encrypted",
    "secret", "password", "token", "yes", "no", "todo", "changeme", "example",
    "redacted", "hidden", "masked", "см", "нет", "да",
}
# Явные префиксы живых ключей — такие режем всегда, без проверки на энтропию.
_KNOWN_PREFIX = re.compile(r"^(sk-|ghp_|gho_|ghs_|xox[baprs]-|AKIA|ASIA|AIza|eyJ|glpat-|lc_)")


def _looks_like_secret(value):
    """Отсев описаний и шаблонов от настоящих значений.

    Осторожность асимметрична: пропустить живой ключ хуже, чем лишний раз
    закрыть безобидную строку, поэтому сомнительное считаем секретом.
    """
    v = value.strip()
    if _KNOWN_PREFIX.match(v):
        return True
    if len(v) < 8 or _PLACEHOLDER.match(v):
        return False
    base = re.split(r"[.\-_/:]", v.lower())[0]
    if base in _NOT_SECRET_WORDS or v.lower() in _NOT_SECRET_WORDS:
        return False
    if v.lower().startswith(("sha", "hmac", "base64", "http://", "https://")):
        return False
    has_digit = any(c.isdigit() for c in v)
    has_alpha = any(c.isalpha() for c in v)
    return (has_digit and has_alpha) or len(v) >= 24


def _luhn_ok(digits):
    """Отсев ложных срабатываний CARD: длинные числа, не проходящие Luhn,
    почти наверняка не номер карты (id, телефон, таймстамп)."""
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def redact(text, collapse_env=True):
    """Возвращает (очищенный текст, [список типов найденного]).

    collapse_env=True схлопывает подряд идущие строки вида VAR=value в сводку:
    полный дамп окружения бесполезен для поиска и опасен целиком.
    """
    if not text:
        return text, []
    found = []

    # Нулевой байт служит меткой-плейсхолдером ниже. Если он встретится во
    # входных данных (бинарный хвост в выводе команды), восстановление примет
    # чужую последовательность за свою — поэтому убираем сразу.
    if "\x00" in text:
        text = text.replace("\x00", " ")

    if collapse_env:
        text = _collapse_env_dump(text, found)

    # Готовые метки прячем за плейсхолдер: иначе <SECRET:API_KEY#…> сам
    # попадает под правило ASSIGNED (там есть «SECRET» и двоеточие) и
    # оборачивается второй раз, ломая текст и теряя исходный тип.
    vault = []

    def stash(tag):
        vault.append(tag)
        return f"\x00{len(vault) - 1}\x00"

    for kind, rx in RULES:
        def sub(m):
            raw = m.group(0)
            if kind == "CARD":
                digits = _LUHN_STRIP.sub("", raw)
                if len(digits) < 13 or not _luhn_ok(digits):
                    return raw  # не карта — не трогаем
            if kind == "ASSIGNED":
                name, value = m.group(1), m.group(2)
                if not _looks_like_secret(value):
                    return raw  # плейсхолдер или описание типа поля
                found.append(kind)
                return f"{name}={stash(_tag('CREDENTIAL', value))}"
            found.append(kind)
            return stash(_tag(kind, raw))
        text = rx.sub(sub, text)

    def unstash(m):
        i = int(m.group(1))
        return vault[i] if i < len(vault) else m.group(0)

    text = re.sub(r"\x00(\d+)\x00", unstash, text)
    return text, sorted(set(found))


def _collapse_env_dump(text, found):
    """Три и более подряд присваивания ЗАГЛАВНЫМИ = дамп окружения."""
    lines = text.split("\n")
    out, run = [], []

    def flush():
        if len(run) >= 3:
            found.append("ENV_DUMP")
            out.append(f"[вывод содержал {len(run)} переменных окружения — значения удалены]")
        else:
            out.extend(run)
        run.clear()

    for ln in lines:
        if _ENVLINE.match(ln):
            run.append(ln)
        else:
            flush()
            out.append(ln)
    flush()
    return "\n".join(out)


def has_secret(text):
    _, kinds = redact(text)
    return bool(kinds)


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Очистка текста от секретов")
    ap.add_argument("--selftest", action="store_true", help="прогнать проверку правил")
    a = ap.parse_args()

    if a.selftest:
        # (текст, ожидаемый тип, подстрока-секрет которой НЕ должно остаться)
        cases = [
            ("ключ sk-abcdefghij0123456789XYZ тут", "OPENAI_KEY", "sk-abcdefghij0123456789XYZ"),
            ("AKIA1234567890ABCDEF", "AWS_KEY", "AKIA1234567890ABCDEF"),
            ("мой SSN 123-45-6789 вот", "SSN", "123-45-6789"),
            ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "GITHUB_TOKEN", "ghp_abcdefghijklmnop"),
            ("DB_PASSWORD=hunter2superlong", "ASSIGNED", "hunter2superlong"),
            ("postgres://user:pass@host:5432/db", "CONNECTION_STRING", "user:pass@host"),
            ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.abcdefghijklmnop", "JWT", "eyJhbGciOiJIUzI1NiJ9"),
            ("номер 4111 1111 1111 1111 карта", "CARD", "4111 1111 1111 1111"),
            ("заказ 1234567890123456789 позиций", None, None),   # не Luhn — не карта
            ("обычный текст без секретов", None, None),
        ]
        bad = 0
        for src, expect, leak in cases:
            out, kinds = redact(src)
            ok = (expect in kinds) if expect else (not kinds)
            if leak and leak in out:      # секрет обязан исчезнуть из текста
                ok = False
                out += "   << секрет остался в тексте!"
            if expect and "\x00" in out:  # плейсхолдер обязан быть развёрнут
                ok = False
            if not ok:
                bad += 1
            print(f"{'OK ' if ok else 'FAIL'} {src[:40]:42} -> {out[:46]}")
        # смысл фразы должен уцелеть: слова вокруг секрета на месте
        keep, _ = redact("подключил GitHub с токеном ghp_abcdefghijklmnopqrstuvwxyz01 и получил 200 OK")
        if "подключил GitHub" not in keep or "200 OK" not in keep:
            bad += 1
            print("FAIL контекст вокруг секрета потерян:", keep)
        else:
            print("OK  контекст сохранён:", keep[:70])
        print(f"\nверсия правил: {VERSION}, провалов: {bad}")
        sys.exit(1 if bad else 0)

    data = sys.stdin.read()
    clean, kinds = redact(data)
    sys.stdout.write(clean)
    if kinds:
        sys.stderr.write(f"[вычищено: {', '.join(kinds)}]\n")
