"""Storage-layer tests for the CRM tables in `candidate/automation.py`:
`negotiation_messages` (full local message history) and `crm_thread_state`
(one derived row per conversation). Same tempdir + VariantStore +
AutomationStore pattern used by `tests/test_inbox.py` / `tests/test_cards.py`.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from candidate.automation import AutomationStore
from candidate.storage import VariantStore


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _message(message_id: str, *, text: str, created_at: str, employer: bool) -> dict:
    return {
        "id": message_id, "text": text, "created_at": created_at,
        "author": {"participant_type": "employer" if employer else "applicant"},
    }


class CrmStoreTestCase(unittest.TestCase):
    """Shared tempdir + store setup for every class below."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "automation.sqlite3"
        VariantStore(path)  # creates candidate_variants, joined by threads_needing_message_sync
        self.store = AutomationStore(path)
        self.addCleanup(self.tmp.cleanup)


class StoreNegotiationMessagesIdempotencyTest(CrmStoreTestCase):
    def test_replaying_overlapping_batch_adds_no_duplicates_and_returns_zero_new(self):
        first_batch = [
            _message("m1", text="a", created_at="2026-08-10T09:00:00+0300", employer=True),
            _message("m2", text="b", created_at="2026-08-10T09:05:00+0300", employer=False),
        ]
        new_count = self.store.store_negotiation_messages(7, "n1", first_batch)
        self.assertEqual(new_count, 2)

        overlapping_batch = first_batch + [
            _message("m3", text="c", created_at="2026-08-10T09:10:00+0300", employer=True),
        ]
        new_count_2 = self.store.store_negotiation_messages(7, "n1", overlapping_batch)
        self.assertEqual(new_count_2, 1)

        # Replaying the exact same batch a third time must add nothing at all.
        new_count_3 = self.store.store_negotiation_messages(7, "n1", overlapping_batch)
        self.assertEqual(new_count_3, 0)

        all_messages = self.store.crm_thread_messages(7, "n1")
        self.assertEqual(len(all_messages), 3)
        self.assertEqual([m["message_id"] for m in all_messages], ["m1", "m2", "m3"])

    def test_thread_uses_full_last_message_for_next_step_classification(self):
        prefix = "Описание вакансии " * 20
        text = prefix + "Перед собеседованием заполните короткую анкету"
        self.store.store_negotiation_messages(7, "n1", [
            _message("m1", text=text, created_at="2026-08-10T09:00:00+0300", employer=True),
        ])
        self.store.recompute_thread_state(7, "n1", hh_state="invitation")

        thread = self.store.crm_thread(7, "n1")
        self.assertEqual(thread["last_message_preview"], text)
        self.assertEqual(self.store.crm_counters(7, silence_days=3)["task"], 1)


