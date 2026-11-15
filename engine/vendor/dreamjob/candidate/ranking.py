"""Deterministic, explainable vacancy ranking.

The scorer uses six generic factors from the supplied vacancy and candidate
profile. It ships without role, location, language, salary or industry
preferences; applications provide those through profile data and arguments.

Design contract (do not weaken when editing)
--------------------------------------------
* **stdlib only.** No third-party imports, no network, no AI calls, no
  file I/O. Fast enough to run on every vacancy in every search cycle.
* **The deterministic score is the backbone.** The LLM may only adjust it
  within ``±MAX_LLM_ADJUSTMENT`` points (see :func:`combine_scores`), so a
  model swap can move a vacancy by at most 15 points instead of moving the
  entire scale.
* **``candidate/traps.py`` is an input, not a dependency.** A trap verdict
  is passed in (``TrapVerdict`` dataclass or its ``as_dict()``); this module
  never imports or calls it, so the automation loop owns when traps run.
* **``detect_profile_blockers``'s cap at 35 keeps working** — apply it via
  ``profile_blockers=`` on :func:`rank_vacancy` or :func:`apply_blocker_cap`.

Vacancy shapes
--------------
Every accessor tolerates both the raw hh.ru API vacancy (``employer`` /
``area`` / ``schedule`` / ``employment`` nested dicts, ``snippet``) and the
MVP's normalized shape from ``candidate/hh_html.py::vacancy_from_api``
(``employer_name``, ``geography``, ``normalized_text``, ``vacancy_type``,
``search_profile_hint``) — exactly like ``candidate/traps.py`` does.

Geography is configurable through ``CANDIDATE_HOME_TERMS``. Remote work is
always treated as reachable; explicit office-only and mandatory-relocation
requirements receive a soft penalty so the human still makes the final call.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

# ── Defaults ──────────────────────────────────────────────────────────────

#: Minimum monthly income when a vacancy publishes a salary.
SALARY_FLOOR = int(os.environ.get("MIN_SALARY_RUB", "0"))

#: How far the LLM may move the deterministic score, in points.
MAX_LLM_ADJUSTMENT = 15

#: Points added to the query's ``profile_hint`` when role/skill fit is compared.
#: Measured agreement between hint and the model's chosen profile was 52/56,
#: so the hint is a decent prior — but only a prior: it breaks near-ties and
#: cannot overturn a deterministic gap wider than this bonus.
PROFILE_HINT_BONUS = 6

#: Conservative defaults. Applications can override both settings.
DEFAULT_MIN_PREVIEW_SCORE = 55
DEFAULT_MIN_APPLY_SCORE = 75

#: Score a hard-rejected vacancy carries. Zero, never negative.
HARD_REJECT_SCORE = 0

#: Cap applied when ``candidate/service.py::detect_profile_blockers`` fires.
PROFILE_BLOCKER_CAP = 35

# ── Factors ───────────────────────────────────────────────────────────────

FACTOR_KEYS = ("salary", "role", "skills", "format", "geography", "risk")

FACTOR_LABELS_RU: Mapping[str, str] = {
    "salary": "Зарплата",
    "role": "Соответствие роли",
    "skills": "Совпадение навыков",
    "format": "Формат работы",
    "geography": "География",
    "risk": "Риски и ловушки",
}

#: Baseline weights, used for an unknown profile key. Must sum to 1.0.
BASE_WEIGHTS: Mapping[str, float] = {
    "salary": 0.20,
    "role": 0.25,
    "skills": 0.22,
    "format": 0.13,
    "geography": 0.10,
    "risk": 0.10,
}

PROFILE_WEIGHTS: Mapping[str, Mapping[str, float]] = {}


def profile_weights(profile_key: str, profile: Mapping | None = None) -> dict[str, float]:
    """Return normalized weights from ``profile['ranking_weights']`` or defaults."""
    candidate = (profile or {}).get("ranking_weights") or PROFILE_WEIGHTS.get(profile_key)
    if not isinstance(candidate, Mapping):
        return dict(BASE_WEIGHTS)
    try:
        weights = {key: max(0.0, float(candidate.get(key, 0.0))) for key in FACTOR_KEYS}
    except (TypeError, ValueError):
        return dict(BASE_WEIGHTS)
    total = sum(weights.values())
    return ({key: value / total for key, value in weights.items()} if total else dict(BASE_WEIGHTS))


# Optional per-profile role vocabularies can be supplied by an application.
# Unknown profiles are matched against their own title and skill set.
_ROLE_VOCABULARY: Mapping[str, Mapping[str, tuple[str, ...]]] = {}

_REMOTE_TERMS = (
    "удалён", "удален", "удалёнк", "удаленк", "remote", "дистанционн",
    "из любой точки", "work from anywhere", "fully distributed",
    "распределённая команда", "распределенная команда",
)

_HYBRID_TERMS = ("гибрид", "hybrid", "частично удал", "2 дня в офисе", "3 дня в офисе")

#: The `(?<!не )` guards matter: "не только офис, но и удалёнка" and
#: "переезд не требуется" are the *opposite* of a hard reject.
_ONSITE_ONLY_RE = re.compile(
    r"(?<!не )только\s+(?:в\s+)?офис\w*"
    r"|(?<!не )работа\s+только\s+в\s+офисе"
    r"|без\s+удал[её]нн?\w*\s+(?:работы|формата)"
    r"|удал[её]нн\w*\s+(?:работа|формат)\w*\s+не\s+(?:рассматрива\w*|предусмотрен\w*|возможн\w*)"
    r"|удал[её]нк\w*\s+не\s+(?:рассматрива\w*|предлага\w*)"
    r"|on[\s-]?site\s+only"
    r"|no\s+remote",
    re.IGNORECASE,
)

_MANDATORY_RELOCATION_RE = re.compile(
    r"(?<!не )релокац\w*\s+(?:обязательн\w*|за\s+счёт|за\s+счет|требуется)"
    r"|обязательн\w*\s+релокац\w*"
    r"|(?<!не )(?:требуется|обязателен|необходим\w*)\s+переезд"
    r"|переезд\s+обязателен"
    r"|готовность\s+к\s+переезду\s+обязательн\w*"
    r"|relocation\s+(?:is\s+)?(?:required|mandatory)",
    re.IGNORECASE,
)

#: Unreachable geography is penalised instead of rejected so a human can decide.
GEO_UNREACHABLE = 0.05

_WORK_PERMIT_RE = re.compile(
    r"разрешени\w*\s+на\s+работу"
    r"|рабоч\w*\s+виз\w*\s+(?:обязательн\w*|требуется|необходим\w*)"
    r"|work\s+permit"
    r"|work\s+authoriz\w*"
    r"|(?:legally\s+)?(?:authorized|eligible)\s+to\s+work\s+in"
    r"|right\s+to\s+work\s+in",
    re.IGNORECASE,
)

_UNPAID_WORK_RE = re.compile(
    r"неоплачива\w*\s+(?:стажир\w*|практик\w*|период|испытательн\w*)"
    r"|стажир\w*\s+без\s+оплаты"
    r"|практик\w*\s+без\s+оплаты"
    r"|испытательн\w*\s+срок\s+без\s+оплаты"
    r"|тестов\w*\s+(?:период|задание|проект)\s+без\s+оплаты"
    r"|unpaid\s+(?:internship|trial|probation)",
    re.IGNORECASE,
)

# ── hh.ru enum handling ───────────────────────────────────────────────────

# Generic defaults keep every supported employment and experience category.
ACCEPTED_EMPLOYMENTS = ("full", "project", "part", "probation", "volunteer")
SEARCH_EXPERIENCE_IDS = ("noExperience", "between1And3", "between3And6", "moreThan6")

_SCHEDULE_SCORES = {
    "remote": 1.0,
    "flexible": 0.85,
    "fullDay": 0.5,
    "shift": 0.2,
    "flyInFlyOut": 0.0,
}

_EMPLOYMENT_SCORES = {
    "full": 1.0,
    "project": 1.0,
    "part": 0.55,
    "probation": 0.2,
    "volunteer": 0.0,
}

#: Rough RUB conversion used only when a non-zero salary floor is configured.
_CURRENCY_TO_RUB = {
    "RUR": 1.0, "RUB": 1.0, "USD": 90.0, "EUR": 100.0, "GBP": 115.0,
    "CNY": 12.5, "KZT": 0.19, "BYR": 28.0, "BYN": 28.0, "UAH": 2.3,
    "GEL": 33.0, "AZN": 53.0, "UZS": 0.007, "KGS": 1.05,
}

#: Rough net-of-tax factor for hh's `salary.gross = true`.
_GROSS_TO_NET = 0.87

_TOKEN_RE = re.compile(r"[a-zа-яё0-9][a-zа-яё0-9+#.\-]{1,}")

_SKILL_STOPWORDS = frozenset({
    "и", "в", "с", "на", "по", "для", "от", "до", "или", "the", "and", "of",
    "for", "with", "работа", "работы", "опыт",
})


# ── Dataclasses ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FactorScore:
    """One weighted factor of the deterministic score."""

    key: str
    label: str            # Russian label for the Telegram card
    raw: float            # 0.0 .. 1.0
    weight: float         # 0.0 .. 1.0, per-profile
    contribution: float   # raw * weight * 100, in points
    detail: str           # Russian one-line explanation

    def as_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "raw": round(self.raw, 3),
            "weight": round(self.weight, 3),
            "contribution": round(self.contribution, 1), "detail": self.detail,
        }


@dataclass(frozen=True)
class RankingVerdict:
    """Everything needed to show and audit one vacancy's score."""

    profile_key: str
    total: int                                  # final 0..100, after everything
    deterministic_total: int                    # backbone, before LLM/caps
    llm_score: int | None                       # what the model said, or None
    llm_adjustment: int                         # bounded delta actually applied
    factors: tuple[FactorScore, ...]
    penalties: tuple[tuple[str, int], ...]      # (Russian reason, points)
    hard_reject: bool
    hard_reject_reason: str | None              # Russian
    pros: tuple[str, ...]                       # Russian
    cons: tuple[str, ...]                       # Russian
    weights: Mapping[str, float] = field(default_factory=dict)
    profile_hint: str = ""
    profile_hint_applied: bool = False          # hint decided a near-tie
    blocker_capped: bool = False
    hard_tech_gated: bool = False               # classic backend/dev role; capped, not rejected
    relevance_gated: bool = False               # weak role+skills fit; capped, not rejected
    fit_reasons: tuple[str, ...] = ()           # Russian — "почему подходит" (spec item f)
    missing_reasons: tuple[str, ...] = ()       # Russian — "чего не хватает" (spec item f)
    reply_chance: str = ""                      # "высокий"/"средний"/"низкий" (spec item f)
    reply_chance_reason: str = ""               # Russian one-liner behind reply_chance

    @property
    def factor_map(self) -> dict[str, FactorScore]:
        return {factor.key: factor for factor in self.factors}

    def explanation_ru(self) -> str:
        """Compact Russian breakdown for the Telegram card."""
        if self.hard_reject:
            return f"Жёсткий отказ: {self.hard_reject_reason}"
        parts = [
            f"{factor.label} {int(round(factor.raw * 100))}%"
            for factor in self.factors
        ]
        line = f"Итог {self.total}/100 (детерминированно {self.deterministic_total}"
        if self.llm_adjustment:
            line += f", модель {self.llm_adjustment:+d}"
        line += "). " + ", ".join(parts) + "."
        if self.penalties:
            line += " Штрафы: " + ", ".join(
                f"{reason} ({points:+d})" for reason, points in self.penalties
            ) + "."
        return line

    def as_dict(self) -> dict:
        return {
            "profile_key": self.profile_key,
            "value": self.total,
            "total": self.total,
            "deterministic_total": self.deterministic_total,
            "llm_score": self.llm_score,
            "llm_adjustment": self.llm_adjustment,
            "factors": [factor.as_dict() for factor in self.factors],
            "penalties": [
                {"reason": reason, "points": points} for reason, points in self.penalties
            ],
            "hard_reject": self.hard_reject,
            "hard_reject_reason": self.hard_reject_reason,
            "pros": list(self.pros),
            "cons": list(self.cons),
            "weights": dict(self.weights),
            "profile_hint": self.profile_hint,
            "profile_hint_applied": self.profile_hint_applied,
            "blocker_capped": self.blocker_capped,
            "hard_tech_gated": self.hard_tech_gated,
            "relevance_gated": self.relevance_gated,
            "fit_reasons": list(self.fit_reasons),
            "missing_reasons": list(self.missing_reasons),
            "reply_chance": self.reply_chance,
            "reply_chance_reason": self.reply_chance_reason,
            "explanation": self.explanation_ru(),
        }


