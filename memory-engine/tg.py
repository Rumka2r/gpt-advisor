# -*- coding: utf-8 -*-
"""Доступ к Telegram-аккаунту Рувима (MTProto через Telethon).

Строка сессии хранится ЗАШИФРОВАННОЙ (DPAPI, CurrentUser) — на диске нет пригодного
к краже файла сессии. Расшифровка только в память процесса.

Авторизация (одноразово):
    python tg.py login-request              — запросить код (придёт В Telegram)
    python tg.py login-code 12345 [пароль]  — ввести код (и облачный пароль, если есть 2FA)

Работа:
    python tg.py whoami
    python tg.py dialogs [N]                — последние N чатов
    python tg.py bots                       — мои боты (через @BotFather)
    python tg.py history <чат> [N]          — последние N сообщений
    python tg.py search "текст" [N]         — поиск по всем чатам
    python tg.py send <чат> "текст"         — отправка (под шлюзом полномочий)
"""
import os, sys, json, asyncio, subprocess

STATE = os.path.expanduser("~/.claude/continuity/state")
SESSION_ENC = os.path.join(STATE, "tg_session.dpapi")
PENDING = os.path.join(STATE, ".tg_pending.json")
API_ID = 31289677
API_HASH = "9c10ead4b5524c14a475a4df7a01d75b"
PHONE = "+19802694748"

PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]


def dpapi_protect(text, path):
    script = (
        "Add-Type -AssemblyName System.Security;"
        "$b=[Console]::In.ReadToEnd();"
        "$e=[Security.Cryptography.ProtectedData]::Protect("
        "[Text.Encoding]::UTF8.GetBytes($b),$null,"
        "[Security.Cryptography.DataProtectionScope]::CurrentUser);"
        "[IO.File]::WriteAllBytes('%s',$e)" % path.replace("\\", "\\\\")
    )
    subprocess.run(PS + [script], input=text, text=True, encoding="utf-8", check=True)


def dpapi_unprotect(path):
    if not os.path.exists(path):
        return None
    script = (
        "Add-Type -AssemblyName System.Security;"
        "$b=[IO.File]::ReadAllBytes('%s');"
        "[Console]::Out.Write([Text.Encoding]::UTF8.GetString("
        "[Security.Cryptography.ProtectedData]::Unprotect($b,$null,"
        "[Security.Cryptography.DataProtectionScope]::CurrentUser)))" % path.replace("\\", "\\\\")
    )
    r = subprocess.run(PS + [script], capture_output=True, text=True, encoding="utf-8")
    return (r.stdout or "").strip() or None


def client(session_str=None):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(session_str or ""), API_ID, API_HASH)


async def _login_request():
    from telethon.sessions import StringSession
    c = client()
    await c.connect()
    sent = await c.send_code_request(PHONE)
    with open(PENDING, "w", encoding="utf-8") as f:
        json.dump({"hash": sent.phone_code_hash,
                   "session": StringSession.save(c.session)}, f)
    await c.disconnect()
    print("Код отправлен в Telegram на %s. Дальше: tg.py login-code <код> [пароль2FA]" % PHONE)


async def _login_code(code, password=None):
    from telethon.sessions import StringSession
    from telethon.errors import SessionPasswordNeededError
    with open(PENDING, encoding="utf-8") as f:
        p = json.load(f)
    c = client(p["session"])
    await c.connect()
    try:
        await c.sign_in(PHONE, code=code, phone_code_hash=p["hash"])
    except SessionPasswordNeededError:
        if not password:
            print("НУЖЕН облачный пароль (2FA): tg.py login-code <код> <пароль>")
            await c.disconnect()
            return
        await c.sign_in(password=password)
    me = await c.get_me()
    dpapi_protect(StringSession.save(c.session), SESSION_ENC)
    try:
        os.remove(PENDING)
    except OSError:
        pass
    await c.disconnect()
    print("Авторизован: %s %s (@%s), id=%s" % (
        me.first_name or "", me.last_name or "", me.username or "-", me.id))
    print("Сессия зашифрована DPAPI →", SESSION_ENC)


