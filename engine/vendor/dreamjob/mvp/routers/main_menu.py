"""Main menu screen: /start, add-vacancy prompt, demo, profile/facts lookup."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from candidate.data import load_candidate_data

from mvp import config
from mvp.formatting import _knowledge_summary
from mvp.generation import _generate_and_show
from mvp.keyboards import _main_keyboard
from mvp.middleware import _is_owner

router = Router()


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Карьерный ассистент готов. Автопоиск HH готовит черновики каждые 20 минут. "
        "Автоотклик заблокирован двумя независимыми предохранителями; ни один preview не отправляется сам.",
        reply_markup=_main_keyboard(),
    )


@router.callback_query(F.data == "mvp:add")
async def add_vacancy(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(
        "Пришлите одним сообщением название, ссылку и полное описание вакансии. Можно просто переслать пост из канала."
    )


@router.callback_query(F.data == "mvp:facts")
async def facts_button(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer("Поиск по базе: <code>/facts управление</code>", parse_mode="HTML")


@router.callback_query(F.data == "mvp:demo")
async def demo(callback: CallbackQuery) -> None:
    await callback.answer()
    await _generate_and_show(callback.message, config.DEMO_VACANCY)


@router.callback_query(F.data == "mvp:knowledge")
async def knowledge(callback: CallbackQuery) -> None:
    if not _is_owner(callback.from_user.id):
        await callback.answer("База фактов доступна только владельцу", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(_knowledge_summary(), parse_mode="HTML")


@router.message(Command("profile"))
async def profile_command(message: Message) -> None:
    if not _is_owner(message.from_user.id):
        await message.answer("База фактов доступна только владельцу.")
        return
    await message.answer(_knowledge_summary(), parse_mode="HTML")


@router.message(Command("facts"))
async def facts_command(message: Message) -> None:
    if not _is_owner(message.from_user.id):
        await message.answer("База фактов доступна только владельцу.")
        return
    query = (message.text or "").partition(" ")[2].strip()
    data = config.ADAPTER.data if config.ADAPTER else load_candidate_data()
    facts = data.search(query, limit=10)
    if not facts:
        await message.answer("Ничего не найдено.")
        return
    rows = [
        f"<b>{html.escape(f.id)}</b> · {html.escape(f.publication_status)}\n{html.escape(f.text)}"
        for f in facts
    ]
    await message.answer("\n\n".join(rows), parse_mode="HTML")
