"""HH.ru easy-login (browser OTP) flow and the /hh_code official-OAuth callback."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from urllib.parse import parse_qs, urlsplit

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from candidate.hh_official import HHOfficialClient, HHOfficialError
from hh.auth import HHAuth

from mvp import config
from mvp.automation import _run_automation_cycle
from mvp.hh_sync import _close_hh_browser_session, _hh_browser_client
from mvp.keyboards import _main_keyboard
from mvp.states import HHEasyLogin

log = logging.getLogger(__name__)

router = Router()


@router.callback_query(F.data == "mvp:hh_easy")
async def hh_easy_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _close_hh_browser_session(callback.from_user.id)
    await state.set_state(HHEasyLogin.login)
    await callback.message.answer(
        "Пришлите телефон или email от HH одним сообщением. Я использую его только для открытия "
        "официальной формы входа HH, сразу удалю сообщение и ничего не запишу в базу.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="mvp:hh_cancel")],
        ]),
    )


@router.callback_query(F.data == "mvp:hh_cancel")
async def hh_easy_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Вход отменён")
    await _close_hh_browser_session(callback.from_user.id)
    await state.clear()
    await callback.message.answer("Вход в HH отменён.", reply_markup=_main_keyboard())


@router.message(HHEasyLogin.login)
async def hh_easy_login(message: Message, state: FSMContext) -> None:
    login = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if not login or len(login) > 160:
        await message.answer("Нужен корректный телефон или email.")
        return
    if not os.environ.get("HH_CLIENT_ID") or not os.environ.get("HH_CLIENT_SECRET"):
        await state.clear()
        await message.answer("HH OAuth-приложение не настроено на сервере.")
        return

    wait = await message.answer("Открываю официальный вход HH и запрашиваю одноразовый код…")
    client = _hh_browser_client()
    auth = HHAuth(client, client.config)
    config.HH_BROWSER_SESSIONS[message.from_user.id] = (auth, client)
    try:
        await auth.start_login(login, progress=wait.edit_text)
    except Exception as exc:
        log.warning("Easy HH login start failed: %s", type(exc).__name__)
        await _close_hh_browser_session(message.from_user.id)
        await state.clear()
        await wait.edit_text(
            "Не удалось открыть форму HH — возможно, HH показал CAPTCHA. Попробуйте официальный OAuth-кнопкой."
        )
        return
    await state.set_state(HHEasyLogin.otp)
    await wait.edit_text(
        "HH отправил одноразовый код. Пришлите сюда только цифры — сообщение сразу удалится.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="mvp:hh_cancel")],
        ]),
    )


@router.message(HHEasyLogin.otp)
async def hh_easy_otp(message: Message, state: FSMContext) -> None:
    code = re.sub(r"\D", "", message.text or "")
    try:
        await message.delete()
    except Exception:
        pass
    session = config.HH_BROWSER_SESSIONS.get(message.from_user.id)
    if not session or not 4 <= len(code) <= 8:
        await message.answer("Код должен содержать 4–8 цифр. Попробуйте ещё раз.")
        return

    auth, client = session
    wait = await message.answer("Проверяю код и подключаю HH…")
    try:
        result = await auth.submit_code(code)
        resumes = await asyncio.to_thread(client.get_resumes)
        account = {
            "access_token": result["access_token"],
            "refresh_token": result.get("refresh_token", ""),
            "expires_at": result.get("expires_at", 0),
            "profile": {
                "id": result.get("hh_user_id"),
                "first_name": result.get("first_name"),
                "last_name": result.get("last_name"),
                "email": result.get("email"),
            },
        }
        await asyncio.to_thread(
            config.AUTOMATION.save_hh_account,
            message.from_user.id,
            account,
            resumes,
            result.get("cookies_json", []),
        )
    except Exception as exc:
        log.warning("Easy HH login completion failed: %s", type(exc).__name__)
        await _close_hh_browser_session(message.from_user.id)
        await state.clear()
        await wait.edit_text("HH не принял код или потребовал дополнительную проверку. Начните вход ещё раз.")
        return

    await _close_hh_browser_session(message.from_user.id)
    await state.clear()
    await wait.edit_text(
        f"✅ HH подключён. Найдено резюме: {len(resumes)}. Запускаю автопоиск и подготовку preview; отклики заблокированы."
    )
    asyncio.create_task(_run_automation_cycle(message.bot, notify_empty=True))


@router.message(Command("hh_code"))
async def hh_code_command(message: Message) -> None:
    raw = (message.text or "").partition(" ")[2].strip()
    if not raw:
        await message.answer("Пришлите <code>/hh_code CODE</code> или callback URL после OAuth.", parse_mode="HTML")
        return
    code = parse_qs(urlsplit(raw).query).get("code", [raw])[0]
    client_id = os.environ.get("HH_CLIENT_ID", "")
    client_secret = os.environ.get("HH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        await message.answer("HH OAuth app не настроено на сервере.")
        return
    wait = await message.answer("🔐 Завершаю официальную HH OAuth-авторизацию…")
    client = HHOfficialClient(client_id=client_id, client_secret=client_secret)
    try:
        tokens = await asyncio.to_thread(client.exchange_code, code)
        profile = await asyncio.to_thread(client.me)
        resumes = await asyncio.to_thread(client.resumes)
        tokens["profile"] = profile
        await asyncio.to_thread(config.AUTOMATION.save_hh_account, message.from_user.id, tokens, resumes)
    except HHOfficialError as exc:
        log.warning("HH OAuth failed: %s", exc.reason)
        await wait.edit_text(f"HH OAuth не завершён: {html.escape(exc.reason)}. Код не сохранён.")
        return
    await wait.edit_text(
        f"✅ HH подключён. Доступных резюме: {len(resumes)}. Запускаю автопоиск и безопасную подготовку preview."
    )
    asyncio.create_task(_run_automation_cycle(message.bot, notify_empty=True))
