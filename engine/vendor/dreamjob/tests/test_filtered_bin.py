"""Tests for the «Отфильтрованные» rejection bin.

Covers the three layers added for it:

- `AutomationStore.filtered_vacancies` / `record_filtered` / `filtered_count` /
  `filtered_counters` in `candidate/automation.py` — the storage layer, plus
  its idempotent-migration guarantee (the bot must survive starting against
  an existing prod SQLite file that predates this table).
- `mvp.automation._filtered_reason` / `_filtered_gate` / `_record_filtered` —
  the pure reason-building logic and the write path wired into the three
  drop points in `_run_automation_cycle` (trap screening, ranker
  hard-reject, below-preview-threshold).
- `mvp.routers.filtered._page_bounds` — the pagination clamp used by the
  bin's list screen.

Same tempdir + VariantStore + AutomationStore pattern as
`tests/test_cards.py` / `tests/test_crm_store.py`.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from candidate.automation import AutomationStore
from candidate.storage import VariantStore

from mvp import config
from mvp.automation import _filtered_gate, _filtered_reason, _record_filtered
from mvp.routers.filtered import _page_bounds


def _vacancy(vacancy_id: str = "hh:1", **overrides) -> dict:
    base = {
        "id": vacancy_id,
        "external_id": vacancy_id,
        "name": "Backend Engineer",
        "employer": {"name": "Вымышленная компания"},
        "area": {"name": "Тестовый регион"},
        "source_url": "https://hh.ru/vacancy/000000000",
    }
    base.update(overrides)
    return base


class StoreTestCase(unittest.TestCase):
    """Shared tempdir + VariantStore + AutomationStore setup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "automation.sqlite3"
        self.variants = VariantStore(self.path)
        self.store = AutomationStore(self.path)
        self.addCleanup(self.tmp.cleanup)


# ---------------------------------------------------------------------------
# Storage layer: persistence, migration idempotency, pagination, no-leak
# ---------------------------------------------------------------------------

class RecordFilteredStoreTest(StoreTestCase):
    def test_record_and_read_back_all_fields(self):
        self.store.record_filtered(
            user_id=7, vacancy_id="hh:1", vacancy_name="Backend Engineer",
            employer_name="Вымышленная компания", area="Тестовый регион", source_url="https://hh.ru/vacancy/000000000",
            score=42, gate="trap", reason="Стоп-фраза пользователя",
        )
        rows = self.store.filtered_vacancies(7, limit=10)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["vacancy_id"], "hh:1")
        self.assertEqual(row["vacancy_name"], "Backend Engineer")
        self.assertEqual(row["employer_name"], "Вымышленная компания")
        self.assertEqual(row["area"], "Тестовый регион")
        self.assertEqual(row["source_url"], "https://hh.ru/vacancy/000000000")
        self.assertEqual(row["score"], 42)
        self.assertEqual(row["gate"], "trap")
        self.assertEqual(row["reason"], "Стоп-фраза пользователя")
        self.assertTrue(row["created_at"])

    def test_other_users_are_not_visible(self):
        self.store.record_filtered(
            user_id=7, vacancy_id="v1", vacancy_name="A", employer_name="", area="",
            source_url="", score=0, gate="trap", reason="r",
        )
        self.assertEqual(self.store.filtered_vacancies(8, limit=10), [])
        self.assertEqual(self.store.filtered_count(8), 0)

    def test_retention_cap_prunes_oldest_first(self):
        with mock.patch("candidate.automation._FILTERED_MAX_ROWS_PER_USER", 5):
            for index in range(9):
                self.store.record_filtered(
                    user_id=7, vacancy_id=f"v{index}", vacancy_name=f"V{index}",
                    employer_name="", area="", source_url="", score=0,
                    gate="trap", reason="r",
                )
            self.assertEqual(self.store.filtered_count(7), 5)
            newest_ids = {row["vacancy_id"] for row in self.store.filtered_vacancies(7, limit=10)}
            self.assertEqual(newest_ids, {"v4", "v5", "v6", "v7", "v8"})

    def test_counters_scope_to_the_last_window_and_rank_top_reasons(self):
        for reason, count in (("A", 3), ("B", 2), ("C", 1)):
            for _ in range(count):
                self.store.record_filtered(
                    user_id=7, vacancy_id="v", vacancy_name="V", employer_name="",
                    area="", source_url="", score=0, gate="trap", reason=reason,
                )
        counters = self.store.filtered_counters(7, hours=24, top=2)
        self.assertEqual(counters["total"], 6)
        self.assertEqual(counters["by_gate"], {"trap": 6})
        self.assertEqual(counters["top_reasons"], [("A", 3), ("B", 2)])

    def test_filtered_vacancies_never_leak_into_the_normal_deck_query(self):
        """The bin is a separate table — the deck's own queries must be blind to it."""
        self.variants.save({
            "id": "draft-1", "user_id": 7, "status": "draft",
            "vacancy": {"id": "hh:draft-1"},
        })
        for gate in ("trap", "hard_reject", "below_threshold"):
            self.store.record_filtered(
                user_id=7, vacancy_id=f"hh:{gate}", vacancy_name="X", employer_name="",
                area="", source_url="", score=0, gate=gate, reason="r",
            )
        self.assertEqual(self.variants.count_by_status(7), {"draft": 1})
        all_statuses = (
            "draft", "changed", "approved", "manual_required",
            "skipped", "failed", "applied", "already_applied",
        )
        self.assertEqual(len(self.variants.list_by_status(7, all_statuses, limit=200)), 1)


