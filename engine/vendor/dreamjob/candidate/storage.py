from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class VariantStore:
    """The journal. SQLite is the source of truth and the only reader.

    A Supabase mirror used to fan every write out to a hosted table that nothing
    in either repo ever read back; it is gone. Restore it only when a second
    reader actually exists.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS candidate_variants (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    vacancy_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_candidate_variants_user ON candidate_variants(user_id, status, created_at)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path)
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def save(self, payload: dict) -> str:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                "INSERT INTO candidate_variants VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    payload["id"], int(payload["user_id"]), str(payload["vacancy"]["id"]),
                    payload.get("status", "draft"), json.dumps(payload, ensure_ascii=False), now, now,
                ),
            )
        return payload["id"]

    def get(self, variant_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT payload, status FROM candidate_variants WHERE id = ?", (variant_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        payload["status"] = row[1]
        return payload

    def get_by_fingerprints(self, user_id: int, *fingerprints: str) -> dict | None:
        values = [value for value in fingerprints if value]
        if not values:
            return None
        placeholders = ",".join("?" for _ in values)
        query = f"""SELECT payload, status FROM candidate_variants
            WHERE user_id = ? AND (
                json_extract(payload, '$.vacancy.fingerprint') IN ({placeholders}) OR
                json_extract(payload, '$.vacancy.content_fingerprint') IN ({placeholders})
            ) ORDER BY created_at DESC LIMIT 1"""
        with self._connect() as db:
            row = db.execute(query, (user_id, *values, *values)).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        payload["status"] = row[1]
        return payload

    def list_recent(self, user_id: int, limit: int = 10) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload, status FROM candidate_variants WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [self._row_to_payload(raw, status) for raw, status in rows]

    def list_by_status(
        self, user_id: int, statuses: tuple[str, ...], limit: int = 200,
    ) -> list[dict]:
        """Newest-first variants in the given statuses.

        Filtering after a LIMIT can drop rows the caller asked for, so status
        filtering happens in SQL before the result limit is applied.
        """
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT payload, status FROM candidate_variants
                    WHERE user_id=? AND status IN ({placeholders})
                    ORDER BY created_at DESC LIMIT ?""",
                (user_id, *statuses, limit),
            ).fetchall()
        return [self._row_to_payload(raw, status) for raw, status in rows]

    def count_by_status(self, user_id: int) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) FROM candidate_variants WHERE user_id=? GROUP BY status",
                (user_id,),
            ).fetchall()
        return {str(status): int(count) for status, count in rows}

    @staticmethod
    def _row_to_payload(raw: str, status: str) -> dict:
        payload = json.loads(raw)
        payload["status"] = status
        return payload

    def health(self) -> bool:
        with self._connect() as db:
            db.execute("SELECT 1").fetchone()
        return True

    def set_status(self, variant_id: str, status: str, expected: tuple[str, ...] | None = None) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            if expected:
                placeholders = ",".join("?" for _ in expected)
                cursor = db.execute(
                    f"UPDATE candidate_variants SET status=?, updated_at=? WHERE id=? AND status IN ({placeholders})",
                    (status, now, variant_id, *expected),
                )
            else:
                cursor = db.execute(
                    "UPDATE candidate_variants SET status=?, updated_at=? WHERE id=?",
                    (status, now, variant_id),
                )
        return cursor.rowcount == 1

    def update_payload(
        self, payload: dict, *, status: str = "draft",
        expected: tuple[str, ...] = ("draft", "approved", "manual_required"),
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" for _ in expected)
        with self._connect() as db:
            cursor = db.execute(
                f"""UPDATE candidate_variants SET payload=?, status=?, updated_at=?
                    WHERE id=? AND user_id=? AND status IN ({placeholders})""",
                (
                    json.dumps(payload, ensure_ascii=False), status, now,
                    payload["id"], int(payload["user_id"]), *expected,
                ),
            )
        return cursor.rowcount == 1