@dataclass(frozen=True)
class ProfileApplicability:
    """Can a generated variant be sent with a linked external resume?"""

    profile_key: str            # the profile the ranker chose
    mapped: bool                # does it have a linked, published hh resume?
    action: str                 # "generate" | "fallback" | "manual_required"
    effective_profile_key: str  # what to actually generate with ("" if none)
    reason: str                 # Russian, safe to show in Telegram

    def as_dict(self) -> dict:
        return {
            "profile_key": self.profile_key, "mapped": self.mapped,
            "action": self.action,
            "effective_profile_key": self.effective_profile_key,
            "reason": self.reason,
        }


# ── Vacancy field accessors (both raw hh API and normalized MVP shape) ────


def _full_text(vacancy: Mapping) -> str:
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
    if isinstance(snippet, Mapping):
        parts.append(str(snippet.get("requirement") or ""))
        parts.append(str(snippet.get("responsibility") or ""))
    return " ".join(part for part in parts if part)


def _enum_id(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("id") or "")
    return str(value or "")


def _enum_name(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("name") or "")
    return str(value or "")


def _area_text(vacancy: Mapping) -> str:
    area = vacancy.get("area")
    parts = [_enum_name(area) if isinstance(area, Mapping) else str(area or "")]
    parts.append(str(vacancy.get("geography") or ""))
    address = vacancy.get("address")
    if isinstance(address, Mapping):
        parts.extend(
            str(address.get(key) or "") for key in ("city", "street", "raw")
        )
    return " ".join(part for part in parts if part)


