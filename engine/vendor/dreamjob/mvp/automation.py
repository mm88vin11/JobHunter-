"""Background auto-search / auto-apply cycle, including the safety gates."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from candidate.data import profile_keys
from candidate.hh_html import discover_vacancies, vacancy_from_api
from candidate.hh_official import HHOfficialClient, HHOfficialError
from candidate.service import analyze_vacancy_description
from candidate.hh_resume_sync import load_profile
from candidate.ranking import choose_profile, rank_vacancy
from candidate.traps import evaluate_vacancy, load_user_lists

from mvp import config
from mvp.formatting import (
    _payload_digest,
    _preview_text,
    _quality_checklist,
    _safe_send,
    vacancy_review,
)
from mvp.generation import _apply_owner_letter_style, _generate_with_failover
from mvp.hh_sync import (
    _fresh_ai_queries,
    _enrich_payload_from_hh,
    _hh_vacancy_id,
    _search_queries,
    hydrate_hh_vacancies,
    mapped_profile_keys,
    profile_applicability,
    search_hh_candidates,
)
from mvp.keyboards import _outcome_keyboard, _preview_keyboard
from mvp.settings_store import (
    get_crm_archive_after_days,
    get_crm_digest_enabled,
    get_crm_digest_last_sent,
    get_crm_digest_time,
    get_crm_digest_zoneinfo,
    get_crm_silence_days,
    set_crm_digest_last_sent,
)

log = logging.getLogger(__name__)

def _minimum_salary() -> int:
    """Optional deployment preference; zero means no configured floor."""
    try:
        return max(0, int(os.environ.get("MIN_SALARY_RUB", "0")))
    except ValueError:
        return 0

_RU_MONTHS = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_QUOTE_LIMIT = 700



# ---------------------------------------------------------------------------
# Отфильтрованные: persist why a vacancy never reached the deck
# ---------------------------------------------------------------------------
# Three gates below cut a vacancy before it becomes visible: trap screening,
# the ranker's hard-reject, and the below-preview-threshold cut. Rejections do
# not enter the main deck, but their reason stays available in a browsable bin
# instead (AutomationStore.record_filtered / mvp/routers/filtered.py), built
# only from RankingVerdict/TrapVerdict fields already computed for scoring,
# never invented separately.

def _filtered_reason(gate: str, *, verdict=None, ranking=None) -> str:
    """One Russian line explaining why a vacancy was cut, from verdict fields."""
    if gate == "trap":
        if verdict is None:
            return "trap-скрининг отклонил вакансию"
        return verdict.hard_reject_reason or "; ".join(verdict.flags) or "trap-скрининг отклонил вакансию"
    if gate == "hard_reject":
        if ranking is None:
            return "жёсткий отказ ранжирования"
        return ranking.hard_reject_reason or "жёсткий отказ ранжирования"
    # below_threshold (including the hard_tech-gated case): explain from what
    # is missing and how likely a reply was — the same fields the card shows.
    if ranking is None:
        return "ниже порога предпросмотра"
    parts: list[str] = []
    if ranking.hard_tech_gated:
        parts.append("классическая инженерная роль (алгоритмы/highload/backend) — оценка занижена")
    if ranking.missing_reasons:
        parts.append("; ".join(ranking.missing_reasons[:3]))
    if ranking.reply_chance:
        chance = f"шанс ответа {ranking.reply_chance}"
        if ranking.reply_chance_reason:
            chance += f" ({ranking.reply_chance_reason})"
        parts.append(chance)
    return " · ".join(parts) or "ниже порога предпросмотра"


def _filtered_gate(base_gate: str, ranking=None) -> str:
    """`below_threshold` becomes the more specific `hard_tech` when the cap on
    classic backend/algorithmic work is what pushed the score under the bar.

    Naming the class in the bin is more useful than a generic threshold label.
    """
    if base_gate == "below_threshold" and ranking is not None and ranking.hard_tech_gated:
        return "hard_tech"
    return base_gate


def _record_filtered(user_id: int, vacancy: dict, *, score: int, gate: str, reason: str) -> None:
    """Write one rejection to the bin.

    Never allowed to break the cut itself — a storage hiccup here must not
    stop the pipeline from moving on to the next vacancy. Callers still run
    this off the event loop (`asyncio.to_thread`) since it is blocking sqlite
    I/O, matching every other store call in this module.
    """
    try:
        config.AUTOMATION.record_filtered(
            user_id=user_id,
            vacancy_id=str(vacancy.get("external_id") or vacancy.get("id") or ""),
            vacancy_name=str(vacancy.get("name") or ""),
            employer_name=str((vacancy.get("employer") or {}).get("name") or vacancy.get("employer_name") or ""),
            area=str((vacancy.get("area") or {}).get("name") or vacancy.get("geography") or ""),
            source_url=str(vacancy.get("source_url") or ""),
            score=score, gate=gate, reason=reason,
        )
    except Exception:
        log.exception("Failed to record filtered vacancy '%s'", vacancy.get("name"))


def _ranking_profiles() -> dict[str, dict]:
    """The manifest-declared hh.ru profile contracts for the ranker.

    A profile that fails to load is skipped rather than crashing the cycle — a
    half-edited file must not stop the search.
    """
    profiles: dict[str, dict] = {}
    for key in profile_keys():
        try:
            profiles[key] = load_profile(key)
        except Exception as exc:
            log.warning("Ranking profile %s unavailable: %s", key, type(exc).__name__)
    return profiles


def _prioritize_vacancies(
    vacancies: list[dict], *, profiles: dict[str, dict], user_lists: dict, limit: int,
    include_query_bonus: bool = False,
) -> list[dict]:
    """Rank before truncating so position 68 can beat position 1."""
    ranked: list[tuple[int, str, dict]] = []
    for vacancy in vacancies:
        verdict = evaluate_vacancy(
            vacancy, user_lists=user_lists,
            expected_salary=_minimum_salary(), recent_vacancies=vacancies,
        )
        if verdict.verdict == "reject":
            continue
        ranking, _ = choose_profile(
            vacancy, profiles, trap_verdict=verdict,
            profile_hint=str(vacancy.get("search_profile_hint") or ""),
            salary_floor=_minimum_salary(),
        )
        if not ranking.hard_reject:
            priority = ranking.total + (
                int(vacancy.get("discovery_bonus") or 0) if include_query_bonus else 0
            )
            ranked.append((priority, str(vacancy.get("published_at") or ""), vacancy))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [vacancy for _, _, vacancy in ranked[:max(0, limit)]]


def _select_hydration_candidates(
    vacancies: list[dict], *, profiles: dict[str, dict], user_lists: dict,
    limit: int, reserved_limit: int = 20,
) -> list[dict]:
    """Reserve detail calls for unusual AI titles, then fill by quick rank."""
    reserved = [vacancy for vacancy in vacancies if vacancy.get("reserve_hydration")]
    reserved = reserved[:min(max(0, reserved_limit), max(0, limit))]
    reserved_ids = {
        str(vacancy.get("external_id") or vacancy.get("id") or "")
        for vacancy in reserved
    }
    remainder = [
        vacancy for vacancy in vacancies
        if str(vacancy.get("external_id") or vacancy.get("id") or "") not in reserved_ids
    ]
    ranked = _prioritize_vacancies(
        remainder, profiles=profiles, user_lists=user_lists,
        limit=max(0, limit - len(reserved)), include_query_bonus=True,
    )
    return [*reserved, *ranked]


def _hh_thread_url(*, chat_id: str = "") -> str:
    """Link into one hh.ru conversation, or to the list when we cannot.

    The old form, `/applicant/negotiations/response?topic=<negotiation_id>`,
    answers 404 — verified against live hh.ru. Conversations moved to the chat
    UI, which is addressed by `chat_id`, a different number from the
    negotiation id. Threads synced before that column existed have no chat id
    yet, and for them the honest fallback is the list: a link that lands
    somewhere wrong is worse than one that admits it only gets you close.
    """
    if chat_id:
        return f"https://hh.ru/chat/{chat_id}"
    return "https://hh.ru/applicant/negotiations"


def _hh_vacancy_url(vacancy_id: str) -> str:
    """The vacancy page — always names the employer and links to its profile.

    Worth showing next to the chat link because hh.ru has no company search:
    once a conversation is hard to find, the vacancy page is the only reliable
    way back to who is on the other side.
    """
    return f"https://hh.ru/vacancy/{vacancy_id}" if vacancy_id else ""


def _format_message_date(raw: str) -> str:
    if not raw:
        return "дата неизвестна"
    text = str(raw).strip()
    candidate_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(candidate_text)
    except ValueError:
        return text
    return f"{dt.day} {_RU_MONTHS.get(dt.month, dt.month)} {dt.year}, {dt.strftime('%H:%M')}"


def _format_salary(salary: dict | None) -> str:
    salary = salary or {}
    lo, hi = salary.get("from"), salary.get("to")
    currency = str(salary.get("currency") or "RUR").upper()
    currency_label = {"RUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€", "KZT": "₸"}.get(currency, currency)
    if lo and hi:
        return f"{int(lo):,} – {int(hi):,} {currency_label}".replace(",", " ")
    if lo:
        return f"от {int(lo):,} {currency_label}".replace(",", " ")
    if hi:
        return f"до {int(hi):,} {currency_label}".replace(",", " ")
    return ""


def _extract_links(text: str) -> list[str]:
    """URLs mentioned in a recruiter message, in order, deduplicated."""
    seen: list[str] = []
    for match in _URL_RE.findall(text or ""):
        url = match.rstrip(").,;!?»\"'")
        if url and url not in seen:
            seen.append(url)
    return seen


def _truncate_quote(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= _QUOTE_LIMIT:
        return text
    return text[:_QUOTE_LIMIT].rstrip() + "… [обрезано, полный текст — во «Входящих»]"


def _is_employer_message(message: dict) -> bool:
    author = message.get("author") or {}
    participant = str(
        author.get("participant_type") or author.get("type") or author.get("role") or ""
    ).lower()
    if participant:
        return participant == "employer"
    return str(message.get("author_type") or "").lower() == "employer"


async def _hh_client_for(user_id: int) -> tuple[HHOfficialClient | None, dict | None]:
    """Build a token-fresh HH client for the user's saved OAuth account, or (None, None)."""
    account = config.AUTOMATION.hh_account(user_id)
    if not account:
        return None, None
    client = HHOfficialClient(
        client_id=os.environ.get("HH_CLIENT_ID", ""),
        client_secret=os.environ.get("HH_CLIENT_SECRET", ""),
        access_token=account["access_token"],
    )
    if float(account.get("expires_at", 0)) <= time.time() + 60 and account.get("refresh_token"):
        tokens = await asyncio.to_thread(client.refresh, account["refresh_token"])
        account.update(tokens)
        config.AUTOMATION.save_hh_account(user_id, account, account.get("resumes") or [])
    return client, account


