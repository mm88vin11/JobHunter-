"""Inline keyboard builders for the candidate MVP bot."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from candidate.data import load_candidate_data

from mvp import config


def _main_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # Only when the Mini App is actually reachable. Telegram renders a web_app
    # button unconditionally and only fails on press, so a button built from a
    # missing URL would look available and break in the owner's hands.
    webapp_url = config.webapp_public_url()
    if webapp_url:
        rows.append([InlineKeyboardButton(
            text="🗂 Открыть приложение", web_app=WebAppInfo(url=webapp_url),
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows + [
        [InlineKeyboardButton(text="📇 CRM", callback_data="mvp:crm"), InlineKeyboardButton(text="📥 Очередь", callback_data="mvp:queue")],
        [InlineKeyboardButton(text="📄 Резюме", callback_data="mvp:resumes"), InlineKeyboardButton(text="➕ Добавить вакансию", callback_data="mvp:add")],
        [InlineKeyboardButton(text="🤖 Автопоиск и автоотклик", callback_data="mvp:search")],
        [InlineKeyboardButton(text="📊 Воронка", callback_data="mvp:stats"), InlineKeyboardButton(text="🕘 История", callback_data="mvp:history")],
        [InlineKeyboardButton(text="🚫 Отфильтрованные", callback_data="mvp:filtered:0")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="mvp:knowledge"), InlineKeyboardButton(text="🧾 Факты", callback_data="mvp:facts")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="mvp:settings")],
        [InlineKeyboardButton(text="🧪 Демо полного flow", callback_data="mvp:demo")],
    ])


def _preview_keyboard(
    variant_id: str, vacancy_url: str, payload: dict | None = None,
) -> InlineKeyboardMarkup:
    # A letter assembled by the deterministic draft composer, or one that failed
    # the pre-send guard, cannot be approved — the card sits in `manual_required`
    # and `mvp:approve` would answer with a meaningless "решение уже обработано".
    # Offer the only action that actually works instead of a dead button.
    needs_manual = bool(
        (payload or {}).get("letter_review", {}).get("requires_manual_letter")
    )
    if needs_manual:
        rows = [
            [InlineKeyboardButton(text="✍️ Написать письмо", callback_data=f"mvp:letter_manual:{variant_id}")],
            [InlineKeyboardButton(text="✨ Сгенерировать заново", callback_data=f"mvp:letter:{variant_id}")],
        ]
    else:
        rows = [
            [InlineKeyboardButton(text="🚀 Проверить и откликнуться", callback_data=f"mvp:apply:{variant_id}")],
            [InlineKeyboardButton(text="✍️ Написать письмо", callback_data=f"mvp:letter_manual:{variant_id}"), InlineKeyboardButton(text="✨ Новый вариант", callback_data=f"mvp:letter:{variant_id}")],
        ]
    rows += [
        [InlineKeyboardButton(text="🔄 Сменить профиль", callback_data=f"mvp:change:{variant_id}")],
        [InlineKeyboardButton(text="📎 Файлы (резюме, письмо, вакансия)", callback_data=f"mvp:files:{variant_id}")],
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"mvp:skip:{variant_id}")],
    ]
    if vacancy_url:
        rows.append([InlineKeyboardButton(text="🔗 Открыть источник", url=vacancy_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _outcome_keyboard(variant_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💬 Ответ", callback_data=f"mvp:outcome:replied:{variant_id}"),
            InlineKeyboardButton(text="📅 Интервью", callback_data=f"mvp:outcome:interview:{variant_id}"),
        ],
        [
            InlineKeyboardButton(text="🎉 Оффер", callback_data=f"mvp:outcome:offer:{variant_id}"),
            InlineKeyboardButton(text="⛔ Отказ", callback_data=f"mvp:outcome:rejected:{variant_id}"),
        ],
        [InlineKeyboardButton(text="⌛ Нет ответа", callback_data=f"mvp:outcome:no_response:{variant_id}")],
    ])


def _profile_keyboard(variant_id: str) -> InlineKeyboardMarkup:
    data = load_candidate_data()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=p.desired_title, callback_data=f"mvp:profile:{key}:{variant_id}")]
        for key, p in data.profiles.items()
    ])


def _application_history_keyboard(user_id: int) -> InlineKeyboardMarkup | None:
    applications = config.AUTOMATION.application_history(user_id, 10)
    if not applications:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📌 {(item['vacancy'].get('name') or 'Вакансия')[:42]}",
            callback_data=f"mvp:application:{item['variant_id']}",
        )]
        for item in applications
    ])
