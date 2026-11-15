"""CRM screen: employer conversations grouped by the candidate's next step.

Replaces `mvp/routers/inbox.py` ("Входящие"). Where inbox.py merged
`negotiation_watch` + `auto_apply_log` and rendered every thread LIVE from
hh.ru, this screen reads `crm_thread_state` / `negotiation_messages`
(populated by `candidate/automation.py`'s `AutomationStore.crm_*` methods) —
local SQLite, so the list and thread view open instantly. The only place
this router calls hh.ru is the explicit "🔄 Обновить с hh.ru" button on the
thread view.

Callback-data namespace claimed in this file:
``mvp:crm`` (main screen), ``mvp:crm:thread:<id>`` (open a thread),
``mvp:crm:reply:<id>`` (start a reply), ``mvp:crm:reply_cancel``,
``mvp:crm:sync:<id>`` (pull fresh messages from hh.ru),
``mvp:crm:earlier:<id>`` (load the full thread, not just the recent tail),
``mvp:crm:thread_archive:<id>`` / ``mvp:crm:unarchive:<id>``,
``mvp:crm:archive`` (archived list), ``mvp:crm:more:<group>:<offset>``
(per-group pagination). Stage marking reuses the existing
``mvp:outcome:<outcome>:<variant_id>`` mechanism from `mvp/routers/history.py`
(via `mvp.keyboards._outcome_keyboard`) — no second outcome mechanism here.

LEGACY SHIM: cards already sitting in the owner's Telegram chat before this
router replaced inbox.py carry the old ``mvp:inbox`` / ``inbox:open:`` /
``inbox:reply:`` / ``inbox:reply_cancel`` callback data. Handlers near the
bottom of this file redirect those to the equivalent CRM view so those
buttons don't go dead.
"""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from candidate.automation import crm_bucket
from candidate.hh_official import HHOfficialError

from mvp import config
from mvp.automation import _fetch_negotiation_messages, _format_message_date, _hh_client_for, _hh_thread_url
from mvp.formatting import send_long_message
from mvp.keyboards import _outcome_keyboard
from mvp.settings_store import get_crm_silence_days
from mvp.states import CrmReply

log = logging.getLogger(__name__)

router = Router()

_GROUP_LIMIT = 10
_THREAD_MESSAGE_LIMIT = 20

_TITLE_REPLY = "⚡ Нужно ответить"
_TITLE_TASK = "📋 Нужно выполнить"
_TITLE_SCHEDULED = "📅 Запланировано"
_TITLE_WAITING = "⏳ Ждём работодателя"
_TITLE_REJECTED = "⛔ Отказы"
_GROUP_TITLES = {
    "reply": _TITLE_REPLY, "task": _TITLE_TASK, "scheduled": _TITLE_SCHEDULED,
    "waiting": _TITLE_WAITING, "rejected": _TITLE_REJECTED, "archive": "🗄 Архив",
}
_LEGACY_GROUPS = {"me": "reply", "employer": "waiting", "new": "reply", "quiet": "waiting"}


