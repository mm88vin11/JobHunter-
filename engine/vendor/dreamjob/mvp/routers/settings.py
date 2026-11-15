"""Settings hub: search/apply toggles, gate status, manual search, plus the
owner-editable settings screens (search queries, filters, trap lists, letter
style, CRM) and a single overview.

Every callback data string and handler that existed before this extension
(``mvp:search``, ``mvp:toggle_search``, ``mvp:toggle_apply``,
``mvp:search_now``, ``mvp:settings``, ``/search_now``) keeps working exactly
as before. New screens live under the ``mvp:cfg:*`` callback-data namespace
and a dedicated FSM state group, ``SettingsInput``, defined below.

Nothing in this file sends an application, writes to hh.ru, or arms
auto-apply -- the auto-apply toggle keeps its existing safety behaviour
(``toggle_apply`` below, unchanged).
"""
from __future__ import annotations

import asyncio
import html
import os

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from mvp.formatting import send_long_message

from candidate.data import load_candidate_data
from candidate.hh_official import authorize_url
from candidate.traps import (
    _employer_id,
    _employer_name,
    add_blocked_employer,
    add_stop_phrase,
    load_user_lists,
    remove_blocked_employer,
    remove_stop_phrase,
)

from mvp import config
from mvp import settings_store as store
from mvp.automation import _run_automation_cycle

router = Router()


class SettingsInput(StatesGroup):
    """Text-input states for the settings hub screens below. Each is entered
    by a dedicated "start" callback and exited either by the matching
    "*_save" message handler or by the shared ``mvp:cfg:cancel`` callback.
    """

    query_add = State()
    filter_daily_limit = State()
    filter_min_preview = State()
    filter_min_apply = State()
    trap_add_phrase = State()
    trap_add_employer = State()
    letter_add_word = State()
    crm_silence_days = State()
    crm_digest_time = State()
    crm_archive_days = State()


# ── shared helpers ──────────────────────────────────────────────────────


def _profile_titles() -> list[str]:
    data = config.ADAPTER.data if config.ADAPTER else load_candidate_data()
    return [p.desired_title for p in data.profiles.values()]


def _current_queries(user_id: int) -> list[str]:
    override = store.get_search_queries(user_id)
    return override if override is not None else store.default_query_list(_profile_titles())


TONE_KEYS = {"calm": "спокойный", "direct": "прямой", "confident": "уверенный"}
TONE_LABELS = {"calm": "😌 Спокойный", "direct": "➡️ Прямой", "confident": "💪 Уверенный"}


def _tone_key(tone: str) -> str:
    for key, value in TONE_KEYS.items():
        if value == tone:
            return key
    return "direct"


def _parse_int(text: str | None) -> int | None:
    text = (text or "").strip()
    if not text or not text.lstrip("-").isdigit():
        return None
    return int(text)


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отмена", callback_data="mvp:cfg:cancel")],
    ])


def _back_keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    rows.append([InlineKeyboardButton(text="⬅️ Назад к обзору", callback_data="mvp:cfg:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _overview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Поисковые запросы", callback_data="mvp:cfg:queries")],
        [InlineKeyboardButton(text="🎚 Фильтры", callback_data="mvp:cfg:filters")],
        [InlineKeyboardButton(text="🚫 Стоп-фразы и компании", callback_data="mvp:cfg:traps")],
        [InlineKeyboardButton(text="✍️ Стиль писем", callback_data="mvp:cfg:letter")],
        [InlineKeyboardButton(text="📇 CRM", callback_data="mvp:cfg:crm")],
        [InlineKeyboardButton(text="🤖 Автопоиск / автоотклик", callback_data="mvp:search")],
    ])


# ── existing screens (unchanged behaviour, kept verbatim) ──────────────


