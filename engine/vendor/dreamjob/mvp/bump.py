"""hh.ru's free "поднять резюме в поиске", used on a schedule instead of never.

hh.ru lets a published resume be re-published once every four hours, which
moves it back to the top of the recruiter's resume search for its profession.
Publishing is scheduled here separately from profile editing.

Only manifest-declared resumes present in ``hh_resume_mappings`` are bumped.
Unmapped resumes are left alone.

The cycle bumps **at most one resume per pass**. With a five-minute loop that
staggers eligible resumes without any offset
arithmetic, and each resume keeps its own independent four-hour clock rather
than drifting a little later every day.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot

from candidate.data import profile_keys
from candidate.hh_official import HHOfficialError

from mvp import config
from mvp.automation import _hh_client_for
from mvp.formatting import _safe_send

log = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo(os.environ.get("BOT_TIMEZONE", "UTC"))

#: hh.ru refuses a re-publish inside four hours; a minute of slack absorbs
#: clock skew between us and them so an eligible resume is not skipped for a
#: whole cycle over two seconds.
_PUBLISH_SLACK = timedelta(minutes=1)


def _window_now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)


def in_bump_window(now: datetime | None = None) -> bool:
    """Are we inside the hours where a bump actually buys a recruiter's attention?

    Bumping at 04:00 burns one of the day's four slots on an empty search page.
    """
    local = _window_now(now)
    if os.environ.get("RESUME_BUMP_WEEKDAYS_ONLY", "1") == "1" and local.weekday() >= 5:
        return False
    start = int(os.environ.get("RESUME_BUMP_HOUR_START", "9"))
    end = int(os.environ.get("RESUME_BUMP_HOUR_END", "22"))
    return start <= local.hour < end


def _parse_hh_time(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def due_resume(resumes: list[dict], mapped_ids: set[str], now: datetime) -> dict | None:
    """The mapped resume that has been stale longest and is publishable right now.

    hh.ru reports ``next_publish_at`` per resume; a resume missing it (or
    reporting ``can_publish_or_update: false``) is skipped rather than guessed
    at, because a rejected publish still counts as a failed request.
    """
    eligible: list[tuple[datetime, dict]] = []
    for resume in resumes:
        if str(resume.get("id") or "") not in mapped_ids:
            continue
        if not resume.get("can_publish_or_update"):
            continue
        next_at = _parse_hh_time(str(resume.get("next_publish_at") or ""))
        if next_at is None or now + _PUBLISH_SLACK < next_at:
            continue
        eligible.append((next_at, resume))
    if not eligible:
        return None
    eligible.sort(key=lambda pair: pair[0])
    return eligible[0][1]


async def run_bump_cycle(bot: Bot, user_id: int) -> str:
    """Bump one resume, or explain in one word why nothing was bumped."""
    if os.environ.get("RESUME_BUMP_ENABLED", "0") != "1":
        return "disabled"
    if not in_bump_window():
        return "outside_window"
    mapped_ids = {
        mapping["resume_id"]
        for mapping in (
            config.AUTOMATION.resume_mapping(user_id, key) for key in profile_keys()
        )
        if mapping
    }
    if not mapped_ids:
        return "no_mapping"
    client, _account = await _hh_client_for(user_id)
    if client is None:
        return "hh_not_connected"
    try:
        resumes = await asyncio.to_thread(client.resumes)
    except HHOfficialError as exc:
        log.warning("Resume bump listing failed safely: %s", exc.reason)
        return f"list_failed:{exc.reason}"
    resume = due_resume(resumes, mapped_ids, datetime.now(timezone.utc))
    if resume is None:
        return "nothing_due"
    resume_id = str(resume["id"])
    try:
        await asyncio.to_thread(client.publish_resume, resume_id)
    except HHOfficialError as exc:
        config.AUTOMATION.record_resume_bump(
            user_id=user_id, resume_id=resume_id, status="failed", reason=exc.reason,
        )
        log.warning("Resume bump refused by hh.ru: %s (%s)", exc.reason, resume_id)
        # A refused bump is routine (hh.ru is the authority on the four-hour
        # clock) — only a dead token is worth interrupting the owner for.
        if exc.status in (401, 403):
            await _safe_send(
                bot, user_id,
                "⚠️ hh.ru не принимает поднятие резюме: похоже, слетел доступ. "
                "Переподключите HH в настройках — резюме сейчас не поднимаются.",
            )
        return f"failed:{exc.reason}"
    config.AUTOMATION.record_resume_bump(user_id=user_id, resume_id=resume_id, status="ok")
    log.info("Resume bumped: %s (%s)", resume.get("title"), resume_id)
    return "ok"


async def _resume_bump_loop(bot: Bot) -> None:
    await asyncio.sleep(45)
    interval = max(60, int(os.environ.get("RESUME_BUMP_INTERVAL_SECONDS", "300")))
    while True:
        try:
            owner_raw = os.environ.get("MVP_OWNER_TELEGRAM_ID", "")
            if owner_raw.isdigit():
                await run_bump_cycle(bot, int(owner_raw))
        except Exception:
            log.exception("Resume bump cycle failed")
        await asyncio.sleep(interval)
