"""One hh.ru follow-up 24 hours after an unanswered application.

Automatic mode sends exactly one fixed status question. A rejection, invitation,
closed chat, archived vacancy, or later message from either side stops it. Manual
mode keeps the owner-approval card as a fail-safe rollout option.

Callback-data prefixes claimed here: ``fu:send:``, ``fu:edit:``, ``fu:skip:``,
``fu:stop:``.
"""
from __future__ import annotations

import asyncio
import difflib
import html
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from candidate.data import profile_keys
from candidate.hh_official import HHOfficialError
from candidate.service import CandidateAdapter, matched_vacancy_markers

from mvp import config
from mvp.automation import (
    _fetch_negotiation_messages,
    _hh_client_for,
    _hh_thread_url,
    _is_employer_message,
)
from mvp.bump import LOCAL_TZ
from mvp.formatting import _safe_send

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FollowupStep:
    key: str
    day: int
    title: str
    intent: str
    must_ask: bool = False


STATUS_STEP = FollowupStep(
    key="auto_24h", day=1, title="Вопрос об актуальности",
    intent="Спросить, актуальна ли вакансия, и подтвердить, что отклик ещё актуален.",
    must_ask=True,
)
STEPS: tuple[FollowupStep, ...] = (STATUS_STEP,)

_STEPS_BY_KEY = {step.key: step for step in STEPS}
_STEP_ORDER = [step.key for step in STEPS]

# By default every unresolved application is eligible. Deployments may still set
# FOLLOWUP_START_AFTER when they intentionally want to exclude an old backlog.
_DEFAULT_START_AFTER = "1970-01-01"

_MIN_CHARS, _MAX_CHARS = 120, 600
#: The first applicant message is HH's copy of the cover letter. Anything later
#: means a real conversation already started and must suppress the automatic nudge.
_INITIAL_MESSAGE_GRACE = timedelta(minutes=5)
_NUMBER_RE = re.compile(r"\d[\d.,]*")
#: A letter touching a digit means it is an identifier, not a claim: n8n, gpt-4o,
#: Web3, S3. Treating those as figures made every honest draft mentioning the
#: vacancy's own stack fail the invented-number check.
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
#: «4 000» and «4000» are the same claim; the fact base writes one, the model writes the other.
_THOUSANDS_RE = re.compile(r"(?<=\d)[\s ](?=\d{3})")
_BANNED_OPENINGS = (
    "напоминаю о себе", "хотел бы напомнить", "поднимаю свой отклик",
    "уточняю статус", "просто напоминаю", "не получил ответа",
)


def _now_msk(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)


def in_send_window(now: datetime | None = None) -> bool:
    """Weekday hours when a recruiter is actually in their hh.ru inbox.

    A nudge that lands at 23:40 on Saturday is read on Monday alongside
    everything else — it spends the touch and buys nothing.
    """
    local = _now_msk(now)
    start = int(os.environ.get("FOLLOWUP_HOUR_START", "10"))
    end = int(os.environ.get("FOLLOWUP_HOUR_END", "18"))
    return start <= local.hour < end