@router.callback_query(F.data == "mvp:search")
async def search_setup(callback: CallbackQuery) -> None:
    await callback.answer()
    settings = config.AUTOMATION.settings(callback.from_user.id)
    account = config.AUTOMATION.hh_account(callback.from_user.id)
    client_id = os.environ.get("HH_CLIENT_ID", "")
    rows = [
        [InlineKeyboardButton(
            text="⏸ Остановить автопоиск" if settings["search_enabled"] else "▶️ Запустить автопоиск",
            callback_data="mvp:toggle_search",
        )],
        [InlineKeyboardButton(text=(
            "⏸ Остановить автоотклик" if settings["auto_apply_enabled"]
            else "🔒 Автоотклик заблокирован"
        ), callback_data="mvp:toggle_apply")],
        [InlineKeyboardButton(text="🔄 Искать сейчас", callback_data="mvp:search_now")],
        [InlineKeyboardButton(text="🔍 Поисковые запросы", callback_data="mvp:cfg:queries")],
    ]
    if client_id and not account:
        rows.append([InlineKeyboardButton(text="⚡ Подключить HH — простой вход", callback_data="mvp:hh_easy")])
        rows.append([InlineKeyboardButton(text="🔐 Официальный OAuth", url=authorize_url(client_id))])
    await callback.message.answer(
        "🤖 <b>Автоматический режим</b>\n\n"
        f"Поиск HH: {'включён' if settings['search_enabled'] else 'остановлен'}; проверка каждые 20 минут.\n"
        f"Автоотклик: {'включён' if settings['auto_apply_enabled'] else 'остановлен'}.\n"
        f"Лимит: {settings['daily_limit']} успешных откликов в день.\n"
        f"HH OAuth: {'подключён' if account else 'нужно подключить один раз'}.\n\n"
        f"Production gate: {'ARMED' if os.environ.get('AUTO_APPLY_ARMED', '0') == '1' else 'LOCKED'}.\n\n"
        "Поиск и подготовка preview работают независимо от отклика. Подключение HH нужно для read-only "
        "проверки резюме и статусов; само по себе оно ничего не отправляет.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "mvp:toggle_search")
async def toggle_search(callback: CallbackQuery) -> None:
    enabled = await asyncio.to_thread(config.AUTOMATION.toggle, callback.from_user.id, "search_enabled")
    await callback.answer("Автопоиск включён" if enabled else "Автопоиск остановлен")
    await search_setup(callback)


@router.callback_query(F.data == "mvp:toggle_apply")
async def toggle_apply(callback: CallbackQuery) -> None:
    if os.environ.get("AUTO_APPLY_ARMED", "0") != "1":
        config.AUTOMATION.set_enabled(callback.from_user.id, "auto_apply_enabled", False)
        await callback.answer(
            "Системный gate заблокирован. Сначала итоговый checklist и отдельное решение владельца.",
            show_alert=True,
        )
        return
    enabled = await asyncio.to_thread(config.AUTOMATION.toggle, callback.from_user.id, "auto_apply_enabled")
    await callback.answer("Автоотклик включён" if enabled else "Автоотклик остановлен")
    await search_setup(callback)


@router.callback_query(F.data == "mvp:search_now")
async def search_now(callback: CallbackQuery) -> None:
    await callback.answer("Поиск запущен")
    await callback.message.answer("🔄 Ищу новые вакансии HH в фоне.")
    asyncio.create_task(_run_automation_cycle(callback.bot, notify_empty=True))


@router.callback_query(F.data == "mvp:settings")
async def settings(callback: CallbackQuery) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    hh_app = bool(os.environ.get("HH_CLIENT_ID") and os.environ.get("HH_CLIENT_SECRET"))
    automation = config.AUTOMATION.settings(user_id)
    account = config.AUTOMATION.hh_account(user_id)
    queries = _current_queries(user_id)
    overridden = store.get_search_queries(user_id) is not None
    lists = load_user_lists(user_id)
    letter = store.get_letter_style(user_id)
    crm = store.get_crm_settings(user_id)
    await send_long_message(
        callback.message,
        "⚙️ <b>Настройки — обзор</b>\n\n"
        f"HH OAuth app: {'настроено, требуется user OAuth' if hh_app else 'не настроено'}\n"
        f"HH account: {'подключён' if account else 'не подключён'}\n"
        f"Автопоиск: {'включён' if automation['search_enabled'] else 'выключен'}\n"
        f"Автоотклик: {'включён' if automation['auto_apply_enabled'] else 'выключен'} "
        f"(production gate: {'ARMED' if os.environ.get('AUTO_APPLY_ARMED', '0') == '1' else 'LOCKED'})\n\n"
        "🎚 <b>Фильтры</b>\n"
        f"Дневной лимит откликов: {automation['daily_limit']}\n"
        f"Порог показа вакансии (preview): {automation['min_preview_score']}%\n"
        f"Порог сильного совпадения (apply): {automation['min_apply_score']}%\n\n"
        "🔍 <b>Поисковые запросы</b>\n"
        f"{len(queries)} шт., сейчас {'свои' if overridden else 'по умолчанию'}\n\n"
        "🚫 <b>Чёрный список</b>\n"
        f"Стоп-фраз: {len(lists['stop_phrases'])}; заблокированных компаний: {len(lists['blocked_employers'])}\n\n"
        "✍️ <b>Стиль писем</b>\n"
        f"Тон: {html.escape(letter['tone'])}; личных стоп-слов: {len(letter['denylist'])}\n\n"
        "📇 <b>CRM</b>\n"
        f"Порог тишины: {crm['silence_days']} дн.; "
        f"дайджест {'включён' if crm['digest_enabled'] else 'выключен'} в {crm['digest_time']}; "
        f"автоархив: {crm['archive_after_days']} дн.",
        parse_mode="HTML",
        reply_markup=_overview_keyboard(),
    )


@router.message(Command("search_now"))
async def search_now_command(message: Message) -> None:
    await message.answer("🔄 Ищу новые вакансии HH в фоне.")
    asyncio.create_task(_run_automation_cycle(message.bot, notify_empty=True))


# ── search queries (mvp:cfg:queries, mvp:cfg:q:*) ───────────────────────


async def _render_queries(message: Message, user_id: int) -> None:
    queries = _current_queries(user_id)
    overridden = store.get_search_queries(user_id) is not None
    window = max(4, int(os.environ.get("AUTO_SEARCH_QUERY_WINDOW", "6")))
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Добавить запрос", callback_data="mvp:cfg:q:add")],
        [InlineKeyboardButton(text="♻️ Сбросить к умолчаниям", callback_data="mvp:cfg:q:reset")],
    ]
    for i, query in enumerate(queries):
        rows.append([InlineKeyboardButton(text=f"❌ {query[:28]}", callback_data=f"mvp:cfg:q:rm:{i}")])
    lines = "\n".join(f"{i + 1}. {html.escape(q)}" for i, q in enumerate(queries)) or "нет запросов"
    warning = "\n\n⚠️ Список пуст — автопоиск не найдёт вакансий." if not queries else ""
    await send_long_message(
        message,
        "🔍 <b>Поисковые запросы</b>\n\n"
        f"Сейчас: {'свои' if overridden else 'по умолчанию'}. За один цикл поиска берётся до {window} "
        f"запросов по кругу.\n\n{lines}{warning}",
        parse_mode="HTML",
        reply_markup=_back_keyboard(rows),
    )