class MigrationIdempotencyTest(StoreTestCase):
    def test_reopening_the_store_twice_does_not_crash_and_keeps_data(self):
        self.store.record_filtered(
            user_id=1, vacancy_id="v1", vacancy_name="A", employer_name="E",
            area="", source_url="", score=10, gate="trap", reason="r",
        )
        # Simulates the bot restarting against an existing prod DB that
        # already has this table — CREATE TABLE IF NOT EXISTS / CREATE INDEX
        # IF NOT EXISTS must be silent no-ops, not raise.
        reopened_once = AutomationStore(self.path)
        reopened_twice = AutomationStore(self.path)
        self.assertEqual(reopened_twice.filtered_count(1), 1)
        reopened_twice.record_filtered(
            user_id=1, vacancy_id="v2", vacancy_name="B", employer_name="E",
            area="", source_url="", score=20, gate="hard_reject", reason="r2",
        )
        self.assertEqual(reopened_once.filtered_count(1), 2)

    def test_fresh_store_on_a_db_with_only_the_older_tables_does_not_crash(self):
        """An old prod DB only ever had `candidate_variants` written by VariantStore
        before this feature shipped; AutomationStore must still boot against it."""
        only_variants_path = Path(self.tmp.name) / "legacy.sqlite3"
        VariantStore(only_variants_path)
        store = AutomationStore(only_variants_path)  # must not raise
        self.assertEqual(store.filtered_count(1), 0)


class PaginationBoundaryTest(unittest.TestCase):
    def test_empty_result_clamps_to_page_zero(self):
        self.assertEqual(_page_bounds(0, 0), (0, 0, 0))
        self.assertEqual(_page_bounds(0, 5), (0, 0, 0))

    def test_single_page_clamps_any_page_to_zero(self):
        self.assertEqual(_page_bounds(1, 0), (0, 0, 0))
        self.assertEqual(_page_bounds(1, 99), (0, 0, 0))

    def test_multi_page_offsets_and_last_page(self):
        # 17 rows, page size 8 -> pages of 8, 8, 1
        self.assertEqual(_page_bounds(17, 0), (0, 0, 2))
        self.assertEqual(_page_bounds(17, 1), (1, 8, 2))
        self.assertEqual(_page_bounds(17, 2), (2, 16, 2))

    def test_page_past_the_end_clamps_to_the_last_page(self):
        self.assertEqual(_page_bounds(17, 99), (2, 16, 2))

    def test_negative_page_clamps_to_zero(self):
        self.assertEqual(_page_bounds(17, -5), (0, 0, 2))


class StorePaginationTest(StoreTestCase):
    def test_offset_and_limit_produce_the_expected_partial_last_page(self):
        for index in range(17):
            self.store.record_filtered(
                user_id=7, vacancy_id=f"v{index}", vacancy_name=f"V{index}",
                employer_name="", area="", source_url="", score=0,
                gate="trap", reason="r",
            )
        page0 = self.store.filtered_vacancies(7, limit=8, offset=0)
        page1 = self.store.filtered_vacancies(7, limit=8, offset=8)
        page2 = self.store.filtered_vacancies(7, limit=8, offset=16)
        self.assertEqual(len(page0), 8)
        self.assertEqual(len(page1), 8)
        self.assertEqual(len(page2), 1)
        # Newest first: the 17th inserted row (v16) leads page 0.
        self.assertEqual(page0[0]["vacancy_id"], "v16")
        self.assertEqual(page2[0]["vacancy_id"], "v0")

    def test_offset_past_the_end_returns_empty_not_an_error(self):
        self.store.record_filtered(
            user_id=7, vacancy_id="v0", vacancy_name="V0", employer_name="",
            area="", source_url="", score=0, gate="trap", reason="r",
        )
        self.assertEqual(self.store.filtered_vacancies(7, limit=8, offset=100), [])


# ---------------------------------------------------------------------------
# mvp.automation: reason building + gate relabelling (pure functions)
# ---------------------------------------------------------------------------

