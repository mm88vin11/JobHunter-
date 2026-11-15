from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from openai import OpenAI, OpenAIError

from .data import CandidateData, MasterProfile, load_candidate_data
from .ranking import PROFILE_BLOCKER_CAP, combine_scores

log = logging.getLogger(__name__)


class UnsafeVariantError(ValueError):
    pass


_PROJECT_MARKERS = (
    "проектная занятость", "проектная работа", "работа на проект", "за проект",
    "сдельная оплата", "гпх", "фриланс", "freelance", "contract", "контрактная работа",
)
_PAYMENT_MARKERS = (
    "фикс за", "фиксированная оплата", "по этапам", "сдельн", "performance-based",
    "performance based", "оплата за проект", "бонус за результат", "вознаграждение за результат",
)
_CASE_MARKERS = (
    "создал", "создала", "создавал", "собрал", "собрала", "запустил", "запускал", "разработал", "внедрил",
    "автоматиз", "реализовал", "построил", "довёл", "довел", "увеличил", "сократил",
    "обеспечил", "настроил", "провёл", "провел", "использует", "работал", "управлял",
    "организовал", "организовывал", "посещал",
)
_PUBLIC_DENYLIST = (
    "кандидат", "репозиторий подтверждает", "рабочий репозиторий",
    "fact_id", "publication_status", "publication status", "manifest", "source line",
    "подтверждено кандидатом", "не переносить в публичное резюме", "требует уточнения",
    "создал и развивает", "имеет опыт",
)
# Internal bookkeeping wording that must never reach a recruiter.
_SERVICE_VOCABULARY = (
    "из подтверждённого опыта", "из подтвержденного опыта", "из проверенного опыта",
    "подтверждённый кейс", "подтвержденный кейс", "подтверждённые факты", "подтвержденные факты",
    "подтверждённый факт", "из банка фактов", "банк фактов", "согласно базе фактов",
    "public_confirmed", "public confirmed", "needs_confirmation", "do_not_publish",
    "knowledge base", "knowledge_base", "master profile", "master-профиль",
    "fallback", "внутренний rationale", "grounding", "denylist", "prompt",
)
# Statements that read as an admission of weakness or as a raw skill list instead of a result.
_SELF_DIMINISHING = (
    "не классический разработчик", "не являюсь разработчиком",
    "нет опыта", "не имею опыта", "не хватает опыта", "мало опыта",
    "не владею",
)
# Nouns that give a bare number its meaning ("около 4 000 человек" of WHAT).
_CONTEXT_NOUNS = (
    "продукт", "продукт", "систем", "бот", "ассистент", "магазин", "бизнес", "решени",
    "платформ", "сервис", "модел", "инференс", "бренд", "компан", "проект", "отчёт",
    "отчет", "документ", "подписк", "выручк", "мощност", "ассортимент", "артикул",
    "пользовател", "аудитор", "контур", "прототип", "закупк", "фабрик", "воронк",
    "агент", "пайплайн",
)
_LETTER_MIN_CHARS = 180
_LETTER_MAX_CHARS = 420
# An employer's own questionnaire or requested case may genuinely need more room. The
# default letter never gets this allowance; it stays short by construction.
_EMPLOYER_LETTER_MIN_CHARS = 80
_EMPLOYER_LETTER_MAX_CHARS = 750
# One write plus repairs; failed checks are fed back into the next attempt.
_LETTER_MAX_ATTEMPTS = 4
# Which named entity each fact-bank section describes, and who owns it.
#
# Facts are handed to the model grouped by project so a claim about one entity
# cannot accidentally lean on a fact that belongs to another.
#
# The open-source core deliberately ships without candidate-specific project names.
# An application may extend these maps when its fact bank needs explicit grouping.
_LETTER_PROJECT_OWNERS: dict[str, tuple[str, str]] = {}
def _own_project_names() -> tuple[str, ...]:
    """Projects the candidate owns, read off the owner descriptions above."""
    return tuple(
        name for name, owner in _LETTER_PROJECT_OWNERS.values() if "собственн" in owner
    )


_LETTER_PROJECT_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = ()
_UNSCOPED_PROJECT = (
    "Общий контекст (без своего проекта)",
    "эти факты не принадлежат ни одному названному проекту: владелец каждого утверждения — "
    "тот, кто назван внутри самого факта (заказчик, клиент, собственный продукт). Если "
    "внутри факта владелец не назван, факт нельзя приписывать ни одному названному проекту",
)
_SERVICE_PREFIX_RE = re.compile(
    r"(?i)\b(?:из\s+(?:подтвержд[её]нного|проверенного)\s+опыта|подтвержд[её]нный\s+кейс"
    r"|подтвержд[её]нный\s+факт|из\s+банка\s+фактов|для\s+справки)\s*[:—-]\s*"
)


def strip_service_vocabulary(letter: str) -> str:
    """Remove internal bookkeeping connectors without touching the claim behind them."""
    cleaned = _SERVICE_PREFIX_RE.sub("", letter)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


_EM_DASH_RE = re.compile(r"\s*—\s*")


def humanize_punctuation(letter: str) -> str:
    """Drop the em dash, the loudest generation tell in Russian, without a model call.

    Spending a repair attempt on punctuation is waste: a comma carries the same clause,
    and the attempt budget is needed for claims the checker cannot fix by itself.
    """
    return re.sub(r",\s*,", ",", _EM_DASH_RE.sub(", ", letter)).strip(" ,")


def vacancy_raises_payment_topic(vacancy: dict) -> bool:
    """True when the employer itself opened the fee/bonus conversation."""
    text = " ".join(str(vacancy.get(key, "")) for key in (
        "name", "description", "raw_text", "normalized_text", "employment", "schedule",
    )).lower()
    return any(marker in text for marker in _PAYMENT_MARKERS + _PROJECT_MARKERS) or any(
        marker in text for marker in ("ожидания по оплате", "зарплатные ожидания", "желаемая зарплата")
    )


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zA-Zа-яёА-ЯЁ0-9][\w-]*", text)


_HARD_BANNED = (
    "очень нужна работа", "что угодно за деньги", "24/7", "24 часа в сутки",
    "я в отчаянии", "восхищён вашей компанией", "компания моей мечты",
    "меня интересуют только деньги", "готов на всё",
)
_TOOL_TOKENS = (
    "python", "docker", "llm", "crm", "sql", "api",
)
# Markers that tie a number to something the candidate actually owns or delivered.
_SUBJECT_MARKERS = (
    "собственн", "свой", "своё", "свои", "своего", "своей", "своих",
    "мой", "моего", "моей", "мою", "у меня",
    "заказчик", "клиент", "компани", "бренд", "фабрик", "магазин",
)
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9./-]{2,}")
#: Tells that a Russian text was generated, in the order they give it away. The em dash is
#: the loudest one in 2026; the «не X, а Y» antithesis is second. Both survive every voice
#: instruction unless something checks for them.
_AI_ANTITHESIS = ("не просто ", "а не ", "больше чем ", "нечто большее")
#: The canonical «не X, а Y», whose two halves sit too far apart for a substring match —
#: «умею строить не разовые скрипты, а воспроизводимые пайплайны» sailed past the list.
_AI_ANTITHESIS_RE = re.compile(r"\bне\s+[^,.;:!?]{2,45},\s*а\s+", re.IGNORECASE)
#: Openings that read the employer's own posting back to them. A letter that starts
#: «Вам нужно быстро находить идеи для мобильных приложений» tells them nothing they
#: did not write themselves — and spends the one paragraph they will actually read.
_PARAPHRASE_OPENERS = (
    "вам нужно", "вам предстоит", "вам требуется", "вам важно", "у вас стоит задача",
    "у вас задача", "вы ищете", "вижу, что вы", "вижу что вы", "судя по вакансии",
    "в вашей вакансии", "ваша задача", "перед вами стоит",
)


def _has_identified_subject(text: str) -> bool:
    """True when the text names what a number refers to, not just 'продукт' in the abstract."""
    lower = text.lower()
    if any(marker in lower for marker in _SUBJECT_MARKERS):
        return True
    if _LATIN_TOKEN_RE.search(text):
        return True
    for sentence in _sentences(text):
        for word in _words(sentence)[1:]:
            if word[:1].isupper() and len(word) > 2:
                return True
    return False


# Words too common in job ads to prove a letter was written for THIS vacancy.
_MARKER_STOPWORDS = frozenset((
    "работа", "работы", "работу", "компания", "компании", "вакансия", "вакансии", "опыт",
    "опыта", "требования", "обязанности", "условия", "будет", "плюсом", "который", "которые",
    "нашей", "нашего", "команды", "команде", "также", "нужно", "можно", "свои", "нами",
    "офис", "офисе", "удаленно", "удалённо", "полная", "занятость", "штат", "оформление",
    "специалист", "менеджер", "старший", "ведущий", "должность", "уровень", "нашу", "если",
))
#: How many of the vacancy's own terms a letter must visibly answer. Two is enough to
#: separate "written for this posting" from "written for any posting", and low enough
#: that a well-aimed 500-character letter clears it without keyword stuffing.
_LETTER_MIN_MARKERS = 2


def vacancy_markers(vacancy: dict, limit: int = 14) -> tuple[str, ...]:
    """The distinctive terms of THIS vacancy, title first, then tasks and requirements.

    A cover letter that shares none of them is a template with the role name pasted on —
    exactly what recruiters bin. `validate_outgoing_letter` requires a few of them back,
    and the letter prompt receives the same list so the writer knows what to answer.
    """
    sections = analyze_vacancy_description(vacancy)["sections"]
    body = " ".join(list(sections.get("tasks") or []) + list(sections.get("requirements") or []))
    # Not every posting uses «Обязанности:»/«Требования:» headings; when the parser finds
    # none, the raw description is still this vacancy's own vocabulary.
    body = body or str(vacancy.get("description") or vacancy.get("normalized_text") or "")
    # hh.ru's own key_skills come right after the title: the employer tagged them itself,
    # so they are this posting's vocabulary with none of the boilerplate around it.
    skills = " ".join(str(item) for item in (vacancy.get("key_skills") or []))
    markers: list[str] = []
    for source in (str(vacancy.get("name") or ""), skills, body):
        for word in _words(source):
            token = word.lower()
            # Tool names are short and are the sharpest markers there are: n8n, SQL, API.
            floor = 3 if token.isascii() else 4
            if len(token) < floor or token.isdigit() or token in _MARKER_STOPWORDS:
                continue
            if token not in markers:
                markers.append(token)
    return tuple(markers[:limit])


def matched_vacancy_markers(letter: str, vacancy: dict) -> tuple[str, ...]:
    """Markers the letter actually picks up, compared on stems so Russian cases match."""
    # ponytail: 5-char prefix instead of a stemmer. «автоматизация» / «автоматизацию»
    # share a stem; a real morphological analyser is a dependency for no extra signal.
    stems: dict[str, str] = {}
    for marker in vacancy_markers(vacancy):
        stems.setdefault(marker[:5], marker)  # first form wins, so the title's wording is reported
    # A concise letter naturally says «AI-автоматизация» or «n8n-интеграции».
    # Treat both sides of a hyphen as words, otherwise the most relevant terms vanish.
    used = {word.lower()[:5] for word in _words(letter.replace("-", " "))}
    return tuple(sorted(marker for stem, marker in stems.items() if stem in used))