async def _fetch_negotiation_messages(
    client: HHOfficialClient, negotiation_id: str, *, per_page: int = 100,
    fail_closed: bool = False,
) -> list[dict] | None:
    """Fetch a negotiation's message thread, oldest first. HH errors are logged and swallowed."""
    if not negotiation_id:
        return []
    try:
        response = await asyncio.to_thread(
            client._request, "GET",
            f"https://api.hh.ru/negotiations/{negotiation_id}/messages?page=0&per_page={per_page}",
        )
    except HHOfficialError as exc:
        log.warning("HH negotiation messages fetch failed safely: %s", exc.reason)
        return None if fail_closed else []
    items = response.get("items") or []
    return sorted(items, key=lambda item: str(item.get("created_at") or ""))


def _last_recruiter_message(messages: list[dict]) -> dict | None:
    employer_messages = [m for m in messages if _is_employer_message(m)]
    if employer_messages:
        return employer_messages[-1]
    return messages[-1] if messages else None


def _event_label(hh_state: str, old_outcome: str, new_outcome: str) -> str:
    """Event word for the push card: never the old merged "приглашение/ответ"."""
    if hh_state == "invitation":
        return "Приглашение"
    if hh_state == "discard":
        return "Отказ"
    if old_outcome not in {"replied", "interview", "offer"} and new_outcome in {"replied", "interview", "offer"}:
        return "Ответ"
    return "Новое сообщение"


