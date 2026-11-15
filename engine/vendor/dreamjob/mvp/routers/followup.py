"""Owner approval controls for every follow-up draft.

``mvp.followup`` only creates a Telegram card. These handlers are the sole path
that may send it, and require the owner's explicit button press or edited text.

Callback-data prefixes claimed in this file: ``fu:send:``, ``fu:edit:``,
``fu:skip:``, ``fu:stop:``.
"""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from mvp import config
from mvp.followup import _STEP_ORDER, _STEPS_BY_KEY, send_followup

log = logging.getLogger(__name__)

router = Router()


class FollowupEdit(StatesGroup):
    text = State()


def _parse(callback_data: str) -> tuple[str, str]:
    parts = callback_data.split(":")
    return (parts[2], parts[3]) if len(parts) >= 4 else (parts[2] if len(parts) > 2 else "", "")


def _step(step_key: str):
    if step_key.startswith("status_"):
        return _STEPS_BY_KEY["status"]
    return _STEPS_BY_KEY.get(step_key)


async def _strip_keyboard(callback: CallbackQuery) -> None:
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def _deliver(target: Message, user_id: int, negotiation_id: str, step_key: str, text: str) -> None:
    reason = await send_followup(
        user_id, negotiation_id, step_key, text, owner_confirmed=True,
    )
    if reason:
        await target.answer(
            f"❌ hh.ru не принял сообщение: <code>{html.escape(reason)}</code>. Оно НЕ отправлено.",
            parse_mode="HTML",
        )
        return
    await target.answer("✅ hh.ru подтвердил: сообщение ушло рекрутёру, тред поднят.")


@router.callback_query(F.data.startswith("fu:send:"))
async def send_now(callback: CallbackQuery) -> None:
    negotiation_id, step_key = _parse(callback.data)
    pending = await asyncio.to_thread(
        config.AUTOMATION.pending_followup, callback.from_user.id, negotiation_id, step_key,
    )
    if not pending:
        await callback.answer("Это касание уже обработано", show_alert=True)
        await _strip_keyboard(callback)
        return
    await callback.answer("Отправляю…")
    await _strip_keyboard(callback)
    await _deliver(callback.message, callback.from_user.id, negotiation_id, step_key, pending["message"])


@router.callback_query(F.data.startswith("fu:skip:"))
async def skip_step(callback: CallbackQuery) -> None:
    negotiation_id, step_key = _parse(callback.data)
    await asyncio.to_thread(
        config.AUTOMATION.record_followup,
        user_id=callback.from_user.id, negotiation_id=negotiation_id, step=step_key,
        status="skipped", reason="owner_skipped",
    )
    await callback.answer("Пропущено — это касание больше не предложу")
    await _strip_keyboard(callback)


@router.callback_query(F.data.startswith("fu:stop:"))
async def stop_thread(callback: CallbackQuery) -> None:
    negotiation_id = callback.data.rsplit(":", 1)[-1]
    done = await asyncio.to_thread(
        config.AUTOMATION.followup_steps, callback.from_user.id, negotiation_id,
    )
    await asyncio.to_thread(
        config.AUTOMATION.stop_followups,
        callback.from_user.id, negotiation_id,
        [key for key in _STEP_ORDER if key not in done], "owner_stopped",
    )
    await callback.answer("Больше не буду писать в этот тред")
    await _strip_keyboard(callback)


@router.callback_query(F.data.startswith("fu:edit:"))
async def start_edit(callback: CallbackQuery, state: FSMContext) -> None:
    negotiation_id, step_key = _parse(callback.data)
    step = _step(step_key)
    if step is None:
        await callback.answer("Неизвестное касание", show_alert=True)
        return
    await state.set_state(FollowupEdit.text)
    await state.update_data(followup_negotiation_id=negotiation_id, followup_step=step_key)
    await callback.answer()
    await callback.message.answer(
        f"✏️ Пришлите свой текст для касания «{html.escape(step.title)}» одним сообщением — "
        "он уйдёт рекрутёру как есть.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Отмена", callback_data="fu:edit_cancel"),
        ]]),
    )


@router.callback_query(F.data == "fu:edit_cancel")
async def cancel_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Правка отменена — черновик остался нетронутым")
    await _strip_keyboard(callback)


@router.message(FollowupEdit.text, F.text)
async def apply_edit(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустое сообщение не отправлено. Пришлите текст ещё раз.")
        return
    data = await state.get_data()
    negotiation_id = str(data.get("followup_negotiation_id") or "")
    step_key = str(data.get("followup_step") or "")
    await state.clear()
    if not negotiation_id or not step_key:
        await message.answer("Черновик потерян — дождитесь следующей карточки.")
        return
    await _deliver(message, message.from_user.id, negotiation_id, step_key, text)
