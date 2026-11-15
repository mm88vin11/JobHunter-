"""Vacancy intake and preview, manual letter editing, and HTML exports.

The plain `F.text | F.caption` handler at the bottom of this file is a
catch-all: it matches any text/caption message regardless of FSM state or
command prefix. It MUST stay registered after every other message handler
(here and in every other router) so that state-filtered and command
handlers get first refusal — see `mvp/routers/__init__.py` for how router
registration order enforces this across files.
"""
from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from candidate.export import (
    render_ats_resume_html, render_cover_letter_html, render_vacancy_html, render_variant_html,
)
from candidate.hh_html import vacancy_from_api
from candidate.hh_official import HHOfficialError
from candidate.service import UnsafeVariantError
from candidate.vacancy import has_meaningful_description, normalize_vacancy_input

from mvp import config
from mvp.automation import _hh_client_for
from mvp.formatting import _preview_text
from mvp.generation import _generate_and_show
from mvp.keyboards import _preview_keyboard, _profile_keyboard
from mvp.states import ManualLetterEdit

router = Router()


@router.callback_query(F.data.startswith("mvp:letter_manual:"))
async def start_manual_letter(callback: CallbackQuery, state: FSMContext) -> None:
    variant_id = callback.data.rsplit(":", 1)[-1]
    payload = await asyncio.to_thread(config.STORE.get, variant_id)
    if not payload or payload.get("status") not in {"draft", "approved", "manual_required"}:
        await callback.answer("Этот вариант уже нельзя редактировать", show_alert=True)
        return
    await state.set_state(ManualLetterEdit.letter)
    await state.update_data(manual_letter_variant_id=variant_id)
    await callback.answer()
    await callback.message.answer(
        "✍️ Пришлите новую версию сопроводительного письма одним сообщением (180–750 знаков). "
        "Числа и факты должны подтверждаться карьерной базой или текстом вакансии. После изменения прежнее "
        "подтверждение пакета будет сброшено.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Отмена", callback_data="mvp:letter_cancel")
        ]]),
    )