def _vacancy_role_names(vacancy: Mapping) -> str:
    roles = vacancy.get("professional_roles")
    if not isinstance(roles, Sequence) or isinstance(roles, (str, bytes)):
        return ""
    return " ".join(_enum_name(role) for role in roles)


def _tokens(text: str) -> set[str]:
    return {match for match in _TOKEN_RE.findall(text.lower())}


#: Short, alphabet-ambiguous terms that MUST be matched as whole words, not
#: substrings. A naive `in` check on bare "rag"/"mcp" would light up
#: "storage", "average", "paragraph"; bare "ии" would match almost every
#: Russian noun ending in "-ии" ("технологии", "компании", "линии"); bare
#: "cursor" would light up "precursor". Every other term in this module
#: keeps the deliberate substring/stem behaviour used throughout (e.g.
#: "продакт" intentionally matching "продакта").
_WHOLE_WORD_TERMS = frozenset({"rag", "mcp", "ии", "cursor"})
_WHOLE_WORD_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _whole_word_re(term: str) -> re.Pattern[str]:
    pattern = _WHOLE_WORD_RE_CACHE.get(term)
    if pattern is None:
        pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
        _WHOLE_WORD_RE_CACHE[term] = pattern
    return pattern


def _contains_any(haystack: str, needles: Iterable[str]) -> str | None:
    for needle in needles:
        if not needle:
            continue
        if needle in _WHOLE_WORD_TERMS:
            if _whole_word_re(needle).search(haystack):
                return needle
        elif needle in haystack:
            return needle
    return None


# ── Trap verdict adapter (duck-typed: dataclass or dict) ──────────────────


def _trap_fields(verdict: Any) -> tuple[float, str, tuple[str, ...], str | None]:
    """Return ``(score, verdict, flags, hard_reject_reason)`` from any shape."""
    if verdict is None:
        return 0.0, "", (), None
    if isinstance(verdict, Mapping):
        score = float(verdict.get("score") or 0.0)
        name = str(verdict.get("verdict") or "")
        flags = tuple(str(flag) for flag in (verdict.get("flags") or ()))
        reason = verdict.get("hard_reject_reason")
    else:
        score = float(getattr(verdict, "score", 0.0) or 0.0)
        name = str(getattr(verdict, "verdict", "") or "")
        flags = tuple(str(flag) for flag in (getattr(verdict, "flags", ()) or ()))
        reason = getattr(verdict, "hard_reject_reason", None)
    return score, name, flags, (str(reason) if reason else None)


# ── Factor: salary ────────────────────────────────────────────────────────


def _salary_bounds(vacancy: Mapping) -> tuple[int, int]:
    """Return ``(bottom, top)`` in approximate net RUB. ``(0, 0)`` = hidden."""
    salary = vacancy.get("salary")
    if not isinstance(salary, Mapping):
        return 0, 0
    currency = str(salary.get("currency") or "RUR").upper()
    rate = _CURRENCY_TO_RUB.get(currency, 1.0)
    factor = rate * (_GROSS_TO_NET if salary.get("gross") else 1.0)
    low = float(salary.get("from") or 0) * factor
    high = float(salary.get("to") or 0) * factor
    bottom = int(low or high)
    top = int(high or low)
    return bottom, top


#: Raw value reported for a vacancy that states no salary. Never contributes:
#: ``score_factors`` drops the salary weight entirely in that case, so this is
#: only what the card displays next to «Зарплата не указана».
SALARY_UNKNOWN = 0.55


def _salary_is_known(vacancy: Mapping) -> bool:
    bottom, top = _salary_bounds(vacancy)
    return bool(bottom or top)


def _renormalised_without(weights: Mapping[str, float], dropped: str) -> dict[str, float]:
    """Spread one factor's weight across the others, keeping the total at 1.0."""
    remaining = {k: v for k, v in weights.items() if k != dropped}
    total = sum(remaining.values())
    if not total:
        return dict(weights)
    scale = 1.0 / total
    rescaled = {k: v * scale for k, v in remaining.items()}
    rescaled[dropped] = 0.0
    return rescaled


def _score_salary(
    vacancy: Mapping, profile: Mapping, salary_floor: int,
) -> tuple[float, str]:
    bottom, top = _salary_bounds(vacancy)
    target = int(((profile.get("salary") or {}) if isinstance(profile, Mapping) else {}).get("amount") or 0)
    if not bottom and not top:
        # DELIBERATE: many good vacancies hide the salary. Punishing them
        # here (and filtering them out with `only_with_salary=true` in the
        # search) would cost real opportunities, so a hidden salary scores
        # slightly above neutral and is judged on the other five factors.
        return SALARY_UNKNOWN, "Зарплата не указана — вес перенесён на остальные факторы"
    if target and bottom >= target:
        return 1.0, f"Нижняя граница {bottom:,} ₽ ≥ целевых {target:,} ₽".replace(",", " ")
    if bottom >= salary_floor:
        return 0.85, f"Нижняя граница {bottom:,} ₽ ≥ минимума {salary_floor:,} ₽".replace(",", " ")
    if top >= salary_floor:
        return 0.7, f"Вилка {bottom:,}–{top:,} ₽ пересекает минимум".replace(",", " ")
    ratio = top / salary_floor if salary_floor else 0.0
    return max(0.0, round(ratio * 0.5, 3)), (
        f"Потолок {top:,} ₽ ниже минимума {salary_floor:,} ₽".replace(",", " ")
    )


# ── Factor: role match ────────────────────────────────────────────────────


def _score_role(
    vacancy: Mapping, profile: Mapping, profile_key: str, full_text_lower: str,
) -> tuple[float, str]:
    title = str(vacancy.get("name") or "").lower()
    title_and_roles = f"{title} {_vacancy_role_names(vacancy)}".lower()
    vocabulary = _ROLE_VOCABULARY.get(profile_key)
    if vocabulary is None:
        strong = tuple(
            token for token in _tokens(str(profile.get("title") or "")) if len(token) > 3
        )
        vocabulary = {"strong": strong, "medium": (), "anti": ()}

    anti_hit = _contains_any(title_and_roles, vocabulary.get("anti", ()))
    strong_title = _contains_any(title_and_roles, vocabulary.get("strong", ()))
    strong_text = strong_title or _contains_any(full_text_lower, vocabulary.get("strong", ()))
    medium_title = _contains_any(title_and_roles, vocabulary.get("medium", ()))
    medium_text = medium_title or _contains_any(full_text_lower, vocabulary.get("medium", ()))

    if strong_title and medium_title:
        score, detail = 1.0, f"Заголовок совпадает с профилем: «{strong_title}» + «{medium_title}»"
    elif strong_title:
        score, detail = 0.9, f"Заголовок совпадает с профилем: «{strong_title}»"
    elif strong_text and medium_title:
        score, detail = 0.7, f"Роль упомянута в описании: «{strong_text}»"
    elif strong_text:
        score, detail = 0.6, f"Роль упомянута в описании: «{strong_text}»"
    elif medium_title:
        score, detail = 0.45, f"В заголовке только домен: «{medium_title}»"
    elif medium_text:
        score, detail = 0.3, f"Домен упомянут только в описании: «{medium_text}»"
    else:
        score, detail = 0.1, "Заголовок и описание не совпадают с ролью профиля"

    if anti_hit:
        score = min(score, 0.35)
        detail += f"; в заголовке чужая роль: «{anti_hit}»"
    return score, detail