@router.callback_query(F.data == "mvp:cfg:queries")
async def queries_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await _render_queries(callback.message, callback.from_user.id)


@router.callback_query(F.data == "mvp:cfg:q:add")
async def query_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsInput.query_add)
    await callback.message.answer(
        "Пришлите текст нового поискового запроса (как в поиске HH) одним сообщением.",
        reply_markup=_cancel_keyboard(),
    )


@router.message(SettingsInput.query_add, F.text)
async def query_add_save(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    await state.clear()
    if not text or len(text) > 200:
        await message.answer("⚠️ Нужен непустой запрос до 200 символов. Откройте «Поисковые запросы» и попробуйте снова.")
        return
    queries = await asyncio.to_thread(store.add_search_query, message.from_user.id, text, _profile_titles())
    await message.answer(f"✅ Добавлено: «{html.escape(text)}». Всего запросов: {len(queries)}.")
    await _render_queries(message, message.from_user.id)


@router.callback_query(F.data.startswith("mvp:cfg:q:rm:"))
async def query_remove(callback: CallbackQuery) -> None:
    try:
        idx = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        idx = -1
    queries = _current_queries(callback.from_user.id)
    if not (0 <= idx < len(queries)):
        await callback.answer("Список уже изменился, открываю заново", show_alert=True)
        await _render_queries(callback.message, callback.from_user.id)
        return
    removed = queries[idx]
    await asyncio.to_thread(store.remove_search_query, callback.from_user.id, removed, _profile_titles())
    await callback.answer(f"Удалено: {removed[:40]}")
    await _render_queries(callback.message, callback.from_user.id)


@router.callback_query(F.data == "mvp:cfg:q:reset")
async def query_reset(callback: CallbackQuery) -> None:
    await asyncio.to_thread(store.reset_search_queries, callback.from_user.id)
    await callback.answer("Сброшено к умолчаниям")
    await _render_queries(callback.message, callback.from_user.id)


# ── filters (mvp:cfg:filters, mvp:cfg:f:*) ──────────────────────────────


async def _render_filters(message: Message, user_id: int) -> None:
    automation = config.AUTOMATION.settings(user_id)
    rows = [
        [InlineKeyboardButton(text="✏️ Дневной лимит", callback_data="mvp:cfg:f:limit")],
        [InlineKeyboardButton(text="✏️ Порог показа (preview)", callback_data="mvp:cfg:f:preview")],
        [InlineKeyboardButton(text="✏️ Порог сильного совпадения (apply)", callback_data="mvp:cfg:f:apply")],
    ]
    await message.answer(
        "🎚 <b>Фильтры</b>\n\n"
        f"Дневной лимит откликов: {automation['daily_limit']} "
        f"(допустимо {store.DAILY_LIMIT_MIN}–{store.DAILY_LIMIT_MAX})\n"
        f"Порог показа вакансии на review: {automation['min_preview_score']}%\n"
        f"Порог сильного совпадения (apply): {automation['min_apply_score']}%\n\n"
        "Порог показа не может быть выше порога сильного совпадения.",
        parse_mode="HTML",
        reply_markup=_back_keyboard(rows),
    )


@router.callback_query(F.data == "mvp:cfg:filters")
async def filters_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await _render_filters(callback.message, callback.from_user.id)


@router.callback_query(F.data == "mvp:cfg:f:limit")
async def filter_limit_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsInput.filter_daily_limit)
    await callback.message.answer(
        f"Пришлите новый дневной лимит откликов ({store.DAILY_LIMIT_MIN}–{store.DAILY_LIMIT_MAX}).",
        reply_markup=_cancel_keyboard(),
    )


