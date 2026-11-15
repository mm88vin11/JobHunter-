"""Selection rules for hh.ru's free "поднять резюме в поиске".

The bump loop is unattended, so the two invariants worth pinning are that it
only ever touches resumes the owner mapped to a master profile (an unrelated
resume must stay out), and that it never asks hh.ru to republish before the
four-hour clock hh.ru itself reports has run down.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from candidate.automation import AutomationStore

from mvp.bump import LOCAL_TZ, due_resume, in_bump_window

NOW = datetime(2026, 8, 12, 11, 0, tzinfo=timezone.utc)

MAPPED = "resume-demo-a"
ALSO_MAPPED = "resume-demo-b"
UNMAPPED = "resume-demo-unmapped"


def _resume(resume_id: str, next_publish_at: str, *, can: bool = True, title: str = "") -> dict:
    return {
        "id": resume_id, "title": title or resume_id[:6],
        "next_publish_at": next_publish_at, "can_publish_or_update": can,
    }


class DueResumeTest(unittest.TestCase):
    def test_picks_the_resume_stale_longest(self):
        resumes = [
            _resume(MAPPED, "2026-08-12T13:00:00+0300"),
            _resume(ALSO_MAPPED, "2026-08-12T09:00:00+0300"),
        ]
        chosen = due_resume(resumes, {MAPPED, ALSO_MAPPED}, NOW)
        self.assertEqual(chosen["id"], ALSO_MAPPED)

    def test_returns_one_resume_at_a_time(self):
        """Staggering is the whole point: four bumps in one second look like a script."""
        resumes = [
            _resume(MAPPED, "2026-08-12T09:00:00+0300"),
            _resume(ALSO_MAPPED, "2026-08-12T09:30:00+0300"),
        ]
        chosen = due_resume(resumes, {MAPPED, ALSO_MAPPED}, NOW)
        self.assertIsInstance(chosen, dict)

    def test_unmapped_resume_is_never_bumped(self):
        resumes = [_resume(UNMAPPED, "2026-08-12T09:00:00+0300")]
        self.assertIsNone(due_resume(resumes, {MAPPED, ALSO_MAPPED}, NOW))

    def test_resume_still_inside_the_four_hour_clock_is_skipped(self):
        resumes = [_resume(MAPPED, "2026-08-12T17:00:00+0300")]
        self.assertIsNone(due_resume(resumes, {MAPPED}, NOW))

    def test_resume_hh_marks_unpublishable_is_skipped(self):
        resumes = [_resume(MAPPED, "2026-08-12T09:00:00+0300", can=False)]
        self.assertIsNone(due_resume(resumes, {MAPPED}, NOW))

    def test_missing_next_publish_at_is_skipped_not_guessed(self):
        resumes = [_resume(MAPPED, "")]
        self.assertIsNone(due_resume(resumes, {MAPPED}, NOW))

    def test_exactly_due_resume_is_eligible(self):
        resumes = [_resume(MAPPED, "2026-08-12T11:00:00+0000")]
        self.assertEqual(due_resume(resumes, {MAPPED}, NOW)["id"], MAPPED)


class BumpWindowTest(unittest.TestCase):
    def test_weekday_daytime_open(self):
        self.assertTrue(in_bump_window(datetime(2026, 8, 12, 10, 0, tzinfo=LOCAL_TZ)))

    def test_weekday_evening_still_open(self):
        self.assertTrue(in_bump_window(datetime(2026, 8, 12, 21, 0, tzinfo=LOCAL_TZ)))

    def test_night_closed(self):
        self.assertFalse(in_bump_window(datetime(2026, 8, 12, 4, 0, tzinfo=LOCAL_TZ)))

    def test_weekend_closed_by_default(self):
        self.assertFalse(in_bump_window(datetime(2026, 8, 16, 12, 0, tzinfo=LOCAL_TZ)))

    def test_weekend_can_be_opened_by_env(self):
        with mock.patch.dict("os.environ", {"RESUME_BUMP_WEEKDAYS_ONLY": "0"}):
            self.assertTrue(in_bump_window(datetime(2026, 8, 16, 12, 0, tzinfo=LOCAL_TZ)))


class BumpLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AutomationStore(Path(self.tmp.name) / "db.sqlite3")
        self.addCleanup(self.tmp.cleanup)

    def test_only_successful_bumps_are_counted(self):
        self.store.record_resume_bump(user_id=1, resume_id=MAPPED, status="ok")
        self.store.record_resume_bump(
            user_id=1, resume_id=ALSO_MAPPED, status="failed", reason="limit_exceeded",
        )
        self.assertEqual(self.store.resume_bumps_today(1), 1)

    def test_other_users_do_not_leak_into_the_count(self):
        self.store.record_resume_bump(user_id=2, resume_id=MAPPED, status="ok")
        self.assertEqual(self.store.resume_bumps_today(1), 0)


if __name__ == "__main__":
    unittest.main()