# ── Factor: skill overlap ─────────────────────────────────────────────────


def _skill_matches(skill: str, text_lower: str, text_tokens: set[str], prefixes: set[str]) -> bool:
    phrase = skill.strip().lower()
    if not phrase:
        return False
    if phrase in text_lower:
        return True
    tokens = [
        token for token in _TOKEN_RE.findall(phrase)
        if token not in _SKILL_STOPWORDS and len(token) >= 2
    ]
    if not tokens:
        return False
    hits = 0
    for token in tokens:
        if token in text_tokens or (len(token) >= 5 and token[:5] in prefixes):
            hits += 1
    needed = 1 if len(tokens) == 1 else max(2, math.ceil(len(tokens) * 0.6))
    return hits >= needed


# Applications may define domain-specific synonym groups.
_SKILL_SYNONYM_GROUPS: tuple[frozenset[str], ...] = ()


def _skill_synonym_match(skill_lower: str, text_lower: str) -> str | None:
    """Return the vacancy-side synonym that matched, or ``None``.

    Only consulted after the literal/token match in `_skill_matches` has
    already failed — this is a fallback, not a replacement.
    """
    for group in _SKILL_SYNONYM_GROUPS:
        if _contains_any(skill_lower, group) and (hit := _contains_any(text_lower, group)):
            return hit
    return None


def _score_skills(
    profile: Mapping, full_text_lower: str,
) -> tuple[float, str, tuple[str, ...]]:
    skill_set = [str(skill) for skill in (profile.get("skill_set") or []) if str(skill).strip()]
    if not skill_set:
        return 0.5, "У профиля нет списка навыков", ()
    text_tokens = _tokens(full_text_lower)
    prefixes = {token[:5] for token in text_tokens if len(token) >= 5}
    matched = tuple(
        skill for skill in skill_set
        if _skill_matches(skill, full_text_lower, text_tokens, prefixes)
        or _skill_synonym_match(skill.strip().lower(), full_text_lower)
    )
    # A realistic vacancy mentions a handful of the profile's skills, not all
    # of them: full marks at ~35% of the profile's skill_set, minimum 3.
    needed = max(3, round(0.35 * len(skill_set)))
    score = min(1.0, len(matched) / needed)
    if matched:
        detail = f"Совпало {len(matched)} из {len(skill_set)} навыков: {', '.join(matched[:4])}"
    else:
        detail = f"Ни один из {len(skill_set)} навыков профиля не упомянут"
    return round(score, 3), detail, matched


# ── Factor: format (employment + schedule) ────────────────────────────────


def _remote_signal(vacancy: Mapping, full_text_lower: str) -> bool:
    if _enum_id(vacancy.get("schedule")) == "remote":
        return True
    if str(vacancy.get("vacancy_type") or "") == "remote":
        return True
    return _contains_any(full_text_lower, _REMOTE_TERMS) is not None


def _score_format(
    vacancy: Mapping, profile: Mapping, full_text_lower: str,
) -> tuple[float, str]:
    schedule_id = _enum_id(vacancy.get("schedule"))
    employment_id = _enum_id(vacancy.get("employment"))
    remote = _remote_signal(vacancy, full_text_lower)

    schedule_score = _SCHEDULE_SCORES.get(schedule_id, 0.5 if schedule_id else 0.45)
    if remote:
        schedule_score = max(schedule_score, 0.95)
    elif _contains_any(full_text_lower, _HYBRID_TERMS):
        schedule_score = max(schedule_score, 0.65)

    accepted = tuple(
        str(item) for item in (profile.get("employments") or ACCEPTED_EMPLOYMENTS)
    )
    if employment_id:
        employment_score = _EMPLOYMENT_SCORES.get(employment_id, 0.5)
        if employment_id in accepted:
            employment_score = max(employment_score, 1.0)
    else:
        vacancy_type = str(vacancy.get("vacancy_type") or "")
        employment_score = {
            "full_time": 1.0, "project": 1.0, "contract": 1.0,
        }.get(vacancy_type, 0.6)

    score = round(0.6 * schedule_score + 0.4 * employment_score, 3)
    schedule_label = _enum_name(vacancy.get("schedule")) or schedule_id or "не указан"
    employment_label = _enum_name(vacancy.get("employment")) or employment_id or "не указана"
    detail = f"График: {schedule_label}; занятость: {employment_label}"
    if remote:
        detail += "; есть признаки удалённой работы"
    return score, detail


# ── Factor: geography (with the P1.3 hard rejects) ────────────────────────


#: Conservative score for an office without remote wording or a home match.
_UNREACHABLE_OFFICE_SCORE = 0.25


def _home_terms() -> tuple[str, ...]:
    return tuple(
        item.strip().casefold()
        for item in os.environ.get("CANDIDATE_HOME_TERMS", "").split(",")
        if item.strip()
    )


def _score_geography(
    vacancy: Mapping, full_text_lower: str, area_lower: str,
) -> tuple[float, str, str | None]:
    """Return ``(score, detail_ru, hard_reject_reason_or_None)``.

    Home locations are configured as comma-separated search terms in
    ``CANDIDATE_HOME_TERMS`` (for example ``москва,moscow``). This function
    never hard-rejects a vacancy on geography alone.
    """
    schedule_id = _enum_id(vacancy.get("schedule"))
    remote = _remote_signal(vacancy, full_text_lower)
    home_terms = _home_terms()
    home = bool(home_terms) and (
        _contains_any(area_lower, home_terms) or _contains_any(full_text_lower, home_terms)
    )

    if schedule_id == "flyInFlyOut":
        return GEO_UNREACHABLE, "Вахтовый метод", None

    if remote:
        return 1.0, "Удалённый формат", None
    if home:
        return 1.0, "Локация совпадает с настройкой кандидата", None
    if not home_terms:
        return 0.5, "Домашняя локация не настроена", None
    if _WORK_PERMIT_RE.search(full_text_lower):
        return GEO_UNREACHABLE, "Требуется разрешение на работу", None
    if _MANDATORY_RELOCATION_RE.search(full_text_lower):
        return GEO_UNREACHABLE, "Обязательный переезд, а переезд не рассматривается", None
    if _ONSITE_ONLY_RE.search(full_text_lower):
        return GEO_UNREACHABLE, "Работа только в офисе вне настроенной домашней локации", None
    if _contains_any(full_text_lower, _HYBRID_TERMS):
        return 0.5, "Гибридный формат — потребуется согласование", None
    if not area_lower.strip():
        return 0.5, "География не указана", None
    return _UNREACHABLE_OFFICE_SCORE, (
        "Офис без признаков удалённой работы и без совпадения с домашней локацией: "
        f"{area_lower.strip()[:60]}"
    ), None


# ── Factor: trap / red-flag risk ──────────────────────────────────────────


