"""HH.ru resume sync: profile -> dictionary-resolved payload -> diff -> apply/publish.

This module turns a semantic ``hh_profiles/<key>.json`` (human Russian text,
plain enum-ish strings) into the real body HH.ru expects for
``PUT /resumes/{resume_id}``, lets a caller preview the change as a
field-by-field diff, and only then performs the write.

Safety contract (do not weaken these when editing this file):

- Importing this module must never call the HH API. There are no
  module-level client calls, no auto-apply, no background task.
- ``apply_profile``, ``publish``, ``create_resume`` and ``restore`` are the
  ONLY functions in this module that write to hh.ru. Every other function
  (``load_profile``, ``HHDictionaries``, ``build_payload``, ``diff``,
  ``snapshot``) is read-only or pure.
- No function here ever logs, prints or returns an OAuth token or secret.
- This module reuses ``candidate.hh_official.HHOfficialClient`` — it does not
  reimplement HTTP/auth handling.
"""

from __future__ import annotations

import difflib
import json
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from candidate.data import DATA_ROOT
from candidate.hh_official import HHOfficialClient, HHOfficialError

# ---------------------------------------------------------------------------
# Paths and limits
# ---------------------------------------------------------------------------

PROFILE_ROOT = DATA_ROOT / "hh_profiles"
DEFAULT_DICTIONARY_CACHE_PATH = Path(__file__).resolve().parents[1] / "var" / "hh_dictionaries_cache.json"
DEFAULT_DICTIONARY_TTL_SECONDS = 7 * 24 * 3600  # 7 days
#: Dictionaries at or below this size are printed in full when a value misses;
#: bigger ones (professional_roles has 304 entries) get nearest-match hints.
_FULL_DICTIONARY_LISTING_LIMIT = 20

# HH's real limits are not published in one place; these mirror the values
# already vetted in run_candidate_mvp.py's _hh_resume_patch (title, skill_set
# item length, skill_set count) plus conservative assumptions for the two
# free-text fields (about/skills and experience description). If HH rejects
# a value despite these limits, apply_profile/publish surface HH's own
# too_long error instead of silently trusting these constants.
MAX_TITLE_LENGTH = 100
MAX_ABOUT_LENGTH = 4000
MAX_SKILL_TAG_LENGTH = 100
MAX_SKILL_SET_COUNT = 30
MAX_EXPERIENCE_DESCRIPTION_LENGTH = 4000

# Fields HH lets you write on a resume. Used both to build a payload (carry
# over everything the profile is silent about) and to filter a snapshot
# before restoring it (drop read-only fields like id/url/status/counters).
WRITABLE_RESUME_FIELDS = (
    "title", "last_name", "first_name", "middle_name", "birth_date", "gender",
    "access", "photo", "portfolio", "certificate", "contact", "resume_locale",
    "citizenship", "metro", "business_trip_readiness", "travel_time",
    "area", "relocation", "salary", "employments", "schedules", "education",
    "language", "experience", "professional_roles", "skills", "skill_set",
)

# The subset of WRITABLE_RESUME_FIELDS that build_payload always overwrites
# from the profile (everything else in WRITABLE_RESUME_FIELDS is carried
# over from current_resume untouched).
PROFILE_CONTROLLED_FIELDS = (
    "title", "skills", "skill_set", "area", "relocation", "employments",
    "schedules", "education", "language", "salary", "experience", "professional_roles",
)

REQUIRED_PROFILE_FIELDS = (
    "profile_key", "title", "salary", "area", "relocation", "employments",
    "schedules", "about", "skill_set", "experience", "education", "languages",
    "professional_roles",
)

FIELD_LABELS_RU = {
    "title": "Заголовок резюме",
    "skills": "О себе",
    "skill_set": "Ключевые навыки",
    "area": "Регион",
    "relocation": "Готовность к переезду",
    "business_trip_readiness": "Готовность к командировкам",
    "employments": "Тип занятости",
    "schedules": "График работы",
    "education": "Образование",
    "language": "Иностранные языки",
    "salary": "Желаемая зарплата",
    "experience": "Опыт работы",
    "professional_roles": "Профессиональная область",
}