def _normalized_letter(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _vacancy_key(vacancy: dict) -> str:
    for key in ("id", "external_id", "source_url", "content_fingerprint", "fingerprint", "name"):
        value = str(vacancy.get(key) or "").strip()
        if value:
            return value
    return ""



DEFAULT_MODEL = "replace-with-model-id"
DEFAULT_LETTER_MODEL = DEFAULT_MODEL
# Settings screen wording -> the tone vocabulary the letter prompt and policy already use.
OWNER_TONES = {
    "спокойный": "calm", "прямой": "direct", "уверенный": "bold",
    "calm": "calm", "direct": "direct", "bold": "bold",
}


def normalize_owner_tone(tone: str | None) -> str:
    """Map the settings choice to a policy tone; empty means no preference."""
    return OWNER_TONES.get(str(tone or "").strip().lower(), "")


@dataclass(frozen=True)
class LetterCheck:
    code: str
    ok: bool
    message: str


@dataclass(frozen=True)
class LetterGuardReport:
    """Result of the pre-send validation of a cover letter."""

    letter_source: str
    checks: tuple[LetterCheck, ...]

    @property
    def failures(self) -> tuple[LetterCheck, ...]:
        return tuple(check for check in self.checks if not check.ok)

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def sendable(self) -> bool:
        """Output that passed every check. A letter the grounding verifier rejected is kept
        for editing under `model_unverified` and is never sendable as it stands."""
        return self.ok and self.letter_source in {"model", "manual", "language_gap"}

    @property
    def reason(self) -> str:
        return "; ".join(check.message for check in self.failures)

    def as_dict(self) -> dict:
        return {
            "letter_source": self.letter_source,
            "ok": self.ok,
            "sendable": self.sendable,
            "failed_checks": [check.code for check in self.failures],
            "reasons": [check.message for check in self.failures],
        }


def classify_vacancy_type(vacancy: dict) -> str:
    explicit = str(vacancy.get("vacancy_type", "")).lower()
    if explicit in {"full_time", "project", "contract", "unknown"}:
        return explicit
    text = " ".join(str(vacancy.get(key, "")) for key in (
        "name", "description", "raw_text", "employment", "schedule",
    )).lower()
    if any(marker in text for marker in _PROJECT_MARKERS):
        return "contract" if any(marker in text for marker in ("contract", "контракт", "гпх")) else "project"
    if any(marker in text for marker in ("полная занятость", "штат", "full-time", "full time")):
        return "full_time"
    return "unknown"


def detect_profile_blockers(vacancy: dict) -> tuple[str, ...]:
    """Return requirements matching an optional operator-defined denylist.

    ``CANDIDATE_REQUIREMENT_BLOCKERS`` is a comma-separated list. With no
    setting, the public core makes no assumptions about the candidate.
    """
    text = " ".join(
        str(vacancy.get(key, "")) for key in ("name", "description", "raw_text")
    ).casefold()
    configured = tuple(
        item.strip().casefold()
        for item in os.environ.get("CANDIDATE_REQUIREMENT_BLOCKERS", "").split(",")
        if item.strip()
    )
    return tuple(
        f"Требование вакансии совпало с настроенным ограничением: «{phrase}»"
        for phrase in configured if phrase in text
    )


def detect_profile_warnings(vacancy: dict) -> tuple[str, ...]:
    """Compatibility hook for application-specific, non-blocking warnings."""
    return ()


def analyze_vacancy_description(vacancy: dict) -> dict:
    text = str(vacancy.get("description") or vacancy.get("normalized_text") or "").strip()
    normalized = re.sub(r"\s+", " ", text)
    # Real headings from live hh.ru postings, not a guess at what employers write.
    # A missing alias is not a cosmetic loss: the card then shows «Условия» alone or a
    # raw fragment, and `vacancy_markers` falls back to the whole description, so the
    # "written for this vacancy" check weakens exactly where the parser already missed.
    aliases = {
        "tasks": (
            "обязанности", "задачи", "основные задачи", "ключевые задачи", "твои задачи",
            "ваши задачи", "что делать", "что нужно делать", "что нужно будет делать",
            "что предстоит делать", "чем вы будете заниматься", "чем будете заниматься",
            "что вы будете делать", "чем ты будешь заниматься", "чем предстоит заниматься",
            "зона ответственности", "функционал",
        ),
        "requirements": (
            "требования", "требования к кандидату", "наши требования", "кого мы ищем",
            "мы ищем", "кто нам нужен", "ожидаем от вас", "наши ожидания", "ждём", "ждем",
            "мы ждём", "мы ждем", "что мы ждём", "что мы ждем",
            "что мы ждём от кандидатов", "что мы ждем от кандидатов", "что важно",
            "необходимый опыт", "будет плюсом",
            "кого мы точно не возьмём", "кого мы точно не возьмем",
        ),
        "conditions": (
            "условия", "условия работы", "что предлагаем", "мы предлагаем",
            "что мы предлагаем", "что даём", "что даем", "что мы даём", "что мы даем",
            "что вы получите", "что ты получишь",
        ),
    }
    heading_names = sorted(
        (name for names in aliases.values() for name in names), key=len, reverse=True,
    )
    heading_pattern = "|".join(re.escape(name) for name in heading_names)
    # Preserve natural HTML/list line breaks and also split compact `Heading: text`
    # descriptions without treating an incidental keyword in a sentence as a heading.
    # A posting pasted as one unbroken paragraph has no line breaks to anchor to, and
    # there its «Задачи:» sits mid-line — the end of the previous sentence is then the
    # only honest boundary, so it is allowed only for those descriptions.
    boundary = (
        r"(?:(?<=\n)|^)" if text.count("\n") >= 2 else r"(?:(?<=[.!?])\s+|(?<=\n)|^)"
    )
    marked = re.sub(
        rf"(?i){boundary}\s*({heading_pattern})(\s*\([^\n)]*\))?\s*:\s*",
        r"\n\1\2:\n", text,
    )
    sections: dict[str, list[str]] = {key: [] for key in aliases}
    current = ""
    for raw_line in marked.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" •-\t")
        if not line:
            continue
        matched = ""
        remainder = ""
        for key, names in aliases.items():
            for name in names:
                # Either the line IS the heading, or a separator follows it. Without this
                # «Ждём вас в команде!» would be read as the requirements heading and
                # swallow the rest of the posting under it.
                heading = re.match(
                    rf"^{re.escape(name)}(?:\s*\([^)]*\))?\s*(?:[:.—-]\s*(.*)|$)",
                    line, flags=re.IGNORECASE,
                )
                if heading:
                    matched = key
                    remainder = (heading.group(1) or "").strip(" ,:—-")
                    break
            if matched:
                break
        if matched:
            current = matched
            if remainder:
                sections[current].append(remainder[:400])
            continue
        if current and len(sections[current]) < 6:
            sections[current].append(line[:400])
    return {
        "full_description_loaded": len(normalized) >= 80,
        "description_chars": len(normalized),
        "description_sha256": hashlib.sha256(normalized.encode()).hexdigest() if normalized else "",
        "sections": sections,
        "apply_instructions": list(extract_apply_instructions(vacancy)),
        "screening_steps": list(extract_screening_steps(vacancy)),
    }


# Sentences that carry an employer's own instruction for the reply — a filter the
# employer reads before the letter itself ("начните письмо со слов «AI first»").
_APPLY_INSTRUCTION_MARKERS = (
    "сопроводительн", "начните письмо", "начни письмо", "начать письмо", "начните отклик",
    "в начале письма", "в начале сопроводительного", "первым словом", "первой строкой",
    "кодовое слово", "кодовую фразу", "кодовая фраза", "в теме письма",
    "укажите в отклике", "напишите в отклике", "ответьте на вопрос", "ответьте на вопросы",
    "чтобы мы поняли, что вы", "чтобы понять, что вы", "чтобы мы видели, что вы",
    # Measured against 104 live postings: the markers above caught 8 of them while these
    # phrasings carried the rest — «Как откликнуться», «при отклике», «пришлите», and the
    # outright rejection rule «отклик без примера не рассматриваем».
    "как откликнуться", "при отклике", "в отклике", "к отклику", "вместе с откликом",
    "вместе с резюме", "отклик без", "не рассматриваем без", "не рассматриваются без",
    "приложите", "пришлите", "прикрепите", "в 3-5 строках", "в 3–5 строках",
)
#: Headings that announce the reply protocol; the sentences under them are the protocol.
_APPLY_HEADING_RE = re.compile(
    r"^(как откликнуться|как отклик\w+|как подать заявку|как отправить отклик|"
    r"порядок отклика|что сделать для отклика|как с нами связаться)\b"
    r"\s*(?:[:.—-]\s*(.*)|$)",
    re.IGNORECASE,
)
#: Stages of the hiring process worth showing the owner, but never instructions to the
#: writer: «после собеседования — тестовое задание» is a fact about the funnel, and a
#: letter that tries to obey it as a requirement writes nonsense.
_SCREENING_MARKERS = (
    "тестовое задание", "тестового задания", "тестовым заданием", "тестовое",
    "анкету", "анкета", "анкеты", "опросник", "пройти опрос", "заполните форму",
)
_QUOTED_PHRASE = re.compile(r"[«\"'“„]([^»\"'”“\n]{2,60})[»\"'”]")
_OPENING_MARKERS = (
    "начните", "начни ", "начать", "первым словом", "первой строкой", "в начале письма",
    "в начале сопроводительного", "начните отклик",
)


def extract_apply_instructions(vacancy: dict) -> tuple[str, ...]:
    """Employer instructions the reply must obey verbatim, lifted out of the description.

    A posting that asks for a code phrase uses it as its first filter: the letter can be
    perfect and still be dropped unread. These sentences used to dissolve into the
    description like any other text, so the writer never saw them as requirements.
    """
    found: list[str] = []
    follow = 0
    for sentence in _instruction_sentences(vacancy):
        heading = _APPLY_HEADING_RE.match(sentence)
        if heading:
            # «Как откликнуться» carries no requirement by itself; the next few sentences do.
            follow = 3
            remainder = (heading.group(2) or "").strip(" ,:—-")
            if remainder and remainder not in found:
                found.append(remainder)
            continue
        if any(marker in sentence.lower() for marker in _APPLY_INSTRUCTION_MARKERS):
            if sentence not in found:
                found.append(sentence)
            continue
        if follow:
            follow -= 1
            # «После собеседования — тестовое задание» sits inside the reply protocol but
            # is not an order to the writer; obeying it as one produces nonsense.
            if sentence not in found and not _is_screening_sentence(sentence):
                found.append(sentence)
    return tuple(found[:6])


def _is_screening_sentence(sentence: str) -> bool:
    lowered = sentence.lower()
    if any(marker in lowered for marker in _APPLY_INSTRUCTION_MARKERS):
        return False  # an explicit demand wins over the stage it happens to mention
    return any(marker in lowered for marker in _SCREENING_MARKERS)


def _instruction_sentences(vacancy: dict) -> list[str]:
    text = str(vacancy.get("description") or vacancy.get("normalized_text") or "")
    sentences = []
    for raw in re.split(r"(?<=[.!?…])\s+|\n+", text):
        sentence = re.sub(r"\s+", " ", raw).strip(" •-\t")
        if sentence and len(sentence) <= 400:
            sentences.append(sentence)
    return sentences


def extract_screening_steps(vacancy: dict) -> tuple[str, ...]:
    """Hiring stages the posting announces — a test task, a form, a questionnaire.

    Shown on the card so the owner knows an application here costs more than a letter.
    Deliberately kept out of `apply_instructions`: these are not orders to the writer.
    """
    found: list[str] = []
    for sentence in _instruction_sentences(vacancy):
        if _is_screening_sentence(sentence) and sentence not in found:
            found.append(sentence)
    return tuple(found[:3])


def required_letter_openings(vacancy: dict) -> tuple[str, ...]:
    """The exact phrases an employer demanded the letter start with, if any."""
    openings: list[str] = []
    for instruction in extract_apply_instructions(vacancy):
        lowered = instruction.lower()
        if not any(marker in lowered for marker in _OPENING_MARKERS):
            continue
        for phrase in _QUOTED_PHRASE.findall(instruction):
            cleaned = phrase.strip()
            if cleaned and cleaned not in openings:
                openings.append(cleaned)
    return tuple(openings)


def letter_opens_with(letter: str, phrase: str) -> bool:
    def normalize(value: str) -> str:
        return re.sub(r"[^0-9a-zа-яё]+", " ", value.lower()).strip()
    return normalize(letter).startswith(normalize(phrase))


# ── LLM judge: fit / gaps / reply-chance verdict ─────────────────────────

_REPLY_CHANCE_VALUES = ("высокий", "средний", "низкий")

_VACANCY_JUDGE_SYSTEM_PROMPT = (
    "Ты внутренний эксперт по найму. Не делай предположений о месте жительства, доходе, "
    "языках или предпочтениях кандидата: используй только переданный профиль. "
    "Сравни конкретные требования вакансии (vacancy_title, vacancy_text) с профилем "
    "кандидата (profile_title, profile_about, profile_skills) и верни строгий JSON по "
    "output_schema. Каждая причина в fit_reasons и missing_reasons ОБЯЗАНА называть конкретное "
    "требование или формулировку из vacancy_text -- не общую похвалу вроде «хороший кандидат» и "
    "не лесть работодателю. Если реального совпадения или реального пробела нет, оставь список "
    "короче, не выдумывай."
)


