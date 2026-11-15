"""Воронка screen: application outcomes broken down by master profile.

Read-only against `var/candidate_mvp.sqlite3` — this router opens its own
`mode=ro` SQLite connection (belt-and-braces on top of never calling any
mutating method) and never touches `config.STORE` / `config.AUTOMATION`
write paths. All counts are derived straight from `auto_apply_log`,
`application_events`, `candidate_variants` and `hh_resume_mappings`; where
the data genuinely can't answer a question (e.g. the raw count of vacancies
HH returned before dedup/generation), the screen says so instead of
inventing a number.
"""
from __future__ import annotations

import asyncio
import html
import json
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from candidate.data import load_candidate_data

from mvp import config

from mvp.formatting import send_long_message as _send_long_message

router = Router()

UNKNOWN_PROFILE = "профиль не определён"
MIN_SAMPLE = 30  # owner's own threshold: conclusions before 30–50 applications/profile are noise

REPLIED_STATUSES = {"replied", "interview", "offer"}
INVITED_STATUSES = {"interview", "offer"}


# ---------------------------------------------------------------------------
# Data access (read-only)
# ---------------------------------------------------------------------------

@contextmanager
def _ro_connect():
    """Open the shared candidate DB read-only; a write attempt would raise, not silently no-op."""
    db = sqlite3.connect(f"file:{config.AUTOMATION.path}?mode=ro", uri=True)
    try:
        yield db
    finally:
        db.close()


def _variant_status_counts(db: sqlite3.Connection, user_id: int) -> dict[str, int]:
    rows = db.execute(
        "SELECT status, COUNT(*) FROM candidate_variants WHERE user_id=? GROUP BY status",
        (user_id,),
    ).fetchall()
    return {status: count for status, count in rows}


def _load_applications(db: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = db.execute(
        """SELECT l.id, l.resume_id, l.status, l.created_at, l.hh_state,
                  l.viewed_by_opponent, l.outcome_status, v.payload
           FROM auto_apply_log AS l
           LEFT JOIN candidate_variants AS v ON v.id = l.variant_id
           WHERE l.user_id=?
           ORDER BY l.created_at ASC""",
        (user_id,),
    ).fetchall()
    applications = []
    for app_id, resume_id, status, created_at, hh_state, viewed, outcome_status, raw_payload in rows:
        payload = {}
        if raw_payload:
            try:
                payload = json.loads(raw_payload)
            except (TypeError, ValueError):
                payload = {}
        vacancy = payload.get("vacancy") or {}
        applications.append({
            "id": app_id, "resume_id": resume_id, "status": status, "created_at": created_at,
            "hh_state": hh_state or "", "viewed_by_opponent": bool(viewed),
            "outcome_status": outcome_status or "",
            "employer_name": vacancy.get("employer_name") or "",
            "source": vacancy.get("source") or "",
            "profile_key": (payload.get("master_profile") or {}).get("key") or "",
        })
    return applications


def _resume_profile_map(db: sqlite3.Connection, user_id: int) -> dict[str, str]:
    rows = db.execute(
        "SELECT resume_id, profile_key FROM hh_resume_mappings WHERE user_id=?",
        (user_id,),
    ).fetchall()
    return {resume_id: profile_key for resume_id, profile_key in rows}


def _first_reply_times(db: sqlite3.Connection, user_id: int) -> dict[int, str]:
    rows = db.execute(
        """SELECT application_id, MIN(created_at) FROM application_events
           WHERE user_id=? AND event_type='hh_sync' AND to_status IN ('replied','interview','offer')
           GROUP BY application_id""",
        (user_id,),
    ).fetchall()
    return {application_id: first_at for application_id, first_at in rows}


def _last_sync_at(db: sqlite3.Connection, user_id: int) -> str | None:
    row = db.execute(
        "SELECT MAX(updated_at) FROM auto_apply_log WHERE user_id=?", (user_id,),
    ).fetchone()
    return row[0] if row and row[0] else None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _pct(part: int, whole: int) -> str:
    return "—" if whole <= 0 else f"{round(part / whole * 100)}%"


def _sample_note(n: int) -> str:
    return f"⚠️ n={n} — мало для вывода, порог владельца 30–50 заявок" if n < MIN_SAMPLE else ""


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _format_ago(delta) -> str:
    seconds = max(0.0, delta.total_seconds())
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} мин назад"
    hours = minutes / 60
    if hours < 48:
        return f"{int(hours)} ч назад"
    return f"{int(hours / 24)} дн назад"


