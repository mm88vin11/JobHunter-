"""Необязательный режим: отклики HR в Telegram с вашего личного аккаунта (Telethon, MTProto).

Ботам Telegram запрещено писать людям первыми, поэтому полная автоматика возможна только от имени вашего аккаунта.
Риск: Telegram банит за массовые сообщения незнакомым людям. Поэтому жёсткий лимит в сутки и только HR-контакты
из самих вакансий. Включается секретами TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE и командой /tglogin в боте.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

log = logging.getLogger("jobhunter")
DAILY_LIMIT = int(os.environ.get("TG_USER_DAILY_LIMIT", "15"))


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_API_ID") and os.environ.get("TELEGRAM_API_HASH") and os.environ.get("TELEGRAM_PHONE"))


def available(store) -> bool:
    return configured() and bool(store.get("tg_user_session"))


def _client(session: str = ""):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(StringSession(session), int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"],
                          device_model="JobHunter", system_version="GitHub", app_version="1.0")


def start_login(store) -> str:
    async def run():
        c = _client()
        await c.connect()
        r = await c.send_code_request(os.environ["TELEGRAM_PHONE"])
        store.set("tg_login", {"hash": r.phone_code_hash, "session": c.session.save()})
        await c.disconnect()
    asyncio.run(run())
    return ("Telegram прислал вам код входа (в приложении Telegram, чат «Telegram»). Отправьте его сюда С ПРОБЕЛАМИ между цифрами, "
            "иначе Telegram аннулирует код: /code 1 2 3 4 5")


def finish_login(store, code: str = "", password: str = "") -> str:
    from telethon.errors import SessionPasswordNeededError

    st = store.get("tg_login") or {}
    if not st:
        return "Сначала /tglogin"

    async def run():
        c = _client(st["session"])
        await c.connect()
        try:
            if password:
                await c.sign_in(password=password)
            else:
                await c.sign_in(os.environ["TELEGRAM_PHONE"], re.sub(r"\D", "", code), phone_code_hash=st["hash"])
        except SessionPasswordNeededError:
            store.set("tg_login", {**st, "session": c.session.save()})
            await c.disconnect()
            return "У аккаунта включён облачный пароль. Отправьте: /password ваш_пароль (сообщение я сразу удалю)"
        me = await c.get_me()
        store.set("tg_user_session", c.session.save())
        store.set("tg_login", None)
        await c.disconnect()
        return f"✅ Готово: отклики в Telegram теперь уходят сами от имени {me.first_name} (не больше {DAILY_LIMIT} в сутки)."
    return asyncio.run(run())


def sent_today(store) -> int:
    day = time.strftime("%Y-%m-%d")
    st = store.get("tg_user_sent") or {}
    return st.get(day, 0)


def send(store, username: str, text: str) -> None:
    if sent_today(store) >= DAILY_LIMIT:
        raise RuntimeError(f"дневной лимит Telegram-откликов ({DAILY_LIMIT}) исчерпан — завтра продолжу")

    async def run():
        c = _client(store.get("tg_user_session"))
        await c.connect()
        try:
            await c.send_message(username, text)
        finally:
            await c.disconnect()
    asyncio.run(run())
    day = time.strftime("%Y-%m-%d")
    store.set("tg_user_sent", {day: sent_today(store) + 1})


def check_replies(store, notify, handle_reply) -> int:
    """Читает ответы HR в личных чатах, куда мы писали."""
    if not available(store):
        return 0
    rows = store.q("""SELECT a.id, v.contacts FROM applications a JOIN vacancies v ON v.id = a.vacancy_id
                      WHERE a.channel = 'telegram' AND a.status NOT IN ('waiting_user','draft','rejected') AND a.sent_at >= datetime('now','-30 days')""")
    if not rows:
        return 0
    import json

    targets = []
    for r in rows:
        tg = (json.loads(r["contacts"] or "{}").get("telegram") or [])
        if tg:
            targets.append((r["id"], tg[0]))
    found = []

    async def run():
        c = _client(store.get("tg_user_session"))
        await c.connect()
        try:
            for app_id, user in targets[:40]:
                try:
                    for m in await c.get_messages(user, limit=5):
                        if not m.out and m.message:
                            found.append((app_id, f"tg:{m.id}", m.message))
                except Exception as e:
                    log.info("tg replies %s: %s", user, e)
        finally:
            await c.disconnect()
    asyncio.run(run())
    n = 0
    for app_id, mid, text in found:
        if store.x("INSERT OR IGNORE INTO messages (application_id, direction, body, external_id) VALUES (?, 'in', ?, ?)", app_id, text[:8000], mid):
            handle_reply(store, app_id, text, notify)
            n += 1
    return n