def _judge_vacancy_text(vacancy: dict) -> str:
    """The vacancy's own requirements/tasks, concise -- what the judge is grounded in."""
    try:
        sections = analyze_vacancy_description(vacancy).get("sections") or {}
    except Exception:  # a malformed vacancy dict must not block the judge call
        sections = {}
    bits = list(sections.get("requirements") or [])[:6] + list(sections.get("tasks") or [])[:4]
    text = " ".join(str(item) for item in bits)
    if not text:
        text = str(vacancy.get("description") or vacancy.get("normalized_text") or "")
    return text[:2000]


def _judge_prompt(vacancy: dict, profile: Mapping) -> str:
    return json.dumps({
        "vacancy_title": str(vacancy.get("name") or ""),
        "vacancy_text": _judge_vacancy_text(vacancy),
        "profile_title": str(profile.get("title") or ""),
        "profile_about": str(profile.get("about") or "")[:800],
        "profile_skills": [str(item) for item in (profile.get("skill_set") or [])][:30],
        "output_schema": {
            "score": "integer 0..100, overall fit for THIS vacancy",
            "fit_reasons": ["short string citing a concrete vacancy requirement the candidate meets"],
            "missing_reasons": [
                "short string citing a concrete requirement the candidate does not meet and how critical it is",
            ],
            "reply_chance": "один из: высокий, средний, низкий",
            "reply_chance_reason": "short string",
        },
    }, ensure_ascii=False)


def _coerce_reason_list(value: object, *, limit: int = 5, max_len: int = 220) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value:
        text = re.sub(r"\s+", " ", str(item)).strip()
        if text:
            result.append(text[:max_len])
        if len(result) >= limit:
            break
    return tuple(result)


def gonka_internal_enabled() -> bool:
    return os.environ.get("GONKA_INTERNAL_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def judge_vacancy_fit(
    vacancy: dict,
    profile: Mapping,
    ranking_score: Mapping,
    *,
    judge_completion: Callable[..., dict] | None = None,
) -> dict | None:
    """One strict-JSON DeepSeek verdict over one vacancy + the chosen profile, or ``None``.

    ``ranking_score`` is ``payload["score"]`` -- ``candidate/ranking.py::RankingVerdict.as_dict()``
    -- the deterministic backbone this only ever *adjusts*, never replaces. Returns ``None``
    (never raises) when: the Gonka flag is off, no keys are configured, the call times out or
    errors, or the response is not exactly the expected shape. ``judge_completion`` is a test
    seam matching ``hh.gonka.complete_json``'s signature (keyword args ``prompt``/``system``/
    ``max_tokens``/``temperature``, returns a dict); production leaves it unset and calls Gonka.
    """
    if judge_completion is None:
        if not gonka_internal_enabled():
            return None
        from hh.gonka import complete_json  # lazy: mirrors CandidateAdapter's own gonka import

        judge_completion = complete_json

    try:
        raw = judge_completion(
            prompt=_judge_prompt(vacancy, profile),
            system=_VACANCY_JUDGE_SYSTEM_PROMPT,
            max_tokens=700,
            temperature=0.0,
        )
    except Exception as exc:  # GonkaError + anything else -- a card must never crash over this
        log.warning("Vacancy LLM judge call failed safely: %s", type(exc).__name__)
        return None

    if not isinstance(raw, dict):
        log.warning("Vacancy LLM judge returned a non-object response")
        return None
    try:
        llm_score = max(0, min(100, int(raw["score"])))
    except (KeyError, TypeError, ValueError):
        log.warning("Vacancy LLM judge response missing a usable 'score'")
        return None
    reply_chance = str(raw.get("reply_chance") or "").strip().lower()
    if reply_chance not in _REPLY_CHANCE_VALUES:
        log.warning("Vacancy LLM judge returned unexpected reply_chance=%r", reply_chance)
        return None

    fit_reasons = _coerce_reason_list(raw.get("fit_reasons"))
    missing_reasons = _coerce_reason_list(raw.get("missing_reasons"))
    reply_chance_reason = re.sub(r"\s+", " ", str(raw.get("reply_chance_reason") or "")).strip()[:300]

    try:
        deterministic_total = int(ranking_score.get("deterministic_total", ranking_score.get("value", 0)) or 0)
    except (TypeError, ValueError):
        deterministic_total = 0
    total_with_llm, adjustment = combine_scores(deterministic_total, llm_score)
    # combine_scores() runs before the profile-blocker / hard-tech caps inside
    # candidate/ranking.py::rank_vacancy, and both caps share the same PROFILE_BLOCKER_CAP
    # value -- replaying either flag here reproduces the same clamp without re-deriving the
    # blocker list, which the stored payload does not carry.
    if ranking_score.get("hard_tech_gated") or ranking_score.get("blocker_capped"):
        total_with_llm = min(total_with_llm, PROFILE_BLOCKER_CAP)

    return {
        "score": llm_score,
        "adjustment": adjustment,
        "total_with_llm": total_with_llm,
        "fit_reasons": list(fit_reasons),
        "missing_reasons": list(missing_reasons),
        "reply_chance": reply_chance,
        "reply_chance_reason": reply_chance_reason,
        # Mirrors hh/gonka.py's own DEFAULT_GONKA_MODEL constant; kept as a literal here so this
        # module never needs an unconditional top-level import of hh.gonka just for one string.
        "model": os.environ.get("GONKA_INTERNAL_MODEL", "").strip() or "deepseek-ai/DeepSeek-V4-Flash-0731",
    }


