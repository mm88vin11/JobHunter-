"""Card browser over the variant journal.

Every vacancy the bot prepares used to exist only as a push message in the
chat log: scroll past it and it was gone, because the "Очередь" screen was a
flat text list with no buttons. This router turns the journal into a browsable
deck — one card at a time, «7 / 33», arrows, and a decision that slides the
next card into place.

Two properties are load-bearing:

- **The resume shown on a card is read from hh.ru, not from the stored
  payload.** A variant generated yesterday carries yesterday's resume
  snapshot; the owner edits resumes between sessions, so a card that renders
  the snapshot can promise hh.ru a document that no longer exists. Every card
  render calls :func:`mvp.hh_sync.live_resume`, and when hh.ru reports a newer
  ``updated_at`` than the payload's snapshot the package is rebuilt from the
  live resume and re-saved before anything is shown.
- **Rebuilding invalidates approval.** ``_payload_digest`` covers
  ``hh_resume_snapshot``, so a rebuilt package no longer matches the approval
  row and :func:`mvp.automation._try_auto_apply` refuses to send it. That is
  the intended outcome, and the card says so instead of silently downgrading.

Callback-data prefixes claimed here: ``mvp:queue`` (bucket overview),
``mvp:cards:``, ``mvp:cards_do:``, ``mvp:cards_noop``.
"""
from __future__ import annotations

import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from candidate.export import (
    enrich_resume_document, render_ats_resume_html, render_cover_letter_html,
    render_vacancy_html, render_variant_html,
)
from candidate.hh_resume_sync import load_profile
from candidate.service import judge_vacancy_fit

from mvp import config
from mvp.automation import _try_auto_apply
from mvp.formatting import _payload_digest, _preview_text, _quality_checklist, split_for_telegram
from mvp.hh_sync import live_resume
from mvp.keyboards import _outcome_keyboard

log = logging.getLogger(__name__)

router = Router()

#: bucket key -> (title, statuses, empty-state text)
BUCKETS: dict[str, dict] = {
    "queue": {
        "title": "📥 Очередь",
        # `approved` belongs here. It used to belong to no bucket at all, so a
        # press on «Откликнуться» moved the package into a status the deck did
        # not render: the card vanished from the queue and the button that
        # actually sends it became unreachable. One press sends now, but the
        # status can still be observed mid-flight, and it must stay visible.
        "statuses": ("draft", "changed", "approved"),
        "empty": "Очередь пуста — новых подготовленных пакетов нет.",
    },
    "manual": {
        "title": "🛠 Требуют руки",
        "statuses": ("manual_required",),
        "empty": "Ручных случаев нет.",
    },
    "filtered": {
        "title": "🧹 Отфильтрованные",
        # `failed` is not written by this flow any more, but rows from the older
        # one are still in the journal. They belong somewhere restorable rather
        # than nowhere at all.
        "statuses": ("skipped", "failed"),
        "empty": "Отфильтрованных вакансий нет.",
    },
    "sent": {
        "title": "🚀 Отправленные",
        "statuses": ("applied", "already_applied"),
        "empty": "Отправленных откликов пока нет.",
    },
}

#: Every status `VariantStore` / `_try_auto_apply` can write. A status missing
#: from `BUCKETS` is invisible in the UI — the package is neither in a deck nor
#: reachable by any button, which is exactly how «нажал откликнуться, и оно
#: пропало» happened. The assert below makes that failure impossible to ship.
KNOWN_STATUSES: frozenset[str] = frozenset({
    "draft", "changed", "approved", "manual_required",
    "skipped", "failed", "applied", "already_applied",
})
_BUCKETED_STATUSES = {status for bucket in BUCKETS.values() for status in bucket["statuses"]}
assert KNOWN_STATUSES <= _BUCKETED_STATUSES, (
    f"Statuses outside every bucket would be invisible: {sorted(KNOWN_STATUSES - _BUCKETED_STATUSES)}"
)

