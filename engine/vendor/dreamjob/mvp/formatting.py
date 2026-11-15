"""Text rendering (preview/checklist/history) and the safe-send helper."""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re

from aiogram import Bot

from candidate.data import load_candidate_data
from candidate.service import analyze_vacancy_description

from mvp import config

log = logging.getLogger(__name__)



def resume_analysis(payload: dict) -> dict:
    """Return the vacancy analysis, wherever the generator happened to put it.

    ``candidate/service.py::as_payload`` nests it under ``analysis_internal.resume``,
    Older payloads may carry a top-level ``resume_analysis`` key. Read both and
    merge them because trap screening appends its flags to the flat key.
    """
    nested = (payload.get("analysis_internal") or {}).get("resume") or {}
    flat = payload.get("resume_analysis") or {}
    if not flat:
        return nested
    if not nested:
        return flat
    merged = dict(nested)
    for key, value in flat.items():
        current = merged.get(key)
        if isinstance(value, list) and isinstance(current, list):
            merged[key] = current + [item for item in value if item not in current]
        else:
            merged[key] = value
    return merged

def vacancy_review(payload: dict) -> dict:
    """The stored description analysis, recomputed from the payload when it is missing.

    Packages written before the analysis existed carry the full vacancy description but
    no review, so the two description checks failed on them forever with nothing the
    owner could press to fix it — a package cannot grow a review it was not born with.
    The analysis is a pure function of the description already stored in the payload, so
    computing it now answers the same question the checks ask: was the whole description
    in hand when the package was built.
    """
    stored = resume_analysis(payload).get("vacancy_review") or {}
    if stored.get("description_sha256"):
        return stored
    return analyze_vacancy_description(payload.get("vacancy") or {})


def _knowledge_summary() -> str:
    data = config.ADAPTER.data if config.ADAPTER else load_candidate_data()
    counts = data.status_counts()
    categories = sorted({f.category for f in data.facts if f.category})
    return (
        "👤 <b>Candidate Knowledge Base v1</b>\n\n"
        f"✅ public_confirmed: {counts.get('public_confirmed', 0)}\n"
        f"🔒 internal: {counts.get('internal', 0)}\n"
        f"❓ needs_confirmation: {counts.get('needs_confirmation', 0)}\n"
        f"⛔ do_not_publish: {counts.get('do_not_publish', 0)}\n\n"
        f"Категорий: {len(categories)}; master-профилей: {len(data.profiles)}.\n"
        "Поиск: <code>/facts управление</code>. Статусы пока доступны только для чтения."
    )


