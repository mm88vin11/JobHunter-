"""Tests for candidate/service.py::judge_vacancy_fit -- the small strict-JSON DeepSeek judge
that wires a fit/gap/reply-chance verdict into candidate/ranking.py's dormant llm_score seam.

No live Gonka calls anywhere here: `judge_completion` is always a test double matching
hh.gonka.complete_json's shape (keyword args in, dict out), or the module-level
`hh.gonka.complete_json` name itself is mocked for the one test that exercises the lazy-import
path used in production.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from candidate.ranking import MAX_LLM_ADJUSTMENT, PROFILE_BLOCKER_CAP, combine_scores
from candidate.service import judge_vacancy_fit

VACANCY = {
    "name": "Backend-разработчик",
    "description": (
        "Обязанности: поддерживать тестовый API. "
        "Требования: Python, REST и техническая документация."
    ),
}
PROFILE = {
    "title": "Тестовый специалист",
    "about": "Полностью вымышленный профиль для unit-теста",
    "skill_set": ["Python", "REST", "Документация"],
}
RANKING_SCORE = {
    "deterministic_total": 60, "value": 60,
    "hard_tech_gated": False, "blocker_capped": False,
}


def _stub(response, *, calls: list | None = None):
    """A judge_completion double: raises if `response` is an exception, else returns it."""

    def call(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        if isinstance(response, BaseException):
            raise response
        return response

    return call


class JudgeVacancyFitGatingTests(unittest.TestCase):
    """The flag, and every way the model call can fail, must never crash a card."""

    def test_flag_off_returns_none_without_calling_the_model(self):
        calls: list = []
        with mock.patch.dict(os.environ, {"GONKA_INTERNAL_ENABLED": ""}, clear=False):
            # judge_completion intentionally omitted -- production path, flag decides.
            result = judge_vacancy_fit(VACANCY, PROFILE, RANKING_SCORE)
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_flag_off_variants_all_skip_the_call(self):
        for value in ("0", "false", "no", "off", ""):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"GONKA_INTERNAL_ENABLED": value}, clear=False):
                    self.assertIsNone(judge_vacancy_fit(VACANCY, PROFILE, RANKING_SCORE))

    def test_flag_on_uses_gonka_complete_json(self):
        response = {
            "score": 65, "fit_reasons": [], "missing_reasons": [],
            "reply_chance": "низкий", "reply_chance_reason": "мало общего",
        }
        with mock.patch.dict(os.environ, {"GONKA_INTERNAL_ENABLED": "1"}, clear=False):
            with mock.patch("hh.gonka.complete_json", return_value=response) as mocked:
                result = judge_vacancy_fit(VACANCY, PROFILE, RANKING_SCORE)
        mocked.assert_called_once()
        self.assertIsNotNone(result)
        self.assertEqual(result["score"], 65)

    def test_completion_exception_returns_none_not_raises(self):
        result = judge_vacancy_fit(
            VACANCY, PROFILE, RANKING_SCORE,
            judge_completion=_stub(RuntimeError("gonka down")),
        )
        self.assertIsNone(result)

    def test_non_dict_response_returns_none(self):
        result = judge_vacancy_fit(
            VACANCY, PROFILE, RANKING_SCORE, judge_completion=_stub(["not", "a", "dict"]),
        )
        self.assertIsNone(result)

    def test_missing_score_returns_none(self):
        response = {
            "fit_reasons": [], "missing_reasons": [],
            "reply_chance": "средний", "reply_chance_reason": "r",
        }
        result = judge_vacancy_fit(VACANCY, PROFILE, RANKING_SCORE, judge_completion=_stub(response))
        self.assertIsNone(result)

    def test_non_numeric_score_returns_none(self):
        response = {
            "score": "не число", "fit_reasons": [], "missing_reasons": [],
            "reply_chance": "средний", "reply_chance_reason": "r",
        }
        result = judge_vacancy_fit(VACANCY, PROFILE, RANKING_SCORE, judge_completion=_stub(response))
        self.assertIsNone(result)

    def test_bad_reply_chance_returns_none(self):
        response = {
            "score": 70, "fit_reasons": [], "missing_reasons": [],
            "reply_chance": "maybe", "reply_chance_reason": "r",
        }
        result = judge_vacancy_fit(VACANCY, PROFILE, RANKING_SCORE, judge_completion=_stub(response))
        self.assertIsNone(result)


class JudgeVacancyFitVerdictTests(unittest.TestCase):
    """The shape and content of a well-formed verdict."""

    def test_valid_response_wires_into_combine_scores(self):
        calls: list = []
        response = {
            "score": 80,
            "fit_reasons": ["Требуется Python — он указан в тестовом профиле"],
            "missing_reasons": ["Не указан опыт нагрузочного тестирования"],
            "reply_chance": "средний",
            "reply_chance_reason": "хорошее техническое совпадение, но виден языковой разрыв",
        }
        result = judge_vacancy_fit(
            VACANCY, PROFILE, RANKING_SCORE, judge_completion=_stub(response, calls=calls),
        )
        self.assertEqual(len(calls), 1)
        expected_total, expected_adjustment = combine_scores(RANKING_SCORE["deterministic_total"], 80)
        self.assertIsNotNone(result)
        self.assertEqual(result["score"], 80)
        self.assertEqual(result["adjustment"], expected_adjustment)
        self.assertEqual(result["total_with_llm"], expected_total)
        self.assertEqual(result["fit_reasons"], response["fit_reasons"])
        self.assertEqual(result["missing_reasons"], response["missing_reasons"])
        self.assertEqual(result["reply_chance"], "средний")
        self.assertEqual(result["reply_chance_reason"], response["reply_chance_reason"])

    def test_adjustment_is_clamped_to_plus_max_llm_adjustment(self):
        response = {
            "score": 100, "fit_reasons": [], "missing_reasons": [],
            "reply_chance": "высокий", "reply_chance_reason": "r",
        }
        ranking_score = {"deterministic_total": 10}
        result = judge_vacancy_fit(VACANCY, PROFILE, ranking_score, judge_completion=_stub(response))
        self.assertEqual(result["adjustment"], MAX_LLM_ADJUSTMENT)
        self.assertEqual(result["total_with_llm"], 10 + MAX_LLM_ADJUSTMENT)

    def test_adjustment_is_clamped_to_minus_max_llm_adjustment(self):
        response = {
            "score": 0, "fit_reasons": [], "missing_reasons": [],
            "reply_chance": "низкий", "reply_chance_reason": "r",
        }
        ranking_score = {"deterministic_total": 90}
        result = judge_vacancy_fit(VACANCY, PROFILE, ranking_score, judge_completion=_stub(response))
        self.assertEqual(result["adjustment"], -MAX_LLM_ADJUSTMENT)
        self.assertEqual(result["total_with_llm"], 90 - MAX_LLM_ADJUSTMENT)

    def test_hard_tech_gated_reapplies_the_profile_blocker_cap(self):
        response = {
            "score": 100, "fit_reasons": [], "missing_reasons": [],
            "reply_chance": "высокий", "reply_chance_reason": "r",
        }
        ranking_score = {"deterministic_total": 30, "hard_tech_gated": True}
        result = judge_vacancy_fit(VACANCY, PROFILE, ranking_score, judge_completion=_stub(response))
        self.assertLessEqual(result["total_with_llm"], PROFILE_BLOCKER_CAP)

    def test_blocker_capped_reapplies_the_profile_blocker_cap(self):
        response = {
            "score": 100, "fit_reasons": [], "missing_reasons": [],
            "reply_chance": "высокий", "reply_chance_reason": "r",
        }
        ranking_score = {"deterministic_total": 30, "blocker_capped": True}
        result = judge_vacancy_fit(VACANCY, PROFILE, ranking_score, judge_completion=_stub(response))
        self.assertLessEqual(result["total_with_llm"], PROFILE_BLOCKER_CAP)

    def test_reason_lists_are_capped_and_cleaned(self):
        response = {
            "score": 50,
            "fit_reasons": [f"reason {i}" for i in range(10)],
            "missing_reasons": ["  multi   space   reason  "],
            "reply_chance": "низкий", "reply_chance_reason": "  padded reason  ",
        }
        result = judge_vacancy_fit(VACANCY, PROFILE, RANKING_SCORE, judge_completion=_stub(response))
        self.assertEqual(len(result["fit_reasons"]), 5)
        self.assertEqual(result["missing_reasons"], ["multi space reason"])
        self.assertEqual(result["reply_chance_reason"], "padded reason")

    def test_score_is_clamped_to_0_100(self):
        response = {
            "score": 500, "fit_reasons": [], "missing_reasons": [],
            "reply_chance": "высокий", "reply_chance_reason": "r",
        }
        result = judge_vacancy_fit(VACANCY, PROFILE, RANKING_SCORE, judge_completion=_stub(response))
        self.assertEqual(result["score"], 100)


if __name__ == "__main__":
    unittest.main()