#: Why hh.ru (or a local guard) refused, in the owner's language. The raw reason
#: is still shown as a code — it is what the logs and tests speak.
APPLY_REASONS: dict[str, str] = {
    "application_writes_disabled": "отправка откликов временно полностью заблокирована на сервере.",
    "hh_not_connected": "hh.ru не подключён — авторизуйтесь в «⚙️ Настройки».",
    "quality_checklist_failed": "пакет не прошёл финальный checklist.",
    "approval_missing_or_stale": "пакет изменился после подтверждения — откройте карточку заново.",
    "stable_resume_mapping_required": "профиль не привязан к резюме на hh.ru — привяжите в «📄 Резюме».",
    "stable_resume_mapping_unavailable": "привязанное резюме больше не существует на hh.ru.",
    "approved_resume_mapping_changed": "привязка резюме изменилась между подтверждением и отправкой.",
    "daily_limit": "дневной лимит откликов исчерпан.",
    "source_not_supported": "вакансия не с hh.ru — откликнуться можно только вручную.",
    "hh_vacancy_id_missing": "у вакансии нет id на hh.ru.",
    "vacancy_changed_after_preview": "работодатель изменил описание — пакет нужно собрать заново.",
    "already_applied": "отклик на эту вакансию уже отправлен раньше.",
    "vacancy_gone": "вакансия снята с публикации на hh.ru — карточка убрана в «Отфильтрованные».",
    "auth_failed": "hh.ru не принял запрос — проверьте авторизацию в «⚙️ Настройки».",
    "local_pair_dedup": "отклик этой парой «вакансия + резюме» уже есть в журнале.",
    "test_required": "работодатель требует тест — отклик только вручную на hh.ru.",
    "status_must_be_approved": "статус пакета изменился, пока шла отправка.",
}

_DECK_LIMIT = 200
_TEXT_LIMIT = 3600


def _load(user_id: int, bucket: str) -> list[dict]:
    statuses = BUCKETS[bucket]["statuses"]
    return config.STORE.list_by_status(user_id, statuses, _DECK_LIMIT)


def _fit(text: str) -> str:
    """First Telegram-sized chunk, cut on a line boundary so no tag is split."""
    chunks = split_for_telegram(text, _TEXT_LIMIT)
    if len(chunks) <= 1:
        return text
    return chunks[0] + "\n\n… карточка обрезана, полный текст — в «📎 Файлы»."


# ---------------------------------------------------------------------------
# Live hh.ru resume: read it, and rebuild the package when it moved
# ---------------------------------------------------------------------------

def _resume_line(live: dict, payload: dict) -> str:
    state = live["state"]
    if state == "unmapped":
        return (
            "📄 <b>Резюме на hh.ru:</b> ⚠️ профиль не привязан — откликнуться нечем.\n"
            "   Привяжите его в «📄 Резюме»."
        )
    if state == "no_hh":
        return "📄 <b>Резюме на hh.ru:</b> ⚠️ hh.ru не подключён, показан сохранённый пакет."
    if state == "error":
        stored = str((payload.get("hh_resume_snapshot") or {}).get("title") or "—")
        return (
            f"📄 <b>Резюме на hh.ru:</b> ⚠️ не прочиталось (<code>{html.escape(live['reason'])}</code>), "
            f"показан сохранённый снимок «{html.escape(stored)}»."
        )
    resume = live["resume"] or {}
    status_obj = resume.get("status") or {}
    status_id = str(status_obj.get("id") if isinstance(status_obj, dict) else status_obj or "").lower()
    published = "опубликовано ✅" if status_id in {"published", "visible"} else f"не опубликовано ({html.escape(status_id or 'unknown')})"
    views = resume.get("total_views")
    new_views = resume.get("new_views")
    views_text = ""
    if views is not None or new_views is not None:
        views_text = f" · просмотры {views if views is not None else '—'} (новых {new_views if new_views is not None else '—'})"
    return (
        f"📄 <b>Резюме на hh.ru (live):</b> «{html.escape(str(resume.get('title') or '—'))}»\n"
        f"   {published}{views_text} · id <code>{html.escape(str(resume.get('id') or ''))}</code>"
    )