def _master_profile_label(payload: dict) -> str:
    master = payload.get("master_profile") or {}
    return str(master.get("name") or master.get("key") or "не определён")


def _build_notification_card(
    *, label: str, employer_name: str, vacancy_title: str, city: str, salary: str,
    message: dict | None, profile_label: str,
) -> str:
    meta_bits = [x for x in (city, salary) if x]
    meta_line = f"📍 {html.escape(' · '.join(meta_bits))}\n" if meta_bits else ""
    message_text = str((message or {}).get("text") or "").strip()
    quote = html.escape(_truncate_quote(message_text)) if message_text else "текст сообщения недоступен — откройте hh.ru"
    date_text = _format_message_date(str((message or {}).get("created_at") or ""))
    links = _extract_links(message_text)
    links_block = ""
    if links:
        rows = "\n".join(f"🔗 {html.escape(link)}" for link in links)
        links_block = f"\n\n<b>Ссылка на следующий шаг:</b>\n{rows}"
    return (
        f"📨 <b>{html.escape(label)}</b>\n\n"
        f"🏢 <b>{html.escape(employer_name or 'Работодатель не указан')}</b>\n"
        f"💼 {html.escape(vacancy_title or 'Вакансия')}\n"
        f"{meta_line}"
        f"👤 Профиль: {html.escape(profile_label)}\n\n"
        f"✉️ <b>Сообщение · {html.escape(date_text)}:</b>\n«{quote}»"
        f"{links_block}\n\n"
        "Отметьте фактический этап или ответьте прямо здесь — без ухода из Telegram."
    )


def _notification_keyboard(variant_id: str, negotiation_id: str, chat_id: str = "") -> InlineKeyboardMarkup:
    if not variant_id:
        return _untracked_notification_keyboard(negotiation_id, chat_id)
    keyboard = _outcome_keyboard(variant_id)
    keyboard.inline_keyboard.append(
        [InlineKeyboardButton(text="🔗 Открыть на hh.ru", url=_hh_thread_url(chat_id=chat_id))]
    )
    keyboard.inline_keyboard.append(
        [InlineKeyboardButton(text="💬 Ответить в Telegram", callback_data=f"mvp:crm:reply:{negotiation_id}")]
    )
    return keyboard