@router.callback_query(F.data == "mvp:cfg:f:preview")
async def filter_preview_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsInput.filter_min_preview)
    await callback.message.answer(
        "Пришлите новый порог показа вакансии на review (0–100, %).",
        reply_markup=_cancel_keyboard(),
    )


@router.callback_query(F.data == "mvp:cfg:f:apply")
async def filter_apply_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsInput.filter_min_apply)
    await callback.message.answer(
        "Пришлите новый порог сильного совпадения (0–100, %).",
        reply_markup=_cancel_keyboard(),
    )


@router.message(SettingsInput.filter_daily_limit, F.text)
async def filter_limit_save(message: Message, state: FSMContext) -> None:
    await state.clear()
    value = _parse_int(message.text)
    if value is None or not (store.DAILY_LIMIT_MIN <= value <= store.DAILY_LIMIT_MAX):
        await message.answer(
            f"⚠️ Нужно целое число от {store.DAILY_LIMIT_MIN} до {store.DAILY_LIMIT_MAX}. Значение не изменено."
        )
        return
    await asyncio.to_thread(
        store.update_automation_thresholds, config.AUTOMATION, message.from_user.id, daily_limit=value,
    )
    await message.answer(f"✅ Дневной лимит установлен: {value}")
    await _render_filters(message, message.from_user.id)


@router.message(SettingsInput.filter_min_preview, F.text)
async def filter_preview_save(message: Message, state: FSMContext) -> None:
    await state.clear()
    value = _parse_int(message.text)
    if value is None or not (store.SCORE_MIN <= value <= store.SCORE_MAX):
        await message.answer("⚠️ Нужно целое число от 0 до 100. Значение не изменено.")
        return
    current = config.AUTOMATION.settings(message.from_user.id)
    if value > current["min_apply_score"]:
        await message.answer(
            f"⚠️ Порог показа не может быть выше порога сильного совпадения ({current['min_apply_score']}%). "
            "Сначала увеличьте порог сильного совпадения."
        )
        return
    await asyncio.to_thread(
        store.update_automation_thresholds, config.AUTOMATION, message.from_user.id, min_preview_score=value,
    )
    await message.answer(f"✅ Порог показа установлен: {value}%")
    await _render_filters(message, message.from_user.id)