async def _refresh_against_live(payload: dict, live: dict) -> tuple[dict, str]:
    """Rebuild the package from the live resume when hh.ru moved ahead of it.

    Returns the payload to render plus a note for the card. Only touches
    variants still open for editing — an already-sent package is history and
    must keep the document it was sent with.
    """
    if live["state"] != "ok" or not live.get("resume"):
        return payload, ""
    status = str(payload.get("status") or "")
    if status not in {"draft", "changed", "approved", "manual_required"}:
        return payload, ""
    snapshot = payload.get("hh_resume_snapshot") or {}
    resume = live["resume"]
    same_resume = str(snapshot.get("resume_id") or "") == str(resume.get("id") or "")
    same_version = str(snapshot.get("updated_at") or "") == str(resume.get("updated_at") or "")
    if same_resume and same_version:
        return payload, ""
    try:
        rebuilt = enrich_resume_document(payload, resume)
    except Exception as exc:  # a malformed old payload must not kill the deck
        log.warning("Live resume merge failed safely for %s: %s", payload.get("id"), type(exc).__name__)
        return payload, ""
    rebuilt["hh_resume_snapshot"]["stable_mapping"] = True
    rebuilt["hh_resume_snapshot"]["profile_key"] = payload["master_profile"]["key"]
    rebuilt["status"] = status
    # An approved package that changes is no longer the package that was
    # approved, so drop it back to draft rather than leaving a stale approval
    # that _try_auto_apply would reject with an opaque reason.
    new_status = "draft" if status == "approved" else status
    saved = await asyncio.to_thread(
        config.STORE.update_payload, rebuilt, status=new_status, expected=(status,),
    )
    if not saved:
        return payload, ""
    rebuilt["status"] = new_status
    note = "♻️ Резюме на hh.ru изменилось — live HH-снимок и контакты обновлены."
    if status == "approved":
        note += " Прежнее подтверждение сброшено, подтвердите заново."
    return rebuilt, note


# ---------------------------------------------------------------------------
# LLM judge: fit / gaps / reply-chance, cached on the payload so a re-render
# never re-bills a model call
# ---------------------------------------------------------------------------

async def _ensure_llm_judge(user_id: int, payload: dict) -> dict:
    """Attach the DeepSeek fit/gap/reply-chance verdict to the card, at most once.

    Cost discipline (spec item 5): a vacancy only earns a model call once it has already
    cleared the owner's own preview bar -- ``min_preview_score``, the exact line
    ``mvp/automation.py`` uses to decide draft vs. skipped. A card destined for
    "Отфильтрованные" would just be paying DeepSeek to restate a rejection the deterministic
    ranker already made for free. Caching: the verdict is written into ``payload["llm_judge"]``
    and persisted via ``config.STORE.update_payload`` (the same call ``_refresh_against_live``
    uses), so the next time this exact payload is opened the cached branch above returns
    immediately -- no second call, no second bill. Any failure along the way (profile file
    missing, judge call failing, the payload changing status underneath the call) leaves the
    payload exactly as it was: the card still renders from the deterministic
    ``payload["score"]`` fields.
    """
    if payload.get("llm_judge") is not None:
        return payload  # cached verdict -- never re-bill a render
    score = payload.get("score") or {}
    try:
        value = int(score.get("value") or 0)
    except (TypeError, ValueError):
        value = 0
    threshold = int(config.AUTOMATION.settings(user_id)["min_preview_score"])
    if value < threshold:
        return payload
    profile_key = str((payload.get("master_profile") or {}).get("key") or "")
    if not profile_key:
        return payload
    try:
        ranking_profile = await asyncio.to_thread(load_profile, profile_key)
    except Exception as exc:
        log.info("LLM judge: ranking profile %s unavailable: %s", profile_key, type(exc).__name__)
        return payload
    verdict = await asyncio.to_thread(
        judge_vacancy_fit, payload.get("vacancy") or {}, ranking_profile, score,
    )
    if verdict is None:
        return payload
    payload["llm_judge"] = verdict
    status = str(payload.get("status") or "draft")
    await asyncio.to_thread(
        config.STORE.update_payload, payload, status=status, expected=(status,),
    )
    return payload