def _parse_time(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def start_after() -> datetime:
    parsed = _parse_time(os.environ.get("FOLLOWUP_START_AFTER", _DEFAULT_START_AFTER))
    return parsed or datetime(2026, 8, 1, tzinfo=timezone.utc)


def next_step(created_at: datetime, done: dict[str, str], now: datetime) -> FollowupStep | None:
    """Return the single idempotent touch once the application is 24 hours old."""
    if now - created_at < timedelta(hours=24):
        return None
    # Transient HH/network failures are retried; every other recorded state is terminal.
    if done.get(STATUS_STEP.key) not in {None, "failed"}:
        return None
    return STATUS_STEP


def outgrown_steps(step: FollowupStep, done: dict[str, str]) -> list[str]:
    """Compatibility hook for the former finite ladder; daily touches do not outgrow."""
    return []


def status_message(vacancy_title: str) -> str:
    title = re.sub(r"\s+", " ", vacancy_title or "").strip()
    subject = f" «{title}»" if title else ""
    return (
        f"Здравствуйте. Подскажите, пожалуйста, вакансия{subject} ещё актуальна? "
        "Мой отклик в силе, я по-прежнему готов обсудить позицию и следующие шаги."
    )


def _profile_key_for_resume(user_id: int, resume_id: str) -> str:
    if not resume_id:
        return ""
    for key in profile_keys():
        mapping = config.AUTOMATION.resume_mapping(user_id, key)
        if mapping and mapping["resume_id"] == resume_id:
            return key
    return ""


def _number_claims(text: str) -> set[str]:
    """The figures a text actually asserts, with tool names like n8n left out."""
    collapsed = _THOUSANDS_RE.sub("", text or "")
    claims: set[str] = set()
    for match in _NUMBER_RE.finditer(collapsed):
        token = match.group().strip(".,")
        if not token:
            continue
        before = collapsed[match.start() - 1] if match.start() else " "
        after = collapsed[match.end()] if match.end() < len(collapsed) else " "
        if _LETTER_RE.match(before) or _LETTER_RE.match(after):
            continue
        claims.add(token)
    return claims


def _supported_numbers(text: str, corpus: str) -> bool:
    """Every figure in the message must already exist in the verified corpus.

    The cover-letter pipeline refuses invented numbers; a follow-up is read by
    the same recruiter and has to clear the same bar.
    """
    return _number_claims(text) <= _number_claims(corpus)


def message_problem(
    text: str, *, step: FollowupStep, vacancy: dict, cover_letter: str, corpus: str,
) -> str:
    """Empty string when the draft is fit to send, otherwise the reason it is not."""
    body = (text or "").strip()
    if not _MIN_CHARS <= len(body) <= _MAX_CHARS:
        return f"length_{len(body)}"
    lowered = body.lower()
    if any(marker in lowered for marker in _BANNED_OPENINGS):
        return "empty_reminder"
    if not CandidateAdapter._public_text_ok(body):
        return "closed_fact"
    if step.must_ask and "?" not in body:
        return "no_question"
    if cover_letter and difflib.SequenceMatcher(None, body.lower(), cover_letter.lower()).ratio() > 0.6:
        return "repeats_cover_letter"
    if not matched_vacancy_markers(body, vacancy):
        return "no_vacancy_terms"
    if not _supported_numbers(body, corpus):
        return "unsupported_number"
    return ""


def _compose_prompt(
    *, step: FollowupStep, vacancy: dict, profile, cover_letter: str, facts: list[str],
) -> str:
    fact_lines = "\n".join(f"- {fact}" for fact in facts) or "- (подходящих фактов не найдено)"
    description = str(vacancy.get("description") or vacancy.get("normalized_text") or "")[:2500]
    return (
        "Ты пишешь короткое follow-up сообщение в чат отклика на hh.ru от лица кандидата.\n"
        f"Задача касания: {step.intent}\n\n"
        f"ВАКАНСИЯ: {vacancy.get('name', '')}\n"
        f"КОМПАНИЯ: {vacancy.get('employer_name', '')}\n"
        f"ОПИСАНИЕ: {description}\n\n"
        f"ПРОФИЛЬ КАНДИДАТА: {getattr(profile, 'name', '')}\n"
        f"О СЕБЕ: {getattr(profile, 'about', '')}\n\n"
        f"УЖЕ ОТПРАВЛЕННОЕ ПИСЬМО (не повторять его содержание):\n{cover_letter[:1200]}\n\n"
        f"ПРОВЕРЕННЫЕ ФАКТЫ (только из этого списка можно брать цифры и достижения):\n{fact_lines}\n\n"
        "ПРАВИЛА:\n"
        f"- {_MIN_CHARS}-{_MAX_CHARS} символов, живой человеческий русский, без канцелярита\n"
        "- НЕ писать «напоминаю о себе», «уточняю статус», «не получил ответа»\n"
        "- ни одной цифры, которой нет в списке фактов\n"
        "- использовать терминологию из описания вакансии\n"
        "- без подписи, без «С уважением», без имени в конце\n"
        "- обращение по имени только если оно есть в описании\n\n"
        'Верни строго JSON: {"message": "текст сообщения"}'
    )


async def compose_message(
    *, step: FollowupStep, vacancy: dict, profile_key: str, cover_letter: str,
) -> tuple[str, str]:
    """Draft one rung. Returns ``(message, "")`` or ``("", reason)``.

    Two attempts, then it gives up rather than offering the owner a draft that
    failed its own checks — a rejected rung is retried on a later cycle.
    """
    adapter = config.ADAPTER
    if adapter is None:
        return "", "adapter_missing"
    profile = adapter.data.profiles.get(profile_key)
    if profile is None:
        return "", "profile_unknown"
    query = f"{vacancy.get('name', '')} {vacancy.get('description', '')}"[:400]
    facts = [fact.text for fact in adapter.data.search(query, limit=12) if fact.publishable]
    corpus = "\n".join(facts) + "\n" + getattr(profile, "markdown", "")
    prompt = _compose_prompt(
        step=step, vacancy=vacancy, profile=profile, cover_letter=cover_letter, facts=facts,
    )
    last_reason = "generation_failed"
    for _ in range(2):
        try:
            generated = await asyncio.to_thread(adapter._complete_json, prompt)
        except Exception as exc:
            log.warning("Follow-up generation failed safely: %s", type(exc).__name__)
            last_reason = "generation_failed"
            continue
        body = str(generated.get("message") or "").strip()
        last_reason = message_problem(
            body, step=step, vacancy=vacancy, cover_letter=cover_letter, corpus=corpus,
        )
        if not last_reason:
            return body, ""
        log.info("Follow-up draft rejected: %s", last_reason)
    return "", last_reason


def followup_keyboard(negotiation_id: str, step_key: str, chat_id: str = "") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data=f"fu:send:{negotiation_id}:{step_key}"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data=f"fu:edit:{negotiation_id}:{step_key}"),
        ],
        [
            InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"fu:skip:{negotiation_id}:{step_key}"),
            InlineKeyboardButton(text="🚫 Больше не писать", callback_data=f"fu:stop:{negotiation_id}"),
        ],
        [InlineKeyboardButton(text="🔗 Открыть на hh.ru", url=_hh_thread_url(chat_id=chat_id))],
    ])