_ERROR_CODE_RU = {
    "not_specified": "поле не заполнено",
    "required": "значение обязательное",
    "too_long": "значение слишком длинное",
    "too_short": "значение слишком короткое",
    "invalid": "недопустимое значение",
    "already_exists": "такое значение уже используется",
    "bad_argument": "некорректный аргумент",
    "forbidden": "запрещено для этого резюме",
    "resume_restricted_edit_field": "поле нельзя редактировать",
    "resume_prooflink_required": "нужна ссылка-подтверждение",
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ProfileValidationError(ValueError):
    """Raised when a hh_profiles/*.json file violates the input contract."""


class DictionaryResolutionError(ValueError):
    """Raised when a semantic profile value cannot be mapped to an HH dictionary id."""


class HHResumeApplyError(RuntimeError):
    """Raised when hh.ru rejects (or fails to confirm) a write.

    ``status`` is the real HTTP status. ``field_errors`` is HH's raw
    per-field errors list (may be empty, e.g. on a network-level failure).
    ``str(exc)`` is already a Russian-language explanation safe to show
    directly in a Telegram confirmation screen.
    """

    def __init__(self, status: int, field_errors: list[dict] | None, message: str):
        self.status = status
        self.field_errors = field_errors or []
        super().__init__(message)


def _format_hh_error(exc: HHOfficialError) -> str:
    if not exc.errors:
        return f"HH API вернул ошибку {exc.status}: {exc.reason}"
    parts = []
    for err in exc.errors:
        if not isinstance(err, dict):
            continue
        # HH speaks two error dialects. The older one names the field in
        # ``field``/``name``; the JSON-schema one puts a JSON pointer in
        # ``pointer`` ("/experience/0/area") and the real cause in
        # ``reason``/``description``. Reading only the first dialect turned
        # every schema error into a useless "поле: код ошибки «experience»".
        pointer = str(err.get("pointer") or "").strip("/")
        field_path = str(
            err.get("field") or err.get("name") or err.get("attribute_name")
            or pointer.replace("/", ".") or err.get("value") or ""
        )
        code = str(err.get("reason") or err.get("value") or err.get("type") or "unknown")
        root = field_path.split(".")[0]
        label = FIELD_LABELS_RU.get(root, FIELD_LABELS_RU.get(str(err.get("value") or ""), root or "поле"))
        reason = _ERROR_CODE_RU.get(code) or str(err.get("description") or "") or f"код ошибки «{code}»"
        parts.append(f"{label} ({field_path}): {reason}" if field_path else f"{label}: {reason}")
    if not parts:
        return f"HH API вернул ошибку {exc.status}: {exc.reason}"
    return "HH отклонил изменения резюме — " + "; ".join(parts)


# ---------------------------------------------------------------------------
# 1. load_profile
# ---------------------------------------------------------------------------

def _fail(profile_key: str, path: str, reason: str) -> None:
    raise ProfileValidationError(f"Профиль '{profile_key}': поле '{path}' {reason}")


def _check_str(container: dict, key: str, profile_key: str, path: str, *, allow_empty: bool = False) -> str:
    value = container.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _fail(profile_key, path, "должно быть непустой строкой" if not allow_empty else "должно быть строкой")
    return value


def _check_list(container: dict, key: str, profile_key: str, path: str, *, allow_empty: bool = False) -> list:
    value = container.get(key)
    if not isinstance(value, list) or (not allow_empty and not value):
        _fail(profile_key, path, "должно быть непустым списком" if not allow_empty else "должно быть списком")
    return value


def _check_dict(container: dict, key: str, profile_key: str, path: str) -> dict:
    value = container.get(key)
    if not isinstance(value, dict):
        _fail(profile_key, path, "должно быть объектом")
    return value


def _validate_profile(raw: dict, profile_key: str) -> None:
    for name in REQUIRED_PROFILE_FIELDS:
        if name not in raw:
            _fail(profile_key, name, "отсутствует")

    if str(raw.get("profile_key") or "") != profile_key:
        _fail(profile_key, "profile_key", f"('{raw.get('profile_key')}') не совпадает с именем файла")

    _check_str(raw, "title", profile_key, "title")
    _check_str(raw, "about", profile_key, "about")
    _check_str(raw, "area", profile_key, "area")
    _check_str(raw, "relocation", profile_key, "relocation")

    salary = _check_dict(raw, "salary", profile_key, "salary")
    amount = salary.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
        _fail(profile_key, "salary.amount", "должно быть положительным числом")
    _check_str(salary, "currency", profile_key, "salary.currency")

    employments = _check_list(raw, "employments", profile_key, "employments")
    for i, item in enumerate(employments):
        if not isinstance(item, str) or not item.strip():
            _fail(profile_key, f"employments[{i}]", "должно быть непустой строкой")

    schedules = _check_list(raw, "schedules", profile_key, "schedules")
    for i, item in enumerate(schedules):
        if not isinstance(item, str) or not item.strip():
            _fail(profile_key, f"schedules[{i}]", "должно быть непустой строкой")

    skill_set = _check_list(raw, "skill_set", profile_key, "skill_set")
    for i, item in enumerate(skill_set):
        if not isinstance(item, str) or not item.strip():
            _fail(profile_key, f"skill_set[{i}]", "должно быть непустой строкой")

    experience = _check_list(raw, "experience", profile_key, "experience")
    for i, entry in enumerate(experience):
        if not isinstance(entry, dict):
            _fail(profile_key, f"experience[{i}]", "должно быть объектом")
        _check_str(entry, "company", profile_key, f"experience[{i}].company")
        _check_str(entry, "position", profile_key, f"experience[{i}].position")
        start = entry.get("start")
        if not isinstance(start, str) or not _DATE_RE.match(start):
            _fail(profile_key, f"experience[{i}].start", "должно быть датой в формате YYYY-MM-DD")
        end = entry.get("end")
        if end is not None and (not isinstance(end, str) or not _DATE_RE.match(end)):
            _fail(profile_key, f"experience[{i}].end", "должно быть null или датой в формате YYYY-MM-DD")

    education = _check_dict(raw, "education", profile_key, "education")
    _check_str(education, "level", profile_key, "education.level")
    primary = education.get("primary")
    if not isinstance(primary, list):
        _fail(profile_key, "education.primary", "должно быть списком")
    for i, entry in enumerate(primary):
        if not isinstance(entry, dict):
            _fail(profile_key, f"education.primary[{i}]", "должно быть объектом")
        _check_str(entry, "name", profile_key, f"education.primary[{i}].name")

    # Курсы и тренинги. Optional: a profile without them stays valid, and an
    # empty list clears the section on hh.ru.
    additional = education.get("additional")
    if additional is not None:
        if not isinstance(additional, list):
            _fail(profile_key, "education.additional", "должно быть списком")
        for i, entry in enumerate(additional):
            if not isinstance(entry, dict):
                _fail(profile_key, f"education.additional[{i}]", "должно быть объектом")
            _check_str(entry, "name", profile_key, f"education.additional[{i}].name")

    languages = _check_list(raw, "languages", profile_key, "languages", allow_empty=True)
    for i, entry in enumerate(languages):
        if not isinstance(entry, dict):
            _fail(profile_key, f"languages[{i}]", "должно быть объектом")
        _check_str(entry, "name", profile_key, f"languages[{i}].name")
        _check_str(entry, "level", profile_key, f"languages[{i}].level")

    professional_roles = _check_list(raw, "professional_roles", profile_key, "professional_roles")
    for i, item in enumerate(professional_roles):
        if not isinstance(item, str) or not item.strip():
            _fail(profile_key, f"professional_roles[{i}]", "должно быть непустой строкой")


def load_profile(profile_key: str, root: Path = PROFILE_ROOT) -> dict:
    """Read and validate ``hh_profiles/<profile_key>.json``.

    Raises ProfileValidationError naming the offending field on any schema
    violation (missing field, wrong type, malformed date, profile_key/file
    name mismatch, invalid JSON).
    """
    path = root / f"{profile_key}.json"
    if not path.exists():
        raise ProfileValidationError(f"Профиль '{profile_key}': файл не найден ({path})")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileValidationError(f"Профиль '{profile_key}': файл не является валидным JSON: {exc}") from exc
    if not isinstance(raw, dict):
        _fail(profile_key, "<root>", "ожидался JSON-объект")
    _validate_profile(raw, profile_key)
    return raw


# ---------------------------------------------------------------------------
# 2. HHDictionaries
# ---------------------------------------------------------------------------

@dataclass
class HHDictionaries:
    """Fetches and caches hh.ru reference data, and resolves semantic values.

    Read-only: ``load``/``resolve_*`` only ever issue GET requests. Results
    are cached to ``cache_path`` (default ``var/hh_dictionaries_cache.json``)
    for ``ttl_seconds`` (default 7 days) so normal runs and tests do not
    hammer the API.
    """

    client: HHOfficialClient
    cache_path: Path = DEFAULT_DICTIONARY_CACHE_PATH
    ttl_seconds: int = DEFAULT_DICTIONARY_TTL_SECONDS
    _raw: dict = field(default_factory=dict, repr=False)
    _loaded: bool = field(default=False, repr=False)

    def load(self, *, force_refresh: bool = False) -> None:
        if not force_refresh and self._load_from_cache():
            self._loaded = True
            return
        dictionaries = self.client._request("GET", "https://api.hh.ru/dictionaries")
        professional_roles = self.client._request("GET", "https://api.hh.ru/professional_roles")
        areas = self.client._request("GET", "https://api.hh.ru/areas")
        # Languages are NOT part of /dictionaries — they have their own endpoint,
        # and hh.ru requires ``language[].id`` ("rus"/"eng"), not just the name.
        languages = self.client._request("GET", "https://api.hh.ru/languages")
        self._raw = {
            "fetched_at": time.time(),
            "dictionaries": dictionaries,
            "professional_roles": professional_roles,
            "areas": areas,
            "languages": languages,
        }
        self._loaded = True
        self._save_to_cache()

    def _load_from_cache(self) -> bool:
        if not self.cache_path.exists():
            return False
        try:
            cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        fetched_at = float(cached.get("fetched_at", 0) or 0)
        if time.time() - fetched_at > self.ttl_seconds:
            return False
        # A cache written before a new dictionary was added is stale by shape,
        # not by age — refetch instead of resolving against a missing key.
        if not all(key in cached for key in ("dictionaries", "professional_roles", "areas", "languages")):
            return False
        self._raw = cached
        return True

    def _save_to_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._raw, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # cache is a convenience; failing to persist it is not fatal

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _dict_entries(self, dict_key: str) -> list[tuple[str, str]]:
        self._ensure_loaded()
        raw_list = (self._raw.get("dictionaries") or {}).get(dict_key) or []
        entries = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", item.get("code", "")))
            name = str(item.get("name", item_id))
            entries.append((item_id, name))
        return entries

    def _currency_entries(self) -> list[tuple[str, str]]:
        self._ensure_loaded()
        raw_list = (self._raw.get("dictionaries") or {}).get("currency") or []
        entries = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", ""))
            entries.append((code, str(item.get("name", code))))
        return entries

    def _professional_role_entries(self) -> list[tuple[str, str]]:
        self._ensure_loaded()
        categories = (self._raw.get("professional_roles") or {}).get("categories") or []
        entries = []
        for category in categories:
            for role in category.get("roles") or []:
                entries.append((str(role.get("id", "")), str(role.get("name", ""))))
        return entries

    def _area_entries(self) -> list[tuple[str, str]]:
        self._ensure_loaded()
        entries: list[tuple[str, str]] = []

        def walk(nodes):
            for node in nodes or []:
                entries.append((str(node.get("id", "")), str(node.get("name", ""))))
                walk(node.get("areas"))

        walk(self._raw.get("areas") or [])
        return entries

    def _language_entries(self) -> list[tuple[str, str]]:
        self._ensure_loaded()
        return [
            (str(item.get("id", "")), str(item.get("name", "")))
            for item in self._raw.get("languages") or []
            if isinstance(item, dict)
        ]

    @staticmethod
    def _resolve_from_entries(value: str, entries: list[tuple[str, str]], *, dict_key: str) -> str:
        needle = str(value).strip().casefold()
        for item_id, _name in entries:
            if item_id.casefold() == needle:
                return item_id
        for item_id, name in entries:
            if name.casefold() == needle:
                return item_id
        names = [name for _id, name in entries]
        # A short dictionary (employment, schedule, education level) is worth
        # printing in full — the owner can just pick. A long one is not:
        # printing the first 50 of 304 professional roles buried the answer,
        # because the nearest match to "Продакт-менеджер" is "Менеджер
        # продукта" and the wall of text opened with Автомойщик and Автослесарь.
        if len(entries) <= _FULL_DICTIONARY_LISTING_LIMIT:
            valid = ", ".join(f"{name} ({item_id})" for item_id, name in entries)
            raise DictionaryResolutionError(
                f"Не удалось сопоставить значение '{value}' со справочником '{dict_key}'. "
                f"Допустимые варианты: {valid}."
            )
        close = difflib.get_close_matches(str(value), names, n=5, cutoff=0.4)
        if not close:
            needle_words = {w for w in re.findall(r"\w{4,}", needle)}
            close = [
                name for name in names
                if needle_words & set(re.findall(r"\w{4,}", name.casefold()))
            ][:5]
        by_name = {name: item_id for item_id, name in entries}
        suggestion = (
            ", ".join(f"«{name}» ({by_name[name]})" for name in close)
            if close else "похожих вариантов не нашлось"
        )
        raise DictionaryResolutionError(
            f"Значения '{value}' нет в справочнике hh.ru '{dict_key}' "
            f"({len(entries)} вариантов). Ближайшее: {suggestion}."
        )

    def resolve_employment(self, value: str) -> str:
        return self._resolve_from_entries(value, self._dict_entries("employment"), dict_key="employment")

    def resolve_schedule(self, value: str) -> str:
        return self._resolve_from_entries(value, self._dict_entries("schedule"), dict_key="schedule")

    def resolve_relocation(self, value: str) -> str:
        return self._resolve_from_entries(value, self._dict_entries("relocation_type"), dict_key="relocation_type")

    def resolve_business_trip_readiness(self, value: str) -> str:
        return self._resolve_from_entries(
            value, self._dict_entries("business_trip_readiness"), dict_key="business_trip_readiness"
        )

    def resolve_education_level(self, value: str) -> str:
        return self._resolve_from_entries(value, self._dict_entries("education_level"), dict_key="education_level")

    def resolve_language_level(self, value: str) -> str:
        return self._resolve_from_entries(value, self._dict_entries("language_level"), dict_key="language_level")

    def resolve_language(self, value: str) -> str:
        return self._resolve_from_entries(value, self._language_entries(), dict_key="languages")

    def resolve_currency(self, value: str) -> str:
        return self._resolve_from_entries(value, self._currency_entries(), dict_key="currency")

    def resolve_professional_role(self, value: str) -> str:
        return self._resolve_from_entries(value, self._professional_role_entries(), dict_key="professional_roles")

    def resolve_area(self, value: str) -> str:
        return self._resolve_from_entries(value, self._area_entries(), dict_key="areas")


# ---------------------------------------------------------------------------
# 3. build_payload
# ---------------------------------------------------------------------------

def _extract_writable_fields(resume: dict) -> dict:
    return {key: deepcopy(resume[key]) for key in WRITABLE_RESUME_FIELDS if key in resume}


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    text = str(text)
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _report_truncation(sink: list[dict] | None, field_name: str, original_length: int, truncated_length: int) -> None:
    if sink is None:
        return
    sink.append({"field": field_name, "original_length": original_length, "truncated_length": truncated_length})


def build_payload(
    profile: dict,
    dictionaries: HHDictionaries,
    current_resume: dict,
    *,
    truncations: list[dict] | None = None,
) -> dict:
    """Build the body for ``PUT /resumes/{resume_id}``.

    Every field the profile describes is resolved to hh.ru dictionary ids
    via ``dictionaries``. Every writable field the profile is silent about
    (photo, contacts, name, birth date, gender, ...) is carried over from
    ``current_resume`` untouched.

    If any value must be truncated to respect an HH length limit, the
    truncation is recorded into ``truncations`` (field name + before/after
    length) instead of happening silently. Pass a list to receive them.
    """
    payload = _extract_writable_fields(current_resume)

    title, truncated = _truncate(profile["title"], MAX_TITLE_LENGTH)
    if truncated:
        _report_truncation(truncations, "title", len(profile["title"]), len(title))
    payload["title"] = title

    about, truncated = _truncate(profile["about"], MAX_ABOUT_LENGTH)
    if truncated:
        _report_truncation(truncations, "skills", len(profile["about"]), len(about))
    payload["skills"] = about

    raw_skill_set = profile["skill_set"]
    if len(raw_skill_set) > MAX_SKILL_SET_COUNT:
        _report_truncation(truncations, "skill_set:count", len(raw_skill_set), MAX_SKILL_SET_COUNT)
    skill_set = []
    for raw_skill in raw_skill_set[:MAX_SKILL_SET_COUNT]:
        tag, truncated = _truncate(raw_skill, MAX_SKILL_TAG_LENGTH)
        if truncated:
            _report_truncation(truncations, f"skill_set:{raw_skill[:20]}", len(raw_skill), len(tag))
        skill_set.append(tag)
    payload["skill_set"] = skill_set

    payload["area"] = {"id": dictionaries.resolve_area(profile["area"])}
    payload["relocation"] = {"type": {"id": dictionaries.resolve_relocation(profile["relocation"])}}

    # Opt-in, unlike the fields above: only overwrite when the profile actually
    # states a readiness. An absent key leaves whatever hh.ru already holds
    # (carried over via WRITABLE_RESUME_FIELDS) instead of blanking it — the
    # same trap `education` fell into, where an empty profile list silently
    # erased the live resume.
    trip_readiness = str(profile.get("business_trip_readiness") or "").strip()
    if trip_readiness:
        payload["business_trip_readiness"] = {
            "id": dictionaries.resolve_business_trip_readiness(trip_readiness)
        }
    payload["employments"] = [{"id": dictionaries.resolve_employment(v)} for v in profile["employments"]]
    payload["schedules"] = [{"id": dictionaries.resolve_schedule(v)} for v in profile["schedules"]]
    payload["professional_roles"] = [
        {"id": dictionaries.resolve_professional_role(v)} for v in profile["professional_roles"]
    ]
    payload["education"] = {
        "level": {"id": dictionaries.resolve_education_level(profile["education"]["level"])},
        "primary": [
            {
                "name": entry.get("name", ""),
                "organization": entry.get("organization", ""),
                "result": entry.get("result", ""),
                "year": entry.get("year"),
            }
            for entry in profile["education"].get("primary") or []
        ],
        "additional": [
            {
                "name": entry.get("name", ""),
                "organization": entry.get("organization", ""),
                "result": entry.get("result", ""),
                "year": entry.get("year"),
            }
            for entry in profile["education"].get("additional") or []
        ],
    }
    # hh.ru rejects a language without ``id`` ("bad_json_data /language/0/id"),
    # so the name is resolved through the /languages dictionary, not passed through.
    payload["language"] = [
        {
            "id": dictionaries.resolve_language(lang["name"]),
            "name": lang["name"],
            "level": {"id": dictionaries.resolve_language_level(lang["level"])},
        }
        for lang in profile["languages"]
    ]
    payload["salary"] = {
        "amount": profile["salary"]["amount"],
        "currency": dictionaries.resolve_currency(profile["salary"]["currency"]),
    }

    experience = []
    for entry in profile["experience"]:
        raw_description = str(entry.get("description", ""))
        description, truncated = _truncate(raw_description, MAX_EXPERIENCE_DESCRIPTION_LENGTH)
        if truncated:
            _report_truncation(
                truncations, f"experience:{entry.get('company', '')}",
                len(raw_description), len(description),
            )
        # ``area`` on an experience entry is an object on hh.ru's side. Sending
        # the plain city name gets the whole PUT rejected with
        # "bad_json_data /experience/N/area"; an absent city is null, not "".
        raw_area = str(entry.get("area") or "").strip()
        experience.append({
            "company": entry.get("company", ""),
            "position": entry.get("position", ""),
            "start": entry.get("start"),
            "end": entry.get("end"),
            "area": {"id": dictionaries.resolve_area(raw_area)} if raw_area else None,
            "description": description,
        })
    payload["experience"] = experience

    return payload


# ---------------------------------------------------------------------------
# 4. diff
# ---------------------------------------------------------------------------

def _summarize_text(text: str) -> dict:
    lines = text.splitlines()
    first_line = lines[0] if lines else ""
    if len(first_line) > 120:
        first_line = first_line[:120] + "…"
    return {"length": len(text), "first_line": first_line}


def _ids(items) -> list[str]:
    result = []
    for item in items or []:
        if isinstance(item, dict):
            result.append(str(item.get("id", "")))
        else:
            result.append(str(item))
    return result


def _names(items) -> list[str]:
    result = []
    for item in items or []:
        if isinstance(item, dict):
            result.append(str(item.get("name") or item.get("id") or ""))
        else:
            result.append(str(item))
    return result


def _as_list(single_or_list) -> list:
    if single_or_list is None:
        return []
    if isinstance(single_or_list, list):
        return single_or_list
    return [single_or_list]


#: hh.ru returns several list fields in its own order and normalises skill tags
#: to its skill dictionary ("Product management" -> "Product Management"). Comparing
#: those lists positionally made ``diff`` report a change on every single open, which
#: trains the owner to click through the confirmation screen without reading it.
#: Order- and case-insensitive comparison keeps the screen meaningful.
def _unordered(values) -> tuple:
    return tuple(sorted(str(v).casefold() for v in values))


def _course_keys(entries: list) -> tuple:
    """Identity of a course list — name + organization + year, order-insensitive."""
    return tuple(sorted(
        (str(e.get("name") or ""), str(e.get("organization") or ""), str(e.get("year") or ""))
        for e in entries if isinstance(e, dict)
    ))


def _education_keys(entries: list) -> tuple:
    return tuple(sorted(
        (
            str(e.get("name") or ""), str(e.get("organization") or ""),
            str(e.get("result") or ""), str(e.get("year") or ""),
        )
        for e in entries if isinstance(e, dict)
    ))


def _experience_key(item: dict) -> tuple:
    area = item.get("area") or {}
    area_id = str(area.get("id") or "") if isinstance(area, dict) else str(area or "")
    description = str(item.get("description") or "").replace("\r\n", "\n").strip()
    return (
        str(item.get("company") or ""), str(item.get("position") or ""),
        str(item.get("start") or ""), str(item.get("end") or ""), area_id, description,
    )


def _experience_display(entries: list) -> dict:
    return {
        "count": len(entries),
        "entries": [
            {
                "company": str(entry.get("company") or ""),
                "position": str(entry.get("position") or ""),
                "description": _summarize_text(str(entry.get("description") or "")),
            }
            for entry in entries
        ],
    }


def diff(current_resume: dict, payload: dict, *, profile: dict | None = None) -> list[dict]:
    """Field-by-field comparison of the live resume against the built payload.

    Returns structured records (not a formatted string) so a Telegram
    confirmation screen can render them:
    ``{"field": str, "label": ru-str, "old": Any, "new": Any, "changed": bool}``.
    Long text fields (``skills``/about) report ``{"length": int, "first_line": str}``
    instead of the full text. When ``profile`` is given, the "new" side of
    dictionary-resolved fields shows the original semantic value (e.g. the
    Russian name) instead of the raw hh.ru id, for readability.
    """
    records: list[dict] = []

    def add(field_key: str, label: str, old, new, *, changed: bool | None = None) -> None:
        if changed is None:
            changed = old != new
        records.append({"field": field_key, "label": label, "old": old, "new": new, "changed": changed})

    old_title = str(current_resume.get("title") or "")
    new_title = str(payload.get("title") or "")
    add("title", FIELD_LABELS_RU["title"], old_title, new_title)

    old_about = str(current_resume.get("skills") or "")
    new_about = str(payload.get("skills") or "")
    add(
        "skills", FIELD_LABELS_RU["skills"],
        _summarize_text(old_about), _summarize_text(new_about),
        changed=old_about != new_about,
    )

    old_skill_set = list(current_resume.get("skill_set") or [])
    new_skill_set = list(payload.get("skill_set") or [])
    add(
        "skill_set", FIELD_LABELS_RU["skill_set"], old_skill_set, new_skill_set,
        changed=_unordered(old_skill_set) != _unordered(new_skill_set),
    )

    old_area_obj = current_resume.get("area") or {}
    new_area_obj = payload.get("area") or {}
    old_area_display = str(old_area_obj.get("name") or old_area_obj.get("id") or "")
    new_area_display = str((profile or {}).get("area") or new_area_obj.get("id") or "")
    add(
        "area", FIELD_LABELS_RU["area"], old_area_display, new_area_display,
        changed=str(old_area_obj.get("id") or "") != str(new_area_obj.get("id") or ""),
    )

    old_relocation_type = (current_resume.get("relocation") or {}).get("type") or {}
    new_relocation_type = (payload.get("relocation") or {}).get("type") or {}
    old_relocation_display = str(old_relocation_type.get("name") or old_relocation_type.get("id") or "")
    new_relocation_display = str((profile or {}).get("relocation") or new_relocation_type.get("id") or "")
    add(
        "relocation", FIELD_LABELS_RU["relocation"], old_relocation_display, new_relocation_display,
        changed=str(old_relocation_type.get("id") or "") != str(new_relocation_type.get("id") or ""),
    )

    old_trip = current_resume.get("business_trip_readiness") or {}
    new_trip = payload.get("business_trip_readiness") or {}
    if new_trip:
        add(
            "business_trip_readiness", FIELD_LABELS_RU["business_trip_readiness"],
            str(old_trip.get("name") or old_trip.get("id") or ""),
            str((profile or {}).get("business_trip_readiness") or new_trip.get("id") or ""),
            changed=str(old_trip.get("id") or "") != str(new_trip.get("id") or ""),
        )

    old_employments = _as_list(current_resume.get("employments") or current_resume.get("employment"))
    new_employments = payload.get("employments") or []
    old_employments_display = _names(old_employments)
    new_employments_display = list((profile or {}).get("employments") or _ids(new_employments))
    add(
        "employments", FIELD_LABELS_RU["employments"], old_employments_display, new_employments_display,
        changed=_unordered(_ids(old_employments)) != _unordered(_ids(new_employments)),
    )

    old_schedules = _as_list(current_resume.get("schedules") or current_resume.get("schedule"))
    new_schedules = payload.get("schedules") or []
    old_schedules_display = _names(old_schedules)
    new_schedules_display = list((profile or {}).get("schedules") or _ids(new_schedules))
    add(
        "schedules", FIELD_LABELS_RU["schedules"], old_schedules_display, new_schedules_display,
        changed=_unordered(_ids(old_schedules)) != _unordered(_ids(new_schedules)),
    )

    old_education = current_resume.get("education") or {}
    new_education = payload.get("education") or {}
    old_level = old_education.get("level") or {}
    new_level = new_education.get("level") or {}
    old_primary = old_education.get("primary") or []
    new_primary = new_education.get("primary") or []
    # Курсы (education.additional) count here too: without them a course list
    # would be written to hh.ru without ever appearing on the confirmation screen.
    old_additional = old_education.get("additional") or []
    new_additional = new_education.get("additional") or []
    old_education_display = {
        "level": str(old_level.get("name") or old_level.get("id") or ""),
        "entries": len(old_primary),
        "courses": len(old_additional),
    }
    new_education_display = {
        "level": str((profile or {}).get("education", {}).get("level") or new_level.get("id") or ""),
        "entries": len(new_primary),
        "courses": len(new_additional),
    }
    add(
        "education", FIELD_LABELS_RU["education"], old_education_display, new_education_display,
        changed=(
            (str(old_level.get("id") or ""), _education_keys(old_primary), _course_keys(old_additional))
            != (str(new_level.get("id") or ""), _education_keys(new_primary), _course_keys(new_additional))
        ),
    )

    old_language = current_resume.get("language") or []
    new_language = payload.get("language") or []

    def _lang_display(item):
        level = item.get("level") or {}
        return f"{item.get('name', '')} ({level.get('name') or level.get('id') or ''})"

    old_language_display = [_lang_display(item) for item in old_language]
    if profile is not None:
        new_language_display = [f"{item['name']} ({item['level']})" for item in profile.get("languages", [])]
    else:
        new_language_display = [_lang_display(item) for item in new_language]

    def _lang_key(item):
        level = item.get("level") or {}
        return (str(item.get("name", "")), str(level.get("id", "")))

    add(
        "language", FIELD_LABELS_RU["language"], old_language_display, new_language_display,
        changed=sorted(_lang_key(i) for i in old_language) != sorted(_lang_key(i) for i in new_language),
    )

    old_salary = current_resume.get("salary") or {}
    new_salary = payload.get("salary") or {}
    old_salary_display = (
        f"{old_salary['amount']} {old_salary.get('currency', '')}" if old_salary.get("amount") else "не указана"
    )
    new_salary_display = (
        f"{new_salary['amount']} {new_salary.get('currency', '')}" if new_salary.get("amount") else "не указана"
    )
    add(
        "salary", FIELD_LABELS_RU["salary"], old_salary_display, new_salary_display,
        changed=(old_salary.get("amount"), old_salary.get("currency")) != (new_salary.get("amount"), new_salary.get("currency")),
    )

    old_experience = current_resume.get("experience") or []
    new_experience = payload.get("experience") or []

    old_experience_display = _experience_display(old_experience)
    new_experience_display = _experience_display(new_experience)
    add(
        "experience", FIELD_LABELS_RU["experience"], old_experience_display, new_experience_display,
        changed=sorted(_experience_key(e) for e in old_experience)
        != sorted(_experience_key(e) for e in new_experience),
    )

    old_roles = current_resume.get("professional_roles") or []
    new_roles = payload.get("professional_roles") or []
    old_roles_display = _names(old_roles)
    new_roles_display = list((profile or {}).get("professional_roles") or _ids(new_roles))
    add(
        "professional_roles", FIELD_LABELS_RU["professional_roles"], old_roles_display, new_roles_display,
        changed=_unordered(_ids(old_roles)) != _unordered(_ids(new_roles)),
    )

    return records


# ---------------------------------------------------------------------------
# 5 & 6. Writes: apply_profile, publish, create_resume (+ snapshot/restore)
# ---------------------------------------------------------------------------

def apply_profile(client: HHOfficialClient, resume_id: str, payload: dict) -> dict:
    """``PUT /resume_profile/{resume_id}``. Writes to hh.ru — call explicitly only.

    Returns ``{"status": int, "body": dict}`` reflecting HH's real response.
    Raises HHResumeApplyError with a Russian-language, field-level message
    on any 4xx/5xx response. Never returns as if it succeeded when HH did
    not confirm it.
    """
    try:
        body = client._request(
            "PUT", f"https://api.hh.ru/resume_profile/{resume_id}",
            json_data={"resume": payload},
        )
    except HHOfficialError as exc:
        raise HHResumeApplyError(exc.status, exc.errors, _format_hh_error(exc)) from exc
    return {"status": client.last_response_status, "body": body}


def publish(client: HHOfficialClient, resume_id: str) -> dict:
    """``POST /resumes/{resume_id}/publish``. Writes to hh.ru — call explicitly only."""
    try:
        body = client._request("POST", f"https://api.hh.ru/resumes/{resume_id}/publish")
    except HHOfficialError as exc:
        raise HHResumeApplyError(exc.status, exc.errors, _format_hh_error(exc)) from exc
    return {"status": client.last_response_status, "body": body}


def create_resume(client: HHOfficialClient, payload: dict) -> str:
    """``POST /resumes``. Writes to hh.ru — call explicitly only.

    Returns the new resume id. Raises HHResumeApplyError if HH does not
    confirm success or does not return an id (no silent "probably worked").
    """
    try:
        body = client._request("POST", "https://api.hh.ru/resumes", json_data=payload)
    except HHOfficialError as exc:
        raise HHResumeApplyError(exc.status, exc.errors, _format_hh_error(exc)) from exc
    resume_id = str(body.get("id") or "") if isinstance(body, dict) else ""
    if not resume_id:
        location = client.last_response_headers.get("Location") or client.last_response_headers.get("location") or ""
        match = re.search(r"/resumes/([^/?]+)", location)
        resume_id = match.group(1) if match else ""
    if not resume_id:
        raise HHResumeApplyError(
            client.last_response_status, [],
            "HH подтвердил запрос, но не вернул id нового резюме — создание не подтверждено.",
        )
    return resume_id


def snapshot(client: HHOfficialClient, resume_id: str, path: str | Path) -> Path:
    """Save the live resume to ``path`` before any write. Read-only HH call."""
    resume = client.resume(resume_id)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(resume, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def restore(client: HHOfficialClient, resume_id: str, path: str | Path) -> dict:
    """Write a previously saved snapshot back to hh.ru. Writes to hh.ru — call explicitly only."""
    snapshot_data = json.loads(Path(path).read_text(encoding="utf-8"))
    payload = _extract_writable_fields(snapshot_data)
    try:
        body = client._request(
            "PUT", f"https://api.hh.ru/resume_profile/{resume_id}",
            json_data={"resume": payload},
        )
    except HHOfficialError as exc:
        raise HHResumeApplyError(exc.status, exc.errors, _format_hh_error(exc)) from exc
    return {"status": client.last_response_status, "body": body}


__all__ = [
    "ProfileValidationError",
    "DictionaryResolutionError",
    "HHResumeApplyError",
    "PROFILE_ROOT",
    "DEFAULT_DICTIONARY_CACHE_PATH",
    "DEFAULT_DICTIONARY_TTL_SECONDS",
    "load_profile",
    "HHDictionaries",
    "build_payload",
    "diff",
    "apply_profile",
    "publish",
    "create_resume",
    "snapshot",
    "restore",
]