@router.message(SettingsInput.filter_min_apply, F.text)
async def filter_apply_save(message: Message, state: FSMContext) -> None:
    await state.clear()
    value = _parse_int(message.text)
    if value is None or not (store.SCORE_MIN <= value <= store.SCORE_MAX):
        await message.answer("⚠️ Нужно целое число от 0 до 100. Значение не изменено.")
        return
    current = config.AUTOMATION.settings(message.from_user.id)
    if value < current["min_preview_score"]:
        await message.answer(
            f"⚠️ Порог сильного совпадения не может быть ниже порога показа ({current['min_preview_score']}%). "
            "Сначала уменьшите порог показа."
        )
        return
    await asyncio.to_thread(
        store.update_automation_thresholds, config.AUTOMATION, message.from_user.id, min_apply_score=value,
    )
    await message.answer(f"✅ Порог сильного совпадения установлен: {value}%")
    await _render_filters(message, message.from_user.id)


# ── trap lists (mvp:cfg:traps, mvp:cfg:t:*) ─────────────────────────────


async def _render_traps(message: Message, user_id: int) -> None:
    lists = load_user_lists(user_id)
    phrases = lists["stop_phrases"]
    employers = lists["blocked_employers"]
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Стоп-фраза", callback_data="mvp:cfg:t:addphrase")],
        [InlineKeyboardButton(text="➕ Заблокировать компанию", callback_data="mvp:cfg:t:addemp")],
    ]
    for i, phrase in enumerate(phrases):
        rows.append([InlineKeyboardButton(text=f"❌ {str(phrase)[:28]}", callback_data=f"mvp:cfg:t:rmphrase:{i}")])
    for i, employer in enumerate(employers):
        name = employer.get("name") if isinstance(employer, dict) else str(employer)
        rows.append([InlineKeyboardButton(text=f"❌ {str(name)[:28]}", callback_data=f"mvp:cfg:t:rmemp:{i}")])
    phrase_lines = "\n".join(f"• {html.escape(str(p))}" for p in phrases) or "нет"
    employer_lines = "\n".join(
        f"• {html.escape(str(e.get('name') if isinstance(e, dict) else e))}" for e in employers
    ) or "нет"
    await send_long_message(
        message,
        "🚫 <b>Стоп-фразы и чёрный список компаний</b>\n\n"
        "Совпадение с любым пунктом ниже — жёсткий отказ независимо от score.\n\n"
        f"<b>Стоп-фразы</b>:\n{phrase_lines}\n\n"
        f"<b>Заблокированные компании</b>:\n{employer_lines}\n\n"
        "Также можно заблокировать компанию из последней показанной вакансии командой /block_employer.",
        parse_mode="HTML",
        reply_markup=_back_keyboard(rows),
    )


@router.callback_query(F.data == "mvp:cfg:traps")
async def traps_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await _render_traps(callback.message, callback.from_user.id)


@router.callback_query(F.data == "mvp:cfg:t:addphrase")
async def trap_add_phrase_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsInput.trap_add_phrase)
    await callback.message.answer(
        "Пришлите стоп-фразу — если она встретится в тексте вакансии, вакансия сразу отклоняется.",
        reply_markup=_cancel_keyboard(),
    )


@router.message(SettingsInput.trap_add_phrase, F.text)
async def trap_add_phrase_save(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    await state.clear()
    if not text or len(text) > 200:
        await message.answer("⚠️ Нужна непустая фраза до 200 символов.")
        return
    await asyncio.to_thread(add_stop_phrase, message.from_user.id, text)
    await message.answer(f"✅ Стоп-фраза добавлена: «{html.escape(text)}»")
    await _render_traps(message, message.from_user.id)


@router.callback_query(F.data == "mvp:cfg:t:addemp")
async def trap_add_employer_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsInput.trap_add_employer)
    await callback.message.answer(
        "Пришлите точное название компании для блокировки.",
        reply_markup=_cancel_keyboard(),
    )


