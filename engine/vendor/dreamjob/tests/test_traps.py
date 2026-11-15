from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from candidate.traps import (
    REJECT_THRESHOLD,
    WARN_THRESHOLD,
    TrapVerdict,
    add_blocked_employer,
    add_stop_phrase,
    evaluate_vacancy,
    load_user_lists,
    remove_blocked_employer,
    remove_stop_phrase,
)


def _vacancy(**overrides) -> dict:
    # Every value is synthetic; numeric salaries only exercise trap thresholds.
    base = {
        "id": "hh:demo",
        "name": "Тестовая вакансия",
        "description": "Обычная вакансия без подозрительных признаков.",
        "employer": {"name": "Вымышленная компания", "id": "demo-company"},
        "salary": {"from": 200_000, "to": 300_000, "currency": "RUR"},
        "experience": {"id": "between3And6"},
        "area": {"name": "Тестовый регион"},
        "has_test": False,
    }
    base.update(overrides)
    return base


class TrapSignalsTest(unittest.TestCase):
    # ── 1. Suspiciously high salary + no experience ────────────────────

    def test_high_salary_no_experience_positive(self):
        vacancy = _vacancy(
            salary={"from": 350_000, "to": 400_000},
            experience={"id": "noExperience"},
        )
        verdict = evaluate_vacancy(vacancy)
        self.assertTrue(any("высокая зарплата" in f for f in verdict.flags))
        self.assertGreater(verdict.score, 0.0)

    def test_high_salary_no_experience_negative(self):
        vacancy = _vacancy(
            salary={"from": 350_000, "to": 400_000},
            experience={"id": "between3And6"},
        )
        verdict = evaluate_vacancy(vacancy)
        self.assertFalse(any("высокая зарплата" in f for f in verdict.flags))

    def test_high_salary_relative_to_expected(self):
        vacancy = _vacancy(
            salary={"from": 250_000, "to": 250_000},
            experience={"id": "noExperience"},
        )
        verdict = evaluate_vacancy(vacancy, expected_salary=50_000)
        self.assertTrue(any("высокая зарплата" in f for f in verdict.flags))

    # ── 2. Messenger redirect ───────────────────────────────────────────

    def test_messenger_redirect_verb_positive(self):
        vacancy = _vacancy(description="Пишите в Telegram для подробностей: @some_bot")
        verdict = evaluate_vacancy(vacancy)
        self.assertTrue(any("Перенаправление в мессенджер" in f for f in verdict.flags))
        self.assertGreaterEqual(verdict.score, 0.9)
        self.assertEqual(verdict.verdict, "reject")

    def test_messenger_bare_link_is_weak_not_reject(self):
        vacancy = _vacancy(description="Контакт для связи: t.me/hr_manager_name")
        verdict = evaluate_vacancy(vacancy)
        self.assertTrue(any("ссылка на мессенджер" in f for f in verdict.flags))
        self.assertLess(verdict.score, WARN_THRESHOLD)
        self.assertEqual(verdict.verdict, "clean")

    def test_messenger_redirect_negative(self):
        vacancy = _vacancy(description="Собеседование пройдёт в офисе компании.")
        verdict = evaluate_vacancy(vacancy)
        self.assertFalse(any("мессенджер" in f for f in verdict.flags))

    # ── 3. Empty / generic employer ─────────────────────────────────────

    def test_generic_employer_positive(self):
        vacancy = _vacancy(employer={"name": "ООО", "id": ""})
        verdict = evaluate_vacancy(vacancy)
        self.assertTrue(any("Подозрительное название работодателя" in f for f in verdict.flags))

    def test_empty_employer_positive(self):
        vacancy = _vacancy(employer={"name": "", "id": ""})
        verdict = evaluate_vacancy(vacancy)
        self.assertTrue(any("Не указан работодатель" in f for f in verdict.flags))

    def test_generic_employer_negative(self):
        vacancy = _vacancy(employer={"name": "Вымышленная продуктовая компания", "id": "demo-company"})
        verdict = evaluate_vacancy(vacancy)
        self.assertFalse(any("работодател" in f for f in verdict.flags))

    # ── 4. Spam vocabulary ──────────────────────────────────────────────

    def test_spam_vocabulary_positive(self):
        vacancy = _vacancy(description="Быстрый заработок без вложений, пассивный доход от дома.")
        verdict = evaluate_vacancy(vacancy)
        self.assertTrue(any("Подозрительные формулировки" in f for f in verdict.flags))
        self.assertGreater(verdict.score, WARN_THRESHOLD)

    def test_spam_vocabulary_negative(self):
        vacancy = _vacancy(description="Стабильная занятость с прозрачной системой мотивации.")
        verdict = evaluate_vacancy(vacancy)
        self.assertFalse(any("формулировки" in f for f in verdict.flags))

    # ── 5. ALL CAPS ──────────────────────────────────────────────────────

    def test_all_caps_positive(self):
        # The ported regex requires TWO separate 3+-word all-caps runs (a
        # single long run of caps words collapses into one greedy match,
        # same quirk as the original hh/scoring.py detector).
        vacancy = _vacancy(
            description="СРОЧНО ОЧЕНЬ СРОЧНО открыт набор. ЗВОНИТЕ ПРЯМО СЕЙЧАС пожалуйста.",
        )
        verdict = evaluate_vacancy(vacancy)
        self.assertTrue(any("ЗАГЛАВНЫЕ БУКВЫ" in f for f in verdict.flags))

    def test_all_caps_negative(self):
        vacancy = _vacancy(description="Обычное описание вакансии без капса.")
        verdict = evaluate_vacancy(vacancy)
        self.assertFalse(any("ЗАГЛАВНЫЕ" in f for f in verdict.flags))

    # ── 6. Excessive emoji ────────────────────────────────────────────────

    def test_excessive_emoji_positive(self):
        vacancy = _vacancy(description="Работа мечты 🔥🔥🔥🔥🔥🔥 приходи к нам")
        verdict = evaluate_vacancy(vacancy)
        self.assertTrue(any("эмодзи" in f for f in verdict.flags))

    def test_excessive_emoji_negative(self):
        vacancy = _vacancy(description="Работа мечты, приходите к нам 🙂")
        verdict = evaluate_vacancy(vacancy)
        self.assertFalse(any("эмодзи" in f for f in verdict.flags))

    # ── 7. External URLs ─────────────────────────────────────────────────

    def test_external_url_positive(self):
        vacancy = _vacancy(description="Подробнее и оплата на сайте https://totally-legit-pay.example.com")
        verdict = evaluate_vacancy(vacancy)
        self.assertTrue(any("внешние сайты" in f for f in verdict.flags))

    def test_external_url_negative_hh_link_ok(self):
        vacancy = _vacancy(description="Ссылка на вакансию: https://hh.ru/vacancy/000000000")
        verdict = evaluate_vacancy(vacancy)
        self.assertFalse(any("внешние сайты" in f for f in verdict.flags))

    # ── New: recruitment-agency vacuum postings ─────────────────────────

    def test_agency_vacuum_positive(self):
        vacancy = _vacancy(
            employer={"name": "Кадровое агентство без работодателя", "id": ""},
            description="Ищем специалистов для нашего клиента, название компании не раскрывается.",
        )
        verdict = evaluate_vacancy(vacancy)
        self.assertTrue(any("кадрового агентства" in f for f in verdict.flags))

    def test_agency_vacuum_negative_named_company(self):
        vacancy = _vacancy(employer={"name": "Вымышленная продуктовая компания", "id": "demo-company"})
        verdict = evaluate_vacancy(vacancy)
        self.assertFalse(any("кадрового агентства" in f for f in verdict.flags))

    # ── New: commission-only / no fixed salary ──────────────────────────

    def test_commission_only_positive(self):
        vacancy = _vacancy(
            salary=None,
            description="Оплата сдельная, процент от продаж, доход не ограничен.",
        )
        verdict = evaluate_vacancy(vacancy)
        self.assertTrue(any("оплату только процентом" in f for f in verdict.flags))

    def test_commission_only_negative(self):
        vacancy = _vacancy(description="Фиксированный оклад плюс квартальная премия.")
        verdict = evaluate_vacancy(vacancy)
        self.assertFalse(any("процентом" in f for f in verdict.flags))

    # ── New: unpaid internship/trial (hard reject) ──────────────────────

    def test_unpaid_internship_hard_reject(self):
        vacancy = _vacancy(description="Предлагаем неоплачиваемую стажировку на 3 месяца.")
        verdict = evaluate_vacancy(vacancy)
        self.assertEqual(verdict.verdict, "reject")
        self.assertEqual(verdict.score, 1.0)
        self.assertIsNotNone(verdict.hard_reject_reason)
        self.assertIn("Неоплачива", verdict.hard_reject_reason or "")

    def test_unpaid_internship_negative_paid(self):
        vacancy = _vacancy(description="Оплачиваемая стажировка с испытательным сроком 1 месяц.")
        verdict = evaluate_vacancy(vacancy)
        self.assertIsNone(verdict.hard_reject_reason)

    # ── New: MLM / own business framing ─────────────────────────────────

    def test_mlm_positive(self):
        vacancy = _vacancy(
            description=(
                "У нас крепкая команда единомышленников. Предлагаем работать на себя "
                "без ограничений по доходу."
            ),
        )
        verdict = evaluate_vacancy(vacancy)
        self.assertTrue(any("МЛМ" in f for f in verdict.flags))

    def test_mlm_negative_only_framing_no_income_language(self):
        # "команда единомышленников" alone (common filler in legit postings)
        # without income-without-employer language must not trigger MLM.
        vacancy = _vacancy(description="У нас дружная команда единомышленников и понятные KPI.")
        verdict = evaluate_vacancy(vacancy)
        self.assertFalse(any("МЛМ" in f for f in verdict.flags))

    # ── New: franchise / paid-training / pay-to-work ─────────────────────

    def test_pay_to_work_positive(self):
        vacancy = _vacancy(
            description="Для начала работы нужно купить стартовый набор за свой счёт.",
        )
        verdict = evaluate_vacancy(vacancy)
        self.assertTrue(any("оплата от соискателя" in f for f in verdict.flags))
        self.assertEqual(verdict.verdict, "reject")

    def test_pay_to_work_negative(self):
        vacancy = _vacancy(description="Обучение и сертификация проводятся за счёт компании.")
        verdict = evaluate_vacancy(vacancy)
        self.assertFalse(any("оплата от соискателя" in f for f in verdict.flags))

    # ── New: repost farming ───────────────────────────────────────────────

    def test_repost_farming_positive_with_recent_context(self):
        vacancy = _vacancy(id="hh:100", name="Менеджер по продажам AI-решений")
        recent = [
            _vacancy(id="hh:101", name="Менеджер по продажам AI решений"),
            _vacancy(id="hh:102", name="Менеджер по продаже AI-решений"),
            _vacancy(id="hh:103", name="Совершенно другая вакансия", employer={"name": "Другая Компания", "id": "1"}),
        ]
        verdict = evaluate_vacancy(vacancy, recent_vacancies=recent)
        self.assertTrue(any("массовую рассылку" in f for f in verdict.flags))

    def test_repost_farming_skipped_without_recent_context(self):
        vacancy = _vacancy(id="hh:100", name="Менеджер по продажам AI-решений")
        verdict = evaluate_vacancy(vacancy, recent_vacancies=None)
        self.assertFalse(any("массовую рассылку" in f for f in verdict.flags))

    def test_repost_farming_negative_different_employers(self):
        vacancy = _vacancy(id="hh:200", name="Тестовая вакансия")
        recent = [
            _vacancy(id="hh:201", name="Тестовая вакансия", employer={"name": "Вымышленная компания Б", "id": "5"}),
            _vacancy(id="hh:202", name="Тестовая вакансия", employer={"name": "Вымышленная компания В", "id": "6"}),
        ]
        verdict = evaluate_vacancy(vacancy, recent_vacancies=recent)
        self.assertFalse(any("массовую рассылку" in f for f in verdict.flags))

    # ── New: mandatory test flag (informational, not scored) ─────────────

    def test_has_test_flag_surfaces_without_affecting_score(self):
        clean = _vacancy(has_test=False)
        with_test = _vacancy(has_test=True)
        clean_verdict = evaluate_vacancy(clean)
        test_verdict = evaluate_vacancy(with_test)
        self.assertFalse(any("тест" in f for f in clean_verdict.flags))
        self.assertTrue(any("требует пройти тест" in f for f in test_verdict.flags))
        self.assertEqual(clean_verdict.score, test_verdict.score)
        self.assertEqual(test_verdict.verdict, "clean")