def _payload_digest(payload: dict) -> str:
    immutable = {
        key: payload.get(key)
        for key in (
            "id", "user_id", "data_version", "vacancy", "master_profile",
            "generated_variant", "resume_document", "used_fact_ids", "model",
            "prompt_version", "hh_resume_snapshot",
        )
    }
    encoded = json.dumps(immutable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _quality_checklist(payload: dict) -> tuple[bool, list[tuple[bool, str, str]]]:
    generated = payload.get("generated_variant") or {}
    resume = payload.get("resume_document") or {}
    basics = resume.get("basics") or {}
    public_ids = {fact.id for fact in (config.ADAPTER.data.public_facts if config.ADAPTER else load_candidate_data().public_facts)}
    used_ids = set(payload.get("used_fact_ids") or [])
    public_text = "\n".join([
        str(generated.get("title") or ""), str(generated.get("about") or ""),
        *(str(x) for x in generated.get("achievements") or []),
        str(generated.get("cover_letter") or ""),
    ]).lower()
    internal_markers = (
        "fact_id", "publication_status", "source line", "репозиторий подтверждает",
        "требует уточнения", "не переносить в публичное резюме",
    )
    is_hh = (payload.get("vacancy") or {}).get("source") == "hh"
    snapshot = payload.get("hh_resume_snapshot") or {}
    hard_blockers = resume_analysis(payload).get("hard_blockers") or []
    review = vacancy_review(payload)
    letter = str(generated.get("cover_letter") or "")
    letter_min = 80 if review.get("apply_instructions") else 180
    letter_range = f"{letter_min}–750"
    checks = [
        (
            str(payload.get("data_version") or "")
            == str((config.ADAPTER.data if config.ADAPTER else load_candidate_data()).version),
            "Пакет собран на актуальной версии карьерной базы",
            "пакет собран на устаревшей версии карьерной базы — пересоберите карточку",
        ),
        (bool(used_ids) and used_ids <= public_ids, "Все использованные fact ID имеют public_confirmed",
         "пакет опирается на факты вне подтверждённой базы"),
        (not any(marker in public_text for marker in internal_markers), "Нет internal/evidence jargon",
         "в тексте осталась служебная лексика базы фактов"),
        (bool(generated.get("title")) and bool(generated.get("about")), "Есть целевой заголовок и раздел «О себе»",
         "нет заголовка или раздела «О себе»"),
        # `projects` holds only the achievements that did NOT fit under an experience entry,
        # so the best-assembled resume — every bullet placed under its job — has an empty
        # list there. The old `projects > 0` check blocked exactly those. Experience being
        # structured is the real question, and it is asked right here.
        (len(resume.get("experience") or []) > 0, "Опыт структурирован", "опыт в резюме не структурирован"),
        (len(generated.get("achievements") or []) >= 4, "Не менее четырёх подтверждённых результатов",
         "подтверждённых результатов меньше четырёх"),
        (letter_min <= len(letter) <= 750, f"Письмо имеет длину {letter_range} знаков",
         f"письмо не той длины ({len(letter)} знаков вместо {letter_range})"),
        (not hard_blockers, "Нет жёстких требований, противоречащих подтверждённому профилю",
         "вакансия требует того, чего нет в подтверждённом профиле"),
        (
            bool((payload.get("letter_review") or {}).get("sendable", True)),
            "Письмо написано моделью или человеком и прошло проверку перед отправкой",
            "письмо не прошло проверку — напишите его вручную или сгенерируйте заново",
        ),
        (bool(review.get("full_description_loaded")), "Полное описание вакансии прочитано до подготовки пакета",
         "описание вакансии прочитано не целиком — пакет собран по обрывку, переделайте карточку"),
        (bool(review.get("description_sha256")), "Версия описания зафиксирована для повторной проверки перед откликом",
         "версия описания не зафиксирована — нечем сверить, не изменилась ли вакансия"),
    ]
    if is_hh:
        checks.extend([
            (bool(basics.get("contacts")), "Контакты подтянуты из live HH-резюме", "контакты не подтянуты из резюме на hh.ru"),
            (
                bool(snapshot.get("resume_id")) and bool(snapshot.get("stable_mapping")),
                "Заранее созданная стабильная HH-копия зафиксирована; при отклике она не изменяется",
                "резюме на hh.ru не зафиксировано за этой карточкой — привяжите его в «Резюме»",
            ),
        ])
    return all(ok for ok, *_ in checks), checks


def _quality_checklist_text(payload: dict) -> str:
    ready, checks = _quality_checklist(payload)
    rows = "\n".join(f"{'✅' if ok else '❌'} {html.escape(label)}" for ok, label, _ in checks)
    gate = os.environ.get("AUTO_APPLY_ARMED", "0") == "1"
    setting = config.AUTOMATION.settings(int(payload["user_id"]))["auto_apply_enabled"]
    # Both gates bind the scheduler only. A press on «Отправить в HH» is the
    # owner's own confirmation and goes through regardless — see
    # mvp.automation._try_auto_apply.
    rows += f"\n{'✅' if gate else '🔒'} systemd AUTO_APPLY_ARMED={'1' if gate else '0'} (только автоотклик)"
    rows += f"\n{'✅' if setting else '🔒'} automation_settings.auto_apply_enabled={int(setting)} (только автоотклик)"
    rows += "\n🖐 Ручная кнопка «Отправить в HH» работает независимо от этих двух."
    result = "готов к отдельному подтверждению" if ready else "не готов к отправке"
    return f"🧾 <b>Итоговый checklist: {result}</b>\n\n{rows}"


#: Section key -> how many lines to show when an old package has no generated summary.
_SUMMARY_SECTIONS = (("tasks", 4), ("requirements", 2), ("conditions", 1))

_CURRENCY_SIGNS = {"RUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€", "KZT": "₸"}


def _money(value) -> str:
    """Thousands-separated amount, or empty when the field is not a number."""
    try:
        return f"{int(float(value)):,}".replace(",", " ")
    except (TypeError, ValueError):
        return ""


def _clean_line(value: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip(" •–—-;:.")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip(" ,;:") + "…"


def _summary_line(value: str, limit: int = 170) -> str:
    """Card prose keeps sentence punctuation while dropping copied list markers."""
    text = re.sub(r"\s+", " ", str(value)).strip()
    text = re.sub(r"^[•–—-]+\s*", "", text)
    if len(text) > limit:
        text = text[:limit].rstrip(" ,;:") + "…"
    elif text and not text.endswith((".", "!", "?", "…")):
        text += "."
    return text


def _vacancy_summary(payload: dict) -> str:
    """A short readable description generated from the whole posting.

    New packages carry the summary produced in the same model call as the letter.
    Older packages remain readable through a deterministic section-based fallback;
    the production queue regeneration replaces that fallback with the generated form.
    """
    vacancy = payload.get("vacancy") or {}
    generated = payload.get("vacancy_summary") or []
    if isinstance(generated, list):
        values = [_summary_line(item) for item in generated[:7]]
        values = [value for value in values if value]
        if len(values) >= 3:
            return "\n".join(html.escape(value) for value in values)

    sections = vacancy_review(payload).get("sections") or {}
    rows: list[str] = []
    for key, count in _SUMMARY_SECTIONS:
        taken = 0
        for item in sections.get(key) or []:
            line = _summary_line(item)
            if line and not line.lower().endswith(("что ждём от тебя", "наши ожидания от кандидата")):
                rows.append(html.escape(line))
                taken += 1
            if taken >= count:
                break
    # «Условия» alone is the least useful third of a posting — «удалёнка, минимум
    # бюрократии» says nothing about the work. When the parser found only that, the raw
    # opening of the description tells the owner more than a tidy but empty card.
    if rows and (sections.get("tasks") or sections.get("requirements")):
        return "\n".join(rows[:7])
    fragment = _clean_line(
        str(vacancy.get("normalized_text") or vacancy.get("description") or ""), 600,
    )
    if not fragment:
        return "📰 Описание не загрузилось — откройте вакансию на hh.ru."
    body = html.escape(fragment)
    return "\n\n".join(rows + [body]) if rows else body


def _vacancy_meta(payload: dict) -> str:
    """City, salary, source and employment type on one line."""
    vacancy = payload.get("vacancy") or {}
    salary = vacancy.get("salary") or {}
    lo, hi = _money(salary.get("from")), _money(salary.get("to"))
    currency = _CURRENCY_SIGNS.get(
        str(salary.get("currency") or "RUR").upper(), str(salary.get("currency") or ""),
    )
    if lo and hi:
        money = f"{lo}–{hi} {currency}"
    elif lo:
        money = f"от {lo} {currency}"
    elif hi:
        money = f"до {hi} {currency}"
    else:
        money = "зарплата не указана"
    bits = [
        str(vacancy.get("geography") or "город не указан"),
        money,
        str(vacancy.get("vacancy_type") or "unknown"),
        str(vacancy.get("source") or "manual"),
    ]
    return html.escape(" · ".join(bit for bit in bits if bit))


def _llm_judge(payload: dict) -> dict:
    """The cached DeepSeek fit/gap/reply-chance verdict, or ``{}`` when there isn't one.

    Cached (and rate-gated) by ``mvp/routers/cards.py::_ensure_llm_judge`` onto
    ``payload["llm_judge"]``. Absent whenever the Gonka flag is off, the vacancy never cleared
    the preview threshold, or the model call failed -- in every one of those cases
    ``payload["score"]`` (``candidate/ranking.py::RankingVerdict.as_dict()``) still carries the
    deterministic ``fit_reasons``/``missing_reasons``/``reply_chance`` computed for every
    vacancy with no model call at all, so the card below always has something to show.
    """
    verdict = payload.get("llm_judge")
    return verdict if isinstance(verdict, dict) else {}


def _score_summary(payload: dict) -> str:
    """Compact ranking verdict shown before the vacancy description."""
    score = payload.get("score") or (payload.get("analysis_internal") or {}).get("ranking") or {}
    try:
        value = max(0, min(100, int(score.get("value", score.get("total")))))
    except (TypeError, ValueError):
        return "📊 <b>Оценка:</b> ещё не рассчитана"

    # The deterministic score stays the headline number (it is the backbone); the LLM judge,
    # when cached, only ever nudges it by at most ranking.MAX_LLM_ADJUSTMENT points -- shown as
    # a signed addendum so the number is never silently replaced, only added to.
    judge = _llm_judge(payload)
    display_value = value
    llm_note = ""
    if judge:
        try:
            display_value = max(0, min(100, int(judge.get("total_with_llm", value))))
        except (TypeError, ValueError):
            display_value = value
        adjustment = int(judge.get("adjustment") or 0)
        if adjustment:
            llm_note = f" (ИИ {adjustment:+d})"

    if display_value >= 85:
        verdict = "отличное совпадение"
    elif display_value >= 75:
        verdict = "хорошее совпадение"
    elif display_value >= 60:
        verdict = "можно рассматривать"
    else:
        verdict = "слабое совпадение"

    pros = score.get("pros") or score.get("reasons") or []
    cons = score.get("cons") or []
    lines = [f"📊 <b>Оценка: {display_value}/100 — {verdict}</b>{llm_note}"]
    if pros:
        lines.append("✅ " + html.escape(_clean_line(str(pros[0]), 220)))
    if cons:
        lines.append("⚠️ " + html.escape(_clean_line(str(cons[0]), 220)))
    return "\n".join(lines)


def _fit_assessment(payload: dict) -> str:
    """The three things the owner asked for on every card, over a bare score.

    "Почему подходишь" and "чего не хватает" are given different headers/emoji on purpose
    (spec item 3: visually distinct at a glance) and always come from the same source pair --
    ``payload["llm_judge"]`` when a cached DeepSeek verdict exists (sharper, cites the
    vacancy's own wording), otherwise ``payload["score"]``'s deterministic fields, which are
    always present. Returns "" when neither has anything to say (e.g. a pre-rewrite payload
    with no ``score`` at all), so an old card degrades to exactly what it rendered before.
    """
    score = payload.get("score") or {}
    judge = _llm_judge(payload)

    fit_reasons = judge.get("fit_reasons") or score.get("fit_reasons") or []
    missing_reasons = judge.get("missing_reasons") or score.get("missing_reasons") or []
    reply_chance = judge.get("reply_chance") or score.get("reply_chance") or ""
    reply_chance_reason = judge.get("reply_chance_reason") or score.get("reply_chance_reason") or ""

    if not (fit_reasons or missing_reasons or reply_chance):
        return ""

    lines: list[str] = []
    if fit_reasons:
        lines.append("✅ <b>Почему подходишь:</b>")
        lines.extend(f"• {html.escape(_clean_line(str(item), 180))}" for item in fit_reasons[:4])
    if missing_reasons:
        lines.append("🚫 <b>Чего не хватает:</b>")
        lines.extend(f"• {html.escape(_clean_line(str(item), 180))}" for item in missing_reasons[:4])
    if reply_chance:
        chance_line = f"🎯 <b>Шанс ответа: {html.escape(str(reply_chance))}</b>"
        if reply_chance_reason:
            chance_line += f" — {html.escape(_clean_line(str(reply_chance_reason), 160))}"
        lines.append(chance_line)
    return "\n" + "\n".join(lines)


def _preview_text(payload: dict) -> str:
    generated = payload["generated_variant"]
    blockers = resume_analysis(payload).get("hard_blockers") or []
    warnings = resume_analysis(payload).get("fit_warnings") or []
    review = vacancy_review(payload)
    blocker_text = ""
    # What the employer demanded of the reply itself. Shown on the card because a missed
    # code phrase gets the application dropped before anyone reads the letter.
    instructions = review.get("apply_instructions") or []
    if instructions:
        blocker_text += "\n\n⚠️ <b>Ловушка вакансии:</b>\n" + "\n".join(
            html.escape(_clean_line(str(item), 300)) for item in instructions[:3]
        )
    # An application here costs more than a letter, and that is worth knowing before
    # pressing the button rather than after the auto-reply arrives.
    steps = review.get("screening_steps") or []
    if steps:
        blocker_text += "\n\n🧭 <b>Этапы отбора:</b>\n" + "\n".join(
            f"• {html.escape(_clean_line(str(item), 200))}" for item in steps[:2]
        )
    if blockers:
        blocker_text += "\n\n⛔ <b>Стоп-факторы:</b>\n" + "\n".join(
            f"• {html.escape(str(item))}" for item in blockers
        )
    if warnings:
        blocker_text += "\n\n🧪 <b>Нужно учесть:</b>\n" + "\n".join(
            f"• {html.escape(str(item))}" for item in warnings
        )
    text = (
        f"💼 <b>{html.escape(payload['vacancy']['name'])}</b>\n"
        f"<b>Компания:</b> {html.escape(payload['vacancy'].get('employer_name', '') or 'не указана')}\n"
        f"📍 {_vacancy_meta(payload)}\n\n"
        f"{_score_summary(payload)}"
        f"{_fit_assessment(payload)}\n\n"
        f"<b>Описание вакансии, кратко:</b>\n{_vacancy_summary(payload)}"
        f"{blocker_text}"
        f"\n\n🎯 <b>Профиль:</b> {html.escape(payload['master_profile']['name'])}\n"
        f"<b>Резюме:</b> {html.escape(generated['title'])}\n\n"
        # An empty «Письмо:» header reads as a broken bot; the reason is one line away.
        f"✉️ <b>Сопроводительное письмо:</b>\n"
        f"{html.escape(generated['cover_letter']) or 'не написано — проверка завернула все попытки, нажмите «Сгенерировать заново»'}\n\n"
        "🛡 Отклик уходит только по вашей кнопке — сам по себе бот ничего не отправляет."
    )
    return text


def _history_text(user_id: int) -> str:
    """The История screen. The queue is a card deck now — see mvp.routers.cards.

    The old ``drafts_only`` branch filtered ``list_recent(12)`` down to drafts
    in Python, which is why the queue could show one card while 33 were stored.
    It is gone rather than fixed: the deck reads ``list_by_status`` directly.
    """
    applications = config.AUTOMATION.application_history(user_id, 12)
    if applications:
        rows = []
        for item in applications:
            vacancy = item["vacancy"]
            outcome = item.get("outcome_status") or item["status"]
            update = " · 📨" if item.get("has_updates") else ""
            rows.append(
                f"• {html.escape(vacancy.get('name', 'Вакансия'))} · "
                f"<b>{html.escape(outcome)}</b> · HH {html.escape(item.get('hh_state') or 'не синхронизирован')}"
                f"{update}\n  negotiation: <code>{html.escape(item.get('negotiation_id') or '—')}</code>"
            )
        return "🕘 <b>История откликов</b>\n\n" + "\n".join(rows)
    items = config.STORE.list_recent(user_id, 12)
    if not items:
        return "История пока пуста."
    rows = []
    for item in items:
        vacancy = item["vacancy"]
        rows.append(f"• {html.escape(vacancy.get('name', 'Вакансия'))} · {html.escape(item.get('status', 'draft'))} · {html.escape(vacancy.get('source', 'manual'))}")
    return "🕘 <b>История</b>\n\n" + "\n".join(rows)


async def _safe_send(bot: Bot, chat_id: int, text: str, **kwargs):
    for attempt in range(2):
        try:
            return await bot.send_message(chat_id, text, **kwargs)
        except Exception as exc:
            if attempt:
                log.warning("Telegram notification failed safely: %s", type(exc).__name__)
                return None
            await asyncio.sleep(2)


TELEGRAM_CHUNK_LIMIT = 3900


def split_for_telegram(text: str, limit: int = TELEGRAM_CHUNK_LIMIT) -> list[str]:
    """Split long text into Telegram-sized chunks without cutting an HTML tag.

    Every screen in this package sends ``parse_mode="HTML"``. Slicing blindly at
    a character offset can land inside ``<b>`` or between a tag and its closer,
    and Telegram rejects the whole message with "can't parse entities". So the
    split walks line boundaries, and only falls back to a hard cut for a single
    line longer than the limit — cutting that one at the last '<' so a partial
    tag never ends a chunk.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line) > limit:
            cut = line.rfind("<", 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(line[:cut])
            line = line[cut:]
        current = line
    if current:
        chunks.append(current)
    return chunks


async def send_long_message(message, text: str, **kwargs) -> None:
    """Answer with ``text``, splitting when it exceeds Telegram's message limit.

    ``parse_mode`` is repeated on every chunk so formatting survives the split;
    everything else (notably ``reply_markup``) lands on the last chunk only, so
    the keyboard appears once, at the bottom.

    This helper stays self-contained so the Telegram layer has no hidden package
    dependency.
    """
    chunks = split_for_telegram(text)
    if not chunks:
        reply_markup = kwargs.get("reply_markup")
        if reply_markup:
            await message.answer("(пустое сообщение)", reply_markup=reply_markup)
        return
    passthrough = {k: kwargs[k] for k in ("parse_mode", "disable_web_page_preview") if k in kwargs}
    for index, chunk in enumerate(chunks):
        if index == len(chunks) - 1:
            await message.answer(chunk, **kwargs)
        else:
            await message.answer(chunk, **passthrough)
