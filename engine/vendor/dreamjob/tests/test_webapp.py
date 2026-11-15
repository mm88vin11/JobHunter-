"""Tests for the Telegram Mini App surface in `mvp/webapp.py`.

Two things are worth pinning here and nothing else is. First, authorization:
this is the only part of the bot reachable over HTTP, so every way of getting
in without the owner's Telegram client must be closed. Second, agreement with
the chat screen: the Mini App must bucket threads exactly like
`candidate.automation.crm_bucket` does, or the two screens start telling the
owner different things about the same conversation.

Same tempdir + VariantStore + AutomationStore setup as `tests/test_crm_store.py`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from aiohttp.test_utils import AioHTTPTestCase

from candidate.automation import AutomationStore
from candidate.storage import VariantStore

from mvp.keyboards import _main_keyboard
from mvp.webapp import build_app

TOKEN = "123456:TEST-TOKEN-NOT-A-REAL-ONE"
OWNER = 7
STRANGER = 999


def _init_data(*, token: str = TOKEN, user_id: int = OWNER, age: timedelta = timedelta(0)) -> str:
    """Forge init data the way Telegram signs it, so tests exercise the real HMAC."""
    auth_date = datetime.now(timezone.utc) - age
    payload = {
        "query_id": "AAA",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}),
        "auth_date": str(int(auth_date.timestamp())),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    payload["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(payload)


def _message(message_id: str, *, text: str, created_at: str, employer: bool) -> dict:
    return {
        "id": message_id, "text": text, "created_at": created_at,
        "author": {"participant_type": "employer" if employer else "applicant"},
    }


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class WebAppTestCase(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "automation.sqlite3"
        VariantStore(self.db_path)
        self.store = AutomationStore(self.db_path)
        self.addCleanup(self.tmp.cleanup)
        self._seed()
        # Store and settings are injected, never patched onto `mvp.config`:
        # rebinding that singleton leaked into other test modules. The settings
        # file does not exist, so the silence threshold is the documented
        # default rather than whatever this machine's owner configured.
        return build_app(
            bot_token=TOKEN, owner_id=OWNER, store=self.store,
            settings_path=Path(self.tmp.name) / "mvp_settings.json",
        )

    def _seed(self) -> None:
        # Employer asked a question -> the owner owes a reply.
        self.store.store_negotiation_messages(OWNER, "n-reply", [
            _message("m1", text="Расскажите про опыт с n8n?", created_at=_days_ago(2), employer=True),
        ])
        self.store.recompute_thread_state(
            OWNER, "n-reply", vacancy_name="AI-инженер",
            employer_name="Ромашка", hh_state="response",
        )
        # Applied, nobody answered -> waiting, and silent long enough to be overdue.
        self.store.store_negotiation_messages(OWNER, "n-wait", [
            _message("m2", text="Здравствуйте, откликаюсь", created_at=_days_ago(9), employer=False),
        ])
        self.store.recompute_thread_state(
            OWNER, "n-wait", vacancy_name="Продакт", employer_name="Тюльпан", hh_state="response",
        )
        # Rejected -> must never sit in the work queues.
        self.store.store_negotiation_messages(OWNER, "n-no", [
            _message("m3", text="К сожалению, отказ", created_at=_days_ago(1), employer=True),
        ])
        self.store.recompute_thread_state(
            OWNER, "n-no", vacancy_name="Аналитик", employer_name="Кактус", hh_state="discard",
        )

    def _auth(self, init_data: str | None = None) -> dict[str, str]:
        return {"Authorization": "tma " + (init_data if init_data is not None else _init_data())}


class AuthorizationTest(WebAppTestCase):
    async def test_valid_owner_init_data_is_accepted(self):
        response = await self.client.get("/api/crm", headers=self._auth())
        self.assertEqual(response.status, 200)

    async def test_request_without_init_data_is_rejected(self):
        response = await self.client.get("/api/crm")
        self.assertEqual(response.status, 401)

    async def test_init_data_signed_with_another_token_is_rejected(self):
        forged = _init_data(token="999999:SOMEONE-ELSES-BOT")
        response = await self.client.get("/api/crm", headers=self._auth(forged))
        self.assertEqual(response.status, 401)

    async def test_tampered_user_id_is_rejected(self):
        # Swap the user id inside otherwise valid init data: the hash no longer
        # matches, which is the whole point of signing the payload.
        tampered = _init_data().replace("%22id%22%3A+7", "%22id%22%3A+999")
        response = await self.client.get("/api/crm", headers=self._auth(tampered))
        self.assertEqual(response.status, 401)

    async def test_correctly_signed_but_stale_init_data_is_rejected(self):
        stale = _init_data(age=timedelta(days=3))
        response = await self.client.get("/api/crm", headers=self._auth(stale))
        self.assertEqual(response.status, 401)

    async def test_correctly_signed_stranger_is_rejected(self):
        # Signed by the real bot token, so the HMAC passes -- only the owner
        # check stands between another Telegram account and the owner's CRM.
        other = _init_data(user_id=STRANGER)
        response = await self.client.get("/api/crm", headers=self._auth(other))
        self.assertEqual(response.status, 401)

    async def test_thread_endpoint_is_authorized_too(self):
        response = await self.client.get("/api/thread/n-reply")
        self.assertEqual(response.status, 401)


class CrmPayloadTest(WebAppTestCase):
    async def test_groups_match_the_chat_screen_buckets(self):
        payload = await (await self.client.get("/api/crm", headers=self._auth())).json()
        groups = payload["groups"]
        self.assertEqual([t["id"] for t in groups["reply"]], ["n-reply"])
        self.assertEqual([t["id"] for t in groups["waiting"]], ["n-wait"])
        self.assertEqual([t["id"] for t in groups["rejected"]], ["n-no"])

    async def test_long_silence_is_flagged_overdue_and_rejections_are_not(self):
        payload = await (await self.client.get("/api/crm", headers=self._auth())).json()
        self.assertTrue(payload["groups"]["waiting"][0]["overdue"])
        self.assertFalse(payload["groups"]["rejected"][0]["overdue"])

    async def test_counters_come_from_the_store_not_from_the_page(self):
        payload = await (await self.client.get("/api/crm", headers=self._auth())).json()
        expected = self.store.crm_counters(OWNER, silence_days=payload["silence_days"])
        self.assertEqual(payload["counters"], expected)

    async def test_archived_threads_are_hidden(self):
        # Regression guard: `archived_at` holds an
        # empty string, not NULL, so any raw `IS NULL` filter here would return
        # nothing at all rather than merely leaking an archived row.
        self.store.archive_thread(OWNER, "n-wait", "проверка")
        payload = await (await self.client.get("/api/crm", headers=self._auth())).json()
        listed = [t["id"] for group in payload["groups"].values() for t in group]
        self.assertNotIn("n-wait", listed)
        self.assertIn("n-reply", listed)

    async def test_thread_view_returns_the_message_history(self):
        payload = await (await self.client.get("/api/thread/n-reply", headers=self._auth())).json()
        self.assertEqual(payload["thread"]["employer"], "Ромашка")
        self.assertEqual([m["direction"] for m in payload["messages"]], ["incoming"])
        self.assertIn("n8n", payload["messages"][0]["text"])

    async def test_unknown_thread_is_a_404_not_an_empty_page(self):
        response = await self.client.get("/api/thread/does-not-exist", headers=self._auth())
        self.assertEqual(response.status, 404)

    async def test_index_serves_the_shell_without_leaking_data(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertIn("telegram-web-app.js", body)
        # The shell is static: no thread ever ships inside the HTML itself.
        self.assertNotIn("Ромашка", body)


class OpenAppButtonTest(unittest.TestCase):
    """The main menu grows an «Открыть приложение» button only when it works."""

    def _labels(self) -> list[str]:
        return [button.text for row in _main_keyboard().inline_keyboard for button in row]

    def test_button_appears_when_a_public_url_is_configured(self):
        with mock.patch.dict(os.environ, {"WEBAPP_PUBLIC_URL": "https://example.org"}):
            labels = self._labels()
        self.assertIn("🗂 Открыть приложение", labels)
        self.assertIn("📇 CRM", labels)  # nothing displaced

    def test_no_button_without_a_url(self):
        with mock.patch.dict(os.environ, {"WEBAPP_PUBLIC_URL": ""}):
            self.assertNotIn("🗂 Открыть приложение", self._labels())

    def test_no_button_for_a_plain_http_url(self):
        # Telegram refuses non-https, and it only fails when pressed -- a button
        # that looks fine and dies in the owner's hands is worse than no button.
        with mock.patch.dict(os.environ, {"WEBAPP_PUBLIC_URL": "http://example.org"}):
            self.assertNotIn("🗂 Открыть приложение", self._labels())


class ReadOnlyTest(WebAppTestCase):
    async def test_serving_the_screens_does_not_write_to_the_database(self):
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        await self.client.get("/api/crm", headers=self._auth())
        await self.client.get("/api/thread/n-reply", headers=self._auth())
        await self.client.get("/", headers=self._auth())
        after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)