class TrapAggregationAndVerdictTest(unittest.TestCase):
    def test_multiple_weak_signals_compound(self):
        single = _vacancy(employer={"name": "ООО", "id": ""})  # generic employer only (0.4)
        single_score = evaluate_vacancy(single).score

        combined = _vacancy(
            employer={"name": "ООО", "id": ""},
            description=(
                "СРОЧНО ОЧЕНЬ СРОЧНО открыт набор. ЗВОНИТЕ ПРЯМО СЕЙЧАС пожалуйста."
            ),  # + all-caps (0.4)
        )
        combined_score = evaluate_vacancy(combined).score
        self.assertGreater(combined_score, single_score)

    def test_verdict_clean(self):
        vacancy = _vacancy()
        verdict = evaluate_vacancy(vacancy)
        self.assertEqual(verdict.verdict, "clean")
        self.assertLessEqual(verdict.score, WARN_THRESHOLD)

    def test_verdict_warn(self):
        # A single spam-vocabulary hit ("букмекер") lands in the 0.4-0.7 warn band.
        vacancy = _vacancy(description="Букмекерская контора ищет сотрудника на удалённую работу.")
        verdict = evaluate_vacancy(vacancy)
        self.assertGreater(verdict.score, WARN_THRESHOLD)
        self.assertLessEqual(verdict.score, REJECT_THRESHOLD)
        self.assertEqual(verdict.verdict, "warn")

    def test_verdict_reject(self):
        vacancy = _vacancy(description="Пишите в Telegram: t.me/hr_scam_bot")
        verdict = evaluate_vacancy(vacancy)
        self.assertGreater(verdict.score, REJECT_THRESHOLD)
        self.assertEqual(verdict.verdict, "reject")

    def test_trap_verdict_as_dict(self):
        verdict = TrapVerdict(score=0.5, verdict="warn", flags=["x"], hard_reject_reason=None)
        self.assertEqual(
            verdict.as_dict(),
            {"score": 0.5, "verdict": "warn", "flags": ["x"], "hard_reject_reason": None},
        )