def _untracked_notification_keyboard(negotiation_id: str, chat_id: str = "") -> InlineKeyboardMarkup:
    """Keyboard for a thread the bot never created — no variant to mark an outcome on.

    ``_outcome_keyboard`` builds ``mvp:outcome:<outcome>:<variant_id>`` callbacks;
    with an empty variant id every one of them would answer "отклик не найден".
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Открыть на hh.ru", url=_hh_thread_url(chat_id=chat_id))],
        [InlineKeyboardButton(text="💬 Ответить в Telegram", callback_data=f"mvp:crm:reply:{negotiation_id}")],
    ])


async def _try_auto_apply(
    user_id: int, payload: dict, *, manual_confirmed: bool = False,
) -> tuple[str, str]:
    # The absolute lock belongs to unattended writes. A manual application has
    # its own owner confirmation, immutable approval digest and dedup guards.
    if not manual_confirmed and os.environ.get("HH_APPLICATION_WRITES_ENABLED", "0") != "1":
        return "not_eligible", "application_writes_disabled"
    settings = config.AUTOMATION.settings(user_id)
    # The two safety gates exist to stop the scheduler from applying on its
    # own. They used to be checked for the owner's own button press too, which
    # made "отклики только по моей кнопке" impossible: with
    # AUTO_APPLY_ARMED=0 the manual card button returned production_gate_disabled
    # and nothing could ever be sent. An explicit press is the confirmation the
    # gates were protecting, so the manual path skips exactly these two checks
    # and keeps every other one — approval row, package sha, resume mapping,
    # quality checklist, daily limit, "vacancy unchanged since preview" and the
    # the already-applied guard all still
    # apply below. The daily limit belongs to the scheduler too: every manual
    # application is already a separate owner-confirmed action.
    if not manual_confirmed:
        if os.environ.get("AUTO_APPLY_ARMED", "0") != "1":
            return "not_eligible", "production_gate_disabled"
        if not settings["auto_apply_enabled"] or payload["score"]["value"] < settings["min_apply_score"]:
            return "not_eligible", "score_or_setting"
    expected_status = "approved" if manual_confirmed else "draft"
    if payload.get("status") != expected_status:
        return "not_eligible", f"status_must_be_{expected_status}"
    account = config.AUTOMATION.hh_account(user_id)
    if not account:
        return "oauth_required", "hh_not_connected"
    ready, _ = _quality_checklist(payload)
    if not ready:
        if not manual_confirmed:
            config.STORE.set_status(payload["id"], "manual_required", ("draft",))
        return "manual_required", "quality_checklist_failed"
    if manual_confirmed:
        approval = config.AUTOMATION.approval(user_id, payload["id"])
        if (
            not approval or approval["consumed_at"]
            or approval["payload_sha256"] != _payload_digest(payload)
        ):
            return "not_eligible", "approval_missing_or_stale"
        mapping = config.AUTOMATION.resume_mapping(user_id, payload["master_profile"]["key"])
        if not mapping:
            return "manual_required", "stable_resume_mapping_required"
        if approval["resume_id"] != mapping["resume_id"]:
            return "not_eligible", "approved_resume_mapping_changed"
    if not manual_confirmed and config.AUTOMATION.applications_today(user_id) >= settings["daily_limit"]:
        return "limit_reached", "daily_limit"
    vacancy = payload.get("vacancy") or {}
    if vacancy.get("source") != "hh":
        return "manual_required", "source_not_supported"
    vacancy_id = _hh_vacancy_id(vacancy)
    if not vacancy_id:
        return "manual_required", "hh_vacancy_id_missing"

    client = HHOfficialClient(
        client_id=os.environ.get("HH_CLIENT_ID", ""),
        client_secret=os.environ.get("HH_CLIENT_SECRET", ""),
        access_token=account["access_token"],
    )
    try:
        if float(account.get("expires_at", 0)) <= time.time() + 60 and account.get("refresh_token"):
            tokens = await asyncio.to_thread(client.refresh, account["refresh_token"])
            account.update(tokens)
        detail = await asyncio.to_thread(client.vacancy, vacancy_id)
        resumes = await asyncio.to_thread(client.resumes)
        account["profile"] = account.get("profile", {})
        config.AUTOMATION.save_hh_account(user_id, account, resumes)
    except HHOfficialError as exc:
        # A posting hh.ru no longer serves is not an authentication problem, and the card
        # must not go back to the queue: the deck would offer the same dead vacancy again,
        # and every press would end in the same opaque refusal.
        if exc.status == 404 or exc.reason in {"not_found", "archived"}:
            config.STORE.set_status(payload["id"], "failed", ("draft", "approved"))
            return "failed", "vacancy_gone"
        return "auth_failed", exc.reason

    live_vacancy = vacancy_from_api(detail)
    approved_review = vacancy_review(payload)
    live_review = analyze_vacancy_description(live_vacancy)
    if approved_review.get("description_sha256") != live_review.get("description_sha256"):
        config.STORE.set_status(payload["id"], "manual_required", ("draft", "approved"))
        return "manual_required", "vacancy_changed_after_preview"

    relations = detail.get("relations") or []
    relation_names = {
        str(item.get("name") or item.get("id") or "") if isinstance(item, dict) else str(item)
        for item in relations
    }
    if relation_names & {"got_response", "response", "already_applied"}:
        config.STORE.set_status(payload["id"], "already_applied", ("draft",))
        return "already_applied", "already_applied"
    if detail.get("test"):
        config.STORE.set_status(payload["id"], "manual_required", ("draft",))
        return "manual_required", "test_required"
    # Applying is deliberately read-only with respect to resumes. Creating or
    # changing a stable HH copy is a separate, explicitly confirmed operation.
    mapping = config.AUTOMATION.resume_mapping(user_id, payload["master_profile"]["key"])
    resume_id = str((mapping or {}).get("resume_id") or "")
    live_resume_ids = {str(item.get("id") or "") for item in resumes}
    if not resume_id:
        return "manual_required", "stable_resume_mapping_required"
    if resume_id not in live_resume_ids:
        return "manual_required", "stable_resume_mapping_unavailable"
    if config.AUTOMATION.application_exists(user_id, vacancy_id, resume_id):
        return "already_applied", "local_pair_dedup"
    try:
        await asyncio.to_thread(
            client.apply,
            vacancy_id=vacancy_id,
            resume_id=resume_id,
            message=payload["generated_variant"]["cover_letter"],
        )
        status, reason = "applied", "api_success"
    except HHOfficialError as exc:
        status = "already_applied" if exc.reason == "already_applied" else "manual_required"
        reason = exc.reason
    negotiation = None
    if status == "applied":
        try:
            negotiation = await asyncio.to_thread(
                client.find_negotiation, vacancy_id=vacancy_id, resume_id=resume_id,
            )
        except HHOfficialError:
            log.warning("HH application succeeded; negotiation lookup will retry during sync")
    config.AUTOMATION.record_application(
        user_id=user_id, vacancy_id=vacancy_id, resume_id=resume_id,
        variant_id=payload["id"], status=status, reason=reason, negotiation=negotiation,
    )
    if status == "applied":
        config.STORE.set_status(payload["id"], "applied", (expected_status,))
        if manual_confirmed:
            config.AUTOMATION.consume_approval(user_id, payload["id"])
    elif status == "manual_required":
        config.STORE.set_status(payload["id"], "manual_required", (expected_status,))
    return status, reason


async def _apply_backlog(bot: Bot, user_id: int) -> int:
    if not config.AUTOMATION.hh_account(user_id):
        return 0
    applied = 0
    for payload in reversed(config.STORE.list_recent(user_id, 40)):
        if payload.get("status") != "draft" or payload.get("vacancy", {}).get("source") != "hh":
            continue
        status, reason = await _try_auto_apply(user_id, payload)
        if status == "applied":
            applied += 1
            await _safe_send(
                bot, user_id,
                f"✅ Автоотклик отправлен: {html.escape(payload['vacancy'].get('name', 'Вакансия'))}",
                reply_markup=_preview_keyboard(payload["id"], payload["vacancy"].get("source_url", ""), payload),
            )
        elif status == "manual_required":
            await _safe_send(
                bot, user_id,
                f"⚠️ Нужна ручная проверка: {html.escape(payload['vacancy'].get('name', 'Вакансия'))} · {html.escape(reason)}",
            )
    return applied


#: How many pages of /negotiations to walk per sync. hh.ru pages 20 threads at
#: a time, so this covers 100 active conversations — far more than a single
#: owner accumulates, while still bounding a runaway account.
_NEGOTIATION_PAGE_LIMIT = 5


async def _notify_tracked(
    bot: Bot, user_id: int, client: HHOfficialClient, application: dict, item: dict,
) -> None:
    """Push for a thread this bot created: full card, outcome buttons, variant link."""
    old_outcome = str(application.get("outcome_status") or "")
    synced = config.AUTOMATION.sync_application(application["id"], item)
    negotiation_id = str(item.get("id") or "")
    messages = await _fetch_negotiation_messages(client, negotiation_id) if synced["notify"] else []
    last_message = _last_recruiter_message(messages)
    config.AUTOMATION.watch_negotiation(
        user_id, item, str((last_message or {}).get("text") or ""),
    )
    if not synced["notify"]:
        return
    payload = config.STORE.get(application["variant_id"]) or {}
    live_vacancy = item.get("vacancy") or {}
    stored_vacancy = payload.get("vacancy") or {}
    employer_name = (
        str((live_vacancy.get("employer") or {}).get("name") or "")
        or str(stored_vacancy.get("employer_name") or "")
    )
    vacancy_title = str(live_vacancy.get("name") or "") or str(stored_vacancy.get("name") or "")
    city = (
        str((live_vacancy.get("area") or {}).get("name") or "")
        or str(stored_vacancy.get("geography") or "")
    )
    text = _build_notification_card(
        label=_event_label(synced["hh_state"], old_outcome, synced["outcome_status"]),
        employer_name=employer_name, vacancy_title=vacancy_title, city=city,
        salary=_format_salary(live_vacancy.get("salary") or stored_vacancy.get("salary")),
        message=last_message, profile_label=_master_profile_label(payload),
    )
    sent = await _safe_send(
        bot, user_id, text, parse_mode="HTML",
        reply_markup=_notification_keyboard(application["variant_id"], negotiation_id),
    )
    if sent is not None:
        config.AUTOMATION.mark_application_notified(application["id"], synced["notification_key"])
        config.AUTOMATION.mark_negotiation_notified(
            user_id, negotiation_id, synced["notification_key"],
        )


async def _notify_untracked(
    bot: Bot, user_id: int, client: HHOfficialClient, item: dict,
) -> None:
    """Push for a thread the owner started on hh.ru by hand.

    There is no variant and no cover letter to show, so the card carries what
    hh.ru knows plus the recruiter's own words, and the keyboard offers the two
    actions that make sense: open the thread, or answer from Telegram.
    """
    negotiation_id = str(item.get("id") or "")
    probe = config.AUTOMATION.watch_negotiation(user_id, item)
    if not probe["notify"]:
        return
    messages = await _fetch_negotiation_messages(client, negotiation_id)
    last_message = _last_recruiter_message(messages)
    config.AUTOMATION.watch_negotiation(user_id, item, str((last_message or {}).get("text") or ""))
    vacancy = item.get("vacancy") or {}
    state = str((item.get("state") or {}).get("id") or "")
    text = _build_notification_card(
        label=_event_label(state, "", ""),
        employer_name=str((vacancy.get("employer") or {}).get("name") or ""),
        vacancy_title=str(vacancy.get("name") or ""),
        city=str((vacancy.get("area") or {}).get("name") or ""),
        salary=_format_salary(vacancy.get("salary")),
        message=last_message,
        profile_label="отклик сделан вручную на hh.ru",
    )
    sent = await _safe_send(
        bot, user_id, text, parse_mode="HTML",
        reply_markup=_untracked_notification_keyboard(negotiation_id, str(item.get("chat_id") or "")),
    )
    if sent is not None:
        config.AUTOMATION.mark_negotiation_notified(
            user_id, negotiation_id, probe["notification_key"],
        )


async def _sync_negotiations(bot: Bot, user_id: int) -> int:
    """Push every recruiter update on hh.ru to Telegram, not just the bot's own.

    Every negotiation is registered in ``negotiation_watch``. A thread created
    by the bot keeps its richer application card; an unknown one still gets a
    push with the recruiter's text and a reply button.
    """
    tracked = config.AUTOMATION.tracked_applications(user_id)
    account = config.AUTOMATION.hh_account(user_id)
    if not account:
        return 0
    client = HHOfficialClient(
        client_id=os.environ.get("HH_CLIENT_ID", ""),
        client_secret=os.environ.get("HH_CLIENT_SECRET", ""),
        access_token=account["access_token"],
    )
    try:
        if float(account.get("expires_at", 0)) <= time.time() + 60 and account.get("refresh_token"):
            tokens = await asyncio.to_thread(client.refresh, account["refresh_token"])
            account.update(tokens)
            config.AUTOMATION.save_hh_account(user_id, account, account.get("resumes") or [])
        by_pair = {(x["vacancy_id"], x["resume_id"]): x for x in tracked}
        # First walk of this account: register the archive as the baseline and
        # stay silent. Otherwise every old rejection
        # ago arrives as a fresh push, all at once.
        seeding = not config.AUTOMATION.has_negotiation_baseline(user_id)
        seen = 0
        pages = 1
        for page in range(_NEGOTIATION_PAGE_LIMIT):
            response = await asyncio.to_thread(client.negotiations, page=page)
            pages = int(response.get("pages") or 0)
            for item in response.get("items", []):
                seen += 1
                negotiation_id = str(item.get("id") or "")
                if not negotiation_id:
                    continue
                # The negotiation list is the only place hh.ru hands us the chat
                # id, and it is what the web UI addresses conversations by, so
                # capture it here even while seeding.
                config.AUTOMATION.set_thread_chat_id(
                    user_id, negotiation_id, str(item.get("chat_id") or ""),
                )
                if seeding:
                    probe = config.AUTOMATION.watch_negotiation(user_id, item)
                    config.AUTOMATION.mark_negotiation_notified(
                        user_id, negotiation_id, probe["notification_key"],
                    )
                    continue
                key = (
                    str((item.get("vacancy") or {}).get("id") or ""),
                    str((item.get("resume") or {}).get("id") or ""),
                )
                application = by_pair.get(key)
                if application is not None:
                    await _notify_tracked(bot, user_id, client, application, item)
                else:
                    await _notify_untracked(bot, user_id, client, item)
            if page + 1 >= pages:
                break
        if seeding and seen:
            await _safe_send(
                bot, user_id,
                f"📥 Синхронизировал {seen} переписок с hh.ru. Прошлые сообщения не пересылаю — "
                "они в «Входящие». Дальше буду присылать только новую активность рекрутёров.",
            )
        return seen
    except HHOfficialError as exc:
        log.warning("HH negotiation sync failed safely: %s", exc.reason)
        return 0


#: Per-cycle cap on how many negotiation threads get their message history
#: synced. hh.ru rate limits matter and a live account can carry 200+ threads;
#: pulling every thread's messages every cycle would hammer the API on a cold
#: start. The remainder is picked up on the next _inbox_loop pass, so a full
#: backlog backfills gradually across several cycles instead of all at once.
_MESSAGE_SYNC_BATCH_LIMIT = 15

_ARCHIVE_REASON_DORMANT = "dormant"
#: Distinct from "dormant" so the archive screen can tell the two apart: this
#: one was filed away despite the user still owing an answer, or despite being
#: a rejection, purely because of age.
_ARCHIVE_REASON_STALE = "stale"


async def _sync_thread_messages(user_id: int) -> int:
    """Pull message history for threads whose local copy is stale or missing.

    ``_sync_negotiations`` only reads the negotiation list (state /
    has_updates / updated_at) -- it never fetches message bodies. This is
    what actually populates ``negotiation_messages`` / ``crm_thread_state``,
    which the CRM screen and the follow-up ladder read locally instead of
    hitting hh.ru live on every check.
    """
    client, _account = await _hh_client_for(user_id)
    if client is None:
        return 0
    threads = config.AUTOMATION.threads_needing_message_sync(
        user_id, limit=_MESSAGE_SYNC_BATCH_LIMIT,
    )
    synced = 0
    for thread in threads:
        negotiation_id = str(thread.get("negotiation_id") or "")
        if not negotiation_id:
            continue
        try:
            messages = await _fetch_negotiation_messages(client, negotiation_id, fail_closed=True)
            if messages is None:
                # Fetch failed for this one thread; already logged inside
                # _fetch_negotiation_messages. Skip it, the rest of the batch
                # (and this thread again next cycle) must not be affected.
                continue
            if messages:
                config.AUTOMATION.store_negotiation_messages(user_id, negotiation_id, messages)
            # threads_needing_message_sync() fills unknown metadata with "" (a
            # table default), not a genuine value -- turn that into None so
            # recompute_thread_state never clobbers a name/state learned on
            # an earlier, better-informed cycle.
            config.AUTOMATION.recompute_thread_state(
                user_id, negotiation_id,
                vacancy_id=thread.get("vacancy_id") or None,
                vacancy_name=thread.get("vacancy_name") or None,
                employer_name=thread.get("employer_name") or None,
                hh_state=thread.get("hh_state") or None,
            )
            synced += 1
        except Exception:
            log.exception("Message sync failed for negotiation %s", negotiation_id)
    return synced


async def _auto_archive_threads(user_id: int) -> int:
    """Archive threads that stopped being work, on two separate cutoffs.

    The near cutoff (`crm_archive_after_days`, user-configurable) only touches
    threads where the employer owes the next step. Anything the user still has
    to answer, and every rejection, stays on screen -- hiding your own todo, or
    a fresh "no", would be hiding the truth.

    The far cutoff (`config.CRM_STALE_AFTER_DAYS`) overrides that, because past
    a season those two guards stop protecting anything and just accumulate: a
    reply owed since spring is not a task, and a rejection from spring is not
    news. Archived, not deleted -- the archive screen still lists them, and the
    funnel reads `auto_apply_log` / `application_events`, so no number moves.
    """
    archive_after_days = get_crm_archive_after_days(user_id)
    archived = 0
    for thread in config.AUTOMATION.crm_threads(user_id, include_archived=False):
        negotiation_id = str(thread.get("negotiation_id") or "")
        if not negotiation_id:
            continue
        days_silent = int(thread.get("days_silent") or 0)
        stale = days_silent >= config.CRM_STALE_AFTER_DAYS
        if not stale:
            if thread.get("awaiting_side") == "me":
                continue
            if str(thread.get("hh_state") or "") == "discard":
                continue
            if days_silent < archive_after_days:
                continue
        reason = _ARCHIVE_REASON_STALE if stale else _ARCHIVE_REASON_DORMANT
        try:
            config.AUTOMATION.archive_thread(user_id, negotiation_id, reason)
            archived += 1
        except Exception:
            log.exception("Auto-archive failed for negotiation %s", negotiation_id)
    return archived


def _crm_digest_text(counters: dict, *, silence_days: int) -> str:
    return (
        f"🗂 Ответить: {counters['reply']} · выполнить: {counters['task']} · "
        f"ждём: {counters['waiting']} ({counters['silent']} дольше {silence_days} дн.) · "
        f"отказы: {counters['rejected']}"
    )


def _crm_digest_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть CRM", callback_data="mvp:crm")],
    ])


async def _run_crm_digest_cycle(bot: Bot, user_id: int) -> None:
    """Send the once-a-day CRM summary if it is due and has not already gone
    out today (configured local calendar day).

    Idempotent across restarts: the last-sent date is persisted in
    ``mvp_settings.json`` via ``settings_store``, so a restart never
    double-sends, and a bot that was down past the digest time still catches
    up once -- the same day, never a retro-fire for a day already covered.
    Today's slot counts as "handled" whether or not a message actually went
    out, so a quiet day with nothing to report is not re-evaluated all day.
    """
    if not get_crm_digest_enabled(user_id):
        return
    tz = get_crm_digest_zoneinfo()
    now_local = datetime.now(tz)
    today = now_local.date().isoformat()
    if get_crm_digest_last_sent(user_id) == today:
        return
    try:
        hour_str, minute_str = get_crm_digest_time(user_id).split(":")
        due_at = now_local.replace(hour=int(hour_str), minute=int(minute_str), second=0, microsecond=0)
    except ValueError:
        return
    if now_local < due_at:
        return
    silence_days = get_crm_silence_days(user_id)
    counters = config.AUTOMATION.crm_counters(user_id, silence_days=silence_days)
    if not (counters["reply"] or counters["task"] or counters["scheduled"] or counters["waiting"]):
        set_crm_digest_last_sent(user_id, today)
        return
    sent = await _safe_send(
        bot, user_id, _crm_digest_text(counters, silence_days=silence_days),
        reply_markup=_crm_digest_keyboard(),
    )
    if sent is not None:
        set_crm_digest_last_sent(user_id, today)


async def _run_automation_cycle(bot: Bot, *, notify_empty: bool = False) -> None:
    async with config.AUTOMATION_LOCK:
        if config.ADAPTER is None:
            return
        owner_raw = os.environ.get("MVP_OWNER_TELEGRAM_ID", "")
        if not owner_raw.isdigit():
            return
        user_id = int(owner_raw)
        # Negotiation sync lives in _inbox_loop now — it must not be hostage to
        # the search cadence or to search_enabled.
        settings = config.AUTOMATION.settings(user_id)
        if not settings["search_enabled"]:
            return
        backlog_applied = await _apply_backlog(bot, user_id) if settings["auto_apply_enabled"] else 0
        remaining_applications = max(0, settings["daily_limit"] - config.AUTOMATION.applications_today(user_id))
        if settings["auto_apply_enabled"] and remaining_applications == 0:
            if notify_empty:
                await bot.send_message(user_id, "Лимит успешных откликов на сегодня достигнут.")
            return
        max_cycle = int(os.environ.get("AUTO_SEARCH_MAX_PER_CYCLE", "3"))
        if settings["auto_apply_enabled"]:
            max_cycle = min(max_cycle, remaining_applications)
        known = config.AUTOMATION.known_hh_ids(user_id)
        user_lists = await asyncio.to_thread(load_user_lists, user_id)
        profiles_for_ranking = await asyncio.to_thread(_ranking_profiles)
        account = config.AUTOMATION.hh_account(user_id)
        if account:
            search_client = HHOfficialClient(
                client_id=os.environ.get("HH_CLIENT_ID", ""),
                client_secret=os.environ.get("HH_CLIENT_SECRET", ""),
                access_token=account["access_token"],
            )
            # Ask hh.ru to filter by salary, format and employment instead of
            # pulling everything and paying a model to discard it, and sort by
            # relevance rather than by publication time.
            mapped = await asyncio.to_thread(mapped_profile_keys, user_id)
            narrow = await asyncio.to_thread(
                search_hh_candidates, search_client, _search_queries(mapped_profiles=mapped),
                known_ids=known, per_page=20,
            )
            fresh = await asyncio.to_thread(
                search_hh_candidates, search_client, _fresh_ai_queries(),
                known_ids=known, per_page=100,
                max_pages=max(1, int(os.environ.get("AUTO_SEARCH_FRESH_PAGES", "2"))),
            )
            candidates_by_id: dict[str, dict] = {}
            for vacancy in [*narrow, *fresh]:
                vacancy_id = str(vacancy.get("external_id") or vacancy["id"])
                existing = candidates_by_id.setdefault(vacancy_id, vacancy)
                if vacancy.get("reserve_hydration"):
                    existing["reserve_hydration"] = True
                    existing["discovery_bonus"] = max(
                        int(existing.get("discovery_bonus") or 0),
                        int(vacancy.get("discovery_bonus") or 0),
                    )
            hydrate_limit = max(
                max_cycle, int(os.environ.get("AUTO_SEARCH_HYDRATE_LIMIT", "30")),
            )
            prioritised = await asyncio.to_thread(
                _select_hydration_candidates, list(candidates_by_id.values()),
                profiles=profiles_for_ranking, user_lists=user_lists, limit=hydrate_limit,
                reserved_limit=max(0, int(os.environ.get("AUTO_SEARCH_RESERVED_HYDRATE", "20"))),
            )
            hydrated = await asyncio.to_thread(
                hydrate_hh_vacancies, search_client, prioritised, max_items=hydrate_limit,
            )
            vacancies = await asyncio.to_thread(
                _prioritize_vacancies, hydrated,
                profiles=profiles_for_ranking, user_lists=user_lists, limit=max_cycle,
            )
        else:
            vacancies = await asyncio.to_thread(
                discover_vacancies, [item["text"] for item in _search_queries()],
                known_ids=known, max_items=max_cycle,
            )
        prepared = manual = trap_rejected = 0
        applied = backlog_applied
        for vacancy in vacancies:
            try:
                verdict = await asyncio.to_thread(
                    evaluate_vacancy, vacancy, user_lists=user_lists,
                    expected_salary=_minimum_salary(), recent_vacancies=vacancies,
                )
                if verdict.verdict == "reject":
                    trap_rejected += 1
                    reason = verdict.hard_reject_reason or "; ".join(verdict.flags)
                    log.info("Trap screening rejected vacancy '%s': %s", vacancy.get("name"), reason)
                    await asyncio.to_thread(
                        _record_filtered, user_id, vacancy, score=0, gate="trap",
                        reason=_filtered_reason("trap", verdict=verdict),
                    )
                    continue
                # Deterministic ranking decides the profile and the score backbone;
                # the model may only nudge it. Doing this before generation means a
                # profile with no linked hh.ru resume never burns a model call — that
                # otherwise an unusable variant could still consume a model call.
                ranking, all_rankings = await asyncio.to_thread(
                    choose_profile, vacancy, profiles_for_ranking,
                    trap_verdict=verdict,
                    profile_hint=str(vacancy.get("search_profile_hint") or ""),
                    salary_floor=_minimum_salary(),
                )
                if ranking.hard_reject:
                    trap_rejected += 1
                    log.info(
                        "Ranking rejected vacancy '%s': %s",
                        vacancy.get("name"), ranking.hard_reject_reason,
                    )
                    await asyncio.to_thread(
                        _record_filtered, user_id, vacancy, score=ranking.total, gate="hard_reject",
                        reason=_filtered_reason("hard_reject", ranking=ranking),
                    )
                    continue
                ranked_alternatives = tuple(
                    key for key, _ in sorted(all_rankings.items(), key=lambda item: -item[1].total)
                    if key != ranking.profile_key
                )
                applicability = await asyncio.to_thread(
                    profile_applicability, user_id, ranking.profile_key,
                    ranked_alternatives=ranked_alternatives,
                )
                if applicability.action == "manual_required":
                    manual += 1
                    await _safe_send(
                        bot, user_id,
                        "🔗 Вакансия подходит, но откликнуться нечем: "
                        f"{html.escape(applicability.reason)}\n\n"
                        f"{html.escape(str(vacancy.get('name') or 'Вакансия'))}\n"
                        "Привяжите резюме в разделе «Резюме».",
                    )
                    continue
                forced_profile = (
                    applicability.effective_profile_key
                    if applicability.action == "fallback" else ranking.profile_key
                )
                _apply_owner_letter_style(user_id)
                variant = await asyncio.to_thread(
                    _generate_with_failover, vacancy, forced_profile,
                )
                master = config.ADAPTER.data.profiles[variant.profile_key]
                payload = variant.as_payload(
                    user_id=user_id, vacancy=vacancy,
                    data_version=config.ADAPTER.data.version, master=master,
                )
                payload = await _enrich_payload_from_hh(user_id, payload)
                if verdict.verdict == "warn" and verdict.flags:
                    fit_warnings = payload.setdefault("resume_analysis", {}).setdefault("fit_warnings", [])
                    for flag in verdict.flags:
                        marked = f"Trap-скрининг: {flag}"
                        if marked not in fit_warnings:
                            fit_warnings.append(marked)
                final = await asyncio.to_thread(
                    rank_vacancy, vacancy, profiles_for_ranking[forced_profile],
                    profile_key=forced_profile, trap_verdict=verdict,
                    profile_hint=str(vacancy.get("search_profile_hint") or ""),
                    salary_floor=_minimum_salary(),
                )
                # The preview and the stored journal both read score["reasons"];
                # the ranker calls them `pros`. Keep the contract intact, and fall
                # back to the model's reasons when the factors produced none.
                score_dict = final.as_dict()
                score_dict["reasons"] = list(final.pros) or list(payload.get("score", {}).get("reasons") or [])
                payload["score"] = score_dict
                payload.setdefault("analysis_internal", {})["ranking"] = score_dict
                if final.total < settings["min_preview_score"]:
                    payload["status"] = "skipped"
                    await asyncio.to_thread(config.STORE.save, payload)
                    gate = _filtered_gate("below_threshold", final)
                    await asyncio.to_thread(
                        _record_filtered, user_id, vacancy, score=final.total, gate=gate,
                        reason=_filtered_reason(gate, ranking=final),
                    )
                    continue
                await asyncio.to_thread(config.STORE.save, payload)
                prepared += 1
                status, reason = (
                    await _try_auto_apply(user_id, payload)
                    if settings["auto_apply_enabled"] else ("not_eligible", "auto_apply_disabled")
                )
                applied += int(status == "applied")
                manual += int(status in {"manual_required", "auth_failed"})
                prefix = "✅ Отклик отправлен автоматически\n\n" if status == "applied" else "🤖 Найдена новая вакансия\n\n"
                await _safe_send(
                    bot,
                    user_id, prefix + _preview_text(payload), parse_mode="HTML",
                    reply_markup=_preview_keyboard(variant.id, vacancy.get("source_url", ""), payload),
                )
                if status == "manual_required":
                    await _safe_send(bot, user_id, f"⚠️ Автоотклик остановлен для этой вакансии: {html.escape(reason)}")
            except Exception:
                log.exception("Automated vacancy processing failed")
        if notify_empty or vacancies:
            oauth_note = "" if config.AUTOMATION.hh_account(user_id) else " Для реальных откликов один раз подключите HH OAuth."
            trap_note = f", отклонено trap-скринингом {trap_rejected}" if trap_rejected else ""
            await _safe_send(
                bot,
                user_id,
                f"Автопоиск завершён: новых {len(vacancies)}, подготовлено {prepared}, отправлено {applied}, "
                f"ручных случаев {manual}{trap_note}.{oauth_note}",
            )


async def _automation_loop(bot: Bot) -> None:
    await asyncio.sleep(15)
    while True:
        try:
            await _run_automation_cycle(bot)
        except Exception:
            log.exception("Automation cycle failed")
        await asyncio.sleep(int(os.environ.get("AUTO_SEARCH_INTERVAL_SECONDS", "1200")))


async def _inbox_loop(bot: Bot) -> None:
    """Recruiter mail on its own fast cadence.

    Negotiation sync used to ride along with the 20-minute search cycle, so an
    invitation could sit unseen for twenty minutes and, if ``search_enabled``
    was ever turned off, forever. Reading ``/negotiations`` is cheap next to a
    search-and-generate pass, so it gets its own loop and its own interval, and
    it deliberately ignores every automation setting — mail is not applying.

    Three passes per cycle, in dependency order: the negotiation list (state /
    has_updates), then message bodies for the threads that need them (bounded
    by ``_MESSAGE_SYNC_BATCH_LIMIT``), then auto-archive, which reads the
    ``crm_thread_state`` the message pass just refreshed.
    """
    await asyncio.sleep(20)
    interval = max(60, int(os.environ.get("INBOX_SYNC_INTERVAL_SECONDS", "180")))
    while True:
        try:
            owner_raw = os.environ.get("MVP_OWNER_TELEGRAM_ID", "")
            if owner_raw.isdigit():
                user_id = int(owner_raw)
                await _sync_negotiations(bot, user_id)
                await _sync_thread_messages(user_id)
                await _auto_archive_threads(user_id)
        except Exception:
            log.exception("Inbox sync cycle failed")
        await asyncio.sleep(interval)


async def _crm_digest_loop(bot: Bot) -> None:
    """Once-a-day CRM summary, checked on its own short cadence.

    The check itself is cheap (a couple of local reads plus, at most, one
    Telegram send), so it polls often enough to catch the configured digest
    time promptly and to recover quickly if the process was down for it.
    """
    await asyncio.sleep(25)
    interval = max(30, int(os.environ.get("CRM_DIGEST_CHECK_INTERVAL_SECONDS", "60")))
    while True:
        try:
            owner_raw = os.environ.get("MVP_OWNER_TELEGRAM_ID", "")
            if owner_raw.isdigit():
                await _run_crm_digest_cycle(bot, int(owner_raw))
        except Exception:
            log.exception("CRM digest cycle failed")
        await asyncio.sleep(interval)