# ---------------------------------------------------------------------------
# Card rendering
# ---------------------------------------------------------------------------

def _checklist(payload: dict) -> tuple[bool, list[tuple[bool, str]]]:
    """``_quality_checklist`` that reports "not ready" instead of raising.

    A pre-rewrite payload can be missing keys the checklist reads; the honest
    answer for the card is "не готов", never a traceback in place of a screen.
    """
    try:
        return _quality_checklist(payload)
    except Exception as exc:
        log.warning("Checklist failed for %s: %s", payload.get("id"), type(exc).__name__)
        return False, [(False, "Пакет старого формата", "пакет старого формата — соберите карточку заново")]


def _fallback_body(payload: dict) -> str:
    """Minimal card for a payload the full preview cannot render.

    The deck browses the whole journal, including variants written before the
    2026-08-11 ranking rewrite changed the score shape. One old row must not
    make the screen unreachable — show what is certainly there and let the
    owner skip or export it.
    """
    vacancy = payload.get("vacancy") or {}
    return (
        f"💼 <b>{html.escape(str(vacancy.get('name') or 'Вакансия'))}</b>\n"
        f"🏢 {html.escape(str(vacancy.get('employer_name') or ''))}\n\n"
        f"⚠️ Старый формат пакета — полный предпросмотр недоступен. "
        f"Статус: <code>{html.escape(str(payload.get('status') or ''))}</code>."
    )


def _card_text(payload: dict, bucket: str, index: int, total: int, live: dict, note: str) -> str:
    head = f"{BUCKETS[bucket]['title']} · <b>{index + 1} из {total}</b>\n\n"
    try:
        body = _preview_text(payload)
    except Exception as exc:
        log.warning("Preview render failed for %s: %s", payload.get("id"), type(exc).__name__)
        body = _fallback_body(payload)
    tail = "\n\n" + _resume_line(live, payload)
    if bucket not in {"sent", "filtered"}:
        ready, checks = _checklist(payload)
        if ready:
            tail += "\n\n🚀 «Откликнуться» отправит реальный отклик на hh.ru сразу, без второго шага."
        else:
            failed = [problem for ok, _, problem in checks if not ok][:3]
            tail += "\n\n❌ Отправка заблокирована:\n" + "\n".join(f"• {html.escape(x)}" for x in failed)
    if note:
        tail += f"\n\n{note}"
    return _fit(head + body + tail)