def _json_object(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise UnsafeVariantError("LLM did not return JSON") from exc
        value = json.loads(match.group())
    if not isinstance(value, dict):
        raise UnsafeVariantError("LLM response is not an object")
    return value


@dataclass(frozen=True)
class CandidateVariant:
    id: str
    profile_key: str
    score: int
    reasons: tuple[str, ...]
    internal_rationale: tuple[str, ...]
    title: str
    about: str
    achievements: tuple[str, ...]
    skills: tuple[str, ...]
    cover_letter: str
    cover_letter_tone: str
    used_fact_ids: tuple[str, ...]
    diff: str
    resume_document: dict
    resume_analysis: dict
    model: str
    letter_source: str = "model"
    letter_review: dict = field(default_factory=dict)
    vacancy_summary: tuple[str, ...] = ()

    @property
    def letter_is_sendable(self) -> bool:
        """False whenever the letter came from the deterministic draft or failed the guard."""
        return bool(self.letter_review.get("sendable", self.letter_source == "model"))

    def as_payload(self, *, user_id: int, vacancy: dict, data_version: str, master: MasterProfile) -> dict:
        sendable = self.letter_is_sendable
        review = dict(self.letter_review)
        review["requires_manual_letter"] = not sendable
        return {
            "id": self.id,
            "user_id": user_id,
            # A degraded letter is never a draft the owner can approve straight through:
            # `manual_required` blocks both auto-apply and the manual send button, and only
            # the "написать письмо" flow can move the card back to `draft`.
            "status": "draft" if sendable else "manual_required",
            "letter_review": review,
            "data_version": data_version,
            "vacancy": vacancy,
            "master_profile": {"key": master.key, "name": master.name, "markdown": master.markdown},
            "generated_variant": {
                "title": self.title,
                "about": self.about,
                "achievements": list(self.achievements),
                "skills": list(self.skills),
                "cover_letter": self.cover_letter,
                "cover_letter_tone": self.cover_letter_tone,
            },
            "resume_document": self.resume_document,
            "diff": self.diff,
            "used_fact_ids": list(self.used_fact_ids),
            "score": {"value": self.score, "reasons": list(self.reasons)},
            "vacancy_summary": list(self.vacancy_summary),
            "analysis_internal": {
                "rationale": list(self.internal_rationale),
                "resume": self.resume_analysis,
            },
            "model": self.model,
            "prompt_version": "candidate-structured-v1",
        }


class CandidateAdapter:
    # Owner preferences from the settings hub; overwritten by `set_owner_preferences`.
    owner_tone: str = ""
    owner_denylist: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        model: str = DEFAULT_MODEL,
        letter_model: str | None = None,
        data: CandidateData | None = None,
        completion: Callable[[str], str] | None = None,
        letter_completion: Callable[[str], str] | None = None,
        owner_tone: str = "",
        owner_denylist: Iterable[str] = (),
    ):
        self.data = data or load_candidate_data()
        self.model = model
        # Bulk work (profile selection, grounding checks) runs on the cheap model; the letter
        # itself gets the strong one from OPENAI_LETTER_MODEL, falling back to `model`.
        self.letter_model = (
            letter_model
            or os.environ.get("OPENAI_LETTER_MODEL", "").strip()
            or model
        )
        self.set_owner_preferences(tone=owner_tone, denylist=owner_denylist)
        if completion is not None:
            self._completion = completion
            self._letter_completion = letter_completion or completion
        elif api_key:
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=180.0, max_retries=1)

            def make_call(model_name: str) -> Callable[[str], str]:
                def call(prompt: str) -> str:
                    response = client.chat.completions.create(
                        model=model_name,
                        temperature=0.1,
                        max_tokens=8000,
                        reasoning_effort="low",
                        messages=[
                            {"role": "system", "content": "Ты строго фактологичный карьерный редактор. Возвращай только JSON."},
                            {"role": "user", "content": prompt},
                        ],
                    )
                    return response.choices[0].message.content or ""

                return call

            self._completion = make_call(model)
            self._letter_completion = (
                self._completion if self.letter_model == model else make_call(self.letter_model)
            )
            # Internal (non-letter) work can optionally use a separate Gonka route.
            # With GONKA_INTERNAL_ENABLED unset, all calls use the configured
            # OpenAI-compatible endpoint above.
            if os.environ.get("GONKA_INTERNAL_ENABLED", "").strip().lower() in {"1", "true", "yes"}:
                from hh.gonka import make_gonka_completion_with_fallback

                self._completion = make_gonka_completion_with_fallback(
                    self._completion,
                    model=os.environ.get("GONKA_INTERNAL_MODEL", "").strip() or None,
                    max_tokens=8000,
                    temperature=0.1,
                )
        else:
            raise ValueError("An OpenAI-compatible API key is required")

    def set_owner_preferences(
        self, *, tone: str | None = None, denylist: Iterable[str] | None = None,
    ) -> None:
        """Hook for the settings screen: preferred tone and personal phrase denylist.

        `tone` accepts «спокойный» / «прямой» / «уверенный» (or calm/direct/bold); an empty
        value means "no preference" and the model picks the tone itself. `denylist` is a list
        of literal phrases the owner never wants to see; they are enforced by
        `validate_outgoing_letter` and avoided by the deterministic draft.
        """
        if tone is not None:
            self.owner_tone = normalize_owner_tone(tone)
        if denylist is not None:
            self.owner_denylist = tuple(
                str(phrase).strip().lower() for phrase in denylist if str(phrase).strip()
            )
        self._voice_denylist_cache = None

    def _complete_json(self, prompt: str, *, completion: Callable[[str], str] | None = None) -> dict:
        call = completion or self._completion
        error: UnsafeVariantError | None = None
        for _ in range(2):
            try:
                return _json_object(call(prompt))
            except OpenAIError as exc:
                raise UnsafeVariantError(f"LLM provider error: {type(exc).__name__}") from exc
            except (UnsafeVariantError, json.JSONDecodeError) as exc:
                error = UnsafeVariantError(str(exc))
        raise error or UnsafeVariantError("LLM did not return JSON")

    @staticmethod
    def _profile_tokens(profile: MasterProfile) -> set[str]:
        return set(re.findall(r"[a-zа-яё]{4,}", (profile.desired_title + " " + profile.about).lower()))

    def _keyword_match(self, vacancy: dict, profile: MasterProfile) -> float:
        """Share of the profile's own vocabulary the vacancy repeats, 0..1.

        Overlap is normalised by profile size on purpose. The raw intersection
        count that used to rank profiles rewarded whichever profile simply had
        the longest ``about``, which is how one profile could win every tie.
        """
        vacancy_tokens = set(re.findall(r"[a-zа-яё]{4,}", json.dumps(vacancy, ensure_ascii=False).lower()))
        profile_tokens = self._profile_tokens(profile)
        if not profile_tokens:
            return 0.0
        return len(vacancy_tokens & profile_tokens) / len(profile_tokens)

    def _best_profile_by_keywords(self, vacancy: dict) -> MasterProfile:
        """Deterministic profile pick when no model verdict is available.

        Ties break on the profile key, not on dict insertion order, so the
        answer does not silently depend on the order files load from disk.
        """
        return max(
            self.data.profiles.values(),
            key=lambda item: (self._keyword_match(vacancy, item), item.key),
        )

    def _forced_selection(self, vacancy: dict, profile: MasterProfile) -> dict:
        """A selection verdict without a model call, for an already-decided profile.

        Every vacancy the automation cycle prepares arrives with ``forced_profile``
        set: ``candidate/ranking.py::choose_profile`` picks the profile
        deterministically *before* generation, and ``mvp/automation.py`` then
        overwrites ``score`` and ``reasons`` with the ranker's own verdict. Paying
        a model — and shipping the entire fact bank, the largest prompt in this
        pipeline — to re-derive a decision already made changed nothing the owner
        could see. So when the profile is forced, the call is simply not made.
        """
        facts = self._relevant_public_facts(vacancy, profile, limit=3)
        return {
            "profile_key": profile.key,
            "score": max(40, min(80, 40 + int(self._keyword_match(vacancy, profile) * 100))),
            "public_reasons": [
                {"text": fact.text[:240], "supporting_fact_ids": [fact.id]} for fact in facts
            ],
            "internal_rationale": [
                "Профиль был выбран заранее (детерминированное ранжирование или ручной выбор); "
                "вызов модели на подбор профиля пропущен.",
            ],
        }

    def _fallback_selection(self, vacancy: dict, forced_profile: str | None) -> dict:
        profile = (
            self.data.profiles.get(forced_profile) if forced_profile
            else self._best_profile_by_keywords(vacancy)
        )
        facts = self._relevant_public_facts(vacancy, profile, limit=2)
        return {
            "profile_key": profile.key, "score": 50,
            "public_reasons": [{"text": fact.text[:240], "supporting_fact_ids": [fact.id]} for fact in facts],
            "internal_rationale": ["LLM selection JSON failed twice; deterministic keyword fallback."],
        }

    def _fallback_resume_response(self, vacancy: dict, profile: MasterProfile) -> dict:
        facts = self._relevant_public_facts(vacancy, profile, limit=20)
        fact_ids = [fact.id for fact in facts if self._is_concrete_fact(fact.text)][:8]
        if not fact_ids:
            fact_ids = [fact.id for fact in facts[:4]]
        return {
            "analysis": {
                "target_title": profile.desired_title, "industry": "",
                "must_have_keywords": [], "nice_to_have_keywords": [],
                "matched_fact_ids": fact_ids, "missing_requirements": [],
                "selection_rationale": ["LLM resume JSON failed twice; deterministic master patch."],
            },
            "resume_patch": {
                "headline": profile.desired_title,
                "summary": self._first_person_master_about(profile),
                "experience_order": [entry.id for entry in profile.experience],
                "project_order": [],
                "selected_skill_ids": [item_id for item_id, _ in profile.skills[:12]],
                "selected_achievement_ids": [item_id for item_id, _ in profile.achievements[:6]],
                "bullet_rewrites": [],
            },
        }

    def _selection_prompt(self, vacancy: dict, forced_profile: str | None) -> str:
        # `source` is dropped and long facts are clipped: the selector picks one
        # of four profiles and cites public ids, and neither task ever needed the
        # file a fact came from or its 300th character. Every publication status
        # still travels — the private half is what makes the internal choice
        # honest, and it never leaves `internal_rationale`.
        facts = [
            {"id": f.id, "text": f.text[:240], "status": f.publication_status}
            for f in self.data.facts
        ]
        profiles = [
            {
                "key": profile.key,
                "title": profile.desired_title,
                "about": profile.about,
                "guardrails": list(profile.guardrails),
            }
            for profile in self.data.profiles.values()
        ]
        return json.dumps({
            "task": (
                "Оцени вакансию и выбери один master profile. Для внутреннего анализа используй ВЕСЬ knowledge_base, "
                "включая internal/needs_confirmation/do_not_publish. public_reasons будут показаны пользователю: "
                "каждый reason должен опираться только на public_confirmed, содержать supporting_fact_ids и не включать числа. "
                "Всё, что основано на закрытых данных, помести только в internal_rationale; оно не попадёт во внешний текст."
            ),
            "forced_profile": forced_profile,
            "vacancy": vacancy,
            "knowledge_base": facts,
            "profiles": profiles,
            "output_schema": {
                "profile_key": "one profile key",
                "score": "integer 0..100",
                "public_reasons": [{"text": "short reason", "supporting_fact_ids": ["public_confirmed IDs"]}],
                "internal_rationale": ["private reasoning; never display"],
            },
        }, ensure_ascii=False)

    def _generation_prompt(self, vacancy: dict, profile: MasterProfile) -> str:
        public_facts = [
            {"id": f.id, "text": f.text, "source": f.source}
            for f in self._relevant_public_facts(vacancy, profile, limit=40)
        ]
        return json.dumps({
            "task": self.data.documents["resume_tailoring_prompt.md"],
            "vacancy": vacancy,
            "public_confirmed_facts": public_facts,
            "resume_voice_policy": self.data.documents["resume_voice.md"],
            "master_profile": {
                "key": profile.key,
                "headline": profile.desired_title,
                "summary": profile.about,
                "achievements": [{"id": i, "text": t} for i, t in profile.achievements],
                "skills": [{"id": i, "text": t} for i, t in profile.skills],
                "experience": [
                    {
                        "id": entry.id, "title": entry.title, "role": entry.role,
                        "dates": entry.dates, "bullet_ids": list(entry.bullet_ids),
                    }
                    for entry in profile.experience
                ],
                "projects": [],
                "guardrails": list(profile.guardrails),
            },
            "output_schema": {
                "analysis": {
                    "target_title": "string", "industry": "string",
                    "must_have_keywords": ["string"], "nice_to_have_keywords": ["string"],
                    "matched_fact_ids": ["public fact IDs"], "missing_requirements": ["string"],
                    "selection_rationale": ["internal strings"],
                },
                "resume_patch": {
                    "headline": "adapted headline", "summary": "first-person summary",
                    "experience_order": ["existing experience IDs"], "project_order": [],
                    "selected_skill_ids": ["existing skill IDs"],
                    "selected_achievement_ids": ["existing achievement IDs"],
                    "bullet_rewrites": [{
                        "source_id": "achievement ID", "text": "grounded rewrite",
                        "supporting_fact_ids": ["public fact IDs"],
                    }],
                },
            },
        }, ensure_ascii=False)

    @staticmethod
    def _facts_by_project(facts: Iterable) -> list[dict]:
        """Group the permitted facts by the project or client they belong to.

        A flat list of sentences is what let the model merge two separate projects into one
        false claim. Grouping is taken from the fact bank's own `### ` sections, so it cannot
        drift away from what the owner wrote.
        """
        groups: dict[str, dict] = {}
        for fact in facts:
            section = fact.category
            if section not in _LETTER_PROJECT_OWNERS:
                lower = fact.text.lower()
                section = next(
                    (
                        target for phrases, target in _LETTER_PROJECT_KEYWORDS
                        if any(phrase in lower for phrase in phrases)
                    ),
                    section,
                )
            name, owner = _LETTER_PROJECT_OWNERS.get(section, _UNSCOPED_PROJECT)
            group = groups.setdefault(name, {"project": name, "owner": owner, "facts": []})
            group["facts"].append({"id": fact.id, "text": fact.text})
        # Named projects first; the unscoped bucket last, so it reads as background.
        return sorted(groups.values(), key=lambda g: g["project"] == _UNSCOPED_PROJECT[0])

    def _letter_prompt_payload(self, vacancy: dict, profile: MasterProfile) -> dict:
        # The writer sees exactly the evidence the verifier will judge against — same call,
        # same limit. A writer with less context than the verifier cites facts it never saw.
        grouped = self._facts_by_project(
            self._relevant_public_facts(vacancy, profile, limit=40)
        )
        named = [
            {"name": group["project"], "owner": group["owner"]}
            for group in grouped if group["project"] != _UNSCOPED_PROJECT[0]
        ]
        vacancy_type = classify_vacancy_type(vacancy)
        payment_allowed = vacancy_type in {"project", "contract"} or vacancy_raises_payment_topic(vacancy)
        analysis = analyze_vacancy_description(vacancy)
        sections = analysis["sections"]
        instructions = list(analysis.get("apply_instructions") or [])
        min_chars, max_chars = (
            (_EMPLOYER_LETTER_MIN_CHARS, _EMPLOYER_LETTER_MAX_CHARS)
            if instructions else (_LETTER_MIN_CHARS, _LETTER_MAX_CHARS)
        )
        return {
            "task": (
                "Напиши отдельное сопроводительное письмо. Пиши от первого лица, живым "
                "разговорным русским, без канцелярита. Не добавляй письмо в резюме. "
                + (
                    "Формат этого письма задал сам работодатель — см. "
                    "employer_apply_instructions и structure_rules."
                    if instructions else
                    "Работодатель формат не задал: пиши обычное человеческое письмо о себе "
                    "и о том, чем будешь полезен, а не разбор их вакансии."
                )
            ),
            "letter_mode": "employer_instructed" if instructions else "free_form_value",
            # Two shapes, decided by the posting itself: obey the employer's own brief when
            # there is one, otherwise write forward — what I would do about their problem.
            # Past employers belong in the resume, not in the letter.
            "structure_rules": [
                "СТРУКТУРУ И СОДЕРЖАНИЕ ПИСЬМА ЗАДАЁТ РАБОТОДАТЕЛЬ: выполнить каждый пункт "
                "employer_apply_instructions дословно, целиком и в том порядке, в каком они "
                "написаны. Просит описать свой кейс в 3–5 строках — письмо и есть это "
                "описание. Просит ответить на вопросы — письмо отвечает на них по очереди. "
                "Просит назвать зарплатные ожидания — они названы числом или вилкой.",
                "Привычный нам формат письма здесь НЕ действует: не подгонять ответ под "
                "свою структуру, если работодатель попросил другую.",
                "Ни один пункт не пропускать: невыполненное требование — это отклик, "
                "который не станут читать.",
                "Если требуется начать письмо с определённых слов — письмо начинается ровно "
                "с них, буква в букву, и только потом идёт ответ по существу.",
            ] if instructions else [
                "Обычное письмо состоит из 3–5 коротких предложений: «Здравствуйте» → "
                "вакансия интересна → в каких направлениях могу быть полезен → с похожими "
                "задачами работал, подробности есть в резюме → готов обсудить.",
                "НЕ ПЕРЕСКАЗЫВАТЬ ВАКАНСИЮ. Работодатель написал её сам и читать её "
                "обратно не станет. Запрещено открывать письмо его задачами: «вам нужно…», "
                "«у вас стоит задача…», «вижу, что вы ищете…», «вам предстоит…», а также "
                "перечислять его же требования, метрики и инструменты списком.",
                "Про вакансию — максимум одна короткая фраза, чем именно она зацепила: "
                "конкретная задача или продукт. Без лести про лидера рынка и мечту.",
                "О пользе говорить ОБОБЩЁННО, через 1–2 направления из вакансии. Не раскрывать "
                "кейс, цифры, названия старых проектов, работодателей и должностей.",
                "Про опыт достаточно одной фразы: «С похожими задачами работал, подробнее в "
                "резюме». Старый опыт не демонстрировать и не доказывать, если работодатель "
                "сам не попросил кейс, пример, портфолио, цифры или ответы на вопросы.",
                "Не писать отдельный абзац о том, чего кандидат хочет от роли.",
            ],
            # Keep the voice concise and avoid over-emphasising personal projects.
            "human_voice_rules": [
                "НИ ОДНОГО длинного тире (—) в письме. В русском тексте 2026 года это "
                "самый узнаваемый признак нейросети. Вместо него: точка и новое "
                "предложение, двоеточие или запятая со связкой.",
                "Запрещены противопоставления «не X, а Y», «не просто X», «больше чем X». "
                "Говорить прямо, что есть.",
                "Не вставлять без необходимости английские слова внутрь русских фраз. "
                "Названия и устоявшиеся термины (API, SQL, LLM, App "
                "Store, Telegram) — нормально.",
                "Не украшать текст красивыми перечислениями («идея, гипотеза, метрики, "
                "деньги») и рублеными связками («без раскачки», «и точка»).",
                "Разная длина предложений. Не три одинаковых подряд.",
                "own_projects — собственные проекты кандидата. Их НЕ выпячивать: нельзя "
                "приводить по ним число пользователей, подписчиков и выручку и нельзя "
                "строить на них главное доказательство. Доказательство берётся из "
                "клиентской работы. Свой проект — максимум одна короткая фраза о том, что "
                "делал руками, без цифр.",
                "Лаконично. Короткое точное письмо сильнее длинного.",
                "Не раскрывать внутренние или запрещённые владельцем названия проектов.",
            ],
            "own_projects": list(_own_project_names()),
            "separation_rules": [
                "named_entities — это РАЗНЫЕ, не связанные между собой проекты и заказчики. "
                "Никогда не объединять их в одно утверждение и не переносить работу одного "
                "на другой.",
                "Утверждение про названный проект может опираться ТОЛЬКО на факты из той "
                "группы public_confirmed_facts, у которой такой же project. Пример "
                "запрещённого слияния: результат клиентского проекта нельзя приписывать "
                "собственному продукту.",
                "Если факт говорит «для заказчика», «клиенту», «заказчик» — работа сделана "
                "для внешнего клиента. Нельзя переписывать её как «в своём проекте» или "
                "«в своём продукте», и наоборот.",
                "Нельзя усиливать факт: писать можно ровно то, что в нём сказано. Если в "
                "факте сказано «отвечал за архитектуру», нельзя писать «отвечал за "
                "архитектуру, метрики и экономику». Если перечислены названия моделей, "
                "нельзя добавлять, что они open-source.",
            ],
            "citation_rules": [
                "public_confirmed_facts сгруппированы по проекту или заказчику (project + "
                "owner), а не свалены в один список.",
                "Если письмо приводит конкретный прошлый опыт, в cover_letter_fact_ids "
                "перечислить id ВСЕХ фактов, на которые оно опирается. Если конкретных "
                "утверждений нет, вернуть пустой список.",
                "Если утверждение опирается на два факта (кто сделал + какой результат), "
                "указать оба id.",
                "Нельзя писать утверждение, под которое нет факта в public_confirmed_facts.",
            ],
            "rules": [
                "Не начинать письмо с обрывка вроде «Работа с Python и API»: первое "
                "предложение — полноценная фраза.",
                "profile_positioning — это уже проверенный текст «о себе» из master-профиля. "
                "Из него можно брать формулировки и позиционирование, но НЕЛЬЗЯ доставать "
                "цифры и результаты: любая цифра в письме берётся только из "
                "public_confirmed_facts и только вместе с владельцем.",
                "Конкретный прошлый опыт использовать ТОЛЬКО когда employer_apply_instructions "
                "прямо просят кейс, пример, портфолио, цифры или ответ о прошлом опыте. Во всех "
                "остальных письмах не упоминать старые проекты, компании и результаты.",
                "Рядом с каждой цифрой — в том же или предыдущем предложении — назвать "
                "владельца: заказчика, компанию, собственный продукт или бренд по имени. "
                "Голая цифра без названия проекта или заказчика не проходит.",
                "Запрещена служебная лексика базы фактов: «Из подтверждённого опыта», "
                "«подтверждённый кейс», fact_id, статусы публикации, названия репозиториев.",
                "Не подавать голый навык («Работа с Python и API») или самоуничижение "
                "(«у меня мало опыта») "
                "как достижение.",
                (
                    "Можно обсуждать фикс по этапам, сдельную оплату или бонус за результат: "
                    "вакансия проектная либо сама подняла тему оплаты."
                    if payment_allowed else
                    "НЕЛЬЗЯ предлагать схему оплаты (фикс по этапам, сдельную оплату, бонус за "
                    "результат): вакансия штатная и сама тему оплаты не поднимала. Финансовую "
                    "мотивацию выражать только через бизнес-эффект."
                ),
                f"Использовать минимум {_LETTER_MIN_MARKERS} термина из vacancy_markers — это "
                "слова самой вакансии (её задачи, инструменты, предметная область). Вплетать их "
                "в живые фразы по смыслу, а не перечислять списком: письмо должно читаться как "
                "написанное под эту вакансию, а не под любую.",
                "structure_rules важнее этих правил: они описывают, из чего письмо состоит, "
                "а этот список — чего в нём быть не должно.",
                f"Итоговая длина строго {min_chars}–{max_chars} знаков. Короче — лучше.",
            ],
            "vacancy": vacancy,
            "vacancy_type": vacancy_type,
            # Requirements the employer wrote for the reply itself. Unmet, they get the
            # application dropped before the writing is read at all.
            "employer_apply_instructions": list(analysis.get("apply_instructions") or []),
            "required_letter_opening": list(required_letter_openings(vacancy)),
            "vacancy_tasks": list(sections.get("tasks") or []),
            # The exact terms the pre-send guard will look for in the letter.
            "vacancy_markers": list(vacancy_markers(vacancy)),
            "vacancy_requirements": list(sections.get("requirements") or []),
            "payment_terms_allowed": payment_allowed,
            "profile_headline": profile.desired_title,
            # The master profile's own "о себе". Costs nothing to send (it is already
            # loaded from candidate_data) and gives the letter a human opening instead
            # of a headline the vacancy has already seen in the resume.
            "profile_positioning": profile.about.strip()[:700],
            "named_entities": named,
            # Grouped, never flat: each item is {project, owner, facts:[{id, text}]}.
            "public_confirmed_facts": grouped,
            "cover_letter_voice_policy": self.data.documents["cover_letter_voice.md"],
            "required_tone": self.owner_tone or "",
            "owner_denylist": list(self.owner_denylist),
            "output_schema": {
                "cover_letter": f"{min_chars}-{max_chars} chars",
                "cover_letter_tone": "calm | direct | bold",
                "cover_letter_fact_ids": [
                    "id фактов, только если письмо действительно приводит конкретный факт; "
                    "для обычного краткого письма оставить список пустым",
                ],
                "vacancy_summary": [
                    "4–7 коротких самостоятельных предложений на русском: 3–4 главные "
                    "задачи, 1–2 ключевых требования и, если есть, отдельная строка о плюсах"
                ],
            },
            "vacancy_summary_rules": [
                "Это описание для владельца бота, а не часть сопроводительного письма.",
                "Только факты из vacancy. Ничего не додумывать и не оценивать компанию.",
                "Каждая строка короче 170 знаков, начинается сразу с сути и заканчивается точкой.",
                "Не копировать длинные абзацы: сжать их до простого человеческого русского.",
                "Не включать employer_apply_instructions: они будут показаны отдельно как "
                "«Ловушка вакансии».",
                "Не перечислять малозначимые условия, если уже есть достаточно задач и требований.",
            ],
        }

    def _letter_prompt(self, vacancy: dict, profile: MasterProfile) -> str:
        return json.dumps(self._letter_prompt_payload(vacancy, profile), ensure_ascii=False)

    def _model_vacancy_summary(self, vacancy: dict) -> tuple[str, ...]:
        """Fill the card summary when the letter path did not produce one.

        This is normally free because the letter response contains the same field. The
        separate call is reserved for language-gap letters, provider repairs that omitted
        the field, and old queue regeneration.
        """
        prompt = json.dumps({
            "task": (
                "Кратко опиши вакансию для владельца Telegram-бота. Верни только JSON. "
                "Используй только факты из vacancy, ничего не додумывай."
            ),
            "vacancy": vacancy,
            "rules": [
                "4–7 коротких самостоятельных предложений на русском.",
                "Первые 3–4 строки — главные задачи; затем 1–2 ключевых требования.",
                "Если есть дополнительные желательные навыки, вынеси их в последнюю строку.",
                "Каждая строка короче 170 знаков и начинается сразу с сути.",
                "Не включай требования к сопроводительному письму: бот покажет их отдельно.",
                "Не добавляй похвалу компании и малозначимые условия.",
            ],
            "output_schema": {"vacancy_summary": ["string"]},
        }, ensure_ascii=False)
        raw = self._complete_json(prompt, completion=self._letter_completion)
        value = raw.get("vacancy_summary") or []
        if not isinstance(value, list):
            raise UnsafeVariantError("Vacancy summary is not a list")
        summary = tuple(
            re.sub(r"\s+", " ", str(item)).strip(" •–—-\t")[:170].rstrip(" ,;:")
            for item in value[:7]
            if re.sub(r"\s+", " ", str(item)).strip(" •–—-\t")
        )
        if len(summary) < 3:
            raise UnsafeVariantError("Vacancy summary has fewer than three lines")
        return summary

    def _letter_repair_prompt(
        self, vacancy: dict, profile: MasterProfile, letter: str, violation: str,
    ) -> str:
        """One bounded second attempt: fix the named claim, leave the rest alone.

        Feeding the checker's own complaint back is far cheaper than falling back to the
        deterministic draft, which makes the whole card unsendable. Exactly one repair is
        allowed; a letter that still fails degrades exactly as before.
        """
        payload = self._letter_prompt_payload(vacancy, profile)
        payload["task"] = (
            "Предыдущая версия письма не прошла проверку. Исправь ТОЛЬКО то утверждение, "
            "которое названо в failed_check, и верни письмо целиком. Всё остальное — "
            "структуру, тон и формулировки — сохрани как есть."
        )
        payload["previous_letter"] = letter
        payload["failed_check"] = violation
        payload["repair_rules"] = [
            "Не придумывать новый кейс и не менять проект: исправляется именно названное "
            "утверждение.",
            "Если проверка говорит, что утверждение не подтверждено фактом — либо убрать "
            "утверждение, либо ослабить его до того, что буквально сказано в факте.",
            "Если проверка говорит, что факт не процитирован — добавить его id в "
            "cover_letter_fact_ids.",
            "Если проверка говорит про слияние проектов — вернуть работу тому владельцу, "
            "который назван в самом факте.",
            "Сохранить диапазон длины из output_schema; все остальные правила действуют.",
        ]
        return json.dumps(payload, ensure_ascii=False)

    def _verification_prompt(
        self, generated: dict, fact_ids: set[str], *, vacancy: dict,
        context_facts: Iterable = (),
    ) -> str:
        """Fail-closed grounding check.

        The verifier must see the same evidence the writer was given, not only the
        subset the writer chose to cite. Verifying against the cited ids alone
        rejected correct letters: a letter mentioning promotion while citing two
        revenue facts was ruled ungrounded even though the promotion fact was in
        the writer's own context — and every such rejection silently degraded the
        card to a template draft. Citations stay in the audit trail; grounding is
        judged against the permitted universe.
        """
        by_id = {f.id: f.text for f in self.data.public_facts}
        facts = {i: by_id[i] for i in fact_ids if i in by_id}
        for fact in context_facts:
            facts.setdefault(fact.id, fact.text)
        return json.dumps({
            "task": (
                "Ты ищешь враньё и пропущенные инструкции, а не вольный пересказ. Нарушение "
                "— только одно из трёх: "
                "(1) утверждение ПРОТИВОРЕЧИТ facts; (2) в утверждении есть проверяемая "
                "конкретика, которой в facts нет вообще: число, срок, название компании, "
                "продукта, инструмента, города, должности, страны. "
                "НЕ нарушение: пересказ факта своими словами, обобщение нескольких фактов "
                "в одну фразу, синоним или более общее слово вместо буквального "
                "(«отгрузки» при факте про операционный цикл закупок), формулировки о "
                "подходе к работе, мотивации, интересе и намерениях, а также то, что "
                "прямо следует из facts по смыслу. "
                "Если сомневаешься, нарушение это или нет — это НЕ нарушение. "
                "(3) письмо не выполняет хотя бы одну явную инструкцию работодателя из "
                "employer_apply_instructions. Проверяй смысл ответа, требуемую структуру, "
                "кодовую фразу и все заданные вопросы. Если инструкции нет, этот пункт не "
                "применяется. violations перечисляй только для пунктов (1), (2) и (3)."
            ),
            "generated": generated,
            "employer_apply_instructions": list(extract_apply_instructions(vacancy)),
            "cited_fact_ids": sorted(fact_ids),
            "facts": [{"id": i, "text": facts[i]} for i in sorted(facts)],
            "output_schema": {"grounded": True, "violations": ["string"]},
        }, ensure_ascii=False)

    @staticmethod
    def _numbers(text: str) -> set[str]:
        raw = re.findall(r"(?<![\w])\d+(?:[\s\u00a0]\d{3})*(?:[.,]\d+)?(?:×|x)?", text, flags=re.IGNORECASE)
        return {re.sub(r"[\s\u00a0]", "", value).replace(",", ".").lower() for value in raw}

    def _add_master_fact_support(self, achievements: list[str], fact_ids: set[str]) -> set[str]:
        """Add public evidence for immutable master bullets; never repairs generated prose."""
        result = set(fact_ids)
        for achievement in achievements:
            numbers = self._numbers(achievement)
            if not numbers:
                continue
            tokens = set(re.findall(r"[a-zа-яё]{4,}", achievement.lower()))
            candidates: list[tuple[int, str]] = []
            for fact in self.data.public_facts:
                if not numbers <= self._numbers(fact.text):
                    continue
                overlap = len(tokens & set(re.findall(r"[a-zа-яё]{4,}", fact.text.lower())))
                if overlap >= 2:
                    candidates.append((overlap, fact.id))
            if candidates:
                result.add(max(candidates)[1])
        return result

    def _ground_master_items(self, items: list[str]) -> tuple[list[str], set[str]]:
        grounded: list[str] = []
        support_ids: set[str] = set()
        for item in items:
            if not self._public_text_ok(item):
                continue
            numbers = self._numbers(item)
            tokens = set(re.findall(r"[a-zа-яё]{4,}", item.lower()))
            candidates: list[tuple[int, str]] = []
            for fact in self.data.public_facts:
                if numbers and not numbers <= self._numbers(fact.text):
                    continue
                overlap = len(tokens & set(re.findall(r"[a-zа-яё]{4,}", fact.text.lower())))
                threshold = 2 if numbers else 3
                if overlap >= threshold:
                    candidates.append((overlap, fact.id))
            if candidates:
                grounded.append(item)
                support_ids.add(max(candidates)[1])
                continue
            if numbers:
                per_number: set[str] = set()
                for number in numbers:
                    number_candidates: list[tuple[int, str]] = []
                    for fact in self.data.public_facts:
                        if number not in self._numbers(fact.text):
                            continue
                        overlap = len(tokens & set(re.findall(r"[a-zа-яё]{4,}", fact.text.lower())))
                        if overlap >= 1:
                            number_candidates.append((overlap, fact.id))
                    if number_candidates:
                        per_number.add(max(number_candidates)[1])
                if len(per_number) == len(numbers):
                    grounded.append(item)
                    support_ids.update(per_number)
        return grounded, support_ids

    @staticmethod
    def _first_content_sentence_ok(letter: str) -> bool:
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", letter) if part.strip()]
        if sentences and re.fullmatch(r"здравствуйте[!.]?", sentences[0], re.IGNORECASE):
            sentences = sentences[1:]
        if not sentences:
            return False
        first = sentences[0].lower()
        words = re.findall(r"[a-zа-яё]+", first)
        return len(words) >= 3 and not first.startswith(("работа с ", "опыт с ", "навыки:", "n8n "))

    @staticmethod
    def _is_concrete_fact(text: str) -> bool:
        lower = text.lower()
        return bool(re.search(r"\d", text) or any(marker in lower for marker in _CASE_MARKERS))

    def _letter_mentions_fact(self, letter: str, fact_ids: set[str]) -> bool:
        by_id = {f.id: f.text for f in self.data.public_facts}
        letter_tokens = set(re.findall(r"[a-zа-яё]{4,}", letter.lower()))
        for fact_id in fact_ids:
            fact = by_id.get(fact_id, "")
            if not self._is_concrete_fact(fact):
                continue
            tokens = set(re.findall(r"[a-zа-яё]{4,}", fact.lower()))
            if len(tokens & letter_tokens) >= min(4, max(2, len(tokens) // 2)):
                return True
        return False

    @staticmethod
    def _public_text_ok(text: str) -> bool:
        lower = text.lower()
        return not any(marker in lower for marker in _PUBLIC_DENYLIST)

    def _public_variant_voice_ok(self, generated: dict) -> bool:
        texts = [
            str(generated.get("title", "")), str(generated.get("about", "")),
            *generated.get("achievements", []), str(generated.get("cover_letter", "")),
        ]
        about = str(generated.get("about", "")).strip().lower()
        first_person = about.startswith((
            "я ", "создаю ", "работаю ", "веду ", "занимаюсь ", "отвечаю ",
            "самостоятельно ", "предприниматель ",
        ))
        return first_person and all(self._public_text_ok(text) for text in texts)

    @staticmethod
    def _first_person_master_about(profile: MasterProfile) -> str:
        about = profile.about.strip()
        if about.lower().startswith("я "):
            return about
        return "Я " + about[:1].lower() + about[1:]

    def _letter_policy_reason(
        self, letter: str, tone: str, vacancy_type: str, fact_ids: set[str],
        *, payment_allowed: bool = False, employer_shaped: bool = False,
    ) -> str:
        """Empty string when the letter satisfies the voice policy, otherwise what broke.

        Same conditions, same order and same strictness as the boolean gate below — it is
        derived from this one so the two can never drift. The reason exists because
        "voice policy failed" alone tells neither the owner nor the repair attempt which
        of seven conditions to fix.
        """
        banned = (
            "очень нужна работа", "что угодно за деньги", "24/7", "я в отчаянии",
            "восхищён вашей компанией", "компания моей мечты",
        )
        lower = letter.lower()
        min_chars, max_chars = (
            (_EMPLOYER_LETTER_MIN_CHARS, _EMPLOYER_LETTER_MAX_CHARS)
            if employer_shaped else (_LETTER_MIN_CHARS, _LETTER_MAX_CHARS)
        )
        if not min_chars <= len(letter) <= max_chars:
            return (
                f"длина {len(letter)} знаков вместо "
                f"{min_chars}–{max_chars}"
            )
        if tone not in {"calm", "direct", "bold"}:
            return f"неизвестный тон «{tone}»"
        hit = next((phrase for phrase in banned if phrase in lower), "")
        if hit:
            return f"запрещённая формулировка «{hit}»"
        if not (
            vacancy_type in {"project", "contract"}
            or payment_allowed
            or not any(x in lower for x in _PAYMENT_MARKERS)
        ):
            marker = next(x for x in _PAYMENT_MARKERS if x in lower)
            return (
                f"вакансия «{vacancy_type}» не поднимала тему оплаты, а письмо предлагает "
                f"схему оплаты («{marker}»)"
            )
        if not employer_shaped and not self._first_content_sentence_ok(letter):
            return "первое содержательное предложение обрывочное или слишком короткое"
        return ""

    def _letter_policy_ok(
        self, letter: str, tone: str, vacancy_type: str, fact_ids: set[str],
        *, payment_allowed: bool = False, employer_shaped: bool = False,
    ) -> bool:
        return not self._letter_policy_reason(
            letter, tone, vacancy_type, fact_ids,
            payment_allowed=payment_allowed, employer_shaped=employer_shaped,
        )

    def _relevant_public_facts(self, vacancy: dict, profile: MasterProfile, limit: int = 2) -> list:
        query = " ".join([
            str(vacancy.get("name", "")), str(vacancy.get("description", "")),
            profile.desired_title, profile.about, *[text for _, text in profile.achievements],
        ]).lower()
        query_tokens = set(re.findall(r"[a-zа-яё]{4,}", query))
        ranked = []
        for fact in self.data.public_facts:
            tokens = set(re.findall(r"[a-zа-яё]{4,}", fact.text.lower()))
            ranked.append((len(query_tokens & tokens), -len(fact.text), fact))
        return [fact for score, _, fact in sorted(ranked, reverse=True, key=lambda x: (x[0], x[1])) if score > 0][:limit]

    def _language_gap_letter(self, vacancy: dict, profile: MasterProfile) -> tuple[str, set[str]]:
        """Extension point; the core has no candidate-specific language facts."""
        raise UnsafeVariantError("Language-gap templates must be configured by the application")

    def _verify_numbers(
        self, generated: dict, fact_ids: set[str], *, source_text: str = "",
        context_facts: Iterable = (),
    ) -> None:
        """Refuse any number that no permitted public fact supports.

        Evidence is the cited facts plus whatever context the writer was given —
        not the citations alone. A numeric claim may be rejected while the same
        figure sits in an approved public fact the writer simply did not
        listed, and every such rejection silently degraded the card to a template.
        A number still cannot be invented: it must appear verbatim in some public
        fact or in the vacancy text.
        """
        by_id = {f.id: f.text for f in self.data.public_facts}
        evidence_texts = [by_id[i] for i in fact_ids if i in by_id]
        evidence_texts.extend(fact.text for fact in context_facts)
        evidence = "\n".join(evidence_texts)
        output = "\n".join(
            [str(generated.get("title", "")), str(generated.get("about", "")),
             *generated.get("achievements", []), *generated.get("public_reasons", []),
             str(generated.get("cover_letter", ""))]
        )
        numbers = self._numbers(output)
        evidence_numbers = self._numbers(evidence) | self._numbers(source_text)
        missing = [number for number in numbers if number not in evidence_numbers]
        if missing:
            locations = [
                key for key, value in generated.items()
                if self._numbers("\n".join(value) if isinstance(value, list) else str(value)) & set(missing)
            ]
            raise UnsafeVariantError(
                f"Unverified numeric claims: {', '.join(sorted(missing))}; fields={','.join(locations)}"
            )

    def _voice_denylist(self) -> tuple[str, ...]:
        """Literal phrases banned by cover_letter_voice.md and resume_voice.md."""
        cached = getattr(self, "_voice_denylist_cache", None)
        if cached is not None:
            return cached
        phrases: list[str] = []
        for name, heading in (
            ("cover_letter_voice.md", "Что запрещено писать буквально"),
            ("resume_voice.md", "Запрещённый служебный язык"),
        ):
            markdown = self.data.documents.get(name, "")
            section = re.search(
                rf"^#+\s*{re.escape(heading)}\s*$\n(.*?)(?=^#+\s|\Z)",
                markdown, flags=re.MULTILINE | re.DOTALL,
            )
            if not section:
                continue
            for quoted in re.findall(r"[«`\"]([^«»`\"]{3,90})[»`\"]", section.group(1)):
                phrase = quoted.strip().strip(" .;,:").lower()
                if len(phrase) >= 4:
                    phrases.append(phrase)
        result = tuple(dict.fromkeys(phrases + list(_HARD_BANNED)))
        self._voice_denylist_cache = result
        return result

    def _active_denylist(self) -> tuple[str, ...]:
        """Voice-policy denylist plus phrases configured in the settings screen."""
        return tuple(dict.fromkeys(self._voice_denylist() + tuple(self.owner_denylist)))

    def _body_sentences(self, letter: str) -> list[str]:
        sentences = _sentences(letter)
        if sentences and re.fullmatch(r"здравствуйте[!.,]?", sentences[0], re.IGNORECASE):
            sentences = sentences[1:]
        return sentences

    def _has_concrete_case(self, letter: str) -> bool:
        body = self._body_sentences(letter)
        for sentence in body:
            lower = sentence.lower()
            has_number = bool(re.search(r"(?<![\w])\d", sentence))
            has_marker = any(marker in lower for marker in _CASE_MARKERS)
            has_context = any(noun in lower for noun in _CONTEXT_NOUNS)
            if (has_number or has_marker) and has_context and len(_words(sentence)) >= 5:
                return True
        return False

    def _numeric_claim_gap(self, letter: str) -> str:
        """A number is only meaningful next to the product or customer it belongs to."""
        sentences = self._body_sentences(strip_service_vocabulary(letter))
        for index, sentence in enumerate(sentences):
            match = re.search(r"(?<![\w])\d[\d\s ]*", sentence)
            if not match:
                continue
            # Never borrow the subject from the opening line, which talks about the vacancy.
            window = " ".join(sentences[max(1, index - 2): index + 1]) if index else sentence
            if _has_identified_subject(window):
                continue
            return match.group().strip()
        return ""

    @staticmethod
    def _own_product_metric(letter: str) -> str:
        """The candidate's own project quoted with a number — users, subscribers, revenue.

        Parading your own product at an employer reads as showing off and invites the
        question of why you need the job at all. Client work carries the same proof
        without either problem, so the numbers stay on the client side.
        """
        for sentence in _sentences(letter):
            if not re.search(r"(?<![\w])\d", sentence):
                continue
            lower = sentence.lower()
            hit = next((name for name in _own_project_names() if name.lower() in lower), "")
            if hit:
                return hit
        return ""

    def _has_bare_skill_proof(self, letter: str) -> bool:
        for sentence in self._body_sentences(letter):
            lower = sentence.lower()
            if len(_words(sentence)) > 7:
                continue
            if not any(token in lower for token in _TOOL_TOKENS):
                continue
            if any(marker in lower for marker in _CASE_MARKERS):
                continue
            return True
        return False

    def validate_outgoing_letter(
        self,
        letter: str,
        *,
        vacancy: dict,
        fact_ids: Iterable[str] = (),
        letter_source: str = "model",
        previously_sent: Iterable[tuple[str, str]] = (),
    ) -> LetterGuardReport:
        """Last gate before a letter may leave the system. Never raises; returns a report.

        `previously_sent` is an iterable of `(vacancy_key, letter_text)` pairs already sent,
        used to make sure the same text is not mailed to two different employers.
        """
        text = letter.strip()
        lower = text.lower()
        checks: list[LetterCheck] = []
        # Generation artefacts, not writing rules: a person who types «а не» by hand meant
        # it, and blocking their own letter over house voice would be absurd.
        written_by_model = letter_source != "manual"

        employer_shaped = bool(extract_apply_instructions(vacancy))
        # The model writer has the stricter 420-character policy above. The final guard
        # also accepts a longer owner-written letter and old stored drafts.
        min_chars, max_chars = _LETTER_MIN_CHARS, _EMPLOYER_LETTER_MAX_CHARS
        if employer_shaped:
            min_chars = _EMPLOYER_LETTER_MIN_CHARS
        checks.append(LetterCheck(
            "length", min_chars <= len(text) <= max_chars,
            f"Длина письма {len(text)} знаков, допустимо {min_chars}–{max_chars}.",
        ))

        banned = next((phrase for phrase in self._active_denylist() if phrase in lower), "")
        checks.append(LetterCheck(
            "denylist", not banned,
            f"Письмо содержит запрещённую формулировку «{banned}».",
        ))

        service = next(
            (phrase for phrase in _SERVICE_VOCABULARY + _PUBLIC_DENYLIST if phrase in lower), "",
        )
        checks.append(LetterCheck(
            "service_vocabulary", not service,
            f"Письмо содержит служебную лексику «{service}» — это внутренний язык базы фактов, "
            "а не текст для работодателя.",
        ))

        # When the employer dictated the shape of the reply, our house rules about where a
        # case must sit stop being quality and start being a reason to fail a letter that
        # does exactly what was asked. The safety checks below all still apply.
        employer_requests_case = any(
            marker in instruction.lower()
            for instruction in extract_apply_instructions(vacancy)
            for marker in ("кейс", "пример", "портфолио", "результат", "опыт")
        )
        checks.append(LetterCheck(
            "concrete_case", not employer_requests_case or self._has_concrete_case(text),
            "Работодатель прямо попросил кейс или пример, но письмо его не содержит.",
        ))

        gap = self._numeric_claim_gap(text)
        checks.append(LetterCheck(
            "number_context", not gap,
            f"Цифра «{gap}» стоит без продукта или заказчика, к которым она относится.",
        ))

        weak = "" if letter_source == "language_gap" else next(
            (phrase for phrase in _SELF_DIMINISHING if phrase in lower), "",
        )
        checks.append(LetterCheck(
            "self_diminishing", not weak,
            f"Формулировка «{weak}» работает против кандидатуры и не является достижением.",
        ))

        checks.append(LetterCheck(
            "bare_skill", not self._has_bare_skill_proof(text),
            "Перечень инструментов подан как достижение; нужен результат, а не строка из навыков.",
        ))

        vacancy_type = classify_vacancy_type(vacancy)
        payment_allowed = (
            vacancy_type in {"project", "contract"} or vacancy_raises_payment_topic(vacancy)
        )
        payment_hit = next((marker for marker in _PAYMENT_MARKERS if marker in lower), "")
        checks.append(LetterCheck(
            "payment_language", payment_allowed or not payment_hit,
            f"Вакансия классифицирована как «{vacancy_type}» и сама не поднимала тему оплаты, "
            f"а письмо предлагает схему оплаты («{payment_hit}»).",
        ))

        duplicate = ""
        normalized = _normalized_letter(text)
        current_key = _vacancy_key(vacancy)
        for other_key, other_letter in previously_sent:
            if str(other_key) == current_key:
                continue
            if other_letter.strip() == text or _normalized_letter(other_letter) == normalized:
                duplicate = str(other_key)
                break
        checks.append(LetterCheck(
            "duplicate", not duplicate,
            f"Точно такое же письмо уже отправлено по другой вакансии ({duplicate}).",
        ))

        checks.append(LetterCheck(
            "first_sentence", employer_shaped or self._first_content_sentence_ok(text),
            "Первое содержательное предложение обрывочное; нужна полная фраза по задаче вакансии.",
        ))

        opener = ""
        if written_by_model and not employer_shaped:
            first = " ".join(self._body_sentences(text)[:1]).lower()
            opener = next((phrase for phrase in _PARAPHRASE_OPENERS if phrase in first), "")
        checks.append(LetterCheck(
            "em_dash", not written_by_model or "—" not in text,
            "В письме есть длинное тире: в русском тексте это самый заметный признак "
            "генерации. Переписать через точку, двоеточие или запятую.",
        ))

        antithesis = next(
            (phrase for phrase in _AI_ANTITHESIS if written_by_model and phrase in lower), "",
        )
        if written_by_model and not antithesis:
            spread = _AI_ANTITHESIS_RE.search(text)
            antithesis = spread.group().strip() if spread else ""
        checks.append(LetterCheck(
            "ai_antithesis", not antithesis,
            f"Оборот «{antithesis.strip()}» — узнаваемая риторика нейросети. Сказать прямо, "
            "что есть, без противопоставления.",
        ))

        metric = self._own_product_metric(text) if written_by_model else ""
        checks.append(LetterCheck(
            "own_product_boast", not metric,
            f"Письмо приводит цифры собственного проекта ({metric}). Своё выпячивать не "
            "нужно: доказательство берём из клиентской работы, свой проект — без цифр.",
        ))

        checks.append(LetterCheck(
            "vacancy_paraphrase", not opener,
            f"Письмо начинается с пересказа вакансии («{opener}») — работодатель написал её "
            "сам. Начинать нужно с себя: чем зацепила вакансия и что я умею.",
        ))

        markers = vacancy_markers(vacancy)
        hits = matched_vacancy_markers(text, vacancy)
        # A posting with a two-word description cannot fairly be asked for two markers back;
        # manually-entered vacancies really do look like that. Demand the full quota only
        # once the vacancy has enough vocabulary of its own to make it a real test.
        required = _LETTER_MIN_MARKERS if len(markers) >= 5 else min(1, len(markers))
        checks.append(LetterCheck(
            "vacancy_markers", len(hits) >= required,
            f"Письмо не привязано к этой вакансии: из её терминов ({', '.join(markers[:8])}) "
            f"использовано {len(hits)}, нужно минимум {required}.",
        ))

        # An employer's own instruction ("начните письмо со слов «AI first»") is the first
        # filter the reply passes, before anyone judges the writing. Failing it silently
        # wastes the whole application, so it is a hard check for model and manual letters alike.
        openings = required_letter_openings(vacancy)
        checks.append(LetterCheck(
            "employer_opening",
            not openings or any(letter_opens_with(text, phrase) for phrase in openings),
            f"Вакансия требует начать письмо со слов «{openings[0] if openings else ''}», "
            "а письмо начинается иначе.",
        ))

        ids = {str(value) for value in fact_ids}
        if ids:
            allowed = {fact.id for fact in self.data.public_facts}
            checks.append(LetterCheck(
                "supported_facts", ids <= allowed,
                "Письмо опирается на факты вне подтверждённой публичной базы.",
            ))

        return LetterGuardReport(letter_source=letter_source, checks=tuple(checks))

    def ensure_letter_sendable(
        self,
        letter: str,
        *,
        vacancy: dict,
        fact_ids: Iterable[str] = (),
        letter_source: str = "model",
        previously_sent: Iterable[tuple[str, str]] = (),
    ) -> LetterGuardReport:
        """Raising wrapper for callers that want the send to abort with a Russian reason."""
        report = self.validate_outgoing_letter(
            letter, vacancy=vacancy, fact_ids=fact_ids,
            letter_source=letter_source, previously_sent=previously_sent,
        )
        if not report.ok:
            raise UnsafeVariantError(report.reason)
        if not report.sendable:
            raise UnsafeVariantError(
                "Письмо не прошло проверку фактов, поэтому отправка возможна только после "
                "того, как оно переписано вручную или сгенерировано заново."
            )
        return report

    def validate_manual_letter(
        self, letter: str, *, vacancy: dict, fact_ids: set[str], tone: str = "direct",
    ) -> str:
        allowed = {fact.id for fact in self.data.public_facts}
        if not fact_ids <= allowed:
            raise UnsafeVariantError("Manual letter uses facts outside public-confirmed support")
        if not self._public_text_ok(letter):
            raise UnsafeVariantError("Manual letter contains internal/evidence jargon")
        self._verify_numbers(
            {"cover_letter": letter}, fact_ids,
            source_text=json.dumps(vacancy, ensure_ascii=False),
        )
        # Service vocabulary is stripped rather than rejected so a good hand-written letter is
        # not thrown away over one internal connector; the caller should store what is returned.
        cleaned = strip_service_vocabulary(letter)
        report = self.validate_outgoing_letter(
            cleaned, vacancy=vacancy, fact_ids=fact_ids, letter_source="manual",
        )
        if not report.ok:
            raise UnsafeVariantError(report.reason)
        return cleaned

    @staticmethod
    def _resume_document(
        profile: MasterProfile,
        *,
        title: str,
        about: str,
        achievements: tuple[str, ...],
        skills: tuple[str, ...],
        experience_order: list[str],
        rewrites: dict[str, str],
    ) -> dict:
        achievement_map = dict(profile.achievements)
        text_by_id = {item_id: rewrites.get(item_id, text) for item_id, text in profile.achievements}
        selected_texts = set(achievements)
        entries = []
        assigned: set[str] = set()
        by_experience = {entry.id: entry for entry in profile.experience}
        for entry_id in experience_order:
            entry = by_experience[entry_id]
            bullets = [
                {"id": bullet_id, "text": text_by_id[bullet_id]}
                for bullet_id in entry.bullet_ids
                if text_by_id.get(bullet_id) in selected_texts
            ]
            if bullets:
                entries.append({
                    "id": entry.id, "title": entry.title, "role": entry.role,
                    "dates": entry.dates, "bullets": bullets,
                })
                assigned.update(item["text"] for item in bullets)
        remaining = [text for text in achievements if text not in assigned]
        projects = [{
            "id": "selected-projects", "title": "Избранные проекты",
            "bullets": [{"id": next((i for i, value in achievement_map.items() if rewrites.get(i, value) == text), ""), "text": text} for text in remaining],
        }] if remaining else []
        return {
            "basics": {"headline": title, "summary": about, "contacts": {}},
            "experience": entries,
            "projects": projects,
            "skills": list(skills),
            "education": [],
        }

    def _check_model_letter(
        self, letter: str, tone: str, fact_ids: set[str], *,
        vacancy: dict, vacancy_type: str, payment_allowed: bool, context_facts: list,
    ) -> None:
        """Every gate a model-written letter must survive, in the order it always ran.

        Nothing here is softer than before; the pre-send guard was folded in because a letter
        that fails it is already unsendable, and letting the model try once to fix a named
        complaint is strictly better than shipping an unsendable card.
        """
        allowed_facts = {fact.id for fact in self.data.public_facts}
        if not fact_ids <= allowed_facts:
            raise UnsafeVariantError("Cover letter uses unknown or non-public facts")
        policy = self._letter_policy_reason(
            letter, tone, vacancy_type, fact_ids, payment_allowed=payment_allowed,
            employer_shaped=bool(extract_apply_instructions(vacancy)),
        )
        if policy:
            raise UnsafeVariantError(f"Cover letter voice policy failed: {policy}")
        if not self._public_text_ok(letter):
            raise UnsafeVariantError("Cover letter contains internal language")
        self._verify_numbers(
            {"cover_letter": letter}, fact_ids,
            source_text=json.dumps(vacancy, ensure_ascii=False), context_facts=context_facts,
        )
        verification = self._complete_json(self._verification_prompt(
            {"cover_letter": letter}, fact_ids, vacancy=vacancy,
            context_facts=context_facts,
        ))
        if verification.get("grounded") is not True or verification.get("violations"):
            violations = json.dumps(
                verification.get("violations") or [], ensure_ascii=False,
            )[:900]
            raise UnsafeVariantError(f"Cover letter grounding failed: {violations}")
        report = self.validate_outgoing_letter(
            letter, vacancy=vacancy, fact_ids=fact_ids, letter_source="model",
        )
        if not report.ok:
            raise UnsafeVariantError(f"Pre-send guard: {report.reason}")

    def _model_letter(
        self, vacancy: dict, profile: MasterProfile, *,
        vacancy_type: str, payment_allowed: bool, context_facts: list,
    ) -> tuple[str, str, set[str], int, tuple[str, ...]]:
        """Write the letter, and give the model exactly one chance to repair a named failure.

        Returns `(letter, tone, fact_ids, attempts, vacancy_summary)`. Raises
        `UnsafeVariantError` when the
        repaired letter still fails, which lands the caller on the deterministic draft — the
        same unsendable `manual_required` card as before. The loop is hard-capped at
        `_LETTER_MAX_ATTEMPTS`: one write plus one repair, never more.
        """
        title = str(vacancy.get("name") or vacancy.get("title") or "?")[:80]
        prompt = self._letter_prompt(vacancy, profile)
        failure: UnsafeVariantError | None = None
        last_summary: tuple[str, ...] = ()
        for attempt in range(1, _LETTER_MAX_ATTEMPTS + 1):
            try:
                raw = self._complete_json(prompt, completion=self._letter_completion)
            except Exception as exc:  # noqa: BLE001
                if failure is None:
                    raise
                # The repair is best-effort. If asking for it fails for any reason, the
                # first verdict stands and the caller degrades exactly as it did before
                # the repair loop existed — never a letter that skipped a check.
                log.warning("Cover letter repair call failed for '%s': %r", title, exc)
                raise failure from exc
            letter = humanize_punctuation(
                strip_service_vocabulary(str(raw.get("cover_letter", "")).strip())
            )
            tone = self.owner_tone or str(raw.get("cover_letter_tone", "direct")).strip().lower()
            fact_ids = {str(value) for value in raw.get("cover_letter_fact_ids", [])}
            summary_value = raw.get("vacancy_summary") or []
            if isinstance(summary_value, list):
                cleaned_summary = tuple(
                    re.sub(r"\s+", " ", str(value)).strip(" •–—-\t")[:170].rstrip(" ,;:")
                    for value in summary_value[:7]
                    if re.sub(r"\s+", " ", str(value)).strip(" •–—-\t")
                )
                if len(cleaned_summary) >= 3:
                    last_summary = cleaned_summary
            try:
                self._check_model_letter(
                    letter, tone, fact_ids, vacancy=vacancy, vacancy_type=vacancy_type,
                    payment_allowed=payment_allowed, context_facts=context_facts,
                )
            except UnsafeVariantError as exc:
                log.warning(
                    "Cover letter attempt %d/%d for '%s' failed: %s",
                    attempt, _LETTER_MAX_ATTEMPTS, title, exc,
                )
                # The rejected text travels with the verdict: the caller keeps it as an
                # openly unverified draft the owner can edit, which beats handing back an
                # empty card and making them write the letter from nothing.
                exc.letter, exc.tone = letter, tone
                exc.vacancy_summary = last_summary
                if attempt >= _LETTER_MAX_ATTEMPTS:
                    raise
                failure = exc
                prompt = self._letter_repair_prompt(vacancy, profile, letter, str(exc))
                continue
            if attempt > 1:
                log.info("Cover letter for '%s' passed after %d attempts", title, attempt)
            return letter, tone, fact_ids, attempt, last_summary
        raise UnsafeVariantError("Cover letter did not pass within the attempt budget")

    def generate(self, vacancy: dict, *, forced_profile: str | None = None) -> CandidateVariant:
        """Select one reviewed master resume and tailor only its cover letter."""
        if forced_profile and forced_profile not in self.data.profiles:
            raise UnsafeVariantError("Unknown forced profile")
        if forced_profile:
            # The profile is already decided; see `_forced_selection`. This skips
            # the largest model call in the pipeline for every vacancy the
            # automation cycle prepares.
            selection = self._forced_selection(vacancy, self.data.profiles[forced_profile])
        else:
            try:
                selection = self._complete_json(self._selection_prompt(vacancy, forced_profile))
            except UnsafeVariantError:
                selection = self._fallback_selection(vacancy, forced_profile)
        profile_key = str(selection.get("profile_key", ""))
        if forced_profile and profile_key != forced_profile:
            raise UnsafeVariantError("LLM ignored forced profile")
        profile = self.data.profiles.get(profile_key)
        if not profile:
            raise UnsafeVariantError("Unknown profile")

        allowed_facts = {fact.id for fact in self.data.public_facts}
        reasons: list[str] = []
        reason_fact_ids: set[str] = set()
        discarded_public_reason = False
        for item in selection.get("public_reasons", [])[:4]:
            if not isinstance(item, dict):
                discarded_public_reason = True
                continue
            ids = {str(value) for value in item.get("supporting_fact_ids", [])}
            if not ids or not ids <= allowed_facts:
                discarded_public_reason = True
                continue
            reason_fact_ids.update(ids)
            reasons.append(str(item.get("text", ""))[:240])
        if not reasons:
            fallback_reasons = self._relevant_public_facts(vacancy, profile)
            reasons = [fact.text[:240] for fact in fallback_reasons]
            reason_fact_ids = {fact.id for fact in fallback_reasons}
        if not reasons:
            raise UnsafeVariantError("No public reasons")
        internal_rationale = tuple(str(value)[:500] for value in selection.get("internal_rationale", [])[:6])
        if discarded_public_reason:
            internal_rationale += ("Закрытая публичная причина отброшена валидатором.",)
        internal_rationale += (
            "Использован неизменяемый проверенный master-resume; под вакансию адаптировано только письмо.",
        )
        score = max(0, min(100, int(selection.get("score", 0))))
        hard_blockers = detect_profile_blockers(vacancy)
        fit_warnings = detect_profile_warnings(vacancy)
        vacancy_review = analyze_vacancy_description(vacancy)
        vacancy_summary: tuple[str, ...] = ()
        if hard_blockers:
            score = min(score, 35)

        title = profile.desired_title
        about = profile.about.strip()
        achievements = tuple(text for _, text in profile.achievements)
        skills = tuple(text for _, text in profile.skills)
        experience_order = [entry.id for entry in profile.experience]
        resume_document = self._resume_document(
            profile, title=title, about=about, achievements=achievements, skills=skills,
            experience_order=experience_order, rewrites={},
        )

        relevant = self._relevant_public_facts(vacancy, profile, limit=40)
        resume_fact_ids = {fact.id for fact in relevant} | reason_fact_ids
        master_numbers = self._numbers(profile.markdown)
        resume_fact_ids.update(
            fact.id for fact in self.data.public_facts
            if self._numbers(fact.text) & master_numbers
        )

        vacancy_type = classify_vacancy_type(vacancy)
        payment_allowed = vacancy_raises_payment_topic(vacancy)
        if fit_warnings:
            letter, letter_fact_ids = self._language_gap_letter(vacancy, profile)
            tone = self.owner_tone or "direct"
            letter_source = "language_gap"
            self._verify_numbers(
                {"cover_letter": letter}, letter_fact_ids,
                source_text=json.dumps(vacancy, ensure_ascii=False),
                context_facts=self._relevant_public_facts(vacancy, profile, limit=40),
            )
            if not self._letter_policy_ok(
                letter, tone, vacancy_type, letter_fact_ids, payment_allowed=payment_allowed,
            ):
                raise UnsafeVariantError("Language-gap cover letter did not satisfy voice policy")
            internal_rationale += ("Обнаружено жёсткое языковое расхождение; письмо раскрывает его явно.",)
        else:
            try:
                letter, tone, letter_fact_ids, attempts, vacancy_summary = self._model_letter(
                    vacancy, profile, vacancy_type=vacancy_type,
                    payment_allowed=payment_allowed,
                    context_facts=self._relevant_public_facts(vacancy, profile, limit=40),
                )
                letter_source = "model"
                if attempts > 1:
                    internal_rationale += (
                        f"Первая версия письма не прошла проверку; принята версия после "
                        f"{attempts - 1} исправления по замечанию проверки.",
                    )
            except UnsafeVariantError as exc:
                # No canned draft — that stock copy only produced text the owner had to
                # rewrite. But an empty card is not honest either: the model's own last
                # attempt is usually a letter with one disputed claim, so it is kept as
                # `model_unverified`, which reads as unsendable everywhere `sendable` is
                # checked and lands the card in `manual_required` exactly as before.
                log.warning(
                    "Cover letter failed for '%s': %s",
                    str(vacancy.get("name") or vacancy.get("title") or "?")[:80], exc,
                )
                tone = getattr(exc, "tone", "") or self.owner_tone or "direct"
                vacancy_summary = tuple(getattr(exc, "vacancy_summary", ()) or ())
                letter, letter_fact_ids = "", set()
                rejected = str(getattr(exc, "letter", "") or "")
                if rejected:
                    try:
                        # Citations are dropped with the verdict, so the kept text still has
                        # to clear the number guard on its own evidence.
                        self._verify_numbers(
                            {"cover_letter": rejected}, set(),
                            source_text=profile.markdown + "\n" + json.dumps(vacancy, ensure_ascii=False),
                            context_facts=self._relevant_public_facts(vacancy, profile, limit=40),
                        )
                    except UnsafeVariantError as numbers_exc:
                        log.warning("Unverified letter dropped, numbers unsupported: %s", numbers_exc)
                    else:
                        letter = rejected
                letter_source = "model_unverified" if letter else "fallback"
                internal_rationale += (f"Письмо не прошло проверку: {exc}"[:500],)

        if len(vacancy_summary) < 3:
            try:
                vacancy_summary = self._model_vacancy_summary(vacancy)
            except Exception as exc:  # summary failure must not discard a safe letter
                log.warning(
                    "Vacancy summary failed for '%s': %s",
                    str(vacancy.get("name") or vacancy.get("title") or "?")[:80], exc,
                )
                internal_rationale += (f"Краткое описание не сгенерировано: {exc}"[:500],)

        letter_report = self.validate_outgoing_letter(
            letter, vacancy=vacancy, fact_ids=letter_fact_ids, letter_source=letter_source,
        )
        if not letter_report.ok:
            internal_rationale += (f"Pre-send guard: {letter_report.reason}"[:500],)

        generated = {
            "title": title, "about": about, "achievements": list(achievements),
            "skills": list(skills), "cover_letter": letter,
        }
        if not self._public_variant_voice_ok(generated):
            raise UnsafeVariantError("Stable master resume did not satisfy public voice policy")
        self._verify_numbers(
            generated, resume_fact_ids | letter_fact_ids,
            source_text=profile.markdown + "\n" + json.dumps(vacancy, ensure_ascii=False),
        )
        return CandidateVariant(
            id=uuid.uuid4().hex[:16], profile_key=profile_key, score=score,
            reasons=tuple(reasons), internal_rationale=internal_rationale,
            title=title, about=about, achievements=achievements, skills=skills,
            cover_letter=letter, cover_letter_tone=tone,
            used_fact_ids=tuple(sorted(resume_fact_ids | letter_fact_ids)), diff="",
            resume_document=resume_document,
            resume_analysis={
                "strategy": "stable_master_resume",
                "profile_key": profile_key,
                "tailored_fields": ["cover_letter"],
                "hard_blockers": list(hard_blockers),
                "fit_warnings": list(fit_warnings),
                "vacancy_review": vacancy_review,
                "letter_review": letter_report.as_dict(),
            },
            model=self.model,
            letter_source=letter_source,
            letter_review=letter_report.as_dict(),
            vacancy_summary=vacancy_summary,
        )