@router.message(SettingsInput.trap_add_employer, F.text)
async def trap_add_employer_save(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    await state.clear()
    if not text or len(text) > 200:
        await message.answer("⚠️ Нужно непустое название компании до 200 символов.")
        return
    await asyncio.to_thread(add_blocked_employer, message.from_user.id, text)
    await message.answer(f"✅ Компания заблокирована: «{html.escape(text)}»")
    await _render_traps(message, message.from_user.id)


@router.callback_query(F.data.startswith("mvp:cfg:t:rmphrase:"))
async def trap_remove_phrase(callback: CallbackQuery) -> None:
    lists = load_user_lists(callback.from_user.id)
    phrases = lists["stop_phrases"]
    try:
        idx = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        idx = -1
    if not (0 <= idx < len(phrases)):
        await callback.answer("Список уже изменился, открываю заново", show_alert=True)
        await _render_traps(callback.message, callback.from_user.id)
        return
    removed = str(phrases[idx])
    await asyncio.to_thread(remove_stop_phrase, callback.from_user.id, removed)
    await callback.answer(f"Удалено: {removed[:40]}")
    await _render_traps(callback.message, callback.from_user.id)


@router.callback_query(F.data.startswith("mvp:cfg:t:rmemp:"))
async def trap_remove_employer(callback: CallbackQuery) -> None:
    lists = load_user_lists(callback.from_user.id)
    employers = lists["blocked_employers"]
    try:
        idx = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        idx = -1
    if not (0 <= idx < len(employers)):
        await callback.answer("Список уже изменился, открываю заново", show_alert=True)
        await _render_traps(callback.message, callback.from_user.id)
        return
    entry = employers[idx]
    name = str(entry.get("name") if isinstance(entry, dict) else entry)
    await asyncio.to_thread(remove_blocked_employer, callback.from_user.id, name)
    await callback.answer(f"Удалено: {name[:40]}")
    await _render_traps(callback.message, callback.from_user.id)


@router.message(Command("block_employer"))
async def block_employer_command(message: Message) -> None:
    """Blocks the employer of the last vacancy shown to the owner -- the
    "from a vacancy card the owner already saw" path. Reads the most recent
    entry from config.STORE (candidate/storage.py, read-only here) rather
    than adding a button to the preview card, since mvp/keyboards.py and
    mvp/routers/vacancies.py are out of scope for this change.
    """
    user_id = message.from_user.id
    recent = await asyncio.to_thread(config.STORE.list_recent, user_id, 1)
    if not recent:
        await message.answer("Нет недавно показанных вакансий, чтобы взять из них компанию.")
        return
    vacancy = recent[0].get("vacancy") or {}
    name = _employer_name(vacancy)
    employer_id = _employer_id(vacancy)
    if not name:
        await message.answer("У последней показанной вакансии не указан работодатель.")
        return
    await asyncio.to_thread(add_blocked_employer, user_id, name, employer_id=employer_id)
    await message.answer(f"✅ Компания из последней показанной вакансии заблокирована: «{html.escape(name)}»")


# ── letter style (mvp:cfg:letter, mvp:cfg:l:*) ──────────────────────────


async def _render_letter(message: Message, user_id: int) -> None:
    style = store.get_letter_style(user_id)
    current_key = _tone_key(style["tone"])
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=("✅ " if key == current_key else "") + label,
            callback_data=f"mvp:cfg:l:tone:{key}",
        )]
        for key, label in TONE_LABELS.items()
    ]
    rows.append([InlineKeyboardButton(text="➕ Личное стоп-слово", callback_data="mvp:cfg:l:addword")])
    for i, word in enumerate(style["denylist"]):
        rows.append([InlineKeyboardButton(text=f"❌ {word[:28]}", callback_data=f"mvp:cfg:l:rmword:{i}")])
    denylist_lines = "\n".join(f"• {html.escape(w)}" for w in style["denylist"]) or "нет"
    await send_long_message(
        message,
        "✍️ <b>Стиль писем</b>\n\n"
        f"Текущий тон: <b>{html.escape(style['tone'])}</b>\n\n"
        f"Личный стоп-лист (слова, которых не должно быть в письме под вашим именем):\n{denylist_lines}\n\n"
        "Общий денай-лист тона (cover_letter_voice.md) хранится отдельно и не меняется отсюда.",
        parse_mode="HTML",
        reply_markup=_back_keyboard(rows),
    )