def _card_keyboard(payload: dict, bucket: str, index: int, total: int, live: dict) -> InlineKeyboardMarkup:
    variant_id = payload["id"]
    rows: list[list[InlineKeyboardButton]] = []

    nav: list[InlineKeyboardButton] = []
    if total > 1:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"mvp:cards:{bucket}:{(index - 1) % total}"))
        nav.append(InlineKeyboardButton(text=f"{index + 1}/{total}", callback_data="mvp:cards_noop"))
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"mvp:cards:{bucket}:{(index + 1) % total}"))
        rows.append(nav)

    if bucket == "sent":
        rows.extend(_outcome_keyboard(variant_id).inline_keyboard)
    elif bucket == "filtered":
        rows.append([InlineKeyboardButton(
            text="♻️ Вернуть в очередь", callback_data=f"mvp:cards_do:restore:{bucket}:{index}",
        )])
    else:
        # One press = one real application. The old two-step (approve, then
        # send) is gone: it hid the second button behind a status no deck
        # rendered, so «Откликнуться» looked like it did nothing.
        if live["state"] == "unmapped":
            rows.append([InlineKeyboardButton(
                text="🔗 Привязать резюме — откликнуться нечем", callback_data="mvp:resumes",
            )])
        else:
            rows.append([InlineKeyboardButton(
                text="🚀 Откликнуться", callback_data=f"mvp:cards_do:apply:{bucket}:{index}",
            )])
        rows.append([
            InlineKeyboardButton(text="✍️ Письмо", callback_data=f"mvp:letter_manual:{variant_id}"),
            InlineKeyboardButton(text="🔄 Профиль", callback_data=f"mvp:change:{variant_id}"),
        ])
        rows.append([
            InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"mvp:cards_do:skip:{bucket}:{index}"),
            InlineKeyboardButton(text="📎 Файлы", callback_data=f"mvp:cards_do:files:{bucket}:{index}"),
        ])

    source_url = (payload.get("vacancy") or {}).get("source_url") or ""
    if source_url:
        rows.append([InlineKeyboardButton(text="🔗 Открыть на HH", url=source_url)])
    rows.append([InlineKeyboardButton(text="⬅️ К спискам", callback_data="mvp:queue")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_card(
    target: Message, user_id: int, bucket: str, index: int, *, edit: bool, note: str = "",
) -> None:
    deck = await asyncio.to_thread(_load, user_id, bucket)
    if not deck:
        await _render_overview(target, user_id, edit=edit, note=BUCKETS[bucket]["empty"])
        return
    index = max(0, min(index, len(deck) - 1))
    payload = deck[index]
    live = await live_resume(user_id, payload["master_profile"]["key"])
    payload, refresh_note = await _refresh_against_live(payload, live)
    payload = await _ensure_llm_judge(user_id, payload)
    note = "\n".join(x for x in (note, refresh_note) if x)
    text = _card_text(payload, bucket, index, len(deck), live, note)
    keyboard = _card_keyboard(payload, bucket, index, len(deck), live)
    await _deliver(target, text, keyboard, edit=edit)


async def _deliver(target: Message, text: str, keyboard: InlineKeyboardMarkup, *, edit: bool) -> None:
    if edit:
        try:
            await target.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
            return
        except Exception:
            pass  # not-modified, too old to edit, or the source was a document
    await target.answer(text, parse_mode="HTML", reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Bucket overview
# ---------------------------------------------------------------------------

async def _render_overview(target: Message, user_id: int, *, edit: bool, note: str = "") -> None:
    counts = await asyncio.to_thread(config.STORE.count_by_status, user_id)
    rows: list[list[InlineKeyboardButton]] = []
    lines = ["🗂 <b>Вакансии</b>"]
    if note:
        lines.append(note)
    lines.append("")
    for key, bucket in BUCKETS.items():
        total = sum(counts.get(status, 0) for status in bucket["statuses"])
        lines.append(f"{bucket['title']} — {total}")
        if total:
            rows.append([InlineKeyboardButton(
                text=f"{bucket['title']} ({total})", callback_data=f"mvp:cards:{key}:0",
            )])
    lines.append("")
    lines.append("Карточки листаются стрелками; решение сразу подставляет следующую.")
    rows.append([InlineKeyboardButton(text="➕ Добавить вакансию", callback_data="mvp:add")])
    await _deliver(target, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows), edit=edit)


@router.callback_query(F.data == "mvp:queue")
async def queue_overview(callback: CallbackQuery) -> None:
    # Answers rather than edits: this is reachable from the main menu, and
    # editing would consume the menu message. Card navigation edits in place.
    await callback.answer()
    await _render_overview(callback.message, callback.from_user.id, edit=False)


@router.message(Command("queue"))
async def queue_command(message: Message) -> None:
    await _render_overview(message, message.from_user.id, edit=False)


@router.callback_query(F.data == "mvp:cards_noop")
async def cards_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("mvp:cards:"))
async def open_card(callback: CallbackQuery) -> None:
    _, _, bucket, raw_index = callback.data.split(":", 3)
    if bucket not in BUCKETS:
        await callback.answer("Неизвестный список", show_alert=True)
        return
    await callback.answer()
    await _render_card(
        callback.message, callback.from_user.id, bucket,
        int(raw_index) if raw_index.isdigit() else 0, edit=True,
    )


# ---------------------------------------------------------------------------
# Card actions
# ---------------------------------------------------------------------------

async def _current(user_id: int, bucket: str, index: int) -> tuple[dict | None, int]:
    deck = await asyncio.to_thread(_load, user_id, bucket)
    if not deck:
        return None, 0
    index = max(0, min(index, len(deck) - 1))
    return deck[index], index


@router.callback_query(F.data.startswith("mvp:cards_do:"))
async def card_action(callback: CallbackQuery) -> None:
    _, _, action, bucket, raw_index = callback.data.split(":", 4)
    if bucket not in BUCKETS:
        await callback.answer("Неизвестный список", show_alert=True)
        return
    user_id = callback.from_user.id
    index = int(raw_index) if raw_index.isdigit() else 0
    payload, index = await _current(user_id, bucket, index)
    if payload is None:
        await callback.answer("Список опустел", show_alert=True)
        await _render_overview(callback.message, user_id, edit=True)
        return
    variant_id = payload["id"]

    if action == "skip":
        await asyncio.to_thread(config.STORE.set_status, variant_id, "skipped", ("draft", "changed", "manual_required"))
        await callback.answer("Пропущено")
        await _render_card(callback.message, user_id, bucket, index, edit=True)
        return

    if action == "restore":
        changed = await asyncio.to_thread(config.STORE.set_status, variant_id, "draft", ("skipped",))
        await callback.answer("Вернул в очередь" if changed else "Статус уже изменился")
        await _render_card(callback.message, user_id, bucket, index, edit=True)
        return

    if action == "unapprove":
        await asyncio.to_thread(config.STORE.set_status, variant_id, "draft", ("approved",))
        await callback.answer("Подтверждение снято")
        await _render_card(callback.message, user_id, bucket, index, edit=True)
        return

    if action == "files":
        await callback.answer("Собираю файлы")
        await _send_files(callback.message, payload)
        return

    if action in {"apply", "approve", "send"}:
        await _confirm_apply(callback, payload, bucket, index)
        return

    await callback.answer("Неизвестное действие", show_alert=True)


@router.callback_query(F.data.startswith(("mvp:apply:", "mvp:approve:", "mvp:send:")))
async def apply_by_id(callback: CallbackQuery) -> None:
    """The same confirmation flow, from a notification card instead of the deck.

    The automation loop pushes a preview for every vacancy it finds, and that card
    `mvp:approve:`/`mvp:send:` are retired callback names kept routed so buttons
    sitting in chat history still open the current confirmation instead of failing.
    """
    variant_id = callback.data.rsplit(":", 1)[-1]
    payload = await asyncio.to_thread(config.STORE.get, variant_id)
    if not payload:
        await callback.answer("Вариант не найден", show_alert=True)
        return
    await _confirm_apply(callback, payload, "", 0)


@router.callback_query(F.data.startswith("mvp:confirm_apply:"))
async def confirmed_apply(callback: CallbackQuery) -> None:
    confirmation = callback.data.removeprefix("mvp:confirm_apply:")
    parts = confirmation.split(":", 2)
    if len(parts) == 3:
        raw_bucket, raw_index, variant_id = parts
        bucket = raw_bucket if raw_bucket in BUCKETS else ""
        index = int(raw_index) if raw_index.isdigit() else 0
    else:
        # Buttons already sent before deck position became part of the callback.
        variant_id = confirmation
        bucket, index = "", 0
    payload = await asyncio.to_thread(config.STORE.get, variant_id)
    if not payload:
        await callback.answer("Вариант не найден", show_alert=True)
        return
    await _apply_now(callback, payload, bucket, index)


async def _confirm_apply(callback: CallbackQuery, payload: dict, bucket: str, index: int) -> None:
    profile = str((payload.get("master_profile") or {}).get("name") or "Резюме")
    score = int((payload.get("score") or {}).get("value") or 0)
    threshold = int(config.AUTOMATION.settings(callback.from_user.id)["min_apply_score"])
    warning = "\n\n⚠️ Ниже порога сильного совпадения." if score < threshold else ""
    position = f"{bucket}:{index}" if bucket in BUCKETS else "-:0"
    await callback.answer()
    await callback.message.answer(
        f"Проверь перед отправкой:\n\n"
        f"💼 <b>{html.escape(str((payload.get('vacancy') or {}).get('name') or 'Вакансия'))}</b>\n"
        f"📄 Резюме: <b>{html.escape(profile)}</b>\n"
        f"🎯 Совпадение: <b>{score}%</b> · порог {threshold}%{warning}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Отправить этим резюме",
                callback_data=f"mvp:confirm_apply:{position}:{payload['id']}",
            )],
            [InlineKeyboardButton(
                text="🔄 Сменить профиль",
                callback_data=f"mvp:change:{payload['id']}",
            )],
        ]),
    )