class FilteredReasonTest(unittest.TestCase):
    def test_trap_reason_prefers_hard_reject_reason(self):
        verdict = SimpleNamespace(hard_reject_reason="Стоп-фраза: развод на деньги", flags=["игнорируется"])
        self.assertEqual(
            _filtered_reason("trap", verdict=verdict), "Стоп-фраза: развод на деньги",
        )

    def test_trap_reason_falls_back_to_joined_flags(self):
        verdict = SimpleNamespace(hard_reject_reason=None, flags=["Подозрительно высокая зарплата", "Мессенджер вместо hh.ru"])
        self.assertEqual(
            _filtered_reason("trap", verdict=verdict),
            "Подозрительно высокая зарплата; Мессенджер вместо hh.ru",
        )

    def test_hard_reject_reason_comes_from_ranking(self):
        ranking = SimpleNamespace(hard_reject_reason="Доход ниже настроенного минимума")
        self.assertEqual(
            _filtered_reason("hard_reject", ranking=ranking), "Доход ниже настроенного минимума",
        )

    def test_below_threshold_reason_uses_missing_and_reply_chance(self):
        ranking = SimpleNamespace(
            hard_tech_gated=False,
            missing_reasons=("Нет опыта с LLM-агентами", "Нет отраслевого опыта"),
            reply_chance="низкий", reply_chance_reason="мало пересечений с профилем",
        )
        reason = _filtered_reason("below_threshold", ranking=ranking)
        self.assertIn("Нет опыта с LLM-агентами", reason)
        self.assertIn("низкий", reason)
        self.assertIn("мало пересечений с профилем", reason)

    def test_hard_tech_reason_mentions_the_cap(self):
        ranking = SimpleNamespace(
            hard_tech_gated=True, missing_reasons=(), reply_chance="", reply_chance_reason="",
        )
        reason = _filtered_reason("hard_tech", ranking=ranking)
        self.assertIn("алгоритм", reason.lower())

    def test_missing_verdict_or_ranking_still_returns_a_readable_default(self):
        self.assertTrue(_filtered_reason("trap"))
        self.assertTrue(_filtered_reason("hard_reject"))
        self.assertTrue(_filtered_reason("below_threshold"))


class FilteredGateTest(unittest.TestCase):
    def test_below_threshold_stays_generic_without_the_hard_tech_cap(self):
        ranking = SimpleNamespace(hard_tech_gated=False)
        self.assertEqual(_filtered_gate("below_threshold", ranking), "below_threshold")

    def test_below_threshold_becomes_hard_tech_when_capped(self):
        ranking = SimpleNamespace(hard_tech_gated=True)
        self.assertEqual(_filtered_gate("below_threshold", ranking), "hard_tech")

    def test_other_gates_pass_through_unchanged(self):
        self.assertEqual(_filtered_gate("trap", None), "trap")
        self.assertEqual(_filtered_gate("hard_reject", SimpleNamespace(hard_tech_gated=True)), "hard_reject")


# ---------------------------------------------------------------------------
# mvp.automation._record_filtered: the write path used at all three gates
# ---------------------------------------------------------------------------

class RecordFilteredWritePathTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(config, "AUTOMATION", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_trap_gate_is_persisted_with_a_real_reason(self):
        verdict = SimpleNamespace(hard_reject_reason="Работодатель заблокирован пользователем: ООО Ромашка", flags=[])
        _record_filtered(
            7, _vacancy(), score=0, gate="trap",
            reason=_filtered_reason("trap", verdict=verdict),
        )
        rows = self.store.filtered_vacancies(7, limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["gate"], "trap")
        self.assertEqual(rows[0]["employer_name"], "Вымышленная компания")
        self.assertEqual(rows[0]["area"], "Тестовый регион")
        self.assertIn("Ромашка", rows[0]["reason"])

    def test_hard_reject_gate_is_persisted_with_the_ranking_score(self):
        ranking = SimpleNamespace(total=0, hard_reject_reason="Доход ниже настроенного минимума")
        _record_filtered(
            7, _vacancy(vacancy_id="hh:2"), score=ranking.total, gate="hard_reject",
            reason=_filtered_reason("hard_reject", ranking=ranking),
        )
        rows = self.store.filtered_vacancies(7, limit=10)
        self.assertEqual(rows[0]["gate"], "hard_reject")
        self.assertEqual(rows[0]["score"], 0)
        self.assertIn("настроенного минимума", rows[0]["reason"])

    def test_below_threshold_gate_relabels_to_hard_tech_when_capped(self):
        ranking = SimpleNamespace(
            total=38, hard_tech_gated=True,
            missing_reasons=("Нет упоминания highload/распределённых систем",),
            reply_chance="низкий", reply_chance_reason="классическая инженерная роль",
        )
        gate = _filtered_gate("below_threshold", ranking)
        _record_filtered(
            7, _vacancy(vacancy_id="hh:3"), score=ranking.total, gate=gate,
            reason=_filtered_reason(gate, ranking=ranking),
        )
        rows = self.store.filtered_vacancies(7, limit=10)
        self.assertEqual(rows[0]["gate"], "hard_tech")
        self.assertEqual(rows[0]["score"], 38)

    def test_storage_failure_never_propagates(self):
        with mock.patch.object(config.AUTOMATION, "record_filtered", side_effect=RuntimeError("db is locked")):
            _record_filtered(7, _vacancy(), score=0, gate="trap", reason="x")  # must not raise


if __name__ == "__main__":
    unittest.main()