@router.callback_query(F.data == "mvp:cfg:letter")
async def letter_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await _render_letter(callback.message, callback.from_user.id)


@router.callback_query(F.data.startswith("mvp:cfg:l:tone:"))
async def letter_tone_set(callback: CallbackQuery) -> None:
    key = callback.data.rsplit(":", 1)[-1]
    tone = TONE_KEYS.get(key)
    if not tone:
        await callback.answer("Неизвестный тон", show_alert=True)
        return
    await asyncio.to_thread(store.set_letter_tone, callback.from_user.id, tone)
    await callback.answer(f"Тон установлен: {tone}")
    await _render_letter(callback.message, callback.from_user.id)


@router.callback_query(F.data == "mvp:cfg:l:addword")
async def letter_add_word_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsInput.letter_add_word)
    await callback.message.answer(
        "Пришлите слово или фразу, которых никогда не должно быть в письме под вашим именем.",
        reply_markup=_cancel_keyboard(),
    )


@router.message(SettingsInput.letter_add_word, F.text)
async def letter_add_word_save(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    await state.clear()
    if not text or len(text) > 100:
        await message.answer("⚠️ Нужно непустое слово/фраза до 100 символов.")
        return
    await asyncio.to_thread(store.add_denylist_word, message.from_user.id, text)
    await message.answer(f"✅ Добавлено в личный стоп-лист: «{html.escape(text)}»")
    await _render_letter(message, message.from_user.id)


@router.callback_query(F.data.startswith("mvp:cfg:l:rmword:"))
async def letter_remove_word(callback: CallbackQuery) -> None:
    style = store.get_letter_style(callback.from_user.id)
    words = style["denylist"]
    try:
        idx = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        idx = -1
    if not (0 <= idx < len(words)):
        await callback.answer("Список уже изменился, открываю заново", show_alert=True)
        await _render_letter(callback.message, callback.from_user.id)
        return
    removed = words[idx]
    await asyncio.to_thread(store.remove_denylist_word, callback.from_user.id, removed)
    await callback.answer(f"Удалено: {removed[:40]}")
    await _render_letter(callback.message, callback.from_user.id)


# ── CRM (mvp:cfg:crm, mvp:cfg:c:*) ──────────────────────────────────────


async def _render_crm(message: Message, user_id: int) -> None:
    crm = store.get_crm_settings(user_id)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="✏️ Порог тишины", callback_data="mvp:cfg:c:silence")],
        [InlineKeyboardButton(
            text="⏸ Выключить дайджест" if crm["digest_enabled"] else "▶️ Включить дайджест",
            callback_data="mvp:cfg:c:digest_toggle",
        )],
        [InlineKeyboardButton(text="✏️ Время дайджеста", callback_data="mvp:cfg:c:digest_time")],
        [InlineKeyboardButton(text="✏️ Автоархив", callback_data="mvp:cfg:c:archive")],
    ]
    await message.answer(
        "📇 <b>CRM</b>\n\n"
        f"Порог тишины работодателя: {crm['silence_days']} дн. "
        f"(допустимо {config.CRM_SILENCE_DAYS_MIN}–{config.CRM_SILENCE_DAYS_MAX}) — "
        "после стольких дней без ответа диалог считается зависшим вопросом.\n\n"
        f"Утренний дайджест: {'включён' if crm['digest_enabled'] else 'выключен'}, "
        f"время {crm['digest_time']} ({config.CRM_DIGEST_TIMEZONE}).\n\n"
        f"Автоархив неактивных диалогов: {crm['archive_after_days']} дн. "
        f"(допустимо {config.CRM_ARCHIVE_AFTER_DAYS_MIN}–{config.CRM_ARCHIVE_AFTER_DAYS_MAX}) — "
        "диалоги без движения дольше этого срока уходят в архив.",
        parse_mode="HTML",
        reply_markup=_back_keyboard(rows),
    )


@router.callback_query(F.data == "mvp:cfg:crm")
async def crm_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await _render_crm(callback.message, callback.from_user.id)


