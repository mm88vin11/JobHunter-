from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


#: Retention cap for `filtered_vacancies` — see `AutomationStore.record_filtered`.
_FILTERED_MAX_ROWS_PER_USER = 5000


_CRM_SCHEDULED_MARKERS = (
    "договорились", "до связи", "успешно записались", "встреча назначена",
    "собеседование назначено", "интервью назначено",
)
_CRM_TASK_MARKERS = (
    "пройдите", "заполните", "заполни", "перейдите", "перейти в", "следуйте инструкциям",
    "следовать инструкциям", "короткую анкету", "тестовое задание",
)
_CRM_AUTO_ACK_MARKERS = (
    "рассмотрим ваше резюме", "если навыки и опыт подойдут", "мы свяжемся с вами",
    "скоро свяжемся с вами", "обязательно свяжемся с вами", "коллеги свяжутся с вами",
    "понравилось ваше резюме", "направлена на рассмотрение", "просим немного подождать",
)
_CRM_REPLY_MARKERS = ("?", "подскажите", "скажите", "опишите", "уточните", "напишите", "ответьте")
#: hh.ru states in which the process has already moved past waiting on the
#: employer -- a date is set, or the job was won. Threads in these states must
#: never fall into "waiting": that bucket counts days of silence and the
#: archiver eventually files them away as dead, which is exactly how a booked
#: interview would disappear from the screen that exists to prevent that.
_CRM_COMMITTED_STATES = ("interview", "hired")


def crm_bucket(thread: dict) -> str:
    """Return the next-step bucket for one HH conversation.

    Message direction alone is not enough: HH rejection templates, interview
    confirmations and automatic acknowledgements are incoming messages too,
    but none of them means that the candidate owes a reply.
    """
    hh_state = str(thread.get("hh_state") or "")
    if hh_state == "discard":
        return "rejected"
    direction = str(thread.get("last_message_direction") or "")
    if not direction and hh_state == "invitation":
        return "task"
    committed = hh_state in _CRM_COMMITTED_STATES
    if direction != "incoming":
        return "scheduled" if committed else "waiting"

    preview = " ".join(str(thread.get("last_message_preview") or "").casefold().split())
    if any(marker in preview for marker in _CRM_SCHEDULED_MARKERS):
        return "scheduled"
    if any(marker in preview for marker in _CRM_TASK_MARKERS):
        return "task"
    if any(marker in preview for marker in _CRM_AUTO_ACK_MARKERS):
        return "scheduled" if committed else "waiting"
    if hh_state == "response" and not any(marker in preview for marker in _CRM_REPLY_MARKERS):
        return "waiting"
    return "reply"


class AutomationStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS automation_settings (
                    user_id INTEGER PRIMARY KEY,
                    search_enabled INTEGER NOT NULL DEFAULT 1,
                    auto_apply_enabled INTEGER NOT NULL DEFAULT 0,
                    daily_limit INTEGER NOT NULL DEFAULT 40,
                    min_preview_score INTEGER NOT NULL DEFAULT 60,
                    min_apply_score INTEGER NOT NULL DEFAULT 75,
                    updated_at TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS hh_oauth_accounts (
                    user_id INTEGER PRIMARY KEY,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    account_json TEXT NOT NULL,
                    resumes_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(hh_oauth_accounts)")}
            if "cookies_json" not in columns:
                db.execute("ALTER TABLE hh_oauth_accounts ADD COLUMN cookies_json TEXT NOT NULL DEFAULT '[]'")
            db.execute(
                """CREATE TABLE IF NOT EXISTS hh_resume_mappings (
                    user_id INTEGER NOT NULL,
                    profile_key TEXT NOT NULL,
                    resume_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, profile_key)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS auto_apply_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    vacancy_id TEXT NOT NULL,
                    resume_id TEXT NOT NULL,
                    variant_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, vacancy_id, resume_id)
                )"""
            )
            application_columns = {row[1] for row in db.execute("PRAGMA table_info(auto_apply_log)")}
            for name, definition in {
                "negotiation_id": "TEXT NOT NULL DEFAULT ''",
                "hh_state": "TEXT NOT NULL DEFAULT ''",
                "has_updates": "INTEGER NOT NULL DEFAULT 0",
                "viewed_by_opponent": "INTEGER NOT NULL DEFAULT 0",
                "external_updated_at": "TEXT NOT NULL DEFAULT ''",
                "outcome_status": "TEXT NOT NULL DEFAULT ''",
                "outcome_note": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
                "notified_state": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in application_columns:
                    db.execute(f"ALTER TABLE auto_apply_log ADD COLUMN {name} {definition}")
            db.execute(
                """CREATE TABLE IF NOT EXISTS variant_approvals (
                    variant_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    resume_id TEXT NOT NULL DEFAULT '',
                    approved_at TEXT NOT NULL,
                    consumed_at TEXT NOT NULL DEFAULT ''
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS application_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    variant_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_application_events_application "
                "ON application_events(application_id, created_at)"
            )
            # Track negotiations created both by the bot and manually on hh.ru.
            # Without this table, updates for manual applications have nowhere
            # to store their notification state.
            db.execute(
                """CREATE TABLE IF NOT EXISTS negotiation_watch (
                    user_id INTEGER NOT NULL,
                    negotiation_id TEXT NOT NULL,
                    vacancy_id TEXT NOT NULL DEFAULT '',
                    vacancy_name TEXT NOT NULL DEFAULT '',
                    employer_name TEXT NOT NULL DEFAULT '',
                    hh_state TEXT NOT NULL DEFAULT '',
                    has_updates INTEGER NOT NULL DEFAULT 0,
                    external_updated_at TEXT NOT NULL DEFAULT '',
                    last_message TEXT NOT NULL DEFAULT '',
                    notified_key TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(user_id, negotiation_id)
                )"""
            )
            # One row per (thread, daily touch). The dynamic date in `step` makes
            # automatic delivery idempotent across scheduler restarts.
            db.execute(
                """CREATE TABLE IF NOT EXISTS followup_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    negotiation_id TEXT NOT NULL,
                    step TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, negotiation_id, step)
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_followup_log_sent "
                "ON followup_log(user_id, status, created_at)"
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS resume_bump_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    resume_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_resume_bump_log_day "
                "ON resume_bump_log(user_id, created_at)"
            )
            # Full local copy of every negotiation's messages, both directions.
            # Before this, negotiation_watch.last_message held only the newest
            # text and was overwritten each sync — no history, no way to answer
            # "how many days has this thread been silent" or search past mail.
            db.execute(
                """CREATE TABLE IF NOT EXISTS negotiation_messages (
                    user_id INTEGER NOT NULL,
                    negotiation_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    direction TEXT NOT NULL DEFAULT '',
                    author_name TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    synced_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (user_id, negotiation_id, message_id)
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_negotiation_messages_thread "
                "ON negotiation_messages(user_id, negotiation_id, created_at)"
            )
            # One CRM row per conversation, unifying auto_apply_log (bot-sent
            # applications) and negotiation_watch (threads the owner started by
            # hand) into a single surface with a derived "who owes a reply" flag.
            db.execute(
                """CREATE TABLE IF NOT EXISTS crm_thread_state (
                    user_id INTEGER NOT NULL,
                    negotiation_id TEXT NOT NULL,
                    -- hh.ru's chat id is not the negotiation id. The web UI addresses conversations
                    -- by chat id, so without this column there is no way to
                    -- link the owner into the actual thread.
                    chat_id TEXT NOT NULL DEFAULT '',
                    vacancy_id TEXT NOT NULL DEFAULT '',
                    vacancy_name TEXT NOT NULL DEFAULT '',
                    employer_name TEXT NOT NULL DEFAULT '',
                    hh_state TEXT NOT NULL DEFAULT '',
                    last_incoming_at TEXT NOT NULL DEFAULT '',
                    last_outgoing_at TEXT NOT NULL DEFAULT '',
                    last_message_preview TEXT NOT NULL DEFAULT '',
                    last_message_direction TEXT NOT NULL DEFAULT '',
                    awaiting_side TEXT NOT NULL DEFAULT 'none',
                    silence_since TEXT NOT NULL DEFAULT '',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    archived_at TEXT NOT NULL DEFAULT '',
                    archive_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (user_id, negotiation_id)
                )"""
            )
            # Databases created before chat_id existed keep working: the column
            # is added empty and the next negotiation sync fills it in.
            crm_columns = {row[1] for row in db.execute("PRAGMA table_info(crm_thread_state)")}
            if "chat_id" not in crm_columns:
                db.execute("ALTER TABLE crm_thread_state ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_crm_thread_state_user "
                "ON crm_thread_state(user_id, archived_at, awaiting_side)"
            )
            # Every vacancy the pipeline cuts before (trap screening, ranker
            # hard-reject) or instead of (below preview threshold) a
            # candidate_variants row, plus the reason it was cut. Not an
            # extension of candidate_variants: the first two gates fire
            # before any payload/variant exists at all, so there is nothing
            # to attach a status or reason to there. One small table here
            # makes "was this a correct rejection or a filter bug"
            # answerable without diffing three code paths in
            # mvp/automation.py. Schema change is additive/idempotent like
            # every other table in this file. This store keeps its schema in
            # code and applies additive changes when it opens the database.
            db.execute(
                """CREATE TABLE IF NOT EXISTS filtered_vacancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    vacancy_id TEXT NOT NULL DEFAULT '',
                    vacancy_name TEXT NOT NULL DEFAULT '',
                    employer_name TEXT NOT NULL DEFAULT '',
                    area TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    score INTEGER NOT NULL DEFAULT 0,
                    gate TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_filtered_vacancies_user "
                "ON filtered_vacancies(user_id, created_at DESC)"
            )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path)
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def settings(self, user_id: int) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO automation_settings
                   (user_id, auto_apply_enabled, updated_at) VALUES (?, 0, ?)""",
                (user_id, now),
            )
            row = db.execute(
                """SELECT search_enabled, auto_apply_enabled, daily_limit,
                          min_preview_score, min_apply_score
                   FROM automation_settings WHERE user_id=?""",
                (user_id,),
            ).fetchone()
        return {
            "search_enabled": bool(row[0]), "auto_apply_enabled": bool(row[1]),
            "daily_limit": int(row[2]), "min_preview_score": int(row[3]),
            "min_apply_score": int(row[4]),
        }

    def toggle(self, user_id: int, field: str) -> bool:
        if field not in {"search_enabled", "auto_apply_enabled"}:
            raise ValueError("Unknown automation setting")
        self.settings(user_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                f"UPDATE automation_settings SET {field}=1-{field}, updated_at=? WHERE user_id=?",
                (now, user_id),
            )
        return bool(self.settings(user_id)[field])

    def set_enabled(self, user_id: int, field: str, enabled: bool) -> None:
        if field not in {"search_enabled", "auto_apply_enabled"}:
            raise ValueError("Unknown automation setting")
        self.settings(user_id)
        with self._connect() as db:
            db.execute(
                f"UPDATE automation_settings SET {field}=?, updated_at=? WHERE user_id=?",
                (int(enabled), datetime.now(timezone.utc).isoformat(), user_id),
            )

    def known_hh_ids(self, user_id: int) -> set[str]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT json_extract(payload, '$.vacancy.external_id')
                   FROM candidate_variants
                   WHERE user_id=? AND status!='failed'
                     AND json_extract(payload, '$.vacancy.source')='hh'""",
                (user_id,),
            ).fetchall()
        return {str(row[0]) for row in rows if row[0]}

    def variants_today(self, user_id: int) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self._connect() as db:
            return int(db.execute(
                "SELECT count(*) FROM candidate_variants WHERE user_id=? AND created_at>=?",
                (user_id, start),
            ).fetchone()[0])

    def save_hh_account(
        self, user_id: int, account: dict, resumes: list[dict], cookies: list[dict] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """INSERT INTO hh_oauth_accounts
                   (user_id, access_token, refresh_token, expires_at, account_json,
                    resumes_json, updated_at, cookies_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET access_token=excluded.access_token,
                   refresh_token=excluded.refresh_token, expires_at=excluded.expires_at,
                   account_json=excluded.account_json, resumes_json=excluded.resumes_json,
                   updated_at=excluded.updated_at,
                   cookies_json=CASE WHEN excluded.cookies_json='[]'
                       THEN hh_oauth_accounts.cookies_json ELSE excluded.cookies_json END""",
                (
                    user_id, account["access_token"], account.get("refresh_token", ""),
                    float(account.get("expires_at", 0)),
                    json.dumps(account.get("profile", {}), ensure_ascii=False),
                    json.dumps(resumes, ensure_ascii=False), now,
                    json.dumps(cookies or [], ensure_ascii=False),
                ),
            )

    def hh_account(self, user_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT access_token, refresh_token, expires_at, account_json, resumes_json, cookies_json
                   FROM hh_oauth_accounts WHERE user_id=?""",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "access_token": row[0], "refresh_token": row[1], "expires_at": row[2],
            "profile": json.loads(row[3]), "resumes": json.loads(row[4]),
            "cookies": json.loads(row[5]),
        }

    def save_resume_mapping(
        self, user_id: int, profile_key: str, resume_id: str, title: str,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO hh_resume_mappings VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, profile_key) DO UPDATE SET
                   resume_id=excluded.resume_id, title=excluded.title,
                   updated_at=excluded.updated_at""",
                (user_id, profile_key, resume_id, title, datetime.now(timezone.utc).isoformat()),
            )

    def resume_mapping(self, user_id: int, profile_key: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT resume_id, title FROM hh_resume_mappings WHERE user_id=? AND profile_key=?",
                (user_id, profile_key),
            ).fetchone()
        return {"resume_id": row[0], "title": row[1]} if row else None

    def application_exists(self, user_id: int, vacancy_id: str, resume_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM auto_apply_log WHERE user_id=? AND vacancy_id=? AND resume_id=?",
                (user_id, vacancy_id, resume_id),
            ).fetchone()
        return bool(row)

    def applications_today(self, user_id: int) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self._connect() as db:
            row = db.execute(
                """SELECT count(*) FROM auto_apply_log
                   WHERE user_id=? AND status='applied' AND created_at>=?""",
                (user_id, start),
            ).fetchone()
        return int(row[0])

    def record_application(
        self, *, user_id: int, vacancy_id: str, resume_id: str,
        variant_id: str, status: str, reason: str, negotiation: dict | None = None,
    ) -> None:
        negotiation = negotiation or {}
        state = negotiation.get("state") or {}
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO auto_apply_log
                   (user_id, vacancy_id, resume_id, variant_id, status, reason, created_at,
                    negotiation_id, hh_state, has_updates, viewed_by_opponent,
                    external_updated_at, outcome_status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id, vacancy_id, resume_id, variant_id, status, reason,
                    now, str(negotiation.get("id") or ""), str(state.get("id") or ""),
                    int(bool(negotiation.get("has_updates"))),
                    int(bool(negotiation.get("viewed_by_opponent"))),
                    str(negotiation.get("updated_at") or ""),
                    "applied" if status == "applied" else "", now,
                ),
            )

    def approve_variant(
        self, *, user_id: int, variant_id: str, payload_sha256: str, resume_id: str = "",
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO variant_approvals
                   (variant_id, user_id, payload_sha256, resume_id, approved_at, consumed_at)
                   VALUES (?, ?, ?, ?, ?, '')
                   ON CONFLICT(variant_id) DO UPDATE SET
                   user_id=excluded.user_id, payload_sha256=excluded.payload_sha256,
                   resume_id=excluded.resume_id, approved_at=excluded.approved_at,
                   consumed_at=''""",
                (
                    variant_id, user_id, payload_sha256, resume_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def approval(self, user_id: int, variant_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT payload_sha256, resume_id, approved_at, consumed_at
                   FROM variant_approvals WHERE user_id=? AND variant_id=?""",
                (user_id, variant_id),
            ).fetchone()
        if not row:
            return None
        return {
            "payload_sha256": row[0], "resume_id": row[1],
            "approved_at": row[2], "consumed_at": row[3],
        }

    def consume_approval(self, user_id: int, variant_id: str) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE variant_approvals SET consumed_at=?
                   WHERE user_id=? AND variant_id=? AND consumed_at=''""",
                (datetime.now(timezone.utc).isoformat(), user_id, variant_id),
            )

    @staticmethod
    def _external_outcome(current: str, hh_state: str, has_updates: bool) -> str:
        if hh_state == "discard":
            return "rejected"
        if current in {"interview", "offer"}:
            return current
        if hh_state == "invitation" or has_updates:
            return "replied"
        return current or "applied"

    def tracked_applications(self, user_id: int) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT id, vacancy_id, resume_id, variant_id, negotiation_id,
                          hh_state, has_updates, viewed_by_opponent, external_updated_at,
                          outcome_status, notified_state
                   FROM auto_apply_log WHERE user_id=? AND status='applied'""",
                (user_id,),
            ).fetchall()
        keys = (
            "id", "vacancy_id", "resume_id", "variant_id", "negotiation_id",
            "hh_state", "has_updates", "viewed_by_opponent", "external_updated_at",
            "outcome_status", "notified_state",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def sync_application(self, application_id: int, negotiation: dict) -> dict:
        state = str((negotiation.get("state") or {}).get("id") or "")
        has_updates = bool(negotiation.get("has_updates"))
        external_updated_at = str(negotiation.get("updated_at") or "")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            row = db.execute(
                """SELECT user_id, variant_id, hh_state, has_updates,
                          external_updated_at, outcome_status, notified_state
                   FROM auto_apply_log WHERE id=?""",
                (application_id,),
            ).fetchone()
            if not row:
                raise KeyError(application_id)
            user_id, variant_id, old_state, old_updates, old_external_at, current, notified = row
            outcome = self._external_outcome(current, state, has_updates)
            notification_key = f"{state}:{int(has_updates)}:{external_updated_at}"
            changed = (
                old_state != state or bool(old_updates) != has_updates
                or old_external_at != external_updated_at or current != outcome
            )
            db.execute(
                """UPDATE auto_apply_log SET negotiation_id=?, hh_state=?, has_updates=?,
                          viewed_by_opponent=?, external_updated_at=?, outcome_status=?, updated_at=?
                   WHERE id=?""",
                (
                    str(negotiation.get("id") or ""), state, int(has_updates),
                    int(bool(negotiation.get("viewed_by_opponent"))), external_updated_at,
                    outcome, now, application_id,
                ),
            )
            if changed:
                db.execute(
                    """INSERT INTO application_events
                       (application_id,user_id,variant_id,event_type,from_status,to_status,details,created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        application_id, user_id, variant_id, "hh_sync",
                        current or old_state or "applied", outcome,
                        json.dumps({
                            "hh_state": state, "has_updates": has_updates,
                            "external_updated_at": external_updated_at,
                        }, ensure_ascii=False), now,
                    ),
                )
        return {
            "changed": changed, "outcome_status": outcome, "hh_state": state,
            "has_updates": has_updates, "notification_key": notification_key,
            "notify": notification_key != notified and (state in {"invitation", "discard"} or has_updates),
        }

    def mark_application_notified(self, application_id: int, notification_key: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE auto_apply_log SET notified_state=?, updated_at=? WHERE id=?",
                (notification_key, datetime.now(timezone.utc).isoformat(), application_id),
            )

    # ------------------------------------------------------------------
    # negotiation_watch: recruiter mail on threads the bot did not create
    # ------------------------------------------------------------------

    def watch_negotiation(self, user_id: int, negotiation: dict, last_message: str = "") -> dict:
        """Record one hh.ru negotiation and say whether it is worth a push.

        ``notify`` is True when the thread carries recruiter activity (an
        invitation, a rejection, or an unread flag) under a state key we have
        not already notified about. The key is only advanced by
        :meth:`mark_negotiation_notified`, so a Telegram send that fails is
        retried on the next cycle instead of being lost.
        """
        negotiation_id = str(negotiation.get("id") or "")
        if not negotiation_id:
            return {"notify": False, "notification_key": ""}
        state = str((negotiation.get("state") or {}).get("id") or "")
        has_updates = bool(negotiation.get("has_updates"))
        external_updated_at = str(negotiation.get("updated_at") or "")
        vacancy = negotiation.get("vacancy") or {}
        notification_key = f"{state}:{external_updated_at}:{int(has_updates)}"
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            row = db.execute(
                "SELECT notified_key FROM negotiation_watch WHERE user_id=? AND negotiation_id=?",
                (user_id, negotiation_id),
            ).fetchone()
            notified = row[0] if row else ""
            db.execute(
                """INSERT INTO negotiation_watch
                       (user_id,negotiation_id,vacancy_id,vacancy_name,employer_name,hh_state,
                        has_updates,external_updated_at,last_message,notified_key,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id,negotiation_id) DO UPDATE SET
                       vacancy_id=excluded.vacancy_id, vacancy_name=excluded.vacancy_name,
                       employer_name=excluded.employer_name, hh_state=excluded.hh_state,
                       has_updates=excluded.has_updates,
                       external_updated_at=excluded.external_updated_at,
                       last_message=excluded.last_message, updated_at=excluded.updated_at""",
                (
                    user_id, negotiation_id, str(vacancy.get("id") or ""),
                    str(vacancy.get("name") or ""),
                    str((vacancy.get("employer") or {}).get("name") or ""),
                    state, int(has_updates), external_updated_at, last_message, notified, now,
                ),
            )
        return {
            "notification_key": notification_key,
            "notify": notification_key != notified and (state in {"invitation", "discard"} or has_updates),
        }

    def has_negotiation_baseline(self, user_id: int) -> bool:
        """Has this account's hh.ru thread list ever been walked?

        The first walk sees the entire archive at once. Without this check
        every historical rejection counts as fresh activity — on 2026-08-12
        that could mean a large burst of stale push messages.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM negotiation_watch WHERE user_id=? LIMIT 1", (user_id,),
            ).fetchone()
        return row is not None

    def mark_negotiation_notified(self, user_id: int, negotiation_id: str, notification_key: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE negotiation_watch SET notified_key=?, updated_at=? WHERE user_id=? AND negotiation_id=?",
                (notification_key, datetime.now(timezone.utc).isoformat(), user_id, negotiation_id),
            )

    def watched_negotiations(self, user_id: int, limit: int = 60) -> list[dict]:
        """Recruiter-active threads, newest first, with the bot's variant when it has one."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT w.negotiation_id, w.vacancy_id, w.vacancy_name, w.employer_name,
                          w.hh_state, w.has_updates, w.external_updated_at, w.last_message,
                          COALESCE(l.variant_id,''), COALESCE(l.outcome_status,'')
                   FROM negotiation_watch AS w
                   LEFT JOIN auto_apply_log AS l
                          ON l.user_id=w.user_id AND l.negotiation_id=w.negotiation_id
                   WHERE w.user_id=?
                   ORDER BY w.external_updated_at DESC, w.updated_at DESC
                   LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        keys = (
            "negotiation_id", "vacancy_id", "vacancy_name", "employer_name", "hh_state",
            "has_updates", "external_updated_at", "last_message", "variant_id", "outcome_status",
        )
        entries = []
        for row in rows:
            entry = dict(zip(keys, row, strict=True))
            entry["has_updates"] = bool(entry["has_updates"])
            entries.append(entry)
        return entries

    def set_outcome(self, user_id: int, variant_id: str, outcome: str, note: str = "") -> bool:
        if outcome not in {"applied", "replied", "rejected", "interview", "offer", "no_response"}:
            raise ValueError("Unknown application outcome")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            row = db.execute(
                """SELECT id, outcome_status FROM auto_apply_log
                   WHERE user_id=? AND variant_id=? ORDER BY id DESC LIMIT 1""",
                (user_id, variant_id),
            ).fetchone()
            if not row:
                return False
            application_id, previous = row
            db.execute(
                """UPDATE auto_apply_log SET outcome_status=?, outcome_note=?, updated_at=?
                   WHERE id=?""",
                (outcome, note, now, application_id),
            )
            db.execute(
                """INSERT INTO application_events
                   (application_id,user_id,variant_id,event_type,from_status,to_status,details,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (application_id, user_id, variant_id, "manual_outcome", previous, outcome, note, now),
            )
        return True

    def application_history(self, user_id: int, limit: int = 20) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT l.id,l.vacancy_id,l.resume_id,l.variant_id,l.status,l.reason,
                          l.created_at,l.negotiation_id,l.hh_state,l.has_updates,
                          l.viewed_by_opponent,l.external_updated_at,l.outcome_status,l.outcome_note,
                          v.payload
                   FROM auto_apply_log AS l
                   LEFT JOIN candidate_variants AS v ON v.id=l.variant_id
                   WHERE l.user_id=? ORDER BY l.created_at DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row[14]) if row[14] else {}
            result.append({
                "id": row[0], "vacancy_id": row[1], "resume_id": row[2],
                "variant_id": row[3], "status": row[4], "reason": row[5],
                "created_at": row[6], "negotiation_id": row[7], "hh_state": row[8],
                "has_updates": bool(row[9]), "viewed_by_opponent": bool(row[10]),
                "external_updated_at": row[11], "outcome_status": row[12] or row[4],
                "outcome_note": row[13], "vacancy": payload.get("vacancy", {}),
                "profile": (payload.get("master_profile") or {}).get("key", ""),
            })
        return result

    # ------------------------------------------------------------------
    # followup_log: daily status questions on live response threads
    # ------------------------------------------------------------------

    def followup_steps(self, user_id: int, negotiation_id: str) -> dict[str, str]:
        """``{step: status}`` for one thread — the follow-up scheduler's memory."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT step, status FROM followup_log WHERE user_id=? AND negotiation_id=?",
                (user_id, negotiation_id),
            ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def record_followup(
        self, *, user_id: int, negotiation_id: str, step: str, status: str,
        message: str = "", reason: str = "",
    ) -> None:
        """Write (or overwrite) one follow-up attempt.

        ``status`` is one of ``offered`` / ``sent`` / ``skipped`` / ``stopped`` /
        ``failed``. Re-offering an already-``sent`` rung must never downgrade it,
        so a send wins over anything that arrives afterwards.
        """
        with self._connect() as db:
            db.execute(
                """INSERT INTO followup_log
                       (user_id, negotiation_id, step, status, message, reason, created_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(user_id, negotiation_id, step) DO UPDATE SET
                       status=excluded.status, message=excluded.message,
                       reason=excluded.reason, created_at=excluded.created_at
                   WHERE followup_log.status!='sent'""",
                (
                    user_id, negotiation_id, step, status, message, reason,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def stop_followups(self, user_id: int, negotiation_id: str, steps: list[str], reason: str) -> None:
        """Close every rung this thread has not used yet, so it is never offered again."""
        for step in steps:
            self.record_followup(
                user_id=user_id, negotiation_id=negotiation_id, step=step,
                status="stopped", reason=reason,
            )

    def followups_sent_today(self, user_id: int) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self._connect() as db:
            row = db.execute(
                "SELECT count(*) FROM followup_log WHERE user_id=? AND status='sent' AND created_at>=?",
                (user_id, start),
            ).fetchone()
        return int(row[0])

    def last_followup_sent_at(self, user_id: int, negotiation_id: str) -> str:
        with self._connect() as db:
            row = db.execute(
                """SELECT max(created_at) FROM followup_log
                   WHERE user_id=? AND negotiation_id=? AND status='sent'""",
                (user_id, negotiation_id),
            ).fetchone()
        return str(row[0] or "")

    def pending_followup(self, user_id: int, negotiation_id: str, step: str) -> dict | None:
        """The text already shown to the owner for this rung, if it is still awaiting his press."""
        with self._connect() as db:
            row = db.execute(
                """SELECT message, status FROM followup_log
                   WHERE user_id=? AND negotiation_id=? AND step=?""",
                (user_id, negotiation_id, step),
            ).fetchone()
        if not row or row[1] != "offered":
            return None
        return {"message": str(row[0]), "status": str(row[1])}

    # ------------------------------------------------------------------
    # resume_bump_log: hh.ru's free "поднять резюме в поиске", every 4 hours
    # ------------------------------------------------------------------

    def record_resume_bump(self, *, user_id: int, resume_id: str, status: str, reason: str = "") -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO resume_bump_log (user_id, resume_id, status, reason, created_at)
                   VALUES (?,?,?,?,?)""",
                (user_id, resume_id, status, reason, datetime.now(timezone.utc).isoformat()),
            )

    def resume_bumps_today(self, user_id: int) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self._connect() as db:
            row = db.execute(
                "SELECT count(*) FROM resume_bump_log WHERE user_id=? AND status='ok' AND created_at>=?",
                (user_id, start),
            ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------
    # CRM: full local message history + one derived state row per thread
    # ------------------------------------------------------------------

    _CRM_THREAD_COLUMNS = (
        "user_id", "negotiation_id", "chat_id", "vacancy_id", "vacancy_name", "employer_name", "hh_state",
        "last_incoming_at", "last_outgoing_at", "last_message_preview", "last_message_direction",
        "awaiting_side", "silence_since", "message_count", "archived_at", "archive_reason",
        "created_at", "updated_at",
    )

    @staticmethod
    def _parse_hh_message(message: dict) -> tuple[str, str, str, str, str] | None:
        """Turn one raw hh.ru message dict into (message_id, direction, author_name, text, created_at).

        hh.ru's own shape uses ``author.participant_type`` (``employer`` /
        ``applicant``), but this is defensive about the exact keys present —
        some call sites only have ``author_type`` or a bare ``role``. A
        message with no usable id gets a deterministic synthetic one derived
        from timestamp+direction+text, so re-syncing the same page twice
        never creates a duplicate row.
        """
        if not isinstance(message, dict):
            return None
        text = str(message.get("text") or "").strip()
        created_at = str(message.get("created_at") or message.get("created") or "")
        author = message.get("author") or {}
        if not isinstance(author, dict):
            author = {}
        participant = str(
            author.get("participant_type") or author.get("type") or author.get("role")
            or message.get("author_type") or ""
        ).lower()
        if not participant:
            # No signal at all: treat as incoming, matching the legacy
            # negotiation_watch behaviour of only ever surfacing employer text.
            participant = "employer"
        direction = "incoming" if participant == "employer" else "outgoing"
        author_name = str(
            author.get("name") or author.get("title")
            or ("Работодатель" if direction == "incoming" else "Вы")
        )
        message_id = str(message.get("id") or "")
        if not message_id:
            if not text and not created_at:
                return None
            digest = hashlib.sha256(f"{created_at}:{direction}:{text}".encode("utf-8")).hexdigest()[:24]
            message_id = f"synthetic:{digest}"
        return message_id, direction, author_name, text, created_at

    def store_negotiation_messages(self, user_id: int, negotiation_id: str, messages: list[dict]) -> int:
        """Upsert a batch of hh.ru message dicts. Returns how many rows were NEW.

        Safe to call repeatedly with overlapping batches: existing rows are
        refreshed in place (text/author can change between fetches) and only
        ids not already present count toward the returned total.
        """
        if not negotiation_id or not messages:
            return 0
        parsed = [p for p in (self._parse_hh_message(m) for m in messages) if p is not None]
        if not parsed:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        new_count = 0
        with self._connect() as db:
            existing = {
                row[0] for row in db.execute(
                    "SELECT message_id FROM negotiation_messages WHERE user_id=? AND negotiation_id=?",
                    (user_id, negotiation_id),
                )
            }
            for message_id, direction, author_name, text, created_at in parsed:
                if message_id not in existing:
                    new_count += 1
                    existing.add(message_id)
                db.execute(
                    """INSERT INTO negotiation_messages
                           (user_id, negotiation_id, message_id, direction, author_name,
                            text, created_at, synced_at)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(user_id, negotiation_id, message_id) DO UPDATE SET
                           direction=excluded.direction, author_name=excluded.author_name,
                           text=excluded.text, created_at=excluded.created_at,
                           synced_at=excluded.synced_at""",
                    (user_id, negotiation_id, message_id, direction, author_name, text, created_at, now),
                )
        return new_count

    def record_outgoing_message(
        self, user_id: int, negotiation_id: str, text: str, *,
        message_id: str | None = None, created_at: str | None = None, author_name: str | None = None,
    ) -> None:
        """Record a message the owner/bot just sent, so the CRM reflects it immediately.

        Called at send time rather than waiting for the next hh.ru sync to pick
        it back up — otherwise ``awaiting_side`` would still read ``employer``
        for a thread the owner just answered.
        """
        if not negotiation_id:
            return
        now = datetime.now(timezone.utc).isoformat()
        created_at = created_at or now
        author_name = author_name or "Вы"
        mid = message_id or (
            f"synthetic:{hashlib.sha256(f'{created_at}:outgoing:{text}'.encode('utf-8')).hexdigest()[:24]}"
        )
        with self._connect() as db:
            db.execute(
                """INSERT INTO negotiation_messages
                       (user_id, negotiation_id, message_id, direction, author_name,
                        text, created_at, synced_at)
                   VALUES (?,?,?,'outgoing',?,?,?,?)
                   ON CONFLICT(user_id, negotiation_id, message_id) DO UPDATE SET
                       direction=excluded.direction, author_name=excluded.author_name,
                       text=excluded.text, created_at=excluded.created_at,
                       synced_at=excluded.synced_at""",
                (user_id, negotiation_id, mid, author_name, text, created_at, now),
            )
        self.recompute_thread_state(user_id, negotiation_id)

    def recompute_thread_state(
        self, user_id: int, negotiation_id: str, *,
        vacancy_id: str | None = None, vacancy_name: str | None = None,
        employer_name: str | None = None, hh_state: str | None = None,
        chat_id: str | None = None,
    ) -> dict:
        """Rebuild crm_thread_state from the stored messages and upsert the row.

        Metadata args are only applied when non-None, so a sync that does not
        know the vacancy name never clobbers a name learned earlier.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            rows = db.execute(
                """SELECT direction, text, created_at FROM negotiation_messages
                   WHERE user_id=? AND negotiation_id=? ORDER BY created_at ASC, message_id ASC""",
                (user_id, negotiation_id),
            ).fetchall()
            message_count = len(rows)
            last_incoming_at = ""
            last_outgoing_at = ""
            for direction, _text, created_at in rows:
                if direction == "incoming":
                    last_incoming_at = created_at
                elif direction == "outgoing":
                    last_outgoing_at = created_at
            if rows:
                last_direction, last_text, last_created_at = rows[-1]
            else:
                last_direction, last_text, last_created_at = "", "", ""
            if last_direction == "incoming":
                awaiting_side = "me"
            elif last_direction == "outgoing":
                awaiting_side = "employer"
            else:
                awaiting_side = "none"
            silence_since = last_created_at
            preview = last_text[:200]

            existing = db.execute(
                """SELECT vacancy_id, vacancy_name, employer_name, hh_state, created_at, chat_id
                   FROM crm_thread_state WHERE user_id=? AND negotiation_id=?""",
                (user_id, negotiation_id),
            ).fetchone()
            if existing:
                (ex_vacancy_id, ex_vacancy_name, ex_employer_name, ex_hh_state,
                 created_at_row, ex_chat_id) = existing
            else:
                ex_vacancy_id = ex_vacancy_name = ex_employer_name = ex_hh_state = ex_chat_id = ""
                created_at_row = now
            final_vacancy_id = vacancy_id if vacancy_id is not None else ex_vacancy_id
            final_vacancy_name = vacancy_name if vacancy_name is not None else ex_vacancy_name
            final_employer_name = employer_name if employer_name is not None else ex_employer_name
            final_hh_state = hh_state if hh_state is not None else ex_hh_state
            final_chat_id = chat_id if chat_id is not None else ex_chat_id

            db.execute(
                """INSERT INTO crm_thread_state
                       (user_id, negotiation_id, chat_id, vacancy_id, vacancy_name, employer_name, hh_state,
                        last_incoming_at, last_outgoing_at, last_message_preview, last_message_direction,
                        awaiting_side, silence_since, message_count, archived_at, archive_reason,
                        created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'','',?,?)
                   ON CONFLICT(user_id, negotiation_id) DO UPDATE SET
                       chat_id=excluded.chat_id,
                       vacancy_id=excluded.vacancy_id, vacancy_name=excluded.vacancy_name,
                       employer_name=excluded.employer_name, hh_state=excluded.hh_state,
                       last_incoming_at=excluded.last_incoming_at,
                       last_outgoing_at=excluded.last_outgoing_at,
                       last_message_preview=excluded.last_message_preview,
                       last_message_direction=excluded.last_message_direction,
                       awaiting_side=excluded.awaiting_side, silence_since=excluded.silence_since,
                       message_count=excluded.message_count, updated_at=excluded.updated_at""",
                (
                    user_id, negotiation_id, final_chat_id, final_vacancy_id, final_vacancy_name,
                    final_employer_name, final_hh_state, last_incoming_at, last_outgoing_at, preview,
                    last_direction, awaiting_side, silence_since, message_count, created_at_row, now,
                ),
            )
        return self.crm_thread(user_id, negotiation_id) or {}

    @staticmethod
    def _days_silent(silence_since: str) -> int:
        if not silence_since:
            return 0
        try:
            dt = datetime.fromisoformat(silence_since)
        except ValueError:
            return 0
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return max(0, delta.days)

    def crm_threads(
        self, user_id: int, *, include_archived: bool = False, archived_only: bool = False,
    ) -> list[dict]:
        columns = ",".join(f"c.{column}" for column in self._CRM_THREAD_COLUMNS)
        query = f"""SELECT {columns}, COALESCE((
            SELECT m.text FROM negotiation_messages AS m
            WHERE m.user_id=c.user_id AND m.negotiation_id=c.negotiation_id
            ORDER BY m.created_at DESC, m.message_id DESC LIMIT 1
        ), c.last_message_preview) FROM crm_thread_state AS c WHERE c.user_id=?"""
        if archived_only:
            query += " AND c.archived_at!=''"
        elif not include_archived:
            query += " AND c.archived_at=''"
        query += " ORDER BY c.silence_since DESC, c.updated_at DESC"
        with self._connect() as db:
            rows = db.execute(query, (user_id,)).fetchall()
        entries = []
        for row in rows:
            entry = dict(zip(self._CRM_THREAD_COLUMNS, row[:-1], strict=True))
            entry["last_message_preview"] = row[-1]
            entry["message_count"] = int(entry["message_count"])
            entry["days_silent"] = self._days_silent(entry["silence_since"])
            entries.append(entry)
        return entries

    def set_thread_chat_id(self, user_id: int, negotiation_id: str, chat_id: str) -> None:
        """Remember hh.ru's chat id for a conversation.

        An UPDATE rather than an upsert on purpose: the row is owned by
        `recompute_thread_state`, and creating a half-filled one here would
        race it. If the row does not exist yet this no-ops and the next sync
        cycle, a few minutes later, fills it in.
        """
        if not chat_id:
            return
        with self._connect() as db:
            db.execute(
                "UPDATE crm_thread_state SET chat_id=? WHERE user_id=? AND negotiation_id=? AND chat_id!=?",
                (chat_id, user_id, negotiation_id, chat_id),
            )

    def crm_thread(self, user_id: int, negotiation_id: str) -> dict | None:
        columns = ",".join(f"c.{column}" for column in self._CRM_THREAD_COLUMNS)
        with self._connect() as db:
            row = db.execute(
                f"""SELECT {columns}, COALESCE((
                        SELECT m.text FROM negotiation_messages AS m
                        WHERE m.user_id=c.user_id AND m.negotiation_id=c.negotiation_id
                        ORDER BY m.created_at DESC, m.message_id DESC LIMIT 1
                    ), c.last_message_preview)
                    FROM crm_thread_state AS c
                    WHERE c.user_id=? AND c.negotiation_id=?""",
                (user_id, negotiation_id),
            ).fetchone()
        if not row:
            return None
        entry = dict(zip(self._CRM_THREAD_COLUMNS, row[:-1], strict=True))
        entry["last_message_preview"] = row[-1]
        entry["message_count"] = int(entry["message_count"])
        entry["days_silent"] = self._days_silent(entry["silence_since"])
        return entry

    def crm_thread_messages(
        self, user_id: int, negotiation_id: str, *, limit: int | None = None,
    ) -> list[dict]:
        """Full local message history for one thread, oldest first."""
        keys = ("message_id", "direction", "author_name", "text", "created_at", "synced_at")
        with self._connect() as db:
            if limit is not None:
                # Grab the newest `limit` rows, then flip them back to chronological
                # order — plain ASC+LIMIT would instead return the oldest ones.
                rows = db.execute(
                    """SELECT * FROM (
                           SELECT message_id, direction, author_name, text, created_at, synced_at
                           FROM negotiation_messages WHERE user_id=? AND negotiation_id=?
                           ORDER BY created_at DESC, message_id DESC LIMIT ?
                       ) ORDER BY created_at ASC, message_id ASC""",
                    (user_id, negotiation_id, int(limit)),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT message_id, direction, author_name, text, created_at, synced_at
                       FROM negotiation_messages WHERE user_id=? AND negotiation_id=?
                       ORDER BY created_at ASC, message_id ASC""",
                    (user_id, negotiation_id),
                ).fetchall()
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def archive_thread(self, user_id: int, negotiation_id: str, reason: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """INSERT INTO crm_thread_state (user_id, negotiation_id, archived_at, archive_reason,
                                                  created_at, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(user_id, negotiation_id) DO UPDATE SET
                       archived_at=excluded.archived_at, archive_reason=excluded.archive_reason,
                       updated_at=excluded.updated_at""",
                (user_id, negotiation_id, now, reason, now, now),
            )

    def unarchive_thread(self, user_id: int, negotiation_id: str) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE crm_thread_state SET archived_at='', archive_reason='', updated_at=?
                   WHERE user_id=? AND negotiation_id=?""",
                (datetime.now(timezone.utc).isoformat(), user_id, negotiation_id),
            )

    def crm_counters(self, user_id: int, *, silence_days: int) -> dict:
        """Counts by next step; rejections never inflate the work queue."""
        threads = self.crm_threads(user_id, include_archived=True)
        counters = {
            "reply": 0, "task": 0, "scheduled": 0, "waiting": 0,
            "rejected": 0, "silent": 0, "active": 0, "archived": 0,
        }
        for thread in threads:
            if thread.get("archived_at"):
                counters["archived"] += 1
                continue
            bucket = crm_bucket(thread)
            counters[bucket] += 1
            if bucket != "rejected":
                counters["active"] += 1
            if bucket == "waiting" and int(thread.get("days_silent") or 0) >= silence_days:
                counters["silent"] += 1
        return counters

    def threads_needing_message_sync(self, user_id: int, *, limit: int | None = None) -> list[dict]:
        """Threads whose local message history is stale or missing.

        Union of every negotiation this account knows about — auto_apply_log
        (bot-sent applications) and negotiation_watch (hand-started threads) —
        filtered to non-archived threads that either have no crm_thread_state
        row yet (never synced) or whose stored newest message predates hh.ru's
        own ``external_updated_at``. Each dict carries whatever vacancy/employer/
        hh_state metadata is known, ready to pass straight into
        :meth:`recompute_thread_state`.
        """
        with self._connect() as db:
            rows = db.execute(
                """SELECT l.negotiation_id, l.vacancy_id, '' AS vacancy_name, '' AS employer_name,
                          l.hh_state, l.external_updated_at, v.payload
                   FROM auto_apply_log AS l
                   LEFT JOIN candidate_variants AS v ON v.id = l.variant_id
                   WHERE l.user_id=? AND l.negotiation_id!=''
                   UNION ALL
                   SELECT w.negotiation_id, w.vacancy_id, w.vacancy_name, w.employer_name,
                          w.hh_state, w.external_updated_at, NULL AS payload
                   FROM negotiation_watch AS w
                   WHERE w.user_id=? AND w.negotiation_id!=''""",
                (user_id, user_id),
            ).fetchall()
            state_rows = db.execute(
                """SELECT negotiation_id, message_count, silence_since, archived_at
                   FROM crm_thread_state WHERE user_id=?""",
                (user_id,),
            ).fetchall()
        states = {
            row[0]: {"message_count": row[1], "silence_since": row[2], "archived_at": row[3]}
            for row in state_rows
        }
        merged: dict[str, dict] = {}
        for negotiation_id, vacancy_id, vacancy_name, employer_name, hh_state, external_updated_at, payload_raw in rows:
            if not negotiation_id:
                continue
            entry = merged.setdefault(negotiation_id, {
                "negotiation_id": negotiation_id, "vacancy_id": "", "vacancy_name": "",
                "employer_name": "", "hh_state": "", "external_updated_at": "",
            })
            if vacancy_id:
                entry["vacancy_id"] = entry["vacancy_id"] or vacancy_id
            if hh_state:
                entry["hh_state"] = hh_state
            if external_updated_at and external_updated_at > entry["external_updated_at"]:
                entry["external_updated_at"] = external_updated_at
            name, employer = vacancy_name, employer_name
            if (not name or not employer) and payload_raw:
                try:
                    payload = json.loads(payload_raw)
                except (TypeError, ValueError):
                    payload = {}
                vacancy_payload = payload.get("vacancy") or {}
                name = name or str(vacancy_payload.get("name") or "")
                employer = employer or str(vacancy_payload.get("employer_name") or "")
            entry["vacancy_name"] = entry["vacancy_name"] or name
            entry["employer_name"] = entry["employer_name"] or employer

        needing = []
        for negotiation_id, entry in merged.items():
            state = states.get(negotiation_id)
            if state and state["archived_at"]:
                continue
            if not state or int(state["message_count"] or 0) == 0:
                needing.append(entry)
                continue
            if entry["external_updated_at"] and entry["external_updated_at"] > (state["silence_since"] or ""):
                needing.append(entry)
        needing.sort(key=lambda item: item["external_updated_at"], reverse=True)
        if limit is not None:
            needing = needing[:max(0, int(limit))]
        return needing

    # ------------------------------------------------------------------
    # filtered_vacancies: the «Отфильтрованные» bin — owner decision
    # 2026-08-14: keep cutting hard (never leak into the main deck), but
    # stop losing the reason. Populated from the three gates in
    # mvp/automation.py::_run_automation_cycle (trap screening, ranker
    # hard-reject, below-preview-threshold).
    # ------------------------------------------------------------------

    _FILTERED_COLUMNS = (
        "id", "vacancy_id", "vacancy_name", "employer_name", "area",
        "source_url", "score", "gate", "reason", "created_at",
    )

    def record_filtered(
        self, *, user_id: int, vacancy_id: str, vacancy_name: str, employer_name: str,
        area: str, source_url: str, score: int, gate: str, reason: str,
    ) -> None:
        """Log one vacancy the pipeline cut, with why.

        Retention: capped at `_FILTERED_MAX_ROWS_PER_USER` rows per user,
        oldest pruned on every insert. This table is write-light — at most a
        few hundred rows a day — so pruning inline is simpler and harder to
        forget than a separate scheduled cleanup job.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """INSERT INTO filtered_vacancies
                       (user_id, vacancy_id, vacancy_name, employer_name, area,
                        source_url, score, gate, reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id, vacancy_id, vacancy_name, employer_name, area,
                    source_url, int(score), gate, reason, now,
                ),
            )
            db.execute(
                """DELETE FROM filtered_vacancies WHERE user_id=? AND id NOT IN (
                       SELECT id FROM filtered_vacancies WHERE user_id=?
                       ORDER BY created_at DESC, id DESC LIMIT ?
                   )""",
                (user_id, user_id, _FILTERED_MAX_ROWS_PER_USER),
            )

    def filtered_vacancies(self, user_id: int, *, limit: int = 10, offset: int = 0) -> list[dict]:
        """Most recent rejections first, for the paginated bin screen."""
        columns = ",".join(self._FILTERED_COLUMNS)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT {columns} FROM filtered_vacancies WHERE user_id=?
                    ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""",
                (user_id, max(0, int(limit)), max(0, int(offset))),
            ).fetchall()
        return [dict(zip(self._FILTERED_COLUMNS, row, strict=True)) for row in rows]

    def filtered_count(self, user_id: int) -> int:
        with self._connect() as db:
            return int(db.execute(
                "SELECT count(*) FROM filtered_vacancies WHERE user_id=?", (user_id,),
            ).fetchone()[0])

    def filtered_counters(self, user_id: int, *, hours: int = 24, top: int = 3) -> dict:
        """Volume + top reasons in the last `hours` — the cheap "is the filter
        wrong" signal the owner actually wants, not just a raw total."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self._connect() as db:
            total = int(db.execute(
                "SELECT count(*) FROM filtered_vacancies WHERE user_id=? AND created_at>=?",
                (user_id, since),
            ).fetchone()[0])
            by_gate = db.execute(
                """SELECT gate, count(*) FROM filtered_vacancies
                   WHERE user_id=? AND created_at>=? GROUP BY gate ORDER BY count(*) DESC""",
                (user_id, since),
            ).fetchall()
            top_reasons = db.execute(
                """SELECT reason, count(*) AS c FROM filtered_vacancies
                   WHERE user_id=? AND created_at>=? GROUP BY reason ORDER BY c DESC LIMIT ?""",
                (user_id, since, max(0, int(top))),
            ).fetchall()
        return {
            "total": total,
            "by_gate": {str(gate): int(count) for gate, count in by_gate},
            "top_reasons": [(str(reason), int(count)) for reason, count in top_reasons],
        }
