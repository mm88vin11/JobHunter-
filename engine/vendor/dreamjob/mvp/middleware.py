"""Owner allowlist: only the configured owner Telegram ID may use this bot."""
from __future__ import annotations

import os

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message


def _is_owner(user_id: int) -> bool:
    owner = os.environ.get("MVP_OWNER_TELEGRAM_ID") or os.environ.get("ADMIN_TELEGRAM_ID", "")
    return bool(owner and owner.isdigit() and int(owner) == user_id)


class OwnerOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and _is_owner(user.id):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("Это приватный карьерный бот", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("Это приватный карьерный бот владельца.")
        return None