async def _apply_now(callback: CallbackQuery, payload: dict, bucket: str, index: int) -> None:
    """After explicit confirmation, approve and send with every existing guard.

    An empty `bucket` means the press came from a notification card, not the deck,
    so there is no deck position to redraw.

    The approval row and its ``payload_sha256`` are still written before the
    call — ``_try_auto_apply`` refuses to send without them, and that digest is
    what stops an edited package from reaching a recruiter. What is gone is the
    pause between the two, which is where packages used to disappear.

    A package that fails still has to be findable, so anything that ends the
    attempt still sitting in ``approved`` (nothing was sent, nothing moved it)
    is put back to ``draft`` — the queue it was in when the owner pressed.
    """
    user_id = callback.from_user.id
    variant_id = payload["id"]
    live = await live_resume(user_id, payload["master_profile"]["key"])
    if live["state"] == "unmapped":
        await callback.answer("Профиль не привязан к резюме на hh.ru", show_alert=True)
        return
    ready, checks = _checklist(payload)
    if not ready:
        failed = next((problem for ok, _, problem in checks if not ok), "проверка не пройдена")
        await callback.answer(f"Не отправлено: {failed}", show_alert=True)
        return

    await callback.answer("Отправляю в hh.ru…")
    if str(payload.get("status") or "") != "approved":
        changed = await asyncio.to_thread(
            config.STORE.set_status, variant_id, "approved", ("draft", "changed", "manual_required"),
        )
        if not changed:
            await callback.message.answer("🔄 Статус пакета изменился — отклик не отправлен.")
            if bucket:
                await _render_card(callback.message, user_id, bucket, index, edit=False)
            return
    payload["status"] = "approved"
    await asyncio.to_thread(
        config.AUTOMATION.approve_variant,
        user_id=user_id, variant_id=variant_id,
        payload_sha256=_payload_digest(payload), resume_id=live["resume_id"],
    )

    status, reason = await _try_auto_apply(user_id, payload, manual_confirmed=True)
    if status == "applied":
        await callback.message.answer("✅ hh.ru подтвердил отклик. Следующая карточка:")
    else:
        await asyncio.to_thread(config.STORE.set_status, variant_id, "draft", ("approved",))
        explanation = APPLY_REASONS.get(reason, "hh.ru или проверка отклонили отправку.")
        await callback.message.answer(
            f"🔒 Не отправлено: {html.escape(explanation)}\n"
            f"<code>{html.escape(reason)}</code>",
            parse_mode="HTML",
        )
    if bucket:
        await _render_card(callback.message, user_id, bucket, index, edit=False)


async def _send_files(target: Message, payload: dict) -> None:
    variant_id = payload["id"]
    exports = config.ROOT / "var" / "exports"
    documents = [
        (render_variant_html(payload, exports / f"resume-{variant_id}.html"), "Резюме (HTML)"),
        (render_ats_resume_html(payload, exports / f"resume-ats-{variant_id}.html"), "ATS-резюме"),
        (render_cover_letter_html(payload, exports / f"cover-letter-{variant_id}.html"), "Сопроводительное письмо"),
        (render_vacancy_html(payload, exports / f"vacancy-{variant_id}.html"), "Вакансия (snapshot)"),
    ]
    for path, caption in documents:
        await target.answer_document(FSInputFile(path), caption=caption)
