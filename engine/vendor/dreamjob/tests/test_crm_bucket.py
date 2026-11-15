"""Bucketing tests for `candidate.automation.crm_bucket`.

`crm_bucket` is the single place that decides which of the five CRM groups a
conversation belongs to, and both surfaces render from it -- the chat screen
(`mvp/routers/crm.py`) and the Mini App (`mvp/webapp.py`). It was the only
piece of CRM logic with no direct tests, which is how the states below stayed
mis-filed: hh.ru moves a thread to `interview`/`hired`, but the function only
ever branched on `discard`/`invitation`/`response`, so a booked interview fell
through to "waiting" and started accruing days of silence.
"""
from __future__ import annotations

import unittest

from candidate.automation import crm_bucket


def _thread(*, state: str, direction: str = "", preview: str = "") -> dict:
    return {
        "hh_state": state,
        "last_message_direction": direction,
        "last_message_preview": preview,
    }


class CommittedStatesTest(unittest.TestCase):
    """A set date or a won job is never a thread waiting on the employer."""

    def test_booked_interview_awaiting_nobody_is_scheduled_not_waiting(self):
        # The regression this file was written for: the owner answered last, so
        # direction is outgoing, and the thread used to land in "Ждём
        # работодателя" -- where the silence counter runs and the archiver
        # eventually files a real, booked interview away as dead.
        thread = _thread(state="interview", direction="outgoing", preview="Спасибо, буду во вторник")
        self.assertEqual(crm_bucket(thread), "scheduled")

    def test_won_job_is_scheduled_not_waiting(self):
        thread = _thread(state="hired", direction="outgoing", preview="Принимаю оффер")
        self.assertEqual(crm_bucket(thread), "scheduled")

    def test_template_acknowledgement_does_not_drag_a_booked_interview_back_to_waiting(self):
        thread = _thread(state="interview", direction="incoming", preview="Мы свяжемся с вами позднее")
        self.assertEqual(crm_bucket(thread), "scheduled")

    def test_a_real_question_still_needs_an_answer_even_with_a_date_set(self):
        thread = _thread(state="interview", direction="incoming", preview="Какой формат вам удобнее?")
        self.assertEqual(crm_bucket(thread), "reply")

    def test_a_test_assignment_still_outranks_the_booked_date(self):
        thread = _thread(state="interview", direction="incoming", preview="Пройдите тестовое задание")
        self.assertEqual(crm_bucket(thread), "task")


class UnchangedBehaviourTest(unittest.TestCase):
    """The states that already worked must keep working."""

    def test_rejection_wins_over_everything(self):
        thread = _thread(state="discard", direction="incoming", preview="Какой у вас опыт?")
        self.assertEqual(crm_bucket(thread), "rejected")

    def test_application_with_no_answer_is_waiting(self):
        thread = _thread(state="response", direction="outgoing", preview="Откликаюсь на вакансию")
        self.assertEqual(crm_bucket(thread), "waiting")

    def test_template_acknowledgement_is_not_a_reply_to_write(self):
        thread = _thread(state="response", direction="incoming", preview="Мы свяжемся с вами")
        self.assertEqual(crm_bucket(thread), "waiting")

    def test_direct_question_needs_a_reply(self):
        thread = _thread(state="response", direction="incoming", preview="Какой у вас опыт с n8n?")
        self.assertEqual(crm_bucket(thread), "reply")

    def test_invitation_with_no_messages_yet_is_a_task(self):
        self.assertEqual(crm_bucket(_thread(state="invitation")), "task")

    def test_scheduling_agreement_is_scheduled(self):
        thread = _thread(state="response", direction="incoming", preview="Договорились, до связи")
        self.assertEqual(crm_bucket(thread), "scheduled")


if __name__ == "__main__":
    unittest.main()