class AwaitingSideAndSilenceTest(CrmStoreTestCase):
    def test_none_with_no_messages(self):
        state = self.store.recompute_thread_state(7, "n1")
        self.assertEqual(state["awaiting_side"], "none")
        self.assertEqual(state["days_silent"], 0)

    def test_employer_side_when_owners_message_is_newest(self):
        self.store.store_negotiation_messages(7, "n1", [
            _message("m1", text="hi", created_at="2026-08-10T09:00:00+0300", employer=True),
        ])
        self.store.record_outgoing_message(7, "n1", "reply", created_at="2026-08-11T09:00:00+0300")
        thread = self.store.crm_thread(7, "n1")
        self.assertEqual(thread["awaiting_side"], "employer")
        self.assertEqual(thread["silence_since"], "2026-08-11T09:00:00+0300")

    def test_me_side_when_employers_message_is_newest(self):
        self.store.record_outgoing_message(7, "n1", "hello", created_at="2026-08-10T09:00:00+0300")
        self.store.store_negotiation_messages(7, "n1", [
            _message("m2", text="reply", created_at="2026-08-11T09:00:00+0300", employer=True),
        ])
        self.store.recompute_thread_state(7, "n1")
        thread = self.store.crm_thread(7, "n1")
        self.assertEqual(thread["awaiting_side"], "me")
        self.assertEqual(thread["silence_since"], "2026-08-11T09:00:00+0300")

    def test_silence_since_tracks_newest_message_regardless_of_direction(self):
        self.store.store_negotiation_messages(7, "n1", [
            _message("m1", text="first", created_at="2026-08-05T09:00:00+0300", employer=True),
        ])
        self.store.record_outgoing_message(7, "n1", "second", created_at="2026-08-08T09:00:00+0300")
        self.store.store_negotiation_messages(7, "n1", [
            _message("m3", text="third, newest", created_at="2026-08-09T09:00:00+0300", employer=True),
        ])
        self.store.recompute_thread_state(7, "n1")
        thread = self.store.crm_thread(7, "n1")
        self.assertEqual(thread["silence_since"], "2026-08-09T09:00:00+0300")

    def test_days_silent_is_derived_from_silence_since(self):
        five_days_ago = _iso(datetime.now(timezone.utc) - timedelta(days=5, hours=1))
        self.store.record_outgoing_message(7, "n1", "hi", created_at=five_days_ago)
        thread = self.store.crm_thread(7, "n1")
        self.assertGreaterEqual(thread["days_silent"], 5)


class RecordOutgoingMessageFlipsAwaitingSideTest(CrmStoreTestCase):
    def test_flips_from_me_to_employer_immediately(self):
        self.store.store_negotiation_messages(7, "n1", [
            _message("m1", text="hi", created_at="2026-08-10T09:00:00+0300", employer=True),
        ])
        self.store.recompute_thread_state(7, "n1")
        self.assertEqual(self.store.crm_thread(7, "n1")["awaiting_side"], "me")

        self.store.record_outgoing_message(7, "n1", "reply text")
        self.assertEqual(self.store.crm_thread(7, "n1")["awaiting_side"], "employer")


class RecomputeThreadStateMetadataTest(CrmStoreTestCase):
    def test_never_overwrites_known_metadata_with_none(self):
        self.store.recompute_thread_state(
            7, "n1", vacancy_id="v1", vacancy_name="Vacancy", employer_name="Employer", hh_state="response",
        )
        # A later call that does not know some fields (None default) must not
        # clobber values learned on an earlier, better-informed call.
        state = self.store.recompute_thread_state(7, "n1")
        self.assertEqual(state["vacancy_id"], "v1")
        self.assertEqual(state["vacancy_name"], "Vacancy")
        self.assertEqual(state["employer_name"], "Employer")
        self.assertEqual(state["hh_state"], "response")

        # An explicit new value for one field still updates just that field.
        state_2 = self.store.recompute_thread_state(7, "n1", hh_state="invitation")
        self.assertEqual(state_2["hh_state"], "invitation")
        self.assertEqual(state_2["vacancy_name"], "Vacancy")


class ArchiveUnarchiveTest(CrmStoreTestCase):
    def test_moves_between_active_and_archived_lists(self):
        self.store.recompute_thread_state(
            7, "n1", vacancy_id="v1", vacancy_name="V", employer_name="E", hh_state="response",
        )
        self.assertEqual([t["negotiation_id"] for t in self.store.crm_threads(7)], ["n1"])
        self.assertEqual(self.store.crm_threads(7, archived_only=True), [])

        self.store.archive_thread(7, "n1", "manual")
        self.assertEqual(self.store.crm_threads(7), [])
        archived = self.store.crm_threads(7, archived_only=True)
        self.assertEqual([t["negotiation_id"] for t in archived], ["n1"])
        self.assertEqual(archived[0]["archive_reason"], "manual")

        self.store.unarchive_thread(7, "n1")
        self.assertEqual([t["negotiation_id"] for t in self.store.crm_threads(7)], ["n1"])
        self.assertEqual(self.store.crm_threads(7, archived_only=True), [])