def _clean_line(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip(" ,;:") + "…"


def _group_threads(threads: list[dict]) -> dict[str, list[dict]]:
    groups = {key: [] for key in ("reply", "task", "scheduled", "waiting", "rejected")}
    for thread in threads:
        groups[crm_bucket(thread)].append(thread)
    for key in ("reply", "task", "waiting"):
        groups[key].sort(key=lambda t: t.get("days_silent") or 0, reverse=True)
    for key in ("scheduled", "rejected"):
        groups[key].sort(key=lambda t: t.get("updated_at") or "", reverse=True)
    return groups


async def _fetch_group_items(user_id: int, group_key: str) -> list[dict]:
    if group_key == "archive":
        threads = await asyncio.to_thread(config.AUTOMATION.crm_threads, user_id, archived_only=True)
        threads.sort(key=lambda t: t.get("archived_at") or "", reverse=True)
        return threads
    group_key = _LEGACY_GROUPS.get(group_key, group_key)
    threads = await asyncio.to_thread(config.AUTOMATION.crm_threads, user_id)
    return _group_threads(threads).get(group_key, [])


def _row_line(thread: dict, group_key: str, silence_days: int) -> str:
    employer = html.escape(thread.get("employer_name") or "Работодатель не указан")
    vacancy = html.escape(thread.get("vacancy_name") or "Вакансия")
    days = thread.get("days_silent") or 0
    preview = html.escape(_clean_line(thread.get("last_message_preview") or "", 90)) or "(нет сообщений)"
    marker = "🔴 " if group_key == "waiting" and silence_days and days >= silence_days else ""
    return f"{marker}<b>{employer}</b> — {vacancy} · {days} дн.\n{preview}"


def _thread_button_label(thread: dict) -> str:
    employer = (thread.get("employer_name") or "Работодатель")[:20]
    vacancy = (thread.get("vacancy_name") or "Вакансия")[:24]
    days = thread.get("days_silent") or 0
    return f"{employer} · {vacancy} · {days}д"[:64]


def _header_text(counters: dict) -> str:
    return (
        "📇 <b>CRM</b>\n"
        "Здесь у каждого диалога один понятный следующий шаг.\n\n"
        f"⚡ Ответить — <b>{counters.get('reply', 0)}</b>\n"
        f"📋 Выполнить — <b>{counters.get('task', 0)}</b>\n"
        f"📅 Запланировано — <b>{counters.get('scheduled', 0)}</b>\n"
        f"⏳ Ждём — <b>{counters.get('waiting', 0)}</b>"
        f" · из них {counters.get('silent', 0)} дольше срока\n"
        f"⛔ Отказы — <b>{counters.get('rejected', 0)}</b>\n"
        f"🗄 Архив — <b>{counters.get('archived', 0)}</b>\n\n"
    )


def _group_block(
    title: str, items: list[dict], group_key: str, silence_days: int,
) -> tuple[str, list[list[InlineKeyboardButton]]]:
    if not items:
        return "", []
    shown = items[:_GROUP_LIMIT]
    lines = [f"<b>{title}</b>"]
    buttons: list[list[InlineKeyboardButton]] = []
    for t in shown:
        lines.append(_row_line(t, group_key, silence_days))
        buttons.append([InlineKeyboardButton(
            text=_thread_button_label(t), callback_data=f"mvp:crm:thread:{t['negotiation_id']}",
        )])
    if len(items) > _GROUP_LIMIT:
        buttons.append([InlineKeyboardButton(
            text=f"Показать ещё ({len(items) - _GROUP_LIMIT})",
            callback_data=f"mvp:crm:more:{group_key}:{_GROUP_LIMIT}",
        )])
    return "\n".join(lines), buttons


async def _show_list(target: Message, user_id: int) -> None:
    silence_days = await asyncio.to_thread(get_crm_silence_days, user_id)
    counters = await asyncio.to_thread(config.AUTOMATION.crm_counters, user_id, silence_days=silence_days)
    threads = await asyncio.to_thread(config.AUTOMATION.crm_threads, user_id)
    groups = _group_threads(threads)

    text = _header_text(counters)
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    rendered_any = False
    for title, key in (
        (_TITLE_REPLY, "reply"), (_TITLE_TASK, "task"),
        (_TITLE_SCHEDULED, "scheduled"),
    ):
        block, buttons = _group_block(title, groups[key], key, silence_days)
        if block:
            text += block + "\n\n"
            keyboard_rows.extend(buttons)
            rendered_any = True
    if not rendered_any:
        text += "Сейчас нет сообщений, на которые нужно ответить, незавершённых анкет или назначенных встреч."
    keyboard_rows.extend([
        [InlineKeyboardButton(
            text=f"⏳ Ждём · {counters.get('waiting', 0)}",
            callback_data="mvp:crm:more:waiting:0",
        ), InlineKeyboardButton(
            text=f"⛔ Отказы · {counters.get('rejected', 0)}",
            callback_data="mvp:crm:more:rejected:0",
        )],
        [InlineKeyboardButton(
            text=f"🗄 Архив · {counters.get('archived', 0)}", callback_data="mvp:crm:archive",
        )],
    ])
    await send_long_message(
        target, text.strip(), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


@router.callback_query(F.data == "mvp:crm")
async def crm_list(callback: CallbackQuery) -> None:
    await callback.answer()
    await _show_list(callback.message, callback.from_user.id)


@router.message(Command("crm"))
async def crm_command(message: Message) -> None:
    await _show_list(message, message.from_user.id)


@router.message(Command("inbox"))
async def crm_inbox_alias(message: Message) -> None:
    """`/inbox` kept as a muscle-memory alias for the renamed `/crm` screen."""
    await _show_list(message, message.from_user.id)


async def _send_group_page(target: Message, user_id: int, group_key: str, offset: int, silence_days: int) -> None:
    group_key = _LEGACY_GROUPS.get(group_key, group_key)
    items = await _fetch_group_items(user_id, group_key)
    chunk = items[offset:offset + _GROUP_LIMIT]
    if not chunk:
        await target.answer("Больше записей нет.")
        return
    suffix = " (продолжение)" if offset else ""
    title = _GROUP_TITLES.get(group_key, group_key)
    lines = [f"<b>{title}{suffix}</b>"]
    buttons: list[list[InlineKeyboardButton]] = []
    for t in chunk:
        lines.append(_row_line(t, group_key, silence_days))
        row = [InlineKeyboardButton(
            text=_thread_button_label(t), callback_data=f"mvp:crm:thread:{t['negotiation_id']}",
        )]
        if group_key == "archive":
            row.append(InlineKeyboardButton(
                text="↩️ Вернуть", callback_data=f"mvp:crm:unarchive:{t['negotiation_id']}",
            ))
        buttons.append(row)
    next_offset = offset + _GROUP_LIMIT
    if len(items) > next_offset:
        buttons.append([InlineKeyboardButton(
            text=f"Показать ещё ({len(items) - next_offset})",
            callback_data=f"mvp:crm:more:{group_key}:{next_offset}",
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ К CRM", callback_data="mvp:crm")])
    await send_long_message(
        target, "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data == "mvp:crm:archive")
async def crm_archive(callback: CallbackQuery) -> None:
    await callback.answer()
    await _send_group_page(callback.message, callback.from_user.id, "archive", 0, 0)


@router.callback_query(F.data.startswith("mvp:crm:more:"))
async def crm_more(callback: CallbackQuery) -> None:
    _, _, _, group_key, offset_str = callback.data.split(":", 4)
    await callback.answer()
    silence_days = await asyncio.to_thread(get_crm_silence_days, callback.from_user.id)
    await _send_group_page(callback.message, callback.from_user.id, group_key, int(offset_str), silence_days)


@router.callback_query(F.data.startswith("mvp:crm:unarchive:"))
async def crm_unarchive(callback: CallbackQuery) -> None:
    negotiation_id = callback.data.rsplit(":", 1)[-1]
    await asyncio.to_thread(config.AUTOMATION.unarchive_thread, callback.from_user.id, negotiation_id)
    await callback.answer("Диалог возвращён из архива")
    thread = await _load_thread(callback.from_user.id, negotiation_id)
    if not thread:
        await callback.message.answer("Диалог не найден.")
        return
    await _render_thread(callback.message, callback.from_user.id, thread, negotiation_id, limit=_THREAD_MESSAGE_LIMIT)


# --------------------------------------------------------------------------
# Thread view — local-only (crm_thread + crm_thread_messages), opens instantly.
# --------------------------------------------------------------------------

async def _load_thread(user_id: int, negotiation_id: str) -> dict | None:
    return await asyncio.to_thread(config.AUTOMATION.crm_thread, user_id, negotiation_id)


def _find_variant_id(user_id: int, negotiation_id: str) -> str:
    """Best-effort lookup for the outcome buttons.

    `crm_thread_state` carries no `variant_id` column, so the `mvp:outcome:*`
    buttons owned by history.py need this side lookup against
    `application_history` to find the row they act on. Threads the owner
    started by hand on hh.ru have no variant — `_thread_keyboard` just omits
    the outcome row for those.
    """
    applications = config.AUTOMATION.application_history(user_id, 200)
    match = next((a for a in applications if a.get("negotiation_id") == negotiation_id), None)
    return str(match.get("variant_id")) if match else ""


def _thread_header(thread: dict) -> str:
    vacancy = html.escape(thread.get("vacancy_name") or "Вакансия")
    employer = html.escape(thread.get("employer_name") or "Работодатель не указан")
    state_label = {
        "response": "Отклик отправлен", "invitation": "Приглашение", "discard": "Отказ",
        "hidden": "Скрыто на hh.ru",
    }.get(str(thread.get("hh_state") or ""), "Статус не определён")
    section_label = _GROUP_TITLES.get(crm_bucket(thread), "CRM")
    header = f"💼 <b>{vacancy}</b>\n🏢 {employer}\n📌 {state_label}\n{section_label}"
    if thread.get("archived_at"):
        header += "\n🗄 В архиве"
    return header + "\n\n"


def _message_block(message: dict) -> str:
    is_employer = message.get("direction") != "outgoing"
    author = message.get("author_name") or ("Рекрутёр" if is_employer else "Вы")
    date_text = _format_message_date(str(message.get("created_at") or ""))
    text = str(message.get("text") or "").strip()
    prefix = "👔" if is_employer else "🙋"
    body = html.escape(text) if text else "(пусто)"
    return f"{prefix} <b>{html.escape(str(author))}, {html.escape(date_text)}:</b>\n{body}"


def _thread_text(thread: dict, messages: list[dict], *, truncated: bool) -> str:
    header = _thread_header(thread)
    if not messages:
        return header + "Сообщений пока нет — «🔄 Обновить с hh.ru», чтобы подтянуть переписку."
    body = "\n\n".join(_message_block(m) for m in messages)
    if truncated:
        body = "<i>…есть более ранние сообщения, нажмите «Показать более ранние»…</i>\n\n" + body
    return header + body


def _thread_keyboard(
    thread: dict, negotiation_id: str, *, has_earlier: bool, variant_id: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_earlier:
        rows.append([InlineKeyboardButton(
            text="⬅️ Показать более ранние", callback_data=f"mvp:crm:earlier:{negotiation_id}",
        )])
    if crm_bucket(thread) == "reply":
        rows.append([InlineKeyboardButton(text="✍️ Ответить", callback_data=f"mvp:crm:reply:{negotiation_id}")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить с hh.ru", callback_data=f"mvp:crm:sync:{negotiation_id}")])
    if thread.get("archived_at"):
        rows.append([InlineKeyboardButton(
            text="↩️ Вернуть из архива", callback_data=f"mvp:crm:unarchive:{negotiation_id}",
        )])
    else:
        rows.append([InlineKeyboardButton(
            text="🗄 В архив", callback_data=f"mvp:crm:thread_archive:{negotiation_id}",
        )])
    rows.append([InlineKeyboardButton(text="🔗 Открыть на hh.ru", url=_hh_thread_url(chat_id=str(thread.get("chat_id") or "")))])
    if variant_id:
        rows.extend(_outcome_keyboard(variant_id).inline_keyboard)
    rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data="mvp:crm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_thread(
    target: Message, user_id: int, thread: dict, negotiation_id: str, *, limit: int | None,
) -> None:
    all_messages = await asyncio.to_thread(config.AUTOMATION.crm_thread_messages, user_id, negotiation_id)
    if limit is not None and len(all_messages) > limit:
        messages = all_messages[-limit:]
        truncated = True
    else:
        messages = all_messages
        truncated = False
    text = _thread_text(thread, messages, truncated=truncated)
    variant_id = await asyncio.to_thread(_find_variant_id, user_id, negotiation_id)
    keyboard = _thread_keyboard(thread, negotiation_id, has_earlier=truncated, variant_id=variant_id)
    await send_long_message(target, text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("mvp:crm:thread:"))
async def crm_thread_view(callback: CallbackQuery) -> None:
    negotiation_id = callback.data.rsplit(":", 1)[-1]
    thread = await _load_thread(callback.from_user.id, negotiation_id)
    if not thread:
        await callback.answer("Диалог не найден", show_alert=True)
        return
    await callback.answer()
    await _render_thread(callback.message, callback.from_user.id, thread, negotiation_id, limit=_THREAD_MESSAGE_LIMIT)


@router.callback_query(F.data.startswith("mvp:crm:earlier:"))
async def crm_thread_earlier(callback: CallbackQuery) -> None:
    negotiation_id = callback.data.rsplit(":", 1)[-1]
    thread = await _load_thread(callback.from_user.id, negotiation_id)
    if not thread:
        await callback.answer("Диалог не найден", show_alert=True)
        return
    await callback.answer()
    await _render_thread(callback.message, callback.from_user.id, thread, negotiation_id, limit=None)


@router.callback_query(F.data.startswith("mvp:crm:thread_archive:"))
async def crm_thread_archive(callback: CallbackQuery) -> None:
    negotiation_id = callback.data.rsplit(":", 1)[-1]
    await asyncio.to_thread(config.AUTOMATION.archive_thread, callback.from_user.id, negotiation_id, "manual")
    await callback.answer("Диалог перенесён в архив")
    thread = await _load_thread(callback.from_user.id, negotiation_id)
    if thread:
        await _render_thread(callback.message, callback.from_user.id, thread, negotiation_id, limit=_THREAD_MESSAGE_LIMIT)


@router.callback_query(F.data.startswith("mvp:crm:sync:"))
async def crm_thread_sync(callback: CallbackQuery) -> None:
    """The ONLY handler in this router that calls hh.ru."""
    negotiation_id = callback.data.rsplit(":", 1)[-1]
    user_id = callback.from_user.id
    thread = await _load_thread(user_id, negotiation_id)
    if not thread:
        await callback.answer("Диалог не найден", show_alert=True)
        return
    await callback.answer("Обновляю с hh.ru…")
    try:
        client, _account = await _hh_client_for(user_id)
    except HHOfficialError as exc:
        log.warning("HH token refresh failed safely: %s", exc.reason)
        client = None
    if client is None:
        await callback.message.answer("HH не подключён или токен недействителен — обновить не удалось.")
        return
    try:
        messages = await _fetch_negotiation_messages(client, negotiation_id)
    except HHOfficialError as exc:
        log.warning("HH message fetch failed safely: %s", exc.reason)
        await callback.message.answer(
            f"❌ hh.ru не ответил: <code>{html.escape(exc.reason)}</code>", parse_mode="HTML",
        )
        return
    await asyncio.to_thread(config.AUTOMATION.store_negotiation_messages, user_id, negotiation_id, messages)
    await asyncio.to_thread(
        config.AUTOMATION.recompute_thread_state, user_id, negotiation_id,
        vacancy_id=thread.get("vacancy_id"), vacancy_name=thread.get("vacancy_name"),
        employer_name=thread.get("employer_name"), hh_state=thread.get("hh_state"),
    )
    refreshed = await _load_thread(user_id, negotiation_id)
    await _render_thread(callback.message, user_id, refreshed or thread, negotiation_id, limit=_THREAD_MESSAGE_LIMIT)


# --------------------------------------------------------------------------
# Reply flow — ported from inbox.py's InboxReply, same send path.
# --------------------------------------------------------------------------

@router.callback_query(F.data.startswith("mvp:crm:reply:"))
async def crm_start_reply(callback: CallbackQuery, state: FSMContext) -> None:
    negotiation_id = callback.data.rsplit(":", 1)[-1]
    thread = await _load_thread(callback.from_user.id, negotiation_id)
    if not thread:
        await callback.answer("Диалог не найден", show_alert=True)
        return
    await state.set_state(CrmReply.text)
    await state.update_data(crm_reply_negotiation_id=negotiation_id)
    await callback.answer()
    vacancy_name = html.escape(thread.get("vacancy_name") or "вакансия")
    await callback.message.answer(
        f"✍️ Пришлите текст ответа рекрутёру по вакансии «{vacancy_name}» одним сообщением. "
        "После отправки он сразу уйдёт в hh.ru от вашего имени.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Отмена", callback_data="mvp:crm:reply_cancel"),
        ]]),
    )


@router.callback_query(F.data == "mvp:crm:reply_cancel")
async def crm_cancel_reply(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Ответ отменён")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.message(CrmReply.text, F.text)
async def crm_send_reply(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустое сообщение не отправлено. Пришлите текст ответа ещё раз.")
        return
    state_data = await state.get_data()
    negotiation_id = str(state_data.get("crm_reply_negotiation_id") or "")
    if not negotiation_id:
        await state.clear()
        await message.answer("Диалог потерян — откройте CRM заново.")
        return
    try:
        client, _account = await _hh_client_for(message.from_user.id)
    except HHOfficialError as exc:
        log.warning("HH token refresh failed safely: %s", exc.reason)
        client = None
    if client is None:
        # State is deliberately NOT cleared: the drafted text stays visible above
        # this message and the owner can resend without pressing "Ответить" again.
        await message.answer(
            "HH не подключён или токен недействителен — сообщение НЕ отправлено. "
            "Переподключите HH и пришлите текст ещё раз, или нажмите «Отмена»."
        )
        return
    try:
        await asyncio.to_thread(
            client._request, "POST",
            f"https://api.hh.ru/negotiations/{negotiation_id}/messages",
            {"message": text},
        )
    except HHOfficialError as exc:
        log.warning("HH reply send failed safely: %s", exc.reason)
        await message.answer(
            f"❌ hh.ru не подтвердил отправку: <code>{html.escape(exc.reason)}</code>. "
            "Сообщение НЕ отправлено, черновик не потерян — пришлите текст ещё раз или нажмите «Отмена».",
            parse_mode="HTML",
        )
        return
    except Exception:
        log.exception("Unexpected error sending HH reply")
        await message.answer(
            "❌ Не удалось отправить сообщение из-за непредвиденной ошибки. "
            "Сообщение НЕ отправлено, черновик не потерян — пришлите текст ещё раз или нажмите «Отмена»."
        )
        return
    await state.clear()
    await asyncio.to_thread(config.AUTOMATION.record_outgoing_message, message.from_user.id, negotiation_id, text)
    await asyncio.to_thread(config.AUTOMATION.recompute_thread_state, message.from_user.id, negotiation_id)
    await message.answer("✅ hh.ru подтвердил: сообщение отправлено рекрутёру.")
    thread = await _load_thread(message.from_user.id, negotiation_id)
    if thread:
        await _render_thread(message, message.from_user.id, thread, negotiation_id, limit=_THREAD_MESSAGE_LIMIT)


# --------------------------------------------------------------------------
# LEGACY shim — old "Входящие" callback data from cards already sent to the
# owner's chat before this router replaced mvp/routers/inbox.py. Each handler
# just redirects into the equivalent CRM handler above; nothing new here.
# --------------------------------------------------------------------------

@router.callback_query(F.data == "mvp:inbox")
async def legacy_inbox_list(callback: CallbackQuery) -> None:
    await crm_list(callback)


@router.callback_query(F.data.startswith("inbox:open:"))
async def legacy_inbox_open(callback: CallbackQuery) -> None:
    await crm_thread_view(callback)


@router.callback_query(F.data.startswith("inbox:reply:"))
async def legacy_inbox_reply(callback: CallbackQuery, state: FSMContext) -> None:
    await crm_start_reply(callback, state)


@router.callback_query(F.data == "inbox:reply_cancel")
async def legacy_inbox_reply_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await crm_cancel_reply(callback, state)
