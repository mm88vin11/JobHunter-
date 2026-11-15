"""Gates on daily follow-up — the parts that decide who gets written to.

Everything here is the decision layer: whether 24 hours elapsed, which threads
are permanently out, and whether today's idempotent touch was already sent.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from candidate.automation import AutomationStore

from mvp.followup import (
    FollowupStep,
    STEPS,
    _STEPS_BY_KEY,
    _conversation_blocker,
    _message_already_sent,
    _supported_numbers,
    _thread_blocker,
    _due_card,
    in_send_window,
    message_problem,
    next_step,
    outgrown_steps,
    status_message,
    send_followup,
)
from mvp import config

from mvp.bump import LOCAL_TZ
NOW = datetime(2026, 8, 12, 11, 0, tzinfo=timezone.utc)


def _thread(**overrides) -> dict:
    item = {
        "id": "negotiation-demo",
        "state": {"id": "response", "name": "Отклик"},
        "created_at": "2026-08-11T11:25:46+0300",
        "messaging_status": "ok",
        "viewed_by_opponent": False,
        "vacancy": {"id": "vacancy-demo", "name": "Backend-разработчик"},
        "resume": {"id": "resume-demo"},
    }
    item.update(overrides)
    return item


class SingleProgressionTest(unittest.TestCase):
    def test_day_one_thread_gets_single_touch(self):
        created = NOW - timedelta(days=1, hours=2)
        step = next_step(created, {}, NOW)
        self.assertEqual(step.key, "auto_24h")

    def test_fresh_thread_is_not_due_yet(self):
        created = NOW - timedelta(hours=5)
        self.assertIsNone(next_step(created, {}, NOW))

    def test_first_touch_waits_the_full_24_hours(self):
        created = NOW - timedelta(hours=23, minutes=59)
        self.assertIsNone(next_step(created, {}, NOW))

    def test_old_unresolved_thread_gets_only_touch(self):
        created = NOW - timedelta(days=3, hours=1)
        step = next_step(created, {}, NOW)
        self.assertEqual(step.key, "auto_24h")
        self.assertEqual(outgrown_steps(step, {}), [])

    def test_sent_touch_is_never_re_offered(self):
        created = NOW - timedelta(days=3, hours=1)
        self.assertIsNone(next_step(created, {"auto_24h": "sent"}, NOW))

    def test_skipped_touch_is_terminal(self):
        created = NOW - timedelta(days=1, hours=2)
        self.assertIsNone(next_step(created, {"auto_24h": "skipped"}, NOW))

    def test_explicit_stop_ends_the_sequence(self):
        created = NOW - timedelta(days=90)
        self.assertIsNone(next_step(created, {"auto_24h": "stopped"}, NOW))

    def test_a_new_day_does_not_create_another_touch(self):
        created = NOW - timedelta(days=3)
        self.assertIsNone(next_step(created, {"auto_24h": "sent"}, NOW + timedelta(days=1)))

    def test_pending_owner_confirmation_blocks_duplicate(self):
        created = NOW - timedelta(days=3)
        pending = {"auto_24h": "offered"}
        self.assertIsNone(next_step(created, pending, NOW))

    def test_transient_failure_can_retry(self):
        created = NOW - timedelta(days=1, minutes=1)
        self.assertEqual(next_step(created, {"auto_24h": "failed"}, NOW).key, "auto_24h")


class ThreadBlockerTest(unittest.TestCase):
    def test_rejection_thread_is_blocked(self):
        blocked = _thread_blocker(_thread(state={"id": "discard"}), NOW)
        self.assertEqual(blocked, "invitation_or_discard")

    def test_invitation_thread_is_blocked(self):
        blocked = _thread_blocker(_thread(state={"id": "invitation"}), NOW)
        self.assertEqual(blocked, "invitation_or_discard")

    def test_thread_hh_closed_for_messaging_is_blocked(self):
        blocked = _thread_blocker(_thread(messaging_status="no_invitation"), NOW)
        self.assertEqual(blocked, "messaging_closed")

    def test_old_unresolved_application_is_in_scope_by_default(self):
        blocked = _thread_blocker(_thread(created_at="2026-04-13T17:23:02+0300"), NOW)
        self.assertEqual(blocked, "")

    def test_live_august_response_passes(self):
        self.assertEqual(_thread_blocker(_thread(), NOW), "")


class SendWindowTest(unittest.TestCase):
    def test_weekday_business_hours_open(self):
        self.assertTrue(in_send_window(datetime(2026, 8, 12, 11, 0, tzinfo=LOCAL_TZ)))

    def test_saturday_business_hours_open(self):
        self.assertTrue(in_send_window(datetime(2026, 8, 15, 11, 0, tzinfo=LOCAL_TZ)))

    def test_late_evening_closed(self):
        self.assertFalse(in_send_window(datetime(2026, 8, 12, 22, 0, tzinfo=LOCAL_TZ)))

    def test_early_morning_closed(self):
        self.assertFalse(in_send_window(datetime(2026, 8, 12, 7, 0, tzinfo=LOCAL_TZ)))


VACANCY = {
    "name": "Разработчик интеграций",
    "employer_name": "Вымышленная компания",
    "description": (
        "Разработка внутренних сервисов на Python, интеграции с внешними API, "
        "SQL и автоматизация тестирования."
    ),
    "key_skills": ["Python", "API", "SQL"],
}
COVER = "В демонстрационном проекте реализовал сервис на Python с внешним API."
CORPUS = "Вымышленный пример: обработано 42 тестовые записи и выполнено 7 сценариев."


def _problem(text: str, step_key: str = "proof") -> str:
    step = FollowupStep(
        key=step_key, day=1, title="test", intent="test",
        must_ask=step_key == "question",
    )
    return message_problem(
        text, step=step, vacancy=VACANCY,
        cover_letter=COVER, corpus=CORPUS,
    )


class DraftValidationTest(unittest.TestCase):
    def test_clean_draft_passes(self):
        text = (
            "Забыл упомянуть про Python: интеграцию с внешним API я вынес в отдельный "
            "сервис и закрыл автоматическими тестами. Если у вас похожая схема, покажу, "
            "как разделил сетевой слой и бизнес-логику."
        )
        self.assertEqual(_problem(text), "")

    def test_too_short_is_rejected(self):
        self.assertTrue(_problem("Здравствуйте, Python!").startswith("length_"))

    def test_empty_reminder_is_rejected(self):
        text = (
            "Напоминаю о себе по вакансии, связанной с Python и внешними API, буду "
            "признателен за любую обратную связь по моему отклику на эту позицию сегодня."
        )
        self.assertEqual(_problem(text), "empty_reminder")

    def test_question_rung_must_actually_ask(self):
        text = (
            "По Python и внешним API у вас, судя по описанию, уже есть контур интеграции "
            "с внешними API, и я вижу там ровно ту развилку, которую проходил сам недавно."
        )
        self.assertEqual(_problem(text, "question"), "no_question")

    def test_question_rung_passes_with_a_question(self):
        text = (
            "По описанию не понял одну вещь про Python: приложение ходит во "
            "внешние API напрямую или через отдельный сервис-прослойку? "
            "От этого сильно зависит, как я бы разложил интеграции на первом этапе."
        )
        self.assertEqual(_problem(text, "question"), "")

    def test_draft_that_repeats_the_cover_letter_is_rejected(self):
        text = COVER + " " + COVER
        self.assertIn(_problem(text), {"repeats_cover_letter", f"length_{len(text)}"})

    def test_draft_with_no_vacancy_terms_is_rejected(self):
        text = (
            "Хотел добавить пару слов о своём опыте вообще: люблю доводить начатое до "
            "конца, быстро вникаю в новое и спокойно отношусь к правкам от заказчика."
        )
        self.assertEqual(_problem(text), "no_vacancy_terms")

    def test_invented_number_is_rejected(self):
        text = (
            "Сервис на Python обслуживает 98765 запросов в сутки, "
            "интеграции с внешними API держат нагрузку без деградации по времени ответа."
        )
        self.assertEqual(_problem(text), "unsupported_number")


class SupportedNumbersTest(unittest.TestCase):
    def test_number_present_in_corpus_is_allowed(self):
        self.assertTrue(_supported_numbers("обработано 42 тестовые записи", CORPUS))

    def test_spaced_thousands_match_their_unspaced_source(self):
        self.assertTrue(_supported_numbers("выполнено 7 сценариев", CORPUS))

    def test_absent_number_is_refused(self):
        self.assertFalse(_supported_numbers("обслуживает 98765 запросов", CORPUS))

    def test_text_without_numbers_is_fine(self):
        self.assertTrue(_supported_numbers("без единой цифры", CORPUS))

    def test_tool_names_containing_digits_are_not_claims(self):
        """n8n / gpt-4o / S3 are the vacancy's own vocabulary, not invented figures.

        Reading the 8 in n8n as an unsupported number failed every honest draft
        that answered the posting in the posting's own terms.
        """
        for identifier in ("n8n", "gpt-4o", "S3", "Web3", "ClickHouse24"):
            with self.subTest(identifier=identifier):
                self.assertTrue(_supported_numbers(f"работал с {identifier} в проде", CORPUS))


class FollowupStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AutomationStore(Path(self.tmp.name) / "db.sqlite3")
        self.addCleanup(self.tmp.cleanup)

    def test_offered_then_sent_is_recorded_once(self):
        self.store.record_followup(
            user_id=1, negotiation_id="n1", step="proof", status="offered", message="draft",
        )
        self.assertEqual(self.store.pending_followup(1, "n1", "proof")["message"], "draft")
        self.store.record_followup(
            user_id=1, negotiation_id="n1", step="proof", status="sent", message="draft",
        )
        self.assertEqual(self.store.followup_steps(1, "n1"), {"proof": "sent"})
        self.assertIsNone(self.store.pending_followup(1, "n1", "proof"))
        self.assertEqual(self.store.followups_sent_today(1), 1)

    def test_a_sent_rung_is_never_downgraded(self):
        """A late cycle must not turn a delivered message back into a draft.

        Without the guard the owner could be shown — and could press — a card for
        a message the recruiter has already received.
        """
        self.store.record_followup(user_id=1, negotiation_id="n1", step="proof", status="sent")
        self.store.record_followup(
            user_id=1, negotiation_id="n1", step="proof", status="offered", message="second",
        )
        self.assertEqual(self.store.followup_steps(1, "n1"), {"proof": "sent"})

    def test_stop_closes_every_remaining_rung(self):
        self.store.record_followup(user_id=1, negotiation_id="n1", step="proof", status="sent")
        self.store.stop_followups(1, "n1", ["auto_24h"], "employer_replied")
        done = self.store.followup_steps(1, "n1")
        self.assertEqual(done["proof"], "sent")
        self.assertEqual(done["auto_24h"], "stopped")
        created = NOW - timedelta(days=30)
        self.assertIsNone(next_step(created, done, NOW))

    def test_daily_count_ignores_other_users(self):
        self.store.record_followup(user_id=1, negotiation_id="n1", step="proof", status="sent")
        self.store.record_followup(user_id=2, negotiation_id="n2", step="proof", status="sent")
        self.assertEqual(self.store.followups_sent_today(1), 1)

class StatusMessageTest(unittest.TestCase):
    def test_message_asks_if_the_vacancy_is_current(self):
        text = status_message("Backend-разработчик")
        self.assertIn("вакансия «Backend-разработчик» ещё актуальна?", text)
        self.assertIn("отклик в силе", text)
        self.assertIn("готов обсудить позицию", text)
        self.assertNotIn("опыт", text.lower())

    def test_hh_send_fails_closed_without_owner_confirmation(self):
        reason = asyncio.run(send_followup(1, "n1", "auto_24h", "draft"))
        self.assertEqual(reason, "owner_confirmation_required")


class ConversationGuardTest(unittest.TestCase):
    def test_initial_cover_letter_is_not_a_conversation(self):
        messages = [{
            "text": "cover", "created_at": (NOW + timedelta(seconds=1)).isoformat(),
            "author": {"participant_type": "applicant"},
        }]
        self.assertEqual(_conversation_blocker(messages, NOW), "")

    def test_later_applicant_message_blocks_automation(self):
        messages = [{
            "text": "manual follow-up", "created_at": (NOW + timedelta(minutes=6)).isoformat(),
            "author": {"participant_type": "applicant"},
        }]
        self.assertEqual(_conversation_blocker(messages, NOW), "applicant_messaged")

    def test_employer_reply_blocks_automation(self):
        messages = [{
            "text": "reply", "created_at": (NOW + timedelta(minutes=1)).isoformat(),
            "author": {"participant_type": "employer"},
        }]
        self.assertEqual(_conversation_blocker(messages, NOW), "employer_replied")

    def test_exact_existing_message_recovers_after_restart(self):
        messages = [{"text": "  Уже   отправлено ", "author": {"participant_type": "applicant"}}]
        self.assertTrue(_message_already_sent(messages, "уже отправлено"))


class DailyDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AutomationStore(Path(self.tmp.name) / "db.sqlite3")
        self.addCleanup(self.tmp.cleanup)

    def test_due_touch_is_only_offered_for_owner_confirmation(self):
        class Client:
            @staticmethod
            def vacancy(_vacancy_id):
                return {"name": "Backend-разработчик", "archived": False}

        item = _thread(created_at=(NOW - timedelta(hours=25)).isoformat())
        with mock.patch.object(config, "AUTOMATION", self.store), \
                mock.patch("mvp.followup._fetch_negotiation_messages", new=mock.AsyncMock(return_value=[])), \
                mock.patch("mvp.followup._safe_send", new=mock.AsyncMock(return_value=object())) as offer, \
                mock.patch("mvp.followup.send_followup", new=mock.AsyncMock()) as send:
            self.assertTrue(asyncio.run(_due_card(None, 1, Client(), item, NOW)))
            self.assertFalse(asyncio.run(_due_card(None, 1, Client(), item, NOW)))
        send.assert_not_called()
        offer.assert_awaited_once()
        pending = self.store.pending_followup(1, item["id"], "auto_24h")
        self.assertIsNotNone(pending)
        self.assertIn("вакансия «Backend-разработчик» ещё актуальна?", pending["message"])

    def test_sent_touch_is_never_repeated(self):
        item = _thread(created_at=(NOW - timedelta(days=3)).isoformat())
        self.store.record_followup(
            user_id=1, negotiation_id=item["id"], step="auto_24h",
            status="sent", message="sent yesterday",
        )

        class Client:
            @staticmethod
            def vacancy(_vacancy_id):
                return {"name": "Backend-разработчик", "archived": False}

        with mock.patch.object(config, "AUTOMATION", self.store), \
                mock.patch("mvp.followup.send_followup") as send:
            self.assertFalse(asyncio.run(_due_card(None, 1, Client(), item, NOW)))
        send.assert_not_called()

    def test_auto_mode_sends_without_owner_card(self):
        class Client:
            @staticmethod
            def vacancy(_vacancy_id):
                return {"name": "Backend-разработчик", "archived": False}

        item = _thread(created_at=(NOW - timedelta(hours=24, minutes=1)).isoformat())
        with mock.patch.object(config, "AUTOMATION", self.store), \
                mock.patch.dict("os.environ", {"FOLLOWUP_AUTO_SEND": "1"}), \
                mock.patch("mvp.followup._fetch_negotiation_messages", new=mock.AsyncMock(return_value=[])), \
                mock.patch("mvp.followup._safe_send", new=mock.AsyncMock(return_value=object())) as notify, \
                mock.patch("mvp.followup.send_followup", new=mock.AsyncMock(return_value="")) as send:
            self.assertTrue(asyncio.run(_due_card(None, 1, Client(), item, NOW)))
        send.assert_awaited_once()
        self.assertTrue(send.await_args.kwargs["automated"])
        notify.assert_awaited_once()

    def test_auto_mode_waits_when_thread_history_is_unavailable(self):
        class Client:
            @staticmethod
            def vacancy(_vacancy_id):
                return {"name": "Backend-разработчик", "archived": False}

        item = _thread(created_at=(NOW - timedelta(hours=24, minutes=1)).isoformat())
        with mock.patch.object(config, "AUTOMATION", self.store), \
                mock.patch.dict("os.environ", {"FOLLOWUP_AUTO_SEND": "1"}), \
                mock.patch("mvp.followup._fetch_negotiation_messages", new=mock.AsyncMock(return_value=None)), \
                mock.patch("mvp.followup.send_followup", new=mock.AsyncMock()) as send:
            self.assertFalse(asyncio.run(_due_card(None, 1, Client(), item, NOW)))
        send.assert_not_awaited()

if __name__ == "__main__":
    unittest.main()