class TrapUserListsHardRejectTest(unittest.TestCase):
    def test_stop_phrase_hard_reject(self):
        vacancy = _vacancy(description="Требуется оплата стартового пакета для трудоустройства.")
        user_lists = {"stop_phrases": ["стартового пакета"], "blocked_employers": []}
        verdict = evaluate_vacancy(vacancy, user_lists=user_lists)
        self.assertEqual(verdict.verdict, "reject")
        self.assertEqual(verdict.score, 1.0)
        self.assertIn("стартового пакета", verdict.hard_reject_reason or "")

    def test_blocked_employer_hard_reject_by_name(self):
        vacancy = _vacancy(employer={"name": "Плохая Компания", "id": "999"})
        user_lists = {"stop_phrases": [], "blocked_employers": [{"name": "Плохая Компания", "id": ""}]}
        verdict = evaluate_vacancy(vacancy, user_lists=user_lists)
        self.assertEqual(verdict.verdict, "reject")
        self.assertIsNotNone(verdict.hard_reject_reason)

    def test_blocked_employer_hard_reject_by_id(self):
        vacancy = _vacancy(employer={"name": "Новое Имя ООО", "id": "999"})
        user_lists = {"stop_phrases": [], "blocked_employers": [{"name": "Старое Имя", "id": "999"}]}
        verdict = evaluate_vacancy(vacancy, user_lists=user_lists)
        self.assertEqual(verdict.verdict, "reject")

    def test_user_lists_do_not_affect_unrelated_vacancy(self):
        vacancy = _vacancy()
        user_lists = {"stop_phrases": ["казино"], "blocked_employers": [{"name": "Другая", "id": ""}]}
        verdict = evaluate_vacancy(vacancy, user_lists=user_lists)
        self.assertEqual(verdict.verdict, "clean")
        self.assertIsNone(verdict.hard_reject_reason)


class TrapUserListStorageTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "trap_lists.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_load_defaults_when_file_missing(self):
        lists = load_user_lists(1, path=self.path)
        self.assertEqual(lists, {"stop_phrases": [], "blocked_employers": []})

    def test_add_and_load_stop_phrase(self):
        add_stop_phrase(1, "казино", path=self.path)
        add_stop_phrase(1, "ставки на спорт", path=self.path)
        lists = load_user_lists(1, path=self.path)
        self.assertEqual(lists["stop_phrases"], ["казино", "ставки на спорт"])

    def test_add_stop_phrase_deduplicates_case_insensitively(self):
        add_stop_phrase(1, "Казино", path=self.path)
        add_stop_phrase(1, "казино", path=self.path)
        lists = load_user_lists(1, path=self.path)
        self.assertEqual(lists["stop_phrases"], ["Казино"])

    def test_remove_stop_phrase(self):
        add_stop_phrase(1, "казино", path=self.path)
        removed = remove_stop_phrase(1, "казино", path=self.path)
        self.assertTrue(removed)
        self.assertEqual(load_user_lists(1, path=self.path)["stop_phrases"], [])

    def test_remove_stop_phrase_missing_returns_false(self):
        removed = remove_stop_phrase(1, "не существует", path=self.path)
        self.assertFalse(removed)

    def test_add_and_load_blocked_employer(self):
        add_blocked_employer(1, "Плохая Компания", employer_id="777", path=self.path)
        lists = load_user_lists(1, path=self.path)
        self.assertEqual(lists["blocked_employers"], [{"name": "Плохая Компания", "id": "777"}])

    def test_remove_blocked_employer(self):
        add_blocked_employer(1, "Плохая Компания", path=self.path)
        removed = remove_blocked_employer(1, "Плохая Компания", path=self.path)
        self.assertTrue(removed)
        self.assertEqual(load_user_lists(1, path=self.path)["blocked_employers"], [])

    def test_lists_are_per_user(self):
        add_stop_phrase(1, "казино", path=self.path)
        add_stop_phrase(2, "форекс", path=self.path)
        self.assertEqual(load_user_lists(1, path=self.path)["stop_phrases"], ["казино"])
        self.assertEqual(load_user_lists(2, path=self.path)["stop_phrases"], ["форекс"])

    def test_end_to_end_with_evaluate_vacancy(self):
        add_stop_phrase(42, "стартового пакета", path=self.path)
        add_blocked_employer(42, "Плохая Компания", path=self.path)
        user_lists = load_user_lists(42, path=self.path)

        blocked_vacancy = _vacancy(employer={"name": "Плохая Компания", "id": ""})
        verdict = evaluate_vacancy(blocked_vacancy, user_lists=user_lists)
        self.assertEqual(verdict.verdict, "reject")

        clean_vacancy = _vacancy()
        verdict = evaluate_vacancy(clean_vacancy, user_lists=user_lists)
        self.assertEqual(verdict.verdict, "clean")


if __name__ == "__main__":
    unittest.main()
