"""History and per-application outcome screens.

The ``mvp:queue`` screen used to live here as a flat text list. It now belongs
to :mod:`mvp.routers.cards`, which renders the same journal as a browsable
deck — see that module for why a list without buttons was unusable.
"""
from __future__ import annotations

import asyncio
import html

from aiogram import F, Router
from aiogram.types import CallbackQuery

from mvp import config
from mvp.formatting import _history_text
from mvp.keyboards import _application_history_keyboard, _outcome_keyboard

router = Router()


@router.callback_query(F.data == "mvp:history")
async def history(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(
        _history_text(callback.from_user.id), parse_mode="HTML",
        reply_markup=_application_history_keyboard(callback.from_user.id),
    )


@router.callback_query(F.data.startswith("mvp:application:"))
async def application_detail(callback: CallbackQuery) -> None:
    variant_id = callback.data.rsplit(":", 1)[-1]
    application = next(
        (item for item in config.AUTOMATION.application_history(callback.from_user.id, 100)
         if item["variant_id"] == variant_id),
        None,
    )
    if not application:
        await callback.answer("Отклик не найден", show_alert=True)
        return
    vacancy = application["vacancy"]
    await callback.answer()
    await callback.message.answer(
        f"💼 <b>{html.escape(vacancy.get('name') or 'Вакансия')}</b>\n"
        f"Компания: {html.escape(vacancy.get('employer_name') or '')}\n"
        f"Локальный результат: {html.escape(application['reason'])}\n"
        f"HH state: <code>{html.escape(application.get('hh_state') or '—')}</code>\n"
        f"Outcome: <b>{html.escape(application.get('outcome_status') or application['status'])}</b>\n"
        f"Negotiation: <code>{html.escape(application.get('negotiation_id') or '—')}</code>",
        parse_mode="HTML", reply_markup=_outcome_keyboard(variant_id),
    )


@router.callback_query(F.data.startswith("mvp:outcome:"))
async def set_application_outcome(callback: CallbackQuery) -> None:
    _, _, outcome, variant_id = callback.data.split(":", 3)
    changed = await asyncio.to_thread(
        config.AUTOMATION.set_outcome, callback.from_user.id, variant_id, outcome,
    )
    await callback.answer("Этап сохранён" if changed else "Отклик не найден", show_alert=not changed)
    if changed:
        await callback.message.answer(f"✅ Этап отклика обновлён: <b>{html.escape(outcome)}</b>", parse_mode="HTML")