def _format_avg_duration(deltas: list) -> str:
    if not deltas:
        return "—"
    avg_hours = sum(d.total_seconds() for d in deltas) / len(deltas) / 3600
    if avg_hours < 1:
        return "<1 ч"
    if avg_hours < 48:
        return f"~{avg_hours:.0f} ч"
    return f"~{avg_hours / 24:.1f} дн"


def _profile_label(key: str, data) -> str:
    if not key:
        return UNKNOWN_PROFILE
    profile = data.profiles.get(key)
    return profile.desired_title if profile else key


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def _funnel_section(variant_counts: dict[str, int], applied: list[dict]) -> str:
    total = sum(variant_counts.values())
    shown = total - variant_counts.get("skipped", 0) - variant_counts.get("failed", 0)
    sent = len(applied)
    viewed = sum(1 for a in applied if a["viewed_by_opponent"])
    replied = sum(1 for a in applied if a["outcome_status"] in REPLIED_STATUSES or a["hh_state"] == "invitation")
    invited = sum(1 for a in applied if a["hh_state"] == "invitation" or a["outcome_status"] in INVITED_STATUSES)
    interview = sum(1 for a in applied if a["outcome_status"] in INVITED_STATUSES)
    offer = sum(1 for a in applied if a["outcome_status"] == "offer")
    rejected = sum(1 for a in applied if a["hh_state"] == "discard" or a["outcome_status"] == "rejected")
    no_response = max(0, sent - replied - rejected)

    lines = ["📊 <b>Общая воронка</b>", ""]
    note = _sample_note(sent)
    if note:
        lines.append(note)
        lines.append("")
    lines.extend([
        f"🔎 Найдено (сгенерирован вариант): {total}",
        "    сырое число вакансий из поиска HH до дедупликации не логируется — «—»",
        f"👀 Показано на подтверждение: {shown}",
        f"📤 Отправлено: {sent}",
        f"👁 Просмотрено работодателем: {viewed} ({_pct(viewed, sent)})",
        f"💬 Ответ: {replied} ({_pct(replied, sent)})",
        f"📅 Приглашение: {invited} ({_pct(invited, sent)})",
        f"🎤 Интервью: {interview} ({_pct(interview, sent)}) — отмечается вручную кнопкой после отклика",
        f"🎉 Оффер: {offer} ({_pct(offer, sent)}) — вручную",
        f"⛔ Отказ: {rejected} ({_pct(rejected, sent)})",
        f"⌛ Нет ответа пока: {no_response} ({_pct(no_response, sent)})",
    ])
    return "\n".join(lines)


def _profile_section(applied: list[dict], resume_map: dict[str, str]) -> str:
    if not applied:
        return "🎯 <b>Разрез по профилям</b>\n\nПока нет отправленных откликов — разрез недоступен."
    buckets: dict[str, list[dict]] = defaultdict(list)
    for a in applied:
        key = resume_map.get(a["resume_id"]) or a["profile_key"] or ""
        buckets[key].append(a)
    data = config.ADAPTER.data if config.ADAPTER else load_candidate_data()
    order = [key for key in data.profiles if key in buckets]
    order += sorted(key for key in buckets if key and key not in data.profiles)
    if "" in buckets:
        order.append("")

    lines = ["🎯 <b>Разрез по профилям</b>", ""]
    for key in order:
        items = buckets[key]
        n = len(items)
        viewed = sum(1 for a in items if a["viewed_by_opponent"])
        replied = sum(1 for a in items if a["outcome_status"] in REPLIED_STATUSES or a["hh_state"] == "invitation")
        invited = sum(1 for a in items if a["hh_state"] == "invitation" or a["outcome_status"] in INVITED_STATUSES)
        rejected = sum(1 for a in items if a["hh_state"] == "discard" or a["outcome_status"] == "rejected")
        label = html.escape(_profile_label(key, data))
        lines.append(f"<b>{label}</b>")
        lines.append(f"  отправлено {n} · просмотрено {viewed} · ответ {replied} ({_pct(replied, n)})")
        lines.append(f"  приглашение {invited} · отказ {rejected}")
        note = _sample_note(n)
        if note:
            lines.append(f"  {note}")
        lines.append("")

    missing = [key for key in data.profiles if key not in buckets]
    if missing:
        names = ", ".join(html.escape(data.profiles[key].desired_title) for key in missing)
        lines.append(f"Ещё не пробовали: {names}")
    return "\n".join(lines).rstrip()