@router.callback_query(F.data == "mvp:cfg:c:silence")
async def crm_silence_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsInput.crm_silence_days)
    await callback.message.answer(
        "Пришлите новый порог тишины работодателя, дней "
        f"({config.CRM_SILENCE_DAYS_MIN}–{config.CRM_SILENCE_DAYS_MAX}).",
        reply_markup=_cancel_keyboard(),
    )


@router.message(SettingsInput.crm_silence_days, F.text)
async def crm_silence_save(message: Message, state: FSMContext) -> None:
    await state.clear()
    value = _parse_int(message.text)
    if value is None or not (config.CRM_SILENCE_DAYS_MIN <= value <= config.CRM_SILENCE_DAYS_MAX):
        await message.answer(
            f"⚠️ Нужно целое число от {config.CRM_SILENCE_DAYS_MIN} до {config.CRM_SILENCE_DAYS_MAX}. "
            "Значение не изменено."
        )
        return
    await asyncio.to_thread(store.set_crm_silence_days, message.from_user.id, value)
    await message.answer(f"✅ Порог тишины установлен: {value} дн.")
    await _render_crm(message, message.from_user.id)


@router.callback_query(F.data == "mvp:cfg:c:digest_toggle")
async def crm_digest_toggle(callback: CallbackQuery) -> None:
    new_value = not store.get_crm_digest_enabled(callback.from_user.id)
    await asyncio.to_thread(store.set_crm_digest_enabled, callback.from_user.id, new_value)
    await callback.answer("Дайджест включён" if new_value else "Дайджест выключен")
    await _render_crm(callback.message, callback.from_user.id)


@router.callback_query(F.data == "mvp:cfg:c:digest_time")
async def crm_digest_time_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsInput.crm_digest_time)
    await callback.message.answer(
        "Пришлите время утреннего дайджеста в формате ЧЧ:ММ (24-часовой формат, "
        f"{config.CRM_DIGEST_TIMEZONE}), например 09:00.",
        reply_markup=_cancel_keyboard(),
    )


@router.message(SettingsInput.crm_digest_time, F.text)
async def crm_digest_time_save(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = (message.text or "").strip()
    if not store.is_valid_digest_time(text):
        await message.answer(
            "⚠️ Нужно время в формате ЧЧ:ММ, 24-часовой формат (например 09:00). Значение не изменено."
        )
        return
    await asyncio.to_thread(store.set_crm_digest_time, message.from_user.id, text)
    await message.answer(f"✅ Время дайджеста установлено: {text} ({config.CRM_DIGEST_TIMEZONE})")
    await _render_crm(message, message.from_user.id)


@router.callback_query(F.data == "mvp:cfg:c:archive")
async def crm_archive_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SettingsInput.crm_archive_days)
    await callback.message.answer(
        "Пришлите новое окно автоархива, дней "
        f"({config.CRM_ARCHIVE_AFTER_DAYS_MIN}–{config.CRM_ARCHIVE_AFTER_DAYS_MAX}).",
        reply_markup=_cancel_keyboard(),
    )


@router.message(SettingsInput.crm_archive_days, F.text)
async def crm_archive_save(message: Message, state: FSMContext) -> None:
    await state.clear()
    value = _parse_int(message.text)
    if value is None or not (config.CRM_ARCHIVE_AFTER_DAYS_MIN <= value <= config.CRM_ARCHIVE_AFTER_DAYS_MAX):
        await message.answer(
            f"⚠️ Нужно целое число от {config.CRM_ARCHIVE_AFTER_DAYS_MIN} до "
            f"{config.CRM_ARCHIVE_AFTER_DAYS_MAX}. Значение не изменено."
        )
        return
    await asyncio.to_thread(store.set_crm_archive_after_days, message.from_user.id, value)
    await message.answer(f"✅ Автоархив установлен: {value} дн.")
    await _render_crm(message, message.from_user.id)


# ── shared navigation for the new screens ───────────────────────────────


@router.callback_query(F.data == "mvp:cfg:back")
async def cfg_back(callback: CallbackQuery) -> None:
    await callback.answer()
    await settings(callback)


@router.callback_query(F.data == "mvp:cfg:cancel")
async def cfg_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.answer("Ввод отменён, ничего не изменено.")
