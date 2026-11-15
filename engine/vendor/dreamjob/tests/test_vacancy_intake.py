from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from mvp import automation
from mvp.routers import vacancies


class VacancyLinkIntakeTest(unittest.TestCase):
    def test_plain_hh_link_loads_the_official_description(self):
        message = mock.Mock()
        message.text = "https://hh.ru/vacancy/000000000?hhtmFrom=share"
        message.caption = None
        message.forward_origin = None
        message.chat.id = 7
        message.answer = mock.AsyncMock()

        client = mock.Mock()
        client.vacancy.return_value = {
            "id": "000000000",
            "name": "Демонстрационная вакансия",
            "alternate_url": "https://hh.ru/vacancy/000000000",
            "description": (
                "<p>Внедрение AI и автоматизация бизнес-процессов в компании. "
                "Интеграции через API, анализ требований и подготовка документации.</p>"
            ),
            "employer": {"name": "Вымышленная компания"},
            "area": {"name": "Тестовый регион"},
            "employment": {"id": "full"},
            "schedule": {"id": "fullDay"},
        }

        with mock.patch.object(
            vacancies, "_hh_client_for", mock.AsyncMock(return_value=(client, {})),
        ), mock.patch.object(vacancies, "_generate_and_show", mock.AsyncMock()) as generate:
            asyncio.run(vacancies.vacancy_text(message))

        generated = generate.await_args.args[1]
        self.assertEqual(generated["external_id"], "000000000")
        self.assertEqual(generated["name"], "Демонстрационная вакансия")
        self.assertGreater(len(generated["description"]), 40)
        client.vacancy.assert_called_once_with("000000000")


class DiscoveryPriorityTest(unittest.TestCase):
    def test_discovery_limit_is_applied_after_ranking(self):
        items = [
            {"id": "low", "published_at": "2026-08-12T10:00:00+0300", "test_score": 20,
             "discovery_bonus": 70},
            {"id": "best", "published_at": "2026-08-10T10:00:00+0300", "test_score": 80},
            {"id": "mid", "published_at": "2026-08-12T11:00:00+0300", "test_score": 60},
        ]
        clean = SimpleNamespace(verdict="clean")

        def choose(vacancy, *_args, **_kwargs):
            verdict = SimpleNamespace(total=vacancy["test_score"], hard_reject=False)
            return verdict, {}

        with mock.patch.object(automation, "evaluate_vacancy", return_value=clean), \
                mock.patch.object(automation, "choose_profile", side_effect=choose):
            selected = automation._prioritize_vacancies(
                items, profiles={"profile": {}}, user_lists={}, limit=2,
                include_query_bonus=True,
            )
        self.assertEqual([item["id"] for item in selected], ["low", "best"])

    def test_hydration_reserves_exact_title_search_results(self):
        items = [
            {
                "id": str(index), "external_id": str(index), "name": "AI role",
                "description": "AI automation", "published_at": str(index),
                "reserve_hydration": index < 3, "test_score": 50,
            }
            for index in range(8)
        ]
        clean = SimpleNamespace(verdict="clean")

        def choose(vacancy, *_args, **_kwargs):
            return SimpleNamespace(total=vacancy["test_score"], hard_reject=False), {}

        with mock.patch.object(automation, "evaluate_vacancy", return_value=clean), \
                mock.patch.object(automation, "choose_profile", side_effect=choose):
            selected = automation._select_hydration_candidates(
                items, profiles={"profile": {}}, user_lists={}, limit=4, reserved_limit=2,
            )
        self.assertEqual([item["external_id"] for item in selected[:2]], ["0", "1"])
        self.assertEqual(len(selected), 4)


if __name__ == "__main__":
    unittest.main()
