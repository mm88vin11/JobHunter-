from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from candidate.automation import AutomationStore
from candidate.storage import VariantStore

from mvp import config
from mvp.routers.stats import UNKNOWN_PROFILE, _build_report, _pct, _sample_note

USER_ID = 4242


def _application_id(db_path: Path, vacancy_id: str, resume_id: str) -> int:
    # `AutomationStore` has no public "just give me the id back" accessor, and
    # `tracked_applications()` has no ORDER BY guarantee — query the row we
    # just inserted directly instead of relying on implicit ordering.
    db = sqlite3.connect(db_path)
    try:
        row = db.execute(
            "SELECT id FROM auto_apply_log WHERE user_id=? AND vacancy_id=? AND resume_id=?",
            (USER_ID, vacancy_id, resume_id),
        ).fetchone()
    finally:
        db.close()
    assert row is not None
    return row[0]


def _seed(db_path: Path) -> None:
    """Mirror the real production shape described in the task brief:

    10 applications, all status='applied'/reason='api_success'; hh_state is
    'response' for nine and 'invitation' for one; exactly one row ends up
    outcome_status='replied'; application_events holds 10 hh_sync rows; one
    application's resume_id has no hh_resume_mappings row.
    """
    store = AutomationStore(db_path)
    variants = VariantStore(db_path)

    def save_variant(variant_id: str, vacancy_id: str, *, status: str, profile_key: str | None,
                      employer: str = "Вымышленная компания") -> None:
        payload = {
            "id": variant_id, "user_id": USER_ID, "status": status,
            "vacancy": {
                "id": vacancy_id, "name": "Тестовая вакансия", "employer_name": employer, "source": "hh",
            },
        }
        if profile_key:
            payload["master_profile"] = {"key": profile_key, "name": profile_key, "markdown": ""}
        variants.save(payload)

    # Extra pipeline noise never turned into an application: two below the
    # preview threshold (skipped) and one still sitting as a draft.
    save_variant("var-skip-1", "vac-skip-1", status="skipped", profile_key="demo_profile_a")
    save_variant("var-skip-2", "vac-skip-2", status="skipped", profile_key="demo_profile_a")
    save_variant("var-draft-1", "vac-draft-1", status="draft", profile_key="demo_profile_b")

    profile_plan = (
        ("demo_profile_a", 3),
        ("demo_profile_b", 3),
        ("demo_profile_c", 2),
        ("demo_profile_d", 1),
    )
    resume_for = {key: f"resume-{key}" for key, _ in profile_plan}
    for key, resume_id in resume_for.items():
        store.save_resume_mapping(USER_ID, key, resume_id, title=key)

    counter = 0
    invitation_assigned = False
    for profile_key, count in profile_plan:
        for _ in range(count):
            counter += 1
            variant_id, vacancy_id = f"var-{counter}", f"vac-{counter}"
            resume_id = resume_for[profile_key]
            save_variant(variant_id, vacancy_id, status="applied", profile_key=profile_key,
                         employer=f"Вымышленный работодатель {counter} <Ltd>")
            store.record_application(
                user_id=USER_ID, vacancy_id=vacancy_id, resume_id=resume_id,
                variant_id=variant_id, status="applied", reason="api_success",
            )
            application_id = _application_id(db_path, vacancy_id, resume_id)
            if not invitation_assigned:
                invitation_assigned = True
                negotiation = {
                    "id": f"neg-{counter}", "state": {"id": "invitation"},
                    "has_updates": True, "viewed_by_opponent": True, "updated_at": "2026-08-01T00:00:00+00:00",
                }
            else:
                negotiation = {
                    "id": f"neg-{counter}", "state": {"id": "response"},
                    "has_updates": False, "viewed_by_opponent": False, "updated_at": "2026-08-01T00:00:00+00:00",
                }
            store.sync_application(application_id, negotiation)

    # 10th application: resume_id has no hh_resume_mappings row AND its
    # candidate_variants payload carries no master_profile either — the
    # genuinely-unresolvable case that must fall back to "профиль не определён".
    counter += 1
    variant_id, vacancy_id = f"var-{counter}", f"vac-{counter}"
    resume_id = "resume-unmapped"
    save_variant(variant_id, vacancy_id, status="applied", profile_key=None, employer="Неизвестный работодатель")
    store.record_application(
        user_id=USER_ID, vacancy_id=vacancy_id, resume_id=resume_id,
        variant_id=variant_id, status="applied", reason="api_success",
    )
    application_id = _application_id(db_path, vacancy_id, resume_id)
    store.sync_application(application_id, {
        "id": "neg-unmapped", "state": {"id": "response"}, "has_updates": False,
        "viewed_by_opponent": False, "updated_at": "2026-08-01T00:00:00+00:00",
    })


class StatsReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "stats_test.sqlite3"
        _seed(self.db_path)
        self._patched_automation = AutomationStore(self.db_path)
        self._patcher = mock.patch.object(config, "AUTOMATION", self._patched_automation)
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_funnel_counts_match_seeded_shape(self) -> None:
        text = _build_report(USER_ID)
        self.assertIn("Найдено (сгенерирован вариант): 13", text)
        self.assertIn("Показано на подтверждение: 11", text)
        self.assertIn("Отправлено: 10", text)
        self.assertIn("Ответ: 1 (10%)", text)
        self.assertIn("Приглашение: 1 (10%)", text)

    def test_unmapped_resume_falls_back_to_unknown_profile_without_crashing(self) -> None:
        text = _build_report(USER_ID)
        self.assertIn(UNKNOWN_PROFILE, text)

    def test_small_sample_gets_a_plain_language_caveat_not_a_verdict(self) -> None:
        text = _build_report(USER_ID)
        self.assertIn("мало для вывода", text)
        self.assertIn("30–50", text)

    def test_employer_name_is_html_escaped(self) -> None:
        text = _build_report(USER_ID)
        self.assertIn("Вымышленный работодатель 1 &lt;Ltd&gt;", text)
        self.assertNotIn("Вымышленный работодатель 1 <Ltd>", text)

    def test_freshness_section_present(self) -> None:
        text = _build_report(USER_ID)
        self.assertIn("Свежесть", text)
        self.assertIn("синхронизация", text.lower())

    def test_empty_database_says_no_data_without_dividing_by_zero(self) -> None:
        text = _build_report(USER_ID + 1)  # never touched by _seed
        self.assertIn("Пока нет данных", text)


class StatsHelperTest(unittest.TestCase):
    def test_pct_guards_against_division_by_zero(self) -> None:
        self.assertEqual(_pct(0, 0), "—")
        self.assertEqual(_pct(1, 4), "25%")

    def test_sample_note_only_fires_below_threshold(self) -> None:
        self.assertEqual(_sample_note(29), _sample_note(29))
        self.assertIn("30–50", _sample_note(5))
        self.assertEqual(_sample_note(30), "")


if __name__ == "__main__":
    unittest.main()
