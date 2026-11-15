"""«Отфильтрованные» — the rejection bin.

The owner's decision (2026-08-14): the pipeline keeps cutting hard — a
trap-screened, hard-rejected or below-threshold vacancy must never appear in
the main deck — but the reason must not be lost. `mvp/automation.py` writes
every cut to `AutomationStore.record_filtered` (table `filtered_vacancies`,
see `candidate/automation.py`); this router is the only way to read it back.

This is a **different** surface from `mvp/routers/cards.py`'s existing
`"filtered"` bucket (title also "🧹 Отфильтрованные", reached via «📥
Очередь»): that bucket only ever showed vacancies that made it as far as a
`candidate_variants` row with `status="skipped"` — i.e. gate 3
(below-threshold) alone. Gates 1 and 2 (trap screening, ranker hard-reject)
`continue` before any payload/variant is ever built, so they have no row to
show there at all and were previously invisible everywhere. This screen
covers all three gates from one small dedicated table, as a concise list
(title / employer / score / reason) rather than full preview cards — cards.py
owns the full-preview rendering and is off-limits here by design (a parallel
change is in flight there).

Callback-data prefixes claimed here: ``mvp:filtered:`` (page N),
``mvp:filtered_noop``, ``mvp:filtered_menu``.
"""
from __future__ import annotations

import asyncio
import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from mvp import config
from mvp.formatting import split_for_telegram
from mvp.keyboards import _main_keyboard

router = Router()

#: Rows per page. Kept small — each row is a few lines of text, and the
#: header (24h counters) already gives the aggregate view.
_PAGE_SIZE = 8

#: Russian labels for the `gate` column, for the list and the counter header.
_GATE_LABELS: dict[str, str] = {
    "trap": "🪤 trap-скрининг",
    "hard_reject": "⛔ жёсткий отказ",
    "below_threshold": "📉 ниже порога",
    "hard_tech": "⚙️ классический tech-гейт",
}

_TEXT_LIMIT = 3600


def _page_bounds(total: int, page: int, page_size: int = _PAGE_SIZE) -> tuple[int, int, int]:
    """Clamp `page` into `[0, last_page]`. Returns `(page, offset, last_page)`.

    A pure function on purpose — the only piece of this screen worth a unit
    test without standing up a fake aiogram callback.
    """
    if total <= 0:
        return 0, 0, 0
    last_page = (total - 1) // page_size
    page = max(0, min(page, last_page))
    return page, page * page_size, last_page


def _gate_label(gate: str) -> str:
    return _GATE_LABELS.get(gate, gate)


def _format_row(position: int, row: dict) -> str:
    title = html.escape(row["vacancy_name"] or "Вакансия")
    source_url = row.get("source_url") or ""
    if source_url:
        title = f'<a href="{html.escape(source_url, quote=True)}">{title}</a>'
    employer = html.escape(row["employer_name"] or "—")
    area = html.escape(row["area"] or "")
    location = f" · {area}" if area else ""
    reason = html.escape(row["reason"] or "—")
    when = str(row["created_at"])[:16].replace("T", " ")
    return (
        f"<b>{position}. {title}</b>\n"
        f"🏢 {employer}{location} · 🎯 {int(row['score'])}% · {_gate_label(row['gate'])}\n"
        f"💬 {reason}\n"
        f"🕘 {when}"
    )


def _counter_text(counters: dict) -> str:
    lines = [f"🚫 <b>Отфильтровано за 24 часа: {counters['total']}</b>"]
    if counters["top_reasons"]:
        lines.append("Топ причин:")
        for reason, count in counters["top_reasons"]:
            lines.append(f"• {html.escape(reason)} — {count}")
    return "\n".join(lines)


def _fit(text: str) -> str:
    chunks = split_for_telegram(text, _TEXT_LIMIT)
    if len(chunks) <= 1:
        return text
    return chunks[0] + "\n\n… список обрезан, откройте следующую страницу."


def _keyboard(page: int, last_page: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if last_page > 0:
        rows.append([
            InlineKeyboardButton(text="⬅️", callback_data=f"mvp:filtered:{max(0, page - 1)}"),
            InlineKeyboardButton(text=f"{page + 1}/{last_page + 1}", callback_data="mvp:filtered_noop"),
            InlineKeyboardButton(text="➡️", callback_data=f"mvp:filtered:{min(last_page, page + 1)}"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="mvp:filtered_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render(target: Message, user_id: int, page: int, *, edit: bool) -> None:
    total = await asyncio.to_thread(config.AUTOMATION.filtered_count, user_id)
    counters = await asyncio.to_thread(config.AUTOMATION.filtered_counters, user_id, hours=24, top=3)
    header = _counter_text(counters)
    if total == 0:
        text = f"{header}\n\n<b>🚫 Отфильтрованные</b>\n\nПока пусто — ничего не отсеяно."
        await _deliver(target, text, _keyboard(0, 0), edit=edit)
        return
    page, offset, last_page = _page_bounds(total, page)
    rows = await asyncio.to_thread(config.AUTOMATION.filtered_vacancies, user_id, limit=_PAGE_SIZE, offset=offset)
    body = "\n\n".join(_format_row(offset + index + 1, row) for index, row in enumerate(rows))
    text = (
        f"{header}\n\n<b>🚫 Отфильтрованные</b> · стр. {page + 1} из {last_page + 1} "
        f"(всего {total})\n\n{body}"
    )
    await _deliver(target, _fit(text), _keyboard(page, last_page), edit=edit)


async def _deliver(target: Message, text: str, keyboard: InlineKeyboardMarkup, *, edit: bool) -> None:
    if edit:
        try:
            await target.edit_text(text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
            return
        except Exception:
            pass  # not-modified, too old to edit, or the source was a document
    await target.answer(text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)


@router.callback_query(F.data == "mvp:filtered_noop")
async def filtered_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "mvp:filtered_menu")
async def filtered_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer("Главное меню:", reply_markup=_main_keyboard())


@router.callback_query(F.data.startswith("mvp:filtered:"))
async def open_filtered(callback: CallbackQuery) -> None:
    raw_page = callback.data.rsplit(":", 1)[-1]
    page = int(raw_page) if raw_page.lstrip("-").isdigit() else 0
    await callback.answer()
    await _render(callback.message, callback.from_user.id, max(0, page), edit=True)


@router.message(Command("filtered"))
async def filtered_command(message: Message) -> None:
    await _render(message, message.from_user.id, 0, edit=False)
