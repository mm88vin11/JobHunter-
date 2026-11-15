"""Generic job-posting trap and scam detection.

The module is standalone, uses no third-party dependencies, makes no network
or AI calls, and has no side effects beyond the explicit user-list helpers at
the bottom of the file.

Aggregation thresholds:
  - WARN_THRESHOLD = 0.4   (score > 0.4 -> verdict "warn")
  - REJECT_THRESHOLD = 0.7 (score > 0.7 -> verdict "reject")
Multiple weak signals compound via a max-based, diminishing-returns formula.

Stop phrases and blocked employers are user-controlled and are a hard reject,
independent of score.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path

# ── Thresholds ─────────────────────────────────────────────────────────
WARN_THRESHOLD = 0.4
REJECT_THRESHOLD = 0.7

# ── 1. Suspiciously high salary + "без опыта" ───────────────────────────
# (handled inline in _score_high_salary_no_experience, no module-level table)

# ── 2. Redirect to external messenger ───────────────────────────────────
# Split from the original single pattern list: an explicit "go talk to us
# on Telegram/WhatsApp instead of here" instruction is a strong funnel
# signal (kept at the original strength). A bare t.me/wa.me link with no
# such instruction is common for legitimate postings that simply list a
# recruiter's Telegram handle as a contact — that alone must not push a
# vacancy toward reject, so it is scored far weaker.
_MESSENGER_REDIRECT_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"перейдите\s+в\s+telegram", re.IGNORECASE),
    re.compile(r"напишите\s+в\s+telegram", re.IGNORECASE),
    re.compile(r"свяжитесь\s+через\s+whatsapp", re.IGNORECASE),
    re.compile(r"напишите\s+в\s+whatsapp", re.IGNORECASE),
    re.compile(r"свяжитесь\s+в\s+telegram", re.IGNORECASE),
    re.compile(r"перейдите\s+в\s+whatsapp", re.IGNORECASE),
    re.compile(r"пишите\s+в\s+telegram", re.IGNORECASE),
    re.compile(r"пишите\s+в\s+whatsapp", re.IGNORECASE),
    re.compile(r"подробности\s+в\s+telegram", re.IGNORECASE),
    re.compile(r"подробности\s+в\s+whatsapp", re.IGNORECASE),
)
_MESSENGER_BARE_LINK_RE = re.compile(r"t\.me/[a-zA-Z0-9_]+|wa\.me/", re.IGNORECASE)

# ── 3. Empty or generic employer name ───────────────────────────────────
_GENERIC_EMPLOYER_NAMES = ("ип", "компания", "организация", "работодатель", "ооо")

# ── 4. Spam/scam vocabulary ──────────────────────────────────────────────
_SPAM_PHRASES = (
    "быстрый заработок",
    "без вложений",
    "лёгкий заработок",
    "легкий заработок",
    "пассивный доход",
    "финансовая пирамида",
    "работа на дому без опыта с высоким доходом",
    "заработок от",
    "доход от",
    "не mlm",
    "не сетевой",
    "криптовалют",
    "ставки на спорт",
    "онлайн казино",
    "букмекер",
)

# ── 5. ALL CAPS sections ─────────────────────────────────────────────────
_ALL_CAPS_RE = re.compile(r"(?:[А-ЯA-Z]{4,}\s+){2,}[А-ЯA-Z]{4,}")

# ── 6. Excessive emoji ───────────────────────────────────────────────────
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF"
    "]+",
    re.UNICODE,
)

# ── 7. External URLs in description ──────────────────────────────────────
_EXTERNAL_URL_RE = re.compile(
    r"https?://(?!(?:[a-z0-9-]+\.)*hh\.ru)[a-z0-9._-]+\.[a-z]{2,}",
    re.IGNORECASE,
)

# ── New: recruitment-agency vacuum postings ──────────────────────────────
# Generic staffing-agency postings may be CV-harvesting funnels.
_AGENCY_EMPLOYER_RE = re.compile(
    r"кадровое агентство|рекрутинговое агентство|рекрутинговая компания|"
    r"агентство (?:по )?подбор\w* персонала|hr[\s-]?агентство|"
    r"staffing agency|recruitment agency|кадровый центр|агентство занятост\w*",
    re.IGNORECASE,
)
_VACUUM_CLIENT_PHRASES = (
    "наш клиент",
    "нашего клиента",
    "название компании не раскрывается",
    "конфиденциальная вакансия",
    "клиент не разглашается",
    "заказчик пожелал остаться неизвестным",
    "крупная компания-заказчик",
)

# ── New: commission-only / no fixed salary ────────────────────────────────
_COMMISSION_ONLY_PHRASES = (
    "процент от продаж",
    "процент от сделки",
    "процент от выручки",
    "сдельная оплата",
    "оплата сдельная",
    "доход не ограничен",
    "заработок зависит только от вас",
    "без оклада",
    "только процент",
    "оплата по результату без оклада",
)

# ── New: unpaid or token-paid internship/trial (P1.3 hard reject) ─────────
_UNPAID_WORK_RE = re.compile(
    r"неоплачива\w*\s+(?:стажир\w*|практик\w*|период|испытательн\w*)"
    r"|стажир\w*\s+без\s+оплаты"
    r"|практик\w*\s+без\s+оплаты"
    r"|испытательн\w*\s+срок\s+без\s+оплаты"
    r"|тестов\w*\s+(?:период|задание|проект)\s+без\s+оплаты",
    re.IGNORECASE,
)

# ── New: MLM / "own business" framing ──────────────────────────────────────
# The existing spam list already contains the *denial* phrases "не mlm" /
# "не сетевой" — scammers write those to pre-empt suspicion, so they stay
# in _SPAM_PHRASES as-is. These are the *positive* framing phrases.
_MLM_FRAMING_PHRASES = (
    "сетевой маркетинг",
    "многоуровневый маркетинг",
    "mlm-компания",
    "партнёрская программа",
    "партнерская программа",
    "свой бизнес",
    "команда единомышленников",
)
_INCOME_WITHOUT_EMPLOYER_PHRASES = (
    "сам себе начальник",
    "будь сам себе начальник",
    "работать на себя",
    "финансовая независимость",
    "доход не ограничен",
    "стройте свою структуру",
    "приглашай друзей",
    "рекрутируй",
)

# ── New: franchise / paid-training / pay-to-work ───────────────────────────
_PAY_TO_WORK_PHRASES = (
    "оплатите обучение",
    "оплата обучения кандидатом",
    "стартовый набор за свой счёт",
    "закупить материалы за свой счёт",
    "вступительный взнос",
    "взнос за обучение",
    "оплата сертификации кандидатом",
    "купить оборудование за свой счёт",
    "франшиза",
    "первоначальный взнос",
    "оплата за трудоустройство",
)

_REPOST_TITLE_SIMILARITY = 0.72
_REPOST_MIN_SIMILAR = 2  # +1 (the current vacancy) => 3 near-identical postings total

_DEFAULT_LISTS_FILENAME = "candidate_trap_lists.json"


@dataclass(frozen=True)
class TrapVerdict:
    """Result of evaluating one vacancy for job-seeker traps."""

    score: float                    # 0.0 clean .. 1.0 certainly a trap
    verdict: str                    # "clean" | "warn" | "reject"
    flags: list[str]                # Russian human-readable flag strings
    hard_reject_reason: str | None  # Russian reason, or None

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "verdict": self.verdict,
            "flags": list(self.flags),
            "hard_reject_reason": self.hard_reject_reason,
        }


# ── Vacancy field access helpers ────────────────────────────────────────
# The candidate MVP's normalized vacancy dict (candidate/hh_html.py::vacancy_from_api)
# flattens `employer.name` -> `employer_name` and drops `snippet`/`experience`/
# `has_test`. The raw hh.ru API vacancy dict keeps them nested. Support both
# shapes so this module works whichever one the caller has on hand.


def _full_text(vacancy: dict) -> str:
    parts = [
        str(vacancy.get("name") or ""),
        str(
            vacancy.get("description")
            or vacancy.get("normalized_text")
            or vacancy.get("raw_text")
            or ""
        ),
    ]
    snippet = vacancy.get("snippet")
    if isinstance(snippet, dict):
        parts.append(str(snippet.get("requirement") or ""))
        parts.append(str(snippet.get("responsibility") or ""))
    return " ".join(p for p in parts if p)


def _employer_name(vacancy: dict) -> str:
    employer = vacancy.get("employer")
    if isinstance(employer, dict) and employer.get("name"):
        return str(employer["name"]).strip()
    return str(vacancy.get("employer_name") or "").strip()


def _employer_id(vacancy: dict) -> str:
    employer = vacancy.get("employer")
    if isinstance(employer, dict) and employer.get("id"):
        return str(employer["id"])
    return str(vacancy.get("employer_id") or "")


def _experience_id(vacancy: dict) -> str:
    experience = vacancy.get("experience")
    if isinstance(experience, dict):
        return str(experience.get("id") or "")
    return ""


# ── Heuristics ─────────────────────────────────────────────────────────


def _score_high_salary_no_experience(
    vacancy: dict, full_text_lower: str, expected_salary: int | None,
) -> tuple[float, str] | None:
    salary = vacancy.get("salary") or {}
    vac_from = salary.get("from") or 0
    vac_to = salary.get("to") or 0
    vac_best = max(vac_from, vac_to)
    is_no_exp = _experience_id(vacancy) == "noExperience" or "без опыта" in full_text_lower
    if vac_best <= 0 or not is_no_exp:
        return None
    if expected_salary and vac_best > expected_salary * 3:
        return 0.8, "Подозрительно высокая зарплата для вакансии без опыта"
    if vac_best >= 300_000:
        return 0.7, "Подозрительно высокая зарплата для вакансии без опыта"
    return None


def _score_messenger_redirect(full_text: str) -> tuple[float, str] | None:
    for pattern in _MESSENGER_REDIRECT_PATTERNS:
        if pattern.search(full_text):
            return 0.9, "Перенаправление в мессенджер (Telegram/WhatsApp) в обход обычного диалога"
    if _MESSENGER_BARE_LINK_RE.search(full_text):
        return 0.3, "В вакансии указана ссылка на мессенджер"
    return None


def _score_generic_employer(employer_name: str) -> tuple[float, str] | None:
    stripped = employer_name.strip().lower()
    if not stripped:
        return 0.5, "Не указан работодатель"
    if stripped in _GENERIC_EMPLOYER_NAMES:
        return 0.4, f"Подозрительное название работодателя: {employer_name}"
    return None


def _score_spam_vocabulary(full_text_lower: str) -> tuple[float, str] | None:
    found = [phrase for phrase in _SPAM_PHRASES if phrase in full_text_lower]
    if not found:
        return None
    strength = min(0.9, 0.5 + 0.15 * len(found))
    return strength, f"Подозрительные формулировки: {', '.join(found[:3])}"


def _score_all_caps(full_text: str) -> tuple[float, str] | None:
    if len(_ALL_CAPS_RE.findall(full_text)) >= 2:
        return 0.4, "Непрофессиональное оформление (ЗАГЛАВНЫЕ БУКВЫ)"
    return None


def _score_excessive_emoji(full_text: str) -> tuple[float, str] | None:
    total = sum(len(m) for m in _EMOJI_RE.findall(full_text))
    if total >= 5:
        return 0.35, "Чрезмерное количество эмодзи"
    return None


def _score_external_url(full_text: str) -> tuple[float, str] | None:
    if _EXTERNAL_URL_RE.search(full_text):
        return 0.45, "Ссылки на внешние сайты в описании"
    return None


# ── New heuristics ─────────────────────────────────────────────────────


def _score_agency_vacuum(employer_name: str, full_text_lower: str) -> tuple[float, str] | None:
    if not _AGENCY_EMPLOYER_RE.search(employer_name):
        return None
    strength = 0.45
    if any(phrase in full_text_lower for phrase in _VACUUM_CLIENT_PHRASES):
        strength = 0.65
    return (
        strength,
        f"Вакансия от кадрового агентства без указания конечного работодателя: {employer_name}",
    )


def _score_commission_only(full_text_lower: str, salary: dict) -> tuple[float, str] | None:
    hits = [phrase for phrase in _COMMISSION_ONLY_PHRASES if phrase in full_text_lower]
    if not hits:
        return None
    vac_from = (salary or {}).get("from") or 0
    vac_to = (salary or {}).get("to") or 0
    strength = 0.35 if (vac_from or vac_to) else 0.55
    return strength, f"Похоже на оплату только процентом без фиксированного оклада: {hits[0]}"


def _score_mlm_own_business(full_text_lower: str) -> tuple[float, str] | None:
    framing_hit = next((p for p in _MLM_FRAMING_PHRASES if p in full_text_lower), None)
    income_hit = next((p for p in _INCOME_WITHOUT_EMPLOYER_PHRASES if p in full_text_lower), None)
    if not framing_hit or not income_hit:
        return None
    return 0.75, f'Признаки МЛМ / "свой бизнес": «{framing_hit}» + «{income_hit}»'


def _score_pay_to_work(full_text_lower: str) -> tuple[float, str] | None:
    hit = next((p for p in _PAY_TO_WORK_PHRASES if p in full_text_lower), None)
    if not hit:
        return None
    return 0.8, f'Требуется оплата от соискателя (обучение/взнос/оборудование): «{hit}»'


def _score_repost_farming(
    vacancy: dict, employer_name: str, recent_vacancies: list[dict] | None,
) -> tuple[float, str] | None:
    """Repost farming: the same employer posting many near-identical vacancies.

    Skips entirely (returns None) when no recent-vacancy context is supplied,
    rather than guessing.
    """
    if not recent_vacancies or not employer_name:
        return None
    current_title = str(vacancy.get("name") or "").strip().lower()
    if not current_title:
        return None
    current_id = str(vacancy.get("id") or vacancy.get("external_id") or "")
    employer_lower = employer_name.strip().lower()
    similar = 0
    for other in recent_vacancies:
        other_id = str(other.get("id") or other.get("external_id") or "")
        if current_id and other_id and other_id == current_id:
            continue
        if _employer_name(other).strip().lower() != employer_lower:
            continue
        other_title = str(other.get("name") or "").strip().lower()
        if not other_title:
            continue
        ratio = difflib.SequenceMatcher(None, current_title, other_title).ratio()
        if ratio >= _REPOST_TITLE_SIMILARITY:
            similar += 1
    if similar >= _REPOST_MIN_SIMILAR:
        return (
            0.55,
            "Похоже на массовую рассылку похожих вакансий от одного работодателя: "
            f"{employer_name} ({similar} похожих среди недавних)",
        )
    return None


def _detect_unpaid_work(full_text_lower: str) -> str | None:
    """P1.3 hard reject: неоплачиваемая работа (unpaid internship/trial)."""
    if _UNPAID_WORK_RE.search(full_text_lower):
        return (
            "Неоплачиваемая стажировка или испытательный период — "
            "не соответствует минимальным требованиям по оплате"
        )
    return None


# ── User-controlled lists (hh_red_flags / hh_blacklist equivalent) ──────


def _match_stop_phrase(full_text_lower: str, stop_phrases: list) -> str | None:
    for phrase in stop_phrases:
        phrase_str = str(phrase).strip()
        if phrase_str and phrase_str.lower() in full_text_lower:
            return phrase_str
    return None


def _match_blocked_employer(
    employer_name: str, vacancy: dict, blocked_employers: list,
) -> str | None:
    employer_lower = employer_name.strip().lower()
    employer_id = _employer_id(vacancy)
    for entry in blocked_employers:
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            blocked_id = str(entry.get("id") or "").strip()
        else:
            name = str(entry).strip()
            blocked_id = ""
        if blocked_id and employer_id and blocked_id == employer_id:
            return name or blocked_id
        if name and employer_lower and name.lower() == employer_lower:
            return name
    return None


# ── Aggregation (max-based, diminishing returns) ───────────────────────


def _aggregate_scores(signals: list[tuple[float, str]]) -> float:
    if not signals:
        return 0.0
    strengths = sorted((strength for strength, _ in signals), reverse=True)
    total = strengths[0]
    for strength in strengths[1:]:
        total += (1.0 - total) * strength * 0.4
    return round(min(1.0, total), 2)


# ── Public API ────────────────────────────────────────────────────────


def evaluate_vacancy(
    vacancy: dict,
    *,
    user_lists: dict | None = None,
    expected_salary: int | None = None,
    recent_vacancies: list[dict] | None = None,
) -> TrapVerdict:
    """Evaluate a vacancy for job-seeker traps.

    Pure, no network/AI calls, fast enough to run on every vacancy in a
    search cycle. `user_lists` is the shape returned by `load_user_lists()`:
    `{"stop_phrases": [...], "blocked_employers": [...]}`. A stop-phrase or
    blocked-employer hit is a HARD reject regardless of score.
    """
    lists = user_lists or {}
    stop_phrases = list(lists.get("stop_phrases") or [])
    blocked_employers = list(lists.get("blocked_employers") or [])

    full_text = _full_text(vacancy)
    full_text_lower = full_text.lower()
    employer_name = _employer_name(vacancy)

    blocked = _match_blocked_employer(employer_name, vacancy, blocked_employers)
    if blocked:
        reason = f"Работодатель заблокирован пользователем: {blocked}"
        return TrapVerdict(score=1.0, verdict="reject", flags=[reason], hard_reject_reason=reason)

    stop_hit = _match_stop_phrase(full_text_lower, stop_phrases)
    if stop_hit:
        reason = f"Найдена стоп-фраза пользователя: {stop_hit}"
        return TrapVerdict(score=1.0, verdict="reject", flags=[reason], hard_reject_reason=reason)

    unpaid_reason = _detect_unpaid_work(full_text_lower)
    if unpaid_reason:
        return TrapVerdict(
            score=1.0, verdict="reject", flags=[unpaid_reason], hard_reject_reason=unpaid_reason,
        )

    signals: list[tuple[float, str]] = []
    for probe in (
        _score_high_salary_no_experience(vacancy, full_text_lower, expected_salary),
        _score_messenger_redirect(full_text),
        _score_generic_employer(employer_name),
        _score_spam_vocabulary(full_text_lower),
        _score_all_caps(full_text),
        _score_excessive_emoji(full_text),
        _score_external_url(full_text),
        _score_agency_vacuum(employer_name, full_text_lower),
        _score_commission_only(full_text_lower, vacancy.get("salary") or {}),
        _score_mlm_own_business(full_text_lower),
        _score_pay_to_work(full_text_lower),
        _score_repost_farming(vacancy, employer_name, recent_vacancies),
    ):
        if probe is not None:
            signals.append(probe)

    info_flags: list[str] = []
    if bool(vacancy.get("has_test")):
        info_flags.append("Вакансия требует пройти тест до контакта с работодателем")

    score = _aggregate_scores(signals)
    if score > REJECT_THRESHOLD:
        verdict = "reject"
    elif score > WARN_THRESHOLD:
        verdict = "warn"
    else:
        verdict = "clean"

    flags = [text for _, text in signals] + info_flags
    return TrapVerdict(score=score, verdict=verdict, flags=flags, hard_reject_reason=None)


# ── User list storage: var/candidate_trap_lists.json ────────────────────
# Plain, hand-editable JSON keyed by string user_id. A Telegram settings
# screen can call add_/remove_ helpers directly; the file can also be edited.


def _default_lists_path() -> Path:
    return Path(__file__).resolve().parent.parent / "var" / _DEFAULT_LISTS_FILENAME


def _resolve_path(path: str | Path | None) -> Path:
    return Path(path) if path is not None else _default_lists_path()


def _read_all(file_path: Path) -> dict:
    if not file_path.exists():
        return {}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_all(file_path: Path, data: dict) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8",
    )
    tmp_path.replace(file_path)


def _empty_entry() -> dict:
    return {"stop_phrases": [], "blocked_employers": []}


def load_user_lists(user_id: int, *, path: str | Path | None = None) -> dict:
    """Return `{"stop_phrases": [str, ...], "blocked_employers": [{"name","id"}, ...]}`."""
    data = _read_all(_resolve_path(path))
    entry = data.get(str(user_id)) or {}
    return {
        "stop_phrases": list(entry.get("stop_phrases") or []),
        "blocked_employers": list(entry.get("blocked_employers") or []),
    }


def add_stop_phrase(user_id: int, phrase: str, *, path: str | Path | None = None) -> None:
    phrase = phrase.strip()
    if not phrase:
        return
    file_path = _resolve_path(path)
    data = _read_all(file_path)
    entry = data.setdefault(str(user_id), _empty_entry())
    entry.setdefault("stop_phrases", [])
    if phrase.lower() not in {str(p).strip().lower() for p in entry["stop_phrases"]}:
        entry["stop_phrases"].append(phrase)
    _write_all(file_path, data)


def remove_stop_phrase(user_id: int, phrase: str, *, path: str | Path | None = None) -> bool:
    file_path = _resolve_path(path)
    data = _read_all(file_path)
    entry = data.get(str(user_id))
    if not entry:
        return False
    before = entry.get("stop_phrases") or []
    after = [p for p in before if str(p).strip().lower() != phrase.strip().lower()]
    if len(after) == len(before):
        return False
    entry["stop_phrases"] = after
    _write_all(file_path, data)
    return True


def add_blocked_employer(
    user_id: int, name: str, *, employer_id: str = "", path: str | Path | None = None,
) -> None:
    name = name.strip()
    if not name:
        return
    file_path = _resolve_path(path)
    data = _read_all(file_path)
    entry = data.setdefault(str(user_id), _empty_entry())
    entry.setdefault("blocked_employers", [])
    existing_names = {
        str(item.get("name", "")).strip().lower()
        for item in entry["blocked_employers"] if isinstance(item, dict)
    }
    existing_ids = {
        str(item.get("id", "")) for item in entry["blocked_employers"]
        if isinstance(item, dict) and item.get("id")
    }
    if name.lower() in existing_names or (employer_id and employer_id in existing_ids):
        return
    entry["blocked_employers"].append({"name": name, "id": employer_id})
    _write_all(file_path, data)


def remove_blocked_employer(
    user_id: int, name: str, *, path: str | Path | None = None,
) -> bool:
    file_path = _resolve_path(path)
    data = _read_all(file_path)
    entry = data.get(str(user_id))
    if not entry:
        return False
    before = entry.get("blocked_employers") or []
    after = [
        item for item in before
        if str(item.get("name", "") if isinstance(item, dict) else item).strip().lower()
        != name.strip().lower()
    ]
    if len(after) == len(before):
        return False
    entry["blocked_employers"] = after
    _write_all(file_path, data)
    return True