def _company_section(applied: list[dict], reply_times: dict[int, str]) -> str:
    if not applied:
        return "🏢 <b>По компаниям и источникам</b>\n\nПока нет данных."
    by_source: dict[str, int] = defaultdict(int)
    by_employer: dict[str, list[dict]] = defaultdict(list)
    for a in applied:
        by_source[a["source"] or "manual"] += 1
        by_employer[a["employer_name"] or "Без названия"].append(a)

    source_line = ", ".join(
        f"{html.escape(src)} {n}" for src, n in sorted(by_source.items(), key=lambda kv: -kv[1])
    )
    lines = ["🏢 <b>По компаниям и источникам</b>", "", f"Источники: {source_line}", ""]
    for employer, items in sorted(by_employer.items(), key=lambda kv: -len(kv[1])):
        n = len(items)
        replied = sum(1 for a in items if a["outcome_status"] in REPLIED_STATUSES or a["hh_state"] == "invitation")
        rejected = sum(1 for a in items if a["hh_state"] == "discard" or a["outcome_status"] == "rejected")
        silent = max(0, n - replied - rejected)
        deltas = []
        for a in items:
            first_reply = reply_times.get(a["id"])
            if not first_reply:
                continue
            try:
                deltas.append(_parse_iso(first_reply) - _parse_iso(a["created_at"]))
            except ValueError:
                continue
        lines.append(
            f"• {html.escape(employer[:40])}\n"
            f"  откликов {n} · ответ {replied} · отказ {rejected} · тишина {silent} "
            f"· время до ответа {_format_avg_duration(deltas)}"
        )
    return "\n".join(lines)


def _freshness_section(last_sync: str | None) -> str:
    if not last_sync:
        return "🕐 <b>Свежесть</b>\n\nСинхронизация с hh.ru ещё ни разу не выполнялась."
    try:
        dt = _parse_iso(last_sync)
    except ValueError:
        return "🕐 <b>Свежесть</b>\n\nВремя последней синхронизации не удалось разобрать."
    age = datetime.now(timezone.utc) - dt
    return (
        "🕐 <b>Свежесть</b>\n\n"
        f"Последняя синхронизация с hh.ru: {_format_ago(age)} ({dt.strftime('%d.%m %H:%M')} UTC)"
    )


def _build_report(user_id: int) -> str:
    try:
        with _ro_connect() as db:
            variant_counts = _variant_status_counts(db, user_id)
            applications = _load_applications(db, user_id)
            resume_map = _resume_profile_map(db, user_id)
            reply_times = _first_reply_times(db, user_id)
            last_sync = _last_sync_at(db, user_id)
    except sqlite3.OperationalError as exc:
        return f"Не удалось прочитать базу заявок: {html.escape(str(exc))}"

    if not variant_counts and not applications:
        return "📊 <b>Воронка</b>\n\nПока нет данных: автопоиск ещё не сохранил ни одной вакансии."

    applied = [a for a in applications if a["status"] == "applied"]
    sections = [
        _funnel_section(variant_counts, applied),
        _profile_section(applied, resume_map),
        _company_section(applied, reply_times),
        _freshness_section(last_sync),
    ]
    return ("\n\n" + "─" * 18 + "\n\n").join(sections)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def _refresh_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить воронку", callback_data="mvp:stats")],
    ])


async def _show_funnel(message: Message, user_id: int) -> None:
    text = await asyncio.to_thread(_build_report, user_id)
    await _send_long_message(message, text, parse_mode="HTML", reply_markup=_refresh_keyboard())


@router.message(Command("voronka"))
async def funnel_command(message: Message) -> None:
    await _show_funnel(message, message.from_user.id)


@router.callback_query(F.data == "mvp:stats")
async def funnel_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    await _show_funnel(callback.message, callback.from_user.id)