def _score_risk(trap_verdict: Any) -> tuple[float, str, tuple[str, ...], str | None]:
    score, name, flags, hard_reason = _trap_fields(trap_verdict)
    if trap_verdict is None:
        return 1.0, "Проверка на ловушки не выполнялась", (), None
    if hard_reason:
        return 0.0, hard_reason, flags, hard_reason
    risk = round(max(0.0, 1.0 - score), 3)
    if name == "clean" and not flags:
        detail = "Подозрительных признаков не найдено"
    else:
        detail = f"Оценка ловушек {score:.2f}" + (f": {flags[0]}" if flags else "")
    return risk, detail, flags, None


# ── Soft penalties ────────────────────────────────────────────────────────

#: Maximum total soft penalty from the text-based rules, in points. The
#: below-floor salary penalty is added on top of this cap.
MAX_SOFT_PENALTY = 20

#: Extra penalty when a configured salary floor is not met.
BELOW_FLOOR_PENALTY = -10

#: Worst case for a salary that is stated and far under the floor.
MAX_BELOW_FLOOR_PENALTY = -35


def below_floor_penalty(salary_top: int, salary_floor: int) -> int:
    """Scale the penalty by how deeply a configured salary floor is missed."""
    if salary_top <= 0 or salary_floor <= 0 or salary_top >= salary_floor:
        return 0
    shortfall = (salary_floor - salary_top) / salary_floor
    extra = (MAX_BELOW_FLOOR_PENALTY - BELOW_FLOOR_PENALTY) * shortfall
    return int(round(BELOW_FLOOR_PENALTY + extra))


def _soft_penalties(full_text_lower: str) -> list[tuple[str, int]]:
    """Compatibility hook for application-defined text penalties."""
    return []


def _detect_unpaid_work(full_text_lower: str) -> str | None:
    """P1.3 hard reject — mirrors ``candidate/traps.py::_detect_unpaid_work``.

    Duplicated (not imported) so ranking stays usable when the automation
    loop has not run traps for this vacancy.
    """
    if _UNPAID_WORK_RE.search(full_text_lower):
        return (
            "Неоплачиваемая работа (стажировка / испытательный период) — "
            "не соответствует минимальным требованиям по оплате"
        )
    return None


# Compatibility hook retained for callers that display this gate.
HARD_TECH_GATE_CAP = 35


def _detect_hard_engineering_gate(
    full_text_lower: str, title_lower: str = "",
) -> str | None:
    """No built-in role exclusions; pass explicit profile blockers instead."""
    return None


# ── Relevance gate (role + skills must earn the free factors) ─────────────

#: Cap applied when `_detect_weak_relevance_gate` fires. Same value as
#: `PROFILE_BLOCKER_CAP`/`HARD_TECH_GATE_CAP` by design — all three mean
#: "needs a human look, not an automatic apply", not "hide it".
RELEVANCE_GATE_CAP = 35

#: Minimum combined role+skill "fit" (see `_role_skill_fit`) a vacancy must
#: clear before format/geography/risk are allowed to carry it over the
#: apply threshold. Calibrated one point above the measured case that
#: motivated this gate (see above): role 0.6, skills 0.27 ->
#: fit = round(60*0.6 + 40*0.27) = 47. Below 48, format/geography/risk
#: cannot matter, because there was not enough demonstrated role+skill
#: fit to build on in the first place; at or above it, a vacancy has
#: earned the right to be judged on the full six factors.
RELEVANCE_GATE_MIN_FIT = 48


def _role_skill_fit(role_raw: float, skills_raw: float) -> int:
    """Combined 0..100 "does this vacancy actually match the profile" signal.

    Same 60/40 role/skills split :func:`choose_profile` already uses to pick
    the best-fitting profile — role carries more weight because a genuine
    title match is, on its own, decent evidence, while a handful of shared
    skill keywords alone is weaker (a keyword can be name-dropped once and
    mean nothing). Deliberately NOT the six-factor weighted total — see the
    module-level comment above `RELEVANCE_GATE_CAP` for why.
    """
    return round(60 * role_raw + 40 * skills_raw)


def _detect_weak_relevance_gate(
    role_raw: float, skills_raw: float, *, min_fit: int = RELEVANCE_GATE_MIN_FIT,
) -> str | None:
    """Return a Russian reason if role+skills fit is too weak to trust the
    free factors, or ``None`` if the vacancy has earned that trust.
    """
    fit = _role_skill_fit(role_raw, skills_raw)
    if fit >= min_fit:
        return None
    return (
        f"Слабое совпадение по роли и навыкам (роль {role_raw:.2f}, навыки "
        f"{skills_raw:.2f}, совместный балл {fit}/100 < {min_fit}) — формат, "
        "география и риски сами по себе не делают вакансию релевантной, "
        "нужна ручная проверка"
    )


# ── Pros / cons ───────────────────────────────────────────────────────────