def followup_card(
    *, step: FollowupStep, employer: str, vacancy_title: str, age_days: int,
    viewed: bool, message: str,
) -> str:
    seen = "работодатель открывал отклик" if viewed else "работодатель ещё не открывал отклик"
    return (
        f"🔔 <b>Повторное сообщение · {html.escape(step.title)}</b>\n\n"
        f"🏢 <b>{html.escape(employer or 'Работодатель не указан')}</b>\n"
        f"💼 {html.escape(vacancy_title or 'Вакансия')}\n"
        f"📅 Отклик {age_days} дн. назад · {seen}\n\n"
        f"<i>Черновик сообщения:</i>\n«{html.escape(message)}»\n\n"
        "⚠️ Само ничего не отправится. Отправить как есть, поправить текст или закрыть этот тред?"
    )


async def send_followup(
    user_id: int, negotiation_id: str, step_key: str, message: str, *,
    owner_confirmed: bool = False, automated: bool = False,
) -> str:
    """Push an explicitly owner-confirmed or deployment-authorized message into hh.ru."""
    if not (owner_confirmed or automated):
        return "owner_confirmation_required"
    client, _account = await _hh_client_for(user_id)
    if client is None:
        return "hh_not_connected"
    try:
        await asyncio.to_thread(
            client._request, "POST",
            f"https://api.hh.ru/negotiations/{negotiation_id}/messages",
            {"message": message},
        )
    except HHOfficialError as exc:
        config.AUTOMATION.record_followup(
            user_id=user_id, negotiation_id=negotiation_id, step=step_key,
            status="stopped" if exc.status in {400, 403, 404} else "failed",
            message=message, reason=exc.reason,
        )
        return exc.reason
    # Recorded at send time rather than left for the next hh.ru sync to pick
    # back up -- otherwise the CRM's message history and silence clock for
    # this thread would lag behind what was actually just sent.
    config.AUTOMATION.record_outgoing_message(user_id, negotiation_id, message)
    config.AUTOMATION.record_followup(
        user_id=user_id, negotiation_id=negotiation_id, step=step_key,
        status="sent", message=message, reason="api_success",
    )
    return ""


