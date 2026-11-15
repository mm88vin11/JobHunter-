"""Durable owner settings for the settings hub: search-query overrides and
letter style (tone + personal denylist).

Search queries
--------------
``mvp/hh_sync.py::_search_queries()`` reads an override from the
``HH_SEARCH_QUERIES`` environment variable (split on ``|``) at CALL time, not
at import time. That makes two things true:

1. Setting ``os.environ["HH_SEARCH_QUERIES"]`` from a running handler takes
   effect on the very next automation cycle -- no restart needed.
2. Environment variables do not survive a process restart on their own, so
   the override is ALSO written to ``var/mvp_settings.json`` (atomic write:
   temp file + ``Path.replace``, mirroring ``candidate/traps.py``'s
   ``_write_all``). ``apply_persisted_search_queries()`` below re-populates
   ``os.environ`` from that file; the process entry point must call it once
   at startup, before the automation loop starts, for the override to
   survive a service restart. See its docstring for the exact call.

Letter style
------------
Tone (one of ``VALID_TONES``) and a personal phrase denylist, both owner
editable from the settings hub. ``get_letter_style()`` is the plain read
function a future letter generator should call; nothing here writes to
``candidate_data/v1/cover_letter_voice.md`` (that document's own denylist is
untouched and out of scope).

Filter thresholds
------------------
``daily_limit`` / ``min_preview_score`` / ``min_apply_score`` already live in
the ``automation_settings`` SQLite table owned by
``candidate/automation.py::AutomationStore``. ``update_automation_thresholds``
writes into that same table through a plain ``sqlite3`` connection on the
store's public ``.path`` attribute -- it does not modify
``candidate/automation.py`` and does not duplicate its schema.

CRM settings
------------
Four owner-editable knobs for the upcoming CRM screen: how many days of
employer silence before a thread counts as a hanging question
(``crm_silence_days``), whether/when a once-a-day morning digest goes out
(``crm_digest_enabled`` / ``crm_digest_time``, the latter "HH:MM" 24h,
interpreted in ``config.CRM_DIGEST_TIMEZONE``), and the no-movement window
before a thread auto-archives (``crm_archive_after_days``). Persisted the
same way as search queries / letter style -- per-user entry in
``var/mvp_settings.json`` -- with a ``None`` sentinel meaning "never
configured, use the ``mvp.config`` default". Default/range constants live in
``mvp/config.py`` (``CRM_*_DEFAULT`` / ``CRM_*_MIN`` / ``CRM_*_MAX``); this
module only validates and reads/writes. ``get_crm_settings()`` is the plain
typed read function background code (a future digest scheduler, an
auto-archive sweep) should call; ``get_crm_digest_zoneinfo()`` hands back a
stdlib ``zoneinfo.ZoneInfo`` for that same scheduler to build an aware
datetime from ``crm_digest_time`` without hardcoding the tz name twice.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from mvp import config

DEFAULT_TONE = "прямой"
VALID_TONES: tuple[str, ...] = ("спокойный", "прямой", "уверенный")

DAILY_LIMIT_MIN, DAILY_LIMIT_MAX = 1, 200
SCORE_MIN, SCORE_MAX = 0, 100

_SETTINGS_FILENAME = "mvp_settings.json"
_ENV_VAR = "HH_SEARCH_QUERIES"


def _default_path() -> Path:
    return Path(__file__).resolve().parent.parent / "var" / _SETTINGS_FILENAME


def _resolve_path(path: str | Path | None) -> Path:
    return Path(path) if path is not None else _default_path()


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
    return {
        "search_queries": None,
        "letter_tone": DEFAULT_TONE,
        "letter_denylist": [],
        "crm_silence_days": None,
        "crm_digest_enabled": None,
        "crm_digest_time": None,
        "crm_archive_after_days": None,
        "crm_digest_last_sent": None,
    }


def _entry(data: dict, user_id: int) -> dict:
    raw = data.get(str(user_id))
    entry = dict(_empty_entry())
    if isinstance(raw, dict):
        entry.update(raw)
    return entry


# ── search queries ──────────────────────────────────────────────────────


def default_query_list(profile_titles: list[str]) -> list[str]:
    """What hh_sync.py._search_queries() falls back to with no env override:
    the caller's live manifest profile titles, de-duplicated
    case-insensitively with order preserved.
    """
    seen: set[str] = set()
    result: list[str] = []
    for text in profile_titles:
        text = str(text).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            result.append(text)
    return result


def get_search_queries(user_id: int, *, path: str | Path | None = None) -> list[str] | None:
    """Persisted override, or None if the owner never overrode it (hh_sync.py's
    own defaults are in effect)."""
    data = _read_all(_resolve_path(path))
    override = _entry(data, user_id).get("search_queries")
    return list(override) if isinstance(override, list) else None


def _sync_env(queries: list[str] | None) -> None:
    if queries:
        os.environ[_ENV_VAR] = "|".join(queries)
    else:
        os.environ.pop(_ENV_VAR, None)


def set_search_queries(user_id: int, queries: list[str], *, path: str | Path | None = None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for text in queries:
        text = str(text).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            cleaned.append(text)
    file_path = _resolve_path(path)
    data = _read_all(file_path)
    entry = data.setdefault(str(user_id), _empty_entry())
    entry["search_queries"] = cleaned
    _write_all(file_path, data)
    _sync_env(cleaned)
    return cleaned


def add_search_query(
    user_id: int, query: str, profile_titles: list[str], *, path: str | Path | None = None,
) -> list[str]:
    query = query.strip()
    current = get_search_queries(user_id, path=path)
    if current is None:
        current = default_query_list(profile_titles)
    if query and query.lower() not in {q.lower() for q in current}:
        current = [*current, query]
    return set_search_queries(user_id, current, path=path)


def remove_search_query(
    user_id: int, query: str, profile_titles: list[str], *, path: str | Path | None = None,
) -> list[str]:
    current = get_search_queries(user_id, path=path)
    if current is None:
        current = default_query_list(profile_titles)
    after = [q for q in current if q.strip().lower() != query.strip().lower()]
    return set_search_queries(user_id, after, path=path)


def reset_search_queries(user_id: int, *, path: str | Path | None = None) -> None:
    file_path = _resolve_path(path)
    data = _read_all(file_path)
    entry = data.setdefault(str(user_id), _empty_entry())
    entry["search_queries"] = None
    _write_all(file_path, data)
    _sync_env(None)


def apply_persisted_search_queries(user_id: int, *, path: str | Path | None = None) -> None:
    """Call once at process startup, before the automation loop starts, so a
    previously saved override survives a service restart.

    Wiring (NOT done here -- run_candidate_mvp.py is out of scope for this
    change): in ``main()``, right after the existing
    ``if owner.isdigit(): config.AUTOMATION.settings(int(owner))`` line, add::

        from mvp.settings_store import apply_persisted_search_queries
        if owner.isdigit():
            apply_persisted_search_queries(int(owner))

    hh_sync.py reads ``HH_SEARCH_QUERIES`` lazily on every call, so populating
    it here before ``dp.start_polling`` / the automation task is created is
    sufficient -- no other wiring is needed.
    """
    _sync_env(get_search_queries(user_id, path=path))


# ── letter style ────────────────────────────────────────────────────────


def get_letter_style(user_id: int, *, path: str | Path | None = None) -> dict:
    """Read function for the letter generator: {"tone": str, "denylist": [str, ...]}.

    Safe to call even if the owner never configured anything -- returns
    DEFAULT_TONE and an empty denylist.
    """
    data = _read_all(_resolve_path(path))
    entry = _entry(data, user_id)
    tone = entry.get("letter_tone") or DEFAULT_TONE
    if tone not in VALID_TONES:
        tone = DEFAULT_TONE
    denylist = entry.get("letter_denylist") or []
    return {"tone": tone, "denylist": [str(x) for x in denylist if str(x).strip()]}


def set_letter_tone(user_id: int, tone: str, *, path: str | Path | None = None) -> None:
    if tone not in VALID_TONES:
        raise ValueError(f"Unknown tone: {tone!r}")
    file_path = _resolve_path(path)
    data = _read_all(file_path)
    entry = data.setdefault(str(user_id), _empty_entry())
    entry["letter_tone"] = tone
    _write_all(file_path, data)


def add_denylist_word(user_id: int, phrase: str, *, path: str | Path | None = None) -> list[str]:
    phrase = phrase.strip()
    file_path = _resolve_path(path)
    data = _read_all(file_path)
    entry = data.setdefault(str(user_id), _empty_entry())
    entry.setdefault("letter_denylist", [])
    if phrase and phrase.lower() not in {str(p).strip().lower() for p in entry["letter_denylist"]}:
        entry["letter_denylist"].append(phrase)
    _write_all(file_path, data)
    return list(entry["letter_denylist"])


def remove_denylist_word(user_id: int, phrase: str, *, path: str | Path | None = None) -> list[str]:
    file_path = _resolve_path(path)
    data = _read_all(file_path)
    entry = data.setdefault(str(user_id), _empty_entry())
    before = entry.get("letter_denylist") or []
    after = [p for p in before if str(p).strip().lower() != phrase.strip().lower()]
    entry["letter_denylist"] = after
    _write_all(file_path, data)
    return after


# ── filter thresholds (writes into automation_settings) ────────────────


def update_automation_thresholds(
    automation_store, user_id: int, *,
    daily_limit: int | None = None,
    min_preview_score: int | None = None,
    min_apply_score: int | None = None,
) -> dict:
    """Persist validated threshold values into automation_settings.

    Range/consistency validation is the caller's job (mvp/routers/settings.py)
    -- this function only writes what it's given. Uses the store's own
    ``.settings()`` upsert to make sure the row exists first, then a plain
    sqlite3 connection on ``automation_store.path`` for the UPDATE, so
    candidate/automation.py itself stays unmodified.
    """
    automation_store.settings(user_id)
    fields: list[str] = []
    values: list[object] = []
    if daily_limit is not None:
        fields.append("daily_limit=?")
        values.append(int(daily_limit))
    if min_preview_score is not None:
        fields.append("min_preview_score=?")
        values.append(int(min_preview_score))
    if min_apply_score is not None:
        fields.append("min_apply_score=?")
        values.append(int(min_apply_score))
    if fields:
        fields.append("updated_at=?")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(user_id)
        db = sqlite3.connect(automation_store.path)
        try:
            db.execute(f"UPDATE automation_settings SET {', '.join(fields)} WHERE user_id=?", values)
            db.commit()
        finally:
            db.close()
    return automation_store.settings(user_id)


# ── CRM (silence threshold, daily digest, auto-archive window) ─────────

_DIGEST_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def is_valid_digest_time(text: str) -> bool:
    """Strict "HH:MM" 24h check (zero-padded, 00-23 / 00-59) -- the router
    uses this to validate before touching the store, mirroring how the
    filter screens validate ranges before calling their setters."""
    return bool(_DIGEST_TIME_RE.match(str(text).strip()))


def get_crm_silence_days(user_id: int, *, path: str | Path | None = None) -> int:
    """Days of employer silence after which a thread counts as a hanging
    question. Defaults to config.CRM_SILENCE_DAYS_DEFAULT when never set."""
    data = _read_all(_resolve_path(path))
    value = _entry(data, user_id).get("crm_silence_days")
    if isinstance(value, int) and not isinstance(value, bool) and (
        config.CRM_SILENCE_DAYS_MIN <= value <= config.CRM_SILENCE_DAYS_MAX
    ):
        return value
    return config.CRM_SILENCE_DAYS_DEFAULT


def set_crm_silence_days(user_id: int, days: int, *, path: str | Path | None = None) -> int:
    if (
        not isinstance(days, int) or isinstance(days, bool)
        or not (config.CRM_SILENCE_DAYS_MIN <= days <= config.CRM_SILENCE_DAYS_MAX)
    ):
        raise ValueError(
            f"crm_silence_days must be an int in {config.CRM_SILENCE_DAYS_MIN}..{config.CRM_SILENCE_DAYS_MAX}: {days!r}"
        )
    file_path = _resolve_path(path)
    data = _read_all(file_path)
    entry = data.setdefault(str(user_id), _empty_entry())
    entry["crm_silence_days"] = days
    _write_all(file_path, data)
    return days


def get_crm_digest_enabled(user_id: int, *, path: str | Path | None = None) -> bool:
    """Whether the once-a-day morning summary is sent. Defaults to
    config.CRM_DIGEST_ENABLED_DEFAULT when never set."""
    data = _read_all(_resolve_path(path))
    value = _entry(data, user_id).get("crm_digest_enabled")
    return value if isinstance(value, bool) else config.CRM_DIGEST_ENABLED_DEFAULT


def set_crm_digest_enabled(user_id: int, enabled: bool, *, path: str | Path | None = None) -> bool:
    file_path = _resolve_path(path)
    data = _read_all(file_path)
    entry = data.setdefault(str(user_id), _empty_entry())
    entry["crm_digest_enabled"] = bool(enabled)
    _write_all(file_path, data)
    return bool(enabled)


def get_crm_digest_time(user_id: int, *, path: str | Path | None = None) -> str:
    """"HH:MM" (24h) the morning digest goes out, interpreted in
    config.CRM_DIGEST_TIMEZONE. Defaults to config.CRM_DIGEST_TIME_DEFAULT
    when never set or when a stored value somehow fails validation."""
    data = _read_all(_resolve_path(path))
    value = _entry(data, user_id).get("crm_digest_time")
    if isinstance(value, str) and is_valid_digest_time(value):
        return value
    return config.CRM_DIGEST_TIME_DEFAULT


def set_crm_digest_time(user_id: int, digest_time: str, *, path: str | Path | None = None) -> str:
    digest_time = str(digest_time).strip()
    if not is_valid_digest_time(digest_time):
        raise ValueError(f"crm_digest_time must be HH:MM in 24h form: {digest_time!r}")
    file_path = _resolve_path(path)
    data = _read_all(file_path)
    entry = data.setdefault(str(user_id), _empty_entry())
    entry["crm_digest_time"] = digest_time
    _write_all(file_path, data)
    return digest_time


def get_crm_digest_zoneinfo() -> ZoneInfo:
    """Stdlib tz object for the configured CRM digest timezone --
    for background code (e.g. a future digest scheduler) that needs an
    aware datetime rather than the bare "HH:MM" string."""
    return ZoneInfo(config.CRM_DIGEST_TIMEZONE)


def get_crm_archive_after_days(user_id: int, *, path: str | Path | None = None) -> int:
    """Days of no thread movement before it gets auto-archived. Defaults to
    config.CRM_ARCHIVE_AFTER_DAYS_DEFAULT when never set."""
    data = _read_all(_resolve_path(path))
    value = _entry(data, user_id).get("crm_archive_after_days")
    if isinstance(value, int) and not isinstance(value, bool) and (
        config.CRM_ARCHIVE_AFTER_DAYS_MIN <= value <= config.CRM_ARCHIVE_AFTER_DAYS_MAX
    ):
        return value
    return config.CRM_ARCHIVE_AFTER_DAYS_DEFAULT


def set_crm_archive_after_days(user_id: int, days: int, *, path: str | Path | None = None) -> int:
    if (
        not isinstance(days, int) or isinstance(days, bool)
        or not (config.CRM_ARCHIVE_AFTER_DAYS_MIN <= days <= config.CRM_ARCHIVE_AFTER_DAYS_MAX)
    ):
        raise ValueError(
            "crm_archive_after_days must be an int in "
            f"{config.CRM_ARCHIVE_AFTER_DAYS_MIN}..{config.CRM_ARCHIVE_AFTER_DAYS_MAX}: {days!r}"
        )
    file_path = _resolve_path(path)
    data = _read_all(file_path)
    entry = data.setdefault(str(user_id), _empty_entry())
    entry["crm_archive_after_days"] = days
    _write_all(file_path, data)
    return days


def get_crm_digest_last_sent(user_id: int, *, path: str | Path | None = None) -> str:
    """ISO calendar date ("YYYY-MM-DD", configured local timezone) the daily digest last
    went out for this owner, or "" if it has never been sent. The digest loop
    in mvp/automation.py reads this to decide whether today's send already
    happened, so a bot restart never double-sends within the same day."""
    data = _read_all(_resolve_path(path))
    value = _entry(data, user_id).get("crm_digest_last_sent")
    return value if isinstance(value, str) else ""


def set_crm_digest_last_sent(user_id: int, date_str: str, *, path: str | Path | None = None) -> str:
    file_path = _resolve_path(path)
    data = _read_all(file_path)
    entry = data.setdefault(str(user_id), _empty_entry())
    entry["crm_digest_last_sent"] = str(date_str)
    _write_all(file_path, data)
    return str(date_str)


def get_crm_settings(user_id: int, *, path: str | Path | None = None) -> dict:
    """Convenience bundle of all four CRM settings, defaults applied --
    {"silence_days": int, "digest_enabled": bool, "digest_time": str,
    "archive_after_days": int}. Mirrors get_letter_style()'s shape for the
    settings-hub renderer; background code can call the individual
    get_crm_* accessors above directly instead."""
    return {
        "silence_days": get_crm_silence_days(user_id, path=path),
        "digest_enabled": get_crm_digest_enabled(user_id, path=path),
        "digest_time": get_crm_digest_time(user_id, path=path),
        "archive_after_days": get_crm_archive_after_days(user_id, path=path),
    }