class CrmCountersTest(CrmStoreTestCase):
    def test_counters_follow_next_steps_and_exclude_archived(self):
        now = datetime.now(timezone.utc)

        # Thread A: a genuine employer question -> reply.
        self.store.store_negotiation_messages(7, "nA", [
            _message("mA", text="Есть ли у вас опыт?", created_at=_iso(now), employer=True),
        ])
        self.store.recompute_thread_state(7, "nA")

        # Thread B: owner wrote last, 10 days ago -> silent at silence_days=3.
        self.store.record_outgoing_message(7, "nB", "hi", created_at=_iso(now - timedelta(days=10)))

        # Thread C: owner wrote last, 1 day ago -> NOT silent at silence_days=3.
        self.store.record_outgoing_message(7, "nC", "hi", created_at=_iso(now - timedelta(days=1)))

        # Thread D: rejection -> its own counter, never reply/active.
        self.store.store_negotiation_messages(7, "nD", [
            _message("mD", text="Мы выбрали другого кандидата", created_at=_iso(now), employer=True),
        ])
        self.store.recompute_thread_state(7, "nD", hh_state="discard")

        # Thread E: archived -- excluded from every active section.
        self.store.recompute_thread_state(7, "nE")
        self.store.archive_thread(7, "nE", "manual")

        counters = self.store.crm_counters(7, silence_days=3)
        self.assertEqual(counters["reply"], 1)
        self.assertEqual(counters["task"], 0)
        self.assertEqual(counters["scheduled"], 0)
        self.assertEqual(counters["waiting"], 2)
        self.assertEqual(counters["rejected"], 1)
        self.assertEqual(counters["silent"], 1)
        self.assertEqual(counters["active"], 3)
        self.assertEqual(counters["archived"], 1)


class ThreadsNeedingMessageSyncTest(CrmStoreTestCase):
    def test_surfaces_from_both_sources_and_stops_once_current(self):
        # A bot-sent application: auto_apply_log + a sync_application call.
        self.store.record_application(
            user_id=7, vacancy_id="v1", resume_id="r1", variant_id="var-1",
            status="applied", reason="api_success",
        )
        application = next(a for a in self.store.tracked_applications(7) if a["variant_id"] == "var-1")
        self.store.sync_application(application["id"], {
            "id": "n-log", "state": {"id": "response"}, "has_updates": False,
            "updated_at": "2026-08-10T10:00:00+0300",
        })

        # A hand-started thread: negotiation_watch only, no bot application.
        self.store.watch_negotiation(7, {
            "id": "n-watch", "state": {"id": "response"}, "has_updates": False,
            "updated_at": "2026-08-10T10:00:00+0300",
            "vacancy": {"id": "v2", "name": "Watch Vacancy", "employer": {"name": "Watch Employer"}},
        })

        needing = self.store.threads_needing_message_sync(7)
        self.assertEqual({t["negotiation_id"] for t in needing}, {"n-log", "n-watch"})

        # Sync just n-log: its stored newest message now matches hh.ru's
        # external_updated_at, so it must stop being surfaced.
        self.store.store_negotiation_messages(7, "n-log", [
            _message("m1", text="hi", created_at="2026-08-10T10:00:00+0300", employer=True),
        ])
        self.store.recompute_thread_state(7, "n-log")

        needing_after = self.store.threads_needing_message_sync(7)
        self.assertEqual({t["negotiation_id"] for t in needing_after}, {"n-watch"})

    def test_limit_caps_the_returned_count(self):
        for i in range(5):
            self.store.watch_negotiation(7, {
                "id": f"n{i}", "state": {"id": "response"}, "has_updates": False,
                "updated_at": "2026-08-10T10:00:00+0300",
                "vacancy": {"id": f"v{i}", "name": "V", "employer": {"name": "E"}},
            })
        self.assertEqual(len(self.store.threads_needing_message_sync(7, limit=2)), 2)
        self.assertEqual(len(self.store.threads_needing_message_sync(7)), 5)


if __name__ == "__main__":
    unittest.main()