def _thread_blocker(item: dict, now: datetime) -> str:
    """Why this thread is permanently out of daily follow-up, or "" if it is still in.

    Separate from the "not due yet" checks: a blocker closes the thread for good,
    a timing miss just means a later cycle.
    """
    state = str((item.get("state") or {}).get("id") or "")
    if state != "response":
        # An invitation needs a human answer within the hour, and writing into a
        # rejection is the worst look there is. Neither should get a status question.
        return "invitation_or_discard" if state in ("invitation", "discard") else f"state_{state}"
    if str(item.get("messaging_status") or "") != "ok":
        return "messaging_closed"
    created = _parse_time(str(item.get("created_at") or ""))
    if created is None:
        return "no_created_at"
    if created < start_after():
        return "before_cutoff"
    return ""


def _conversation_blocker(
    messages: list[dict], created_at: datetime, *,
    user_id: int | None = None, negotiation_id: str | None = None,
) -> str:
    """Ignore HH's initial cover-letter copy; block any real later conversation.

    Prefers the locally synced CRM thread state over a live message scan --
    cheaper, and consistent with what the CRM screen shows. ``awaiting_side ==
    "me"`` means the employer's message is the newest one in the thread, so
    the automated "still relevant?" question would land on top of an unread
    reply and must not be sent. Falls back to scanning ``messages`` directly
    when there is no local state yet (``user_id``/``negotiation_id`` not
    given, or ``crm_thread`` returns None / an empty thread), so a cold DB or
    a thread mvp/automation.py has not synced messages for yet still behaves
    the way it always did.
    """
    if user_id is not None and negotiation_id:
        thread = config.AUTOMATION.crm_thread(user_id, negotiation_id)
        if thread and int(thread.get("message_count") or 0) > 0:
            return "employer_replied" if thread.get("awaiting_side") == "me" else ""
    for message in messages:
        if _is_employer_message(message):
            return "employer_replied"
        sent_at = _parse_time(str(message.get("created_at") or ""))
        if sent_at is not None and sent_at - created_at > _INITIAL_MESSAGE_GRACE:
            return "applicant_messaged"
    return ""


def _message_already_sent(messages: list[dict], expected: str) -> bool:
    normalized = " ".join(expected.split()).casefold()
    return any(
        not _is_employer_message(message)
        and " ".join(str(message.get("text") or "").split()).casefold() == normalized
        for message in messages
    )