async def _with_client(fn):
    s = dpapi_unprotect(SESSION_ENC)
    if not s:
        print("Нет сессии. Сначала: tg.py login-request")
        return
    c = client(s)
    await c.connect()
    if not await c.is_user_authorized():
        print("Сессия недействительна — нужна повторная авторизация.")
        await c.disconnect()
        return
    try:
        await fn(c)
    finally:
        await c.disconnect()


async def _whoami(c):
    me = await c.get_me()
    print(json.dumps({"id": me.id, "first_name": me.first_name,
                      "last_name": me.last_name, "username": me.username,
                      "phone": me.phone, "premium": getattr(me, "premium", None)},
                     ensure_ascii=False, indent=1))


def _kind(d):
    if d.is_user:
        return "бот" if getattr(d.entity, "bot", False) else "личный"
    return "канал" if d.is_channel else "группа"


async def _dialogs(c, n):
    async for d in c.iter_dialogs(limit=n):
        print("%-9s | %-38s | unread %-4s | id %s" % (
            _kind(d), (d.name or "")[:38], d.unread_count, d.id))


async def _bots(c):
    found = []
    async for d in c.iter_dialogs():
        if d.is_user and getattr(d.entity, "bot", False):
            found.append((d.name, getattr(d.entity, "username", None), d.entity.id))
    print("Ботов в диалогах: %d" % len(found))
    for name, un, i in found:
        print("  %-32s @%-24s id %s" % ((name or "")[:32], un or "-", i))


async def resolve(c, chat):
    """StringSession не хранит кэш сущностей — числовой id приходится искать по диалогам."""
    if isinstance(chat, str) and not chat.lstrip("-").isdigit():
        return await c.get_entity(chat if chat.startswith("@") else "@" + chat)
    cid = int(chat)
    try:
        return await c.get_entity(cid)
    except (ValueError, TypeError):
        async for d in c.iter_dialogs():
            if d.id == cid or getattr(d.entity, "id", None) == cid:
                return d.entity
        raise ValueError("чат %s не найден среди диалогов" % chat)


async def _history(c, chat, n):
    chat = await resolve(c, chat)
    async for m in c.iter_messages(chat, limit=n):
        who = getattr(m.sender, "username", None) or getattr(m.sender, "first_name", "?")
        txt = (m.text or "").replace("\n", " ")[:160]
        print("[%s] %-16s %s" % (m.date.strftime("%m-%d %H:%M"), str(who)[:16], txt))


async def _search(c, q, n):
    got = 0
    async for m in c.iter_messages(None, search=q, limit=n):
        chat = getattr(m.chat, "title", None) or getattr(m.chat, "username", "?")
        print("[%s] %-22s %s" % (m.date.strftime("%Y-%m-%d"), str(chat)[:22],
                                 (m.text or "").replace("\n", " ")[:150]))
        got += 1
    if not got:
        print("ничего не найдено")


async def _send(c, chat, text):
    chat = await resolve(c, chat)
    m = await c.send_message(chat, text)
    print("отправлено, id=%s" % m.id)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, a = sys.argv[1], sys.argv[2:]
    loop = asyncio.new_event_loop()
    try:
        if cmd == "login-request":
            loop.run_until_complete(_login_request())
        elif cmd == "login-code":
            loop.run_until_complete(_login_code(a[0], a[1] if len(a) > 1 else None))
        elif cmd == "whoami":
            loop.run_until_complete(_with_client(_whoami))
        elif cmd == "dialogs":
            n = int(a[0]) if a else 30
            loop.run_until_complete(_with_client(lambda c: _dialogs(c, n)))
        elif cmd == "bots":
            loop.run_until_complete(_with_client(_bots))
        elif cmd == "history":
            n = int(a[1]) if len(a) > 1 else 20
            loop.run_until_complete(_with_client(lambda c: _history(c, a[0], n)))
        elif cmd == "search":
            n = int(a[1]) if len(a) > 1 else 20
            loop.run_until_complete(_with_client(lambda c: _search(c, a[0], n)))
        elif cmd == "send":
            loop.run_until_complete(_with_client(lambda c: _send(c, a[0], a[1])))
        else:
            print(__doc__)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