def _build_pros_cons(
    factors: Mapping[str, FactorScore],
    matched_skills: Sequence[str],
    trap_flags: Sequence[str],
    penalties: Sequence[tuple[str, int]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    pros: list[str] = []
    cons: list[str] = []

    salary, role = factors["salary"], factors["role"]
    skills, fmt = factors["skills"], factors["format"]
    geo, risk = factors["geography"], factors["risk"]

    if role.raw >= 0.7:
        pros.append(f"Роль совпадает с профилем — {role.detail}")
    elif role.raw < 0.45:
        cons.append(f"Роль слабо совпадает с профилем — {role.detail}")

    if salary.raw >= 0.85:
        pros.append(salary.detail)
    elif salary.raw < 0.5 or "не указана" in salary.detail:
        cons.append(salary.detail)

    if matched_skills:
        pros.append(f"Совпадают навыки: {', '.join(matched_skills[:4])}")
    if skills.raw < 0.4:
        cons.append(skills.detail)

    if geo.raw >= 0.85:
        pros.append(geo.detail)
    elif geo.raw <= 0.5:
        cons.append(f"География: {geo.detail}")

    if fmt.raw >= 0.85:
        pros.append(fmt.detail)
    elif fmt.raw < 0.5:
        cons.append(f"Формат работы не идеален — {fmt.detail}")

    if risk.raw < 1.0 and trap_flags:
        cons.append(f"Риски: {'; '.join(trap_flags[:2])}")

    cons.extend(reason for reason, _ in penalties)
    return tuple(pros), tuple(cons)


# ── Fit summary: "why you fit" / "what's missing" / "reply chance" ────────
# The vacancy card shows these three things explicitly rather than burying
# them inside the generic pros/cons mix (which also carries
# salary/format/risk noise). Deliberately additive and separate from
# `_build_pros_cons` above — that function's output and callers are
# unchanged. Another agent is expected to layer an LLM judge over this later
# (see the module docstring's MAX_LLM_ADJUSTMENT contract), so every field
# here is optional/additive on RankingVerdict and derived only from data
# already computed deterministically in this module.


def _build_fit_summary(
    factors: Mapping[str, FactorScore],
    matched_skills: Sequence[str],
    missing_skills: Sequence[str],
    profile_blockers: Sequence[str],
    hard_tech_reason: str | None,
    relevance_reason: str | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(fit_reasons, missing_reasons)`` — "почему подходит" /
    "чего не хватает" — scoped to role/skills fit and hard blockers only.
    """
    fit: list[str] = []
    missing: list[str] = []

    role = factors["role"]
    if role.raw >= 0.6:
        fit.append(f"Роль подходит: {role.detail}")
    if matched_skills:
        fit.append(f"Совпадающие навыки: {', '.join(matched_skills[:5])}")

    if missing_skills:
        missing.append(f"Не встречены в вакансии: {', '.join(missing_skills[:5])}")
    if factors["skills"].raw < 0.4:
        missing.append(factors["skills"].detail)
    missing.extend(profile_blockers)
    if hard_tech_reason:
        missing.append(hard_tech_reason)
    if relevance_reason:
        missing.append(relevance_reason)

    return tuple(fit), tuple(missing)


def _estimate_reply_chance(
    deterministic_total: int,
    salary_detail: str,
    penalties: Sequence[tuple[str, int]],
    hard_tech_reason: str | None,
    relevance_reason: str | None = None,
) -> tuple[str, str]:
    """Rough, deterministic heuristic for "chance of a reply" (spec item f).

    Not a promise — an LLM judge may refine this later. Inputs are whether the vacancy states a
    salary (a stated number usually means a live, budgeted opening), the
    requirement/friction load (soft penalties and relevance gates), and the
    deterministic score itself (it already folds in
    role/skills/format/geography/risk).

    Returns ``(level_ru, reason_ru)`` where ``level_ru`` is one of
    "высокий" / "средний" / "низкий".
    """
    friction = (
        len(penalties)
        + (1 if hard_tech_reason else 0)
        + (1 if relevance_reason else 0)
    )
    salary_known = "не указана" not in salary_detail
    if deterministic_total >= 80 and friction <= 1:
        level = "высокий"
    elif deterministic_total >= 60 and friction <= 2:
        level = "средний"
    else:
        level = "низкий"
    reason_bits = [
        f"детерминированный балл {deterministic_total}/100",
        "зарплата указана" if salary_known else "зарплата скрыта",
    ]
    if friction:
        reason_bits.append(f"требований-барьеров: {friction}")
    return level, ", ".join(reason_bits)


# ── The ranker ────────────────────────────────────────────────────────────


def score_factors(
    vacancy: Mapping,
    profile: Mapping,
    *,
    profile_key: str = "",
    trap_verdict: Any = None,
    salary_floor: int = SALARY_FLOOR,
) -> tuple[dict[str, FactorScore], tuple[str, ...], str | None, tuple[str, ...]]:
    """Score the six factors.

    Returns ``(factors, matched_skills, hard_reject_reason, trap_flags)``.
    """
    key = profile_key or str(profile.get("profile_key") or "")
    weights = profile_weights(key, profile)
    full_text = _full_text(vacancy)
    full_text_lower = full_text.lower()
    area_lower = _area_text(vacancy).lower()

    salary_raw, salary_detail = _score_salary(vacancy, profile, salary_floor)
    role_raw, role_detail = _score_role(vacancy, profile, key, full_text_lower)
    skills_raw, skills_detail, matched = _score_skills(profile, full_text_lower)
    format_raw, format_detail = _score_format(vacancy, profile, full_text_lower)
    geo_raw, geo_detail, geo_reject = _score_geography(vacancy, full_text_lower, area_lower)
    risk_raw, risk_detail, trap_flags, trap_reject = _score_risk(trap_verdict)

    raw_values = {
        "salary": (salary_raw, salary_detail),
        "role": (role_raw, role_detail),
        "skills": (skills_raw, skills_detail),
        "format": (format_raw, format_detail),
        "geography": (geo_raw, geo_detail),
        "risk": (risk_raw, risk_detail),
    }

    # A hidden salary is missing information, so redistribute its weight.
    if not _salary_is_known(vacancy):
        weights = _renormalised_without(weights, "salary")
    factors = {
        factor_key: FactorScore(
            key=factor_key,
            label=FACTOR_LABELS_RU[factor_key],
            raw=raw,
            weight=weights[factor_key],
            contribution=round(raw * weights[factor_key] * 100, 2),
            detail=detail,
        )
        for factor_key, (raw, detail) in raw_values.items()
    }

    hard_reject = (
        trap_reject
        or _detect_unpaid_work(full_text_lower)
        or geo_reject
    )
    return factors, matched, hard_reject, trap_flags


def combine_scores(
    deterministic: int,
    llm_score: int | None,
    *,
    max_adjustment: int = MAX_LLM_ADJUSTMENT,
) -> tuple[int, int]:
    """Combine the deterministic backbone with the model's judgement.

    The rule, stated once so it can never drift again::

        adjustment = clamp(llm_score - deterministic, -max_adjustment, +max_adjustment)
        final      = clamp(deterministic + adjustment, 0, 100)

    The deterministic score sets the scale; the model is allowed to nudge it
    by at most ``max_adjustment`` points because it *is* better than any
    regex at reading a messy Russian job description — and worse than any
    formula at being a stable scale. ``llm_score is None`` (the model failed,
    or the caller deliberately ran deterministic-only) means no adjustment
    at all — which is also what makes ``_fallback_selection``'s hardcoded
    ``"score": 50`` harmless: pass ``None`` instead of 50 and the backbone
    decides.

    Returns ``(final, adjustment)``.
    """
    deterministic = max(0, min(100, int(deterministic)))
    if llm_score is None:
        return deterministic, 0
    llm = max(0, min(100, int(llm_score)))
    bound = max(0, int(max_adjustment))
    adjustment = max(-bound, min(bound, llm - deterministic))
    return max(0, min(100, deterministic + adjustment)), adjustment


def apply_blocker_cap(
    total: int, blockers: Sequence[str], *, cap: int = PROFILE_BLOCKER_CAP,
) -> int:
    """Keep ``candidate/service.py::detect_profile_blockers``'s cap working."""
    return min(int(total), cap) if blockers else int(total)


def rank_vacancy(
    vacancy: Mapping,
    profile: Mapping,
    *,
    profile_key: str = "",
    trap_verdict: Any = None,
    llm_score: int | None = None,
    profile_blockers: Sequence[str] = (),
    profile_hint: str = "",
    salary_floor: int = SALARY_FLOOR,
    max_llm_adjustment: int = MAX_LLM_ADJUSTMENT,
) -> RankingVerdict:
    """Rank one vacancy against one hh profile.

    ``profile`` is a loaded ``candidate_data/v1/hh_profiles/<key>.json``
    (see ``candidate.hh_resume_sync.load_profile``). ``trap_verdict`` is a
    ``candidate.traps.TrapVerdict`` (or its ``as_dict()``), or ``None`` when
    traps have not been run. ``llm_score`` is the model's 0..100 number, or
    ``None`` to score deterministically only.
    """
    key = profile_key or str(profile.get("profile_key") or "")
    factors, matched, hard_reject_reason, trap_flags = score_factors(
        vacancy, profile, profile_key=key,
        trap_verdict=trap_verdict, salary_floor=salary_floor,
    )
    weights = profile_weights(key, profile)
    ordered = tuple(factors[factor_key] for factor_key in FACTOR_KEYS)

    if hard_reject_reason:
        return RankingVerdict(
            profile_key=key, total=HARD_REJECT_SCORE, deterministic_total=HARD_REJECT_SCORE,
            llm_score=llm_score, llm_adjustment=0, factors=ordered, penalties=(),
            hard_reject=True, hard_reject_reason=hard_reject_reason,
            pros=(), cons=(hard_reject_reason,), weights=weights,
            profile_hint=profile_hint,
            missing_reasons=(hard_reject_reason,),
            reply_chance="низкий", reply_chance_reason="Жёсткий отказ",
        )

    full_text_lower = _full_text(vacancy).lower()
    penalties = _soft_penalties(full_text_lower)
    _, salary_top = _salary_bounds(vacancy)
    if salary_top and salary_top < salary_floor:
        penalties.append((
            f"Доход ниже минимума {salary_floor:,} ₽".replace(",", " "),
            below_floor_penalty(salary_top, salary_floor),
        ))
    weighted = sum(factor.contribution for factor in ordered)
    deterministic = int(round(weighted)) + sum(points for _, points in penalties)
    deterministic = max(0, min(100, deterministic))

    total, adjustment = combine_scores(
        deterministic, llm_score, max_adjustment=max_llm_adjustment,
    )
    after_blockers = apply_blocker_cap(total, profile_blockers)
    hard_tech_reason = _detect_hard_engineering_gate(
        full_text_lower, str(vacancy.get("name") or "").lower(),
    )
    after_hard_tech = apply_blocker_cap(
        after_blockers, (hard_tech_reason,) if hard_tech_reason else (),
        cap=HARD_TECH_GATE_CAP,
    )
    relevance_reason = _detect_weak_relevance_gate(
        factors["role"].raw, factors["skills"].raw,
    )
    capped_total = apply_blocker_cap(
        after_hard_tech, (relevance_reason,) if relevance_reason else (),
        cap=RELEVANCE_GATE_CAP,
    )
    pros, cons = _build_pros_cons(factors, matched, trap_flags, penalties)
    if profile_blockers:
        cons = cons + tuple(profile_blockers)
    if hard_tech_reason:
        cons = cons + (hard_tech_reason,)
    if relevance_reason:
        cons = cons + (relevance_reason,)

    skill_set = tuple(
        str(skill) for skill in (profile.get("skill_set") or []) if str(skill).strip()
    )
    missing_skills = tuple(skill for skill in skill_set if skill not in matched)
    fit_reasons, missing_reasons = _build_fit_summary(
        factors, matched, missing_skills, profile_blockers, hard_tech_reason,
        relevance_reason,
    )
    reply_chance, reply_chance_reason = _estimate_reply_chance(
        deterministic, factors["salary"].detail, penalties, hard_tech_reason,
        relevance_reason,
    )

    return RankingVerdict(
        profile_key=key,
        total=capped_total,
        deterministic_total=deterministic,
        llm_score=llm_score,
        llm_adjustment=adjustment,
        factors=ordered,
        penalties=tuple(penalties),
        hard_reject=False,
        hard_reject_reason=None,
        pros=pros,
        cons=cons,
        weights=weights,
        profile_hint=profile_hint,
        blocker_capped=bool(profile_blockers) and after_blockers != total,
        hard_tech_gated=bool(hard_tech_reason),
        relevance_gated=bool(relevance_reason),
        fit_reasons=fit_reasons,
        missing_reasons=missing_reasons,
        reply_chance=reply_chance,
        reply_chance_reason=reply_chance_reason,
    )


def rank_all_profiles(
    vacancy: Mapping,
    profiles: Mapping[str, Mapping],
    *,
    trap_verdict: Any = None,
    llm_score: int | None = None,
    profile_blockers: Sequence[str] = (),
    salary_floor: int = SALARY_FLOOR,
) -> dict[str, RankingVerdict]:
    """Rank the vacancy against every profile, keyed by profile key."""
    return {
        key: rank_vacancy(
            vacancy, profile, profile_key=key, trap_verdict=trap_verdict,
            llm_score=llm_score, profile_blockers=profile_blockers,
            salary_floor=salary_floor,
        )
        for key, profile in profiles.items()
    }


def choose_profile(
    vacancy: Mapping,
    profiles: Mapping[str, Mapping],
    *,
    profile_hint: str = "",
    trap_verdict: Any = None,
    llm_score: int | None = None,
    llm_profile_key: str = "",
    profile_blockers: Sequence[str] = (),
    salary_floor: int = SALARY_FLOOR,
    hint_bonus: int = PROFILE_HINT_BONUS,
) -> tuple[RankingVerdict, dict[str, RankingVerdict]]:
    """Pick the best profile, using the search query's hint as a prior.

    Profile *selection* is deterministic on purpose and compares only the
    profile-specific factors (role and skills). Salary, format, geography and
    risk decide whether the vacancy is good, but cannot make an unrelated
    profile win merely because that profile weights remote work more heavily.
    ``profile_hint``
    defaults to the vacancy's own ``search_profile_hint`` (set by
    ``candidate/hh_html.py::vacancy_from_api`` from the query that found the
    vacancy) and adds ``hint_bonus`` points *to the comparison only* — the
    reported total is untouched, so the hint breaks a near-tie but cannot
    overturn a wider role/skill gap. ``llm_profile_key`` (the profile the
    model picked) is a second prior of the same weight.

    ``llm_score`` is applied only once the winner is known, so all profiles
    are compared on one scale.

    Returns ``(best_verdict, all_verdicts_by_key)``.
    """
    if not profiles:
        raise ValueError("No profiles supplied")
    hint = profile_hint or str(vacancy.get("search_profile_hint") or "")
    verdicts: dict[str, RankingVerdict] = {
        key: rank_vacancy(
            vacancy, profile, profile_key=key, trap_verdict=trap_verdict,
            llm_score=None, profile_blockers=profile_blockers,
            profile_hint=hint, salary_floor=salary_floor,
        )
        for key, profile in profiles.items()
    }

    def profile_fit(verdict: RankingVerdict) -> int:
        factors = {factor.key: factor.raw for factor in verdict.factors}
        return _role_skill_fit(factors.get("role", 0.0), factors.get("skills", 0.0))

    def comparison(item: tuple[str, RankingVerdict]) -> tuple[int, int, int, int]:
        key, verdict = item
        bonus = hint_bonus if (hint and key == hint) else 0
        bonus += hint_bonus if (llm_profile_key and key == llm_profile_key) else 0
        fit = profile_fit(verdict)
        return (fit + bonus, int(bool(bonus)), fit, verdict.total)

    best_key = max(verdicts.items(), key=comparison)[0]
    plain_best_key = max(
        verdicts.items(), key=lambda item: (profile_fit(item[1]), item[1].total),
    )[0]

    if llm_score is not None:
        verdicts[best_key] = rank_vacancy(
            vacancy, profiles[best_key], profile_key=best_key,
            trap_verdict=trap_verdict, llm_score=llm_score,
            profile_blockers=profile_blockers, profile_hint=hint,
            salary_floor=salary_floor,
        )
    best = replace(
        verdicts[best_key],
        profile_hint_applied=bool(hint) and best_key != plain_best_key,
    )
    verdicts[best_key] = best
    return best, verdicts


# ── Wasted-generation guard: is the chosen profile even applicable? ───────


def resolve_applicable_profile(
    profile_key: str,
    mapped_profile_keys: Iterable[str],
    *,
    ranked_alternatives: Sequence[str] = (),
) -> ProfileApplicability:
    """Decide whether generating a variant for ``profile_key`` can ever be sent.

    ``mapped_profile_keys`` is the set of profiles that have a linked,
    published hh.ru resume (``hh_resume_mappings``). ``ranked_alternatives``
    is the other profiles ordered best-first (e.g. from
    :func:`choose_profile`'s second return value).

    * profile is mapped               -> ``action="generate"``
    * a better-ranked mapped fallback -> ``action="fallback"``
    * nothing mapped                  -> ``action="manual_required"``

    This avoids generating variants that cannot be sent.
    """
    mapped = {str(key) for key in mapped_profile_keys if str(key)}
    if profile_key and profile_key in mapped:
        return ProfileApplicability(
            profile_key=profile_key, mapped=True, action="generate",
            effective_profile_key=profile_key,
            reason="Профиль привязан к опубликованному резюме HH",
        )
    for alternative in ranked_alternatives:
        if alternative and alternative != profile_key and alternative in mapped:
            return ProfileApplicability(
                profile_key=profile_key, mapped=False, action="fallback",
                effective_profile_key=alternative,
                reason=(
                    f"У профиля «{profile_key}» нет привязанного резюме HH — "
                    f"используем «{alternative}»"
                ),
            )
    return ProfileApplicability(
        profile_key=profile_key, mapped=False, action="manual_required",
        effective_profile_key="",
        reason=(
            f"У профиля «{profile_key}» нет привязанного резюме HH и нет "
            "подходящей замены — вакансия уходит в ручной режим"
        ),
    )


# ── hh.ru search filters derived from a profile ──────────────────────────

ORDER_BY_RELEVANCE = "relevance"
ORDER_BY_FRESH = "publication_time"

#: hh.ru area ids we are certain of. Deliberately tiny — ids are resolved
#: from the live dictionary (``candidate.hh_resume_sync.HHDictionaries``)
#: when a caller needs more, never guessed here.
HH_AREA_IDS: Mapping[str, str] = {
    "россия": "113", "москва": "1", "санкт-петербург": "2",
}


def hh_search_filters(
    profile: Mapping | None = None,
    *,
    salary_floor: int = SALARY_FLOOR,
    only_with_salary: bool = False,
    order_by: str = ORDER_BY_RELEVANCE,
    period: int = 30,
    include_area: bool = False,
    professional_role_ids: Sequence[str] = (),
) -> dict:
    """Derive hh.ru search parameters from a profile and optional salary floor.

    What is sent, and why:

    * ``salary`` — included only when ``salary_floor`` is positive.
    * ``employment`` — from the profile's own ``employments``.
    * ``schedule`` — deliberately omitted. hh.ru deprecated this parameter in
      favour of ``work_schedule_by_days`` / ``work_format`` and combining the
      old ``remote`` value with ``fullDay`` now excludes ordinary office jobs.
      Format and geography are already scored below, without losing discovery.
    * ``experience`` — all supported categories by default.
    * ``order_by`` — ``relevance`` by default (see module docstring).
    * ``area`` — omitted by default; pass ``include_area=True`` to use a
      known profile area.
    * ``professional_role`` — omitted unless the caller passes resolved ids;
      hh's role ids are not guessed here.
    """
    profile = profile or {}
    employments = tuple(
        str(item) for item in (profile.get("employments") or ACCEPTED_EMPLOYMENTS)
        if str(item) in _EMPLOYMENT_SCORES
    ) or ACCEPTED_EMPLOYMENTS
    params: dict = {
        "only_with_salary": bool(only_with_salary),
        "employment": employments,
        "experience": SEARCH_EXPERIENCE_IDS,
        "order_by": order_by,
        "period": int(period),
    }
    if salary_floor > 0:
        params["salary"] = int(salary_floor)
    if include_area:
        area_id = HH_AREA_IDS.get(str(profile.get("area") or "").strip().lower())
        if area_id:
            params["area"] = (area_id,)
    if professional_role_ids:
        params["professional_role"] = tuple(str(item) for item in professional_role_ids)
    return params


# ── Threshold calibration helpers ────────────────────────────────────────


def threshold_report(scores: Sequence[int]) -> dict:
    """Percentile summary of a batch of totals, for re-checking thresholds.

    Feed it the deterministic totals produced by re-ranking the stored
    variants (see :func:`rescore_payloads`) instead of guessing at
    ``min_preview_score`` / ``min_apply_score``::

        from candidate.ranking import rescore_payloads, threshold_report
        rows = rescore_payloads(payloads, profiles)
        print(threshold_report([row["deterministic_total"] for row in rows]))
    """
    values = sorted(int(score) for score in scores)
    if not values:
        return {"count": 0}

    def percentile(fraction: float) -> int:
        index = min(len(values) - 1, max(0, int(round(fraction * (len(values) - 1)))))
        return values[index]

    return {
        "count": len(values),
        "min": values[0],
        "max": values[-1],
        "mean": round(sum(values) / len(values), 1),
        "p10": percentile(0.10), "p25": percentile(0.25), "p50": percentile(0.50),
        "p75": percentile(0.75), "p90": percentile(0.90),
        "distinct": len(set(values)),
        "suggested_min_preview_score": percentile(0.35),
        "suggested_min_apply_score": percentile(0.75),
    }


def rescore_payloads(
    payloads: Iterable[Mapping],
    profiles: Mapping[str, Mapping],
    *,
    trap_verdicts: Mapping[str, Any] | None = None,
    salary_floor: int = SALARY_FLOOR,
) -> list[dict]:
    """Re-rank stored ``candidate_variants`` payloads with the new model.

    Each payload is the dict saved by ``CandidateVariant.as_payload`` — it
    carries ``vacancy``, ``master_profile.key`` and the old
    ``score.value``. Returns one row per payload with both scores so the old
    and new scales can be compared side by side.
    """
    traps = trap_verdicts or {}
    rows: list[dict] = []
    for payload in payloads:
        vacancy = payload.get("vacancy") or {}
        key = str((payload.get("master_profile") or {}).get("key") or "")
        profile = profiles.get(key)
        if profile is None:
            continue
        old_score = int(((payload.get("score") or {}).get("value")) or 0)
        verdict = rank_vacancy(
            vacancy, profile, profile_key=key,
            trap_verdict=traps.get(str(payload.get("id") or "")),
            llm_score=None, salary_floor=salary_floor,
        )
        rows.append({
            "id": str(payload.get("id") or ""),
            "profile_key": key,
            "status": str(payload.get("status") or ""),
            "old_score": old_score,
            "deterministic_total": verdict.deterministic_total,
            "total": verdict.total,
            "hard_reject": verdict.hard_reject,
            "hard_reject_reason": verdict.hard_reject_reason,
            "factors": {factor.key: round(factor.raw, 3) for factor in verdict.factors},
        })
    return rows