async def _due_card(bot: Bot, user_id: int, client, item: dict, now: datetime) -> bool:
    """Offer one due status question in Telegram; never send it to hh.ru."""
    negotiation_id = str(item.get("id") or "")
    done = config.AUTOMATION.followup_steps(user_id, negotiation_id)
    blocker = _thread_blocker(item, now)
    if blocker:
        if blocker != "before_cutoff":  # the archive is skipped silently, not marked
            config.AUTOMATION.stop_followups(
                user_id, negotiation_id, [k for k in _STEP_ORDER if k not in done], blocker,
            )
        return False
    created = _parse_time(str(item.get("created_at") or ""))
    step = next_step(created, done, now)
    if step is None:
        return False
    vacancy_id = str((item.get("vacancy") or {}).get("id") or "")
    try:
        detail = await asyncio.to_thread(client.vacancy, vacancy_id) if vacancy_id else {}
    except HHOfficialError as exc:
        if exc.status == 404 or exc.reason in {"not_found", "archived"}:
            config.AUTOMATION.stop_followups(
                user_id, negotiation_id, [k for k in _STEP_ORDER if k not in done], "vacancy_gone",
            )
        return False
    if detail.get("archived"):
        config.AUTOMATION.stop_followups(
            user_id, negotiation_id, [k for k in _STEP_ORDER if k not in done], "vacancy_archived",
        )
        return False

    title = str(detail.get("name") or (item.get("vacancy") or {}).get("name") or "")
    message = status_message(title)
    messages = await _fetch_negotiation_messages(client, negotiation_id, fail_closed=True)
    if messages is None:
        return False
    if _message_already_sent(messages, message):
        config.AUTOMATION.record_followup(
            user_id=user_id, negotiation_id=negotiation_id, step=step.key,
            status="sent", message=message, reason="recovered_from_hh_thread",
        )
        return False
    conversation_blocker = _conversation_blocker(
        messages, created, user_id=user_id, negotiation_id=negotiation_id,
    )
    if conversation_blocker:
        config.AUTOMATION.stop_followups(
            user_id, negotiation_id, [step.key], conversation_blocker,
        )
        return False

    if os.environ.get("FOLLOWUP_AUTO_SEND", "0") == "1":
        reason = await send_followup(
            user_id, negotiation_id, step.key, message, automated=True,
        )
        if reason:
            await _safe_send(
                bot, user_id,
                f"⚠️ Не удалось автоматически написать по вакансии «{html.escape(title)}»: "
                f"<code>{html.escape(reason)}</code>.", parse_mode="HTML",
            )
            return False
        await _safe_send(
            bot, user_id,
            f"📨 Через 24 часа отправил работодателю по вакансии «{html.escape(title)}»:\n"
            f"«{html.escape(message)}»", parse_mode="HTML",
        )
        return True

    config.AUTOMATION.record_followup(
        user_id=user_id, negotiation_id=negotiation_id, step=step.key,
        status="offered", message=message, reason="awaiting_owner_confirmation",
    )
    card = followup_card(
        step=step,
        employer=str((item.get("employer") or {}).get("name") or ""),
        vacancy_title=title,
        age_days=step.day,
        viewed=bool(item.get("viewed_by_opponent")),
        message=message,
    )
    sent = await _safe_send(
        bot, user_id, card, parse_mode="HTML",
        reply_markup=followup_keyboard(negotiation_id, step.key, str(item.get("chat_id") or "")),
    )
    if sent is None:
        config.AUTOMATION.record_followup(
            user_id=user_id, negotiation_id=negotiation_id, step=step.key,
            status="failed", message=message, reason="telegram_delivery_failed",
        )
        return False
    return True


async def run_followup_cycle(bot: Bot, user_id: int) -> int:
    """One pass over live applications. Returns how many reminders were sent/offered."""
    if os.environ.get("FOLLOWUP_ENABLED", "0") != "1":
        return 0
    if os.environ.get("FOLLOWUP_AUTO_SEND", "0") != "1" and not in_send_window():
        return 0
    daily_cap = int(os.environ.get("FOLLOWUP_DAILY_LIMIT", "100"))
    if config.AUTOMATION.followups_sent_today(user_id) >= daily_cap:
        return 0
    per_cycle = int(os.environ.get("FOLLOWUP_MAX_PER_CYCLE", "100"))
    client, _account = await _hh_client_for(user_id)
    if client is None:
        return 0
    now = datetime.now(timezone.utc)
    pushed = 0
    try:
        for page in range(5):
            response = await asyncio.to_thread(client.negotiations, page=page, per_page=100)
            for item in response.get("items", []):
                if pushed >= per_cycle:
                    return pushed
                try:
                    if await _due_card(bot, user_id, client, item, now):
                        pushed += 1
                except Exception:
                    log.exception("Follow-up evaluation failed for one thread")
            if page + 1 >= int(response.get("pages") or 0):
                break
    except HHOfficialError as exc:
        log.warning("Follow-up cycle stopped safely: %s", exc.reason)
    return pushed


async def _followup_loop(bot: Bot) -> None:
    await asyncio.sleep(15)
    interval = max(30, int(os.environ.get("FOLLOWUP_INTERVAL_SECONDS", "60")))
    while True:
        try:
            owner_raw = os.environ.get("MVP_OWNER_TELEGRAM_ID", "")
            if owner_raw.isdigit():
                await run_followup_cycle(bot, int(owner_raw))
        except Exception:
            log.exception("Follow-up cycle failed")
        await asyncio.sleep(interval)