@router.callback_query(F.data == "mvp:letter_cancel")
async def cancel_manual_letter(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Редактирование отменено")
    await callback.message.edit_reply_markup(reply_markup=None)


@router.message(ManualLetterEdit.letter, F.text)
async def save_manual_letter(message: Message, state: FSMContext) -> None:
    state_data = await state.get_data()
    variant_id = str(state_data.get("manual_letter_variant_id") or "")
    payload = await asyncio.to_thread(config.STORE.get, variant_id)
    if not payload:
        await state.clear()
        await message.answer("Вариант больше не найден. Откройте очередь и начните заново.")
        return
    letter = (message.text or "").strip()
    fact_ids = set(payload.get("used_fact_ids") or [])
    tone = str((payload.get("generated_variant") or {}).get("cover_letter_tone") or "direct")
    try:
        config.ADAPTER.validate_manual_letter(
            letter, vacancy=payload.get("vacancy") or {}, fact_ids=fact_ids, tone=tone,
        )
    except UnsafeVariantError as exc:
        reason = str(exc)
        if "Длина письма" in reason:
            hint = f"Сейчас {len(letter)} знаков; обычно нужно 180–750."
        elif "numeric" in reason.lower():
            hint = "В письме есть число, которого нет среди подтверждённых фактов или в вакансии."
        elif "jargon" in reason.lower():
            hint = "Уберите внутренние пометки вроде fact_id, статусов доказательств или редакторских комментариев."
        else:
            hint = "Проверьте длину, связь с вакансией и обязательные инструкции работодателя."
        await message.answer(f"⚠️ Письмо пока не прошло проверку. {hint}\n\nИсправьте и пришлите его ещё раз.")
        return
    payload["generated_variant"]["cover_letter"] = letter
    payload["generated_variant"]["cover_letter_edit"] = {"mode": "manual"}
    changed = await asyncio.to_thread(
        config.STORE.update_payload, payload, status="draft",
        expected=("draft", "approved", "manual_required"),
    )
    if not changed:
        await state.clear()
        await message.answer("Состояние варианта уже изменилось. Откройте свежую версию из очереди.")
        return
    payload["status"] = "draft"
    await state.clear()
    await message.answer(
        "✅ Ручная версия письма сохранена. Предыдущее подтверждение сброшено: проверьте весь пакет и "
        "подтвердите его заново. HH ничего не отправлено."
    )
    await message.answer(
        _preview_text(payload), parse_mode="HTML",
        reply_markup=_preview_keyboard(variant_id, payload["vacancy"].get("source_url", ""), payload),
    )


@router.message(F.text | F.caption)
async def vacancy_text(message: Message) -> None:
    text = message.text or message.caption or ""
    if text.startswith("/"):
        return
    forwarded_from = type(message.forward_origin).__name__ if message.forward_origin else None
    vacancy = normalize_vacancy_input(text, forwarded_from=forwarded_from)
    if vacancy.get("source") == "hh" and vacancy.get("external_id") and not has_meaningful_description(vacancy):
        client, _ = await _hh_client_for(message.chat.id)
        if client is None:
            await message.answer("Подключите HH в настройках, и я сам загружу вакансию по ссылке.")
            return
        try:
            detail = await asyncio.to_thread(client.vacancy, vacancy["external_id"])
        except HHOfficialError as exc:
            await message.answer(f"Не удалось загрузить вакансию с HH: {exc.reason}.")
            return
        vacancy = vacancy_from_api(detail, search_query="manual_link")
    if not has_meaningful_description(vacancy):
        await message.answer(
            "Не могу надёжно прочитать содержимое только по ссылке. Пришлите или перешлите полное описание вакансии вместе с URL — ничего выдумывать не буду."
        )
        return
    await _generate_and_show(message, vacancy)


@router.callback_query(F.data.startswith("mvp:change:"))
async def change(callback: CallbackQuery) -> None:
    variant_id = callback.data.rsplit(":", 1)[-1]
    await callback.answer()
    await callback.message.answer("Выберите другой master-профиль:", reply_markup=_profile_keyboard(variant_id))


@router.callback_query(F.data.startswith("mvp:letter:"))
async def change_letter(callback: CallbackQuery) -> None:
    variant_id = callback.data.rsplit(":", 1)[-1]
    payload = await asyncio.to_thread(config.STORE.get, variant_id)
    if not payload:
        await callback.answer("Вариант не найден", show_alert=True)
        return
    await asyncio.to_thread(config.STORE.set_status, variant_id, "changed", ("draft",))
    await callback.answer("Готовлю другой вариант письма")
    await _generate_and_show(callback.message, payload["vacancy"], forced_profile=payload["master_profile"]["key"])


@router.callback_query(F.data.startswith("mvp:profile:"))
async def choose_profile(callback: CallbackQuery) -> None:
    _, _, profile_key, variant_id = callback.data.split(":", 3)
    payload = await asyncio.to_thread(config.STORE.get, variant_id)
    if not payload:
        await callback.answer("Вариант не найден", show_alert=True)
        return
    await asyncio.to_thread(config.STORE.set_status, variant_id, "changed", ("draft",))
    await callback.answer()
    await _generate_and_show(callback.message, payload["vacancy"], forced_profile=profile_key)


@router.callback_query(F.data.startswith("mvp:skip:"))
async def skip(callback: CallbackQuery) -> None:
    variant_id = callback.data.rsplit(":", 1)[-1]
    changed = await asyncio.to_thread(config.STORE.set_status, variant_id, "skipped", ("draft",))
    await callback.answer("Пропущено" if changed else "Решение уже обработано", show_alert=not changed)
    if changed:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("mvp:files:"))
async def export_files(callback: CallbackQuery) -> None:
    variant_id = callback.data.rsplit(":", 1)[-1]
    payload = await asyncio.to_thread(config.STORE.get, variant_id)
    if not payload:
        await callback.answer("Вариант не найден", show_alert=True)
        return
    await callback.answer("Собираю файлы")
    documents = [
        (render_variant_html(payload, config.ROOT / "var" / "exports" / f"resume-{variant_id}.html"), "Резюме (HTML)"),
        (render_ats_resume_html(payload, config.ROOT / "var" / "exports" / f"resume-ats-{variant_id}.html"), "ATS-резюме"),
        (render_cover_letter_html(payload, config.ROOT / "var" / "exports" / f"cover-letter-{variant_id}.html"), "Сопроводительное письмо"),
        (render_vacancy_html(payload, config.ROOT / "var" / "exports" / f"vacancy-{variant_id}.html"), "Вакансия (snapshot)"),
    ]
    for path, caption in documents:
        await callback.message.answer_document(FSInputFile(path), caption=caption)
