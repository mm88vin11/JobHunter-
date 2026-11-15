"""Tests for the settings hub: mvp/settings_store.py (pure persistence logic)
and the FSM/callback flows in mvp/routers/settings.py.

Every test that touches persistence uses an explicit tempdir path (or patches
the module's default-path helpers to point at one) so nothing here ever
reads or writes the real var/candidate_trap_lists.json, var/mvp_settings.json,
or var/candidate_mvp.sqlite3 used by the running bot.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from candidate.automation import AutomationStore

from mvp import settings_store as store
from mvp.routers import settings as settings_router


class SearchQueriesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mvp_settings.json"
        # HH_SEARCH_QUERIES is a real env var mvp/hh_sync.py reads; make sure
        # tests never leak a value into the rest of the suite.
        self._original_env = os.environ.pop("HH_SEARCH_QUERIES", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._original_env is None:
            os.environ.pop("HH_SEARCH_QUERIES", None)
        else:
            os.environ["HH_SEARCH_QUERIES"] = self._original_env

    def test_default_query_list_dedups_case_insensitively_and_preserves_order(self):
        result = store.default_query_list(["n8n", "Тестовый профиль"])
        self.assertEqual(result[0], "n8n")
        self.assertEqual(result.count("n8n"), 1)  # "n8n" is also a hardcoded extra
        self.assertIn("Тестовый профиль", result)

    def test_no_override_by_default(self):
        self.assertIsNone(store.get_search_queries(1, path=self.path))
        self.assertNotIn("HH_SEARCH_QUERIES", os.environ)

    def test_add_search_query_seeds_from_defaults_and_syncs_env(self):
        profiles = ["Тестовый профиль"]
        result = store.add_search_query(1, "Тестовый запрос", profiles, path=self.path)
        self.assertIn("Тестовый запрос", result)
        self.assertIn("Тестовый профиль", result)
        self.assertEqual(os.environ["HH_SEARCH_QUERIES"], "|".join(result))
        # Persisted to disk too, not just process env.
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["1"]["search_queries"], result)

    def test_add_search_query_is_idempotent(self):
        profiles: list[str] = []
        store.add_search_query(1, "n8n", profiles, path=self.path)
        result = store.add_search_query(1, "n8n", profiles, path=self.path)
        self.assertEqual(result.count("n8n"), 1)

    def test_remove_search_query(self):
        profiles: list[str] = []
        store.add_search_query(1, "Custom Query", profiles, path=self.path)
        result = store.remove_search_query(1, "Custom Query", profiles, path=self.path)
        self.assertNotIn("Custom Query", result)

    def test_remove_search_query_can_empty_the_list(self):
        # Deliberately not blocked: an empty override is a legitimate (if
        # unhelpful) owner choice, surfaced as a warning in the UI layer.
        result = store.set_search_queries(1, [], path=self.path)
        self.assertEqual(result, [])
        self.assertNotIn("HH_SEARCH_QUERIES", os.environ)

    def test_reset_clears_override_and_env(self):
        store.add_search_query(1, "Custom Query", [], path=self.path)
        self.assertIsNotNone(store.get_search_queries(1, path=self.path))
        store.reset_search_queries(1, path=self.path)
        self.assertIsNone(store.get_search_queries(1, path=self.path))
        self.assertNotIn("HH_SEARCH_QUERIES", os.environ)

    def test_apply_persisted_search_queries_survives_a_simulated_restart(self):
        store.add_search_query(1, "Custom Query", [], path=self.path)
        saved = store.get_search_queries(1, path=self.path)
        # Simulate a process restart: env var is gone, only the file remains.
        os.environ.pop("HH_SEARCH_QUERIES", None)
        store.apply_persisted_search_queries(1, path=self.path)
        self.assertEqual(os.environ["HH_SEARCH_QUERIES"].split("|"), saved)

    def test_apply_persisted_search_queries_noop_when_never_configured(self):
        store.apply_persisted_search_queries(999, path=self.path)
        self.assertNotIn("HH_SEARCH_QUERIES", os.environ)

    def test_write_is_atomic_and_recovers_from_corrupt_file(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        # A corrupt file must not crash reads/writes -- treated as empty.
        self.assertIsNone(store.get_search_queries(1, path=self.path))
        store.add_search_query(1, "Recovered Query", [], path=self.path)
        self.assertIn("Recovered Query", store.get_search_queries(1, path=self.path))
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())


class LetterStyleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mvp_settings.json"

    def test_default_style(self):
        style = store.get_letter_style(1, path=self.path)
        self.assertEqual(style["tone"], store.DEFAULT_TONE)
        self.assertEqual(style["denylist"], [])

    def test_set_letter_tone_rejects_unknown_values(self):
        with self.assertRaises(ValueError):
            store.set_letter_tone(1, "агрессивный", path=self.path)

    def test_set_letter_tone_persists(self):
        store.set_letter_tone(1, "спокойный", path=self.path)
        self.assertEqual(store.get_letter_style(1, path=self.path)["tone"], "спокойный")

    def test_denylist_add_remove_dedup(self):
        store.add_denylist_word(1, "синергия", path=self.path)
        store.add_denylist_word(1, "Синергия", path=self.path)  # dup, case-insensitive
        style = store.get_letter_style(1, path=self.path)
        self.assertEqual(style["denylist"], ["синергия"])
        remaining = store.remove_denylist_word(1, "СИНЕРГИЯ", path=self.path)
        self.assertEqual(remaining, [])

    def test_corrupt_tone_on_disk_falls_back_to_default(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"1": {"letter_tone": "bogus", "letter_denylist": []}}), encoding="utf-8",
        )
        self.assertEqual(store.get_letter_style(1, path=self.path)["tone"], store.DEFAULT_TONE)


class AutomationThresholdsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.automation = AutomationStore(Path(self.tmp.name) / "automation.sqlite3")

    def test_update_single_field_leaves_others_untouched(self):
        before = self.automation.settings(7)
        self.assertEqual(before["daily_limit"], 40)
        updated = store.update_automation_thresholds(self.automation, 7, daily_limit=25)
        self.assertEqual(updated["daily_limit"], 25)
        self.assertEqual(updated["min_preview_score"], before["min_preview_score"])
        self.assertEqual(updated["min_apply_score"], before["min_apply_score"])

    def test_update_multiple_fields(self):
        updated = store.update_automation_thresholds(
            self.automation, 7, min_preview_score=50, min_apply_score=80,
        )
        self.assertEqual(updated["min_preview_score"], 50)
        self.assertEqual(updated["min_apply_score"], 80)

    def test_update_with_no_fields_is_a_noop_read(self):
        before = self.automation.settings(7)
        after = store.update_automation_thresholds(self.automation, 7)
        self.assertEqual(before, after)


class RouterPureHelpersTest(unittest.TestCase):
    """Handler-adjacent pure functions that don't need a live Telegram object."""

    def test_parse_int(self):
        self.assertEqual(settings_router._parse_int("42"), 42)
        self.assertEqual(settings_router._parse_int("  7 "), 7)
        self.assertIsNone(settings_router._parse_int("abc"))
        self.assertIsNone(settings_router._parse_int(""))
        self.assertIsNone(settings_router._parse_int(None))

    def test_tone_key_round_trip(self):
        for key, label in settings_router.TONE_KEYS.items():
            self.assertEqual(settings_router._tone_key(label), key)
        self.assertEqual(settings_router._tone_key("что-то незнакомое"), "direct")

class FakeMessage:
    def __init__(self, *, text: str | None = None, user_id: int = 4242):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.answer = mock.AsyncMock(return_value=None)


class FakeCallback:
    def __init__(self, data: str, *, user_id: int = 4242):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage(user_id=user_id)
        self.answer = mock.AsyncMock(return_value=None)


def _fake_state() -> mock.MagicMock:
    state = mock.MagicMock()
    state.set_state = mock.AsyncMock()
    state.clear = mock.AsyncMock()
    return state


class SettingsHandlersTest(unittest.IsolatedAsyncioTestCase):
    """Exercises the new mvp:cfg:* handlers end to end with fake Telegram
    objects, redirecting every file-backed store to a tempdir so real bot
    state under var/ is never touched.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.automation = AutomationStore(Path(self.tmp.name) / "automation.sqlite3")
        self.settings_path = Path(self.tmp.name) / "mvp_settings.json"
        self.trap_path = Path(self.tmp.name) / "trap_lists.json"

        automation_patch = mock.patch("mvp.routers.settings.config.AUTOMATION", self.automation)
        automation_patch.start()
        self.addCleanup(automation_patch.stop)

        settings_default_path = mock.patch.object(store, "_default_path", return_value=self.settings_path)
        settings_default_path.start()
        self.addCleanup(settings_default_path.stop)

        trap_default_path = mock.patch(
            "candidate.traps._default_lists_path", return_value=self.trap_path,
        )
        trap_default_path.start()
        self.addCleanup(trap_default_path.stop)

        env_snapshot = os.environ.pop("HH_SEARCH_QUERIES", None)
        self.addCleanup(lambda: env_snapshot and os.environ.__setitem__("HH_SEARCH_QUERIES", env_snapshot))
        self.addCleanup(lambda: os.environ.pop("HH_SEARCH_QUERIES", None))

        armed_snapshot = os.environ.pop("AUTO_APPLY_ARMED", None)
        self.addCleanup(lambda: armed_snapshot and os.environ.__setitem__("AUTO_APPLY_ARMED", armed_snapshot))
        self.addCleanup(lambda: os.environ.pop("AUTO_APPLY_ARMED", None))

    # -- search queries ---------------------------------------------------

    async def test_query_add_flow(self):
        callback = FakeCallback("mvp:cfg:q:add")
        state = _fake_state()
        await settings_router.query_add_start(callback, state)
        state.set_state.assert_awaited_once_with(settings_router.SettingsInput.query_add)

        message = FakeMessage(text="Тестовый запрос")
        await settings_router.query_add_save(message, state)
        state.clear.assert_awaited()
        confirmation = message.answer.call_args_list[0].args[0]
        self.assertIn("Тестовый запрос", confirmation)
        self.assertIn("Тестовый запрос", os.environ["HH_SEARCH_QUERIES"])

    async def test_query_add_rejects_empty_text(self):
        message = FakeMessage(text="   ")
        state = _fake_state()
        await settings_router.query_add_save(message, state)
        warning = message.answer.call_args_list[0].args[0]
        self.assertIn("непустой запрос", warning)
        self.assertNotIn("HH_SEARCH_QUERIES", os.environ)

    async def test_query_remove_out_of_range_is_safe(self):
        callback = FakeCallback("mvp:cfg:q:rm:9999")
        await settings_router.query_remove(callback)
        callback.answer.assert_awaited()
        self.assertTrue(callback.answer.call_args.kwargs.get("show_alert"))

    async def test_query_reset(self):
        store.add_search_query(callback_user_id := 4242, "Custom", [], path=self.settings_path)
        self.assertIsNotNone(store.get_search_queries(callback_user_id, path=self.settings_path))
        callback = FakeCallback("mvp:cfg:q:reset")
        await settings_router.query_reset(callback)
        self.assertIsNone(store.get_search_queries(callback_user_id, path=self.settings_path))

    # -- filters ------------------------------------------------------------

    async def test_filter_limit_save_valid(self):
        state = _fake_state()
        message = FakeMessage(text="30")
        await settings_router.filter_limit_save(message, state)
        self.assertEqual(self.automation.settings(4242)["daily_limit"], 30)
        self.assertIn("30", message.answer.call_args_list[0].args[0])

    async def test_filter_limit_save_rejects_out_of_range(self):
        state = _fake_state()
        message = FakeMessage(text="99999")
        await settings_router.filter_limit_save(message, state)
        self.assertIn("целое число", message.answer.call_args_list[0].args[0])
        self.assertEqual(self.automation.settings(4242)["daily_limit"], 40)  # unchanged

    async def test_filter_preview_cannot_exceed_apply(self):
        # Defaults: preview=60, apply=75. Try to push preview above apply.
        state = _fake_state()
        message = FakeMessage(text="90")
        await settings_router.filter_preview_save(message, state)
        warning = message.answer.call_args_list[0].args[0]
        self.assertIn("не может быть выше", warning)
        self.assertEqual(self.automation.settings(4242)["min_preview_score"], 60)

    async def test_filter_apply_cannot_go_below_preview(self):
        state = _fake_state()
        message = FakeMessage(text="10")
        await settings_router.filter_apply_save(message, state)
        warning = message.answer.call_args_list[0].args[0]
        self.assertIn("не может быть ниже", warning)
        self.assertEqual(self.automation.settings(4242)["min_apply_score"], 75)

    async def test_filter_preview_and_apply_accept_valid_consistent_values(self):
        state = _fake_state()
        await settings_router.filter_apply_save(FakeMessage(text="85"), state)
        await settings_router.filter_preview_save(FakeMessage(text="70"), state)
        settings = self.automation.settings(4242)
        self.assertEqual(settings["min_apply_score"], 85)
        self.assertEqual(settings["min_preview_score"], 70)

    # -- traps ----------------------------------------------------------------

    async def test_trap_add_and_remove_phrase(self):
        state = _fake_state()
        message = FakeMessage(text="удалённая работа без опыта")
        await settings_router.trap_add_phrase_save(message, state)
        from candidate.traps import load_user_lists
        lists = load_user_lists(4242, path=self.trap_path)
        self.assertIn("удалённая работа без опыта", lists["stop_phrases"])

        callback = FakeCallback("mvp:cfg:t:rmphrase:0")
        await settings_router.trap_remove_phrase(callback)
        lists = load_user_lists(4242, path=self.trap_path)
        self.assertEqual(lists["stop_phrases"], [])

    async def test_trap_add_and_remove_employer(self):
        state = _fake_state()
        message = FakeMessage(text="ООО Рога и Копыта")
        await settings_router.trap_add_employer_save(message, state)
        from candidate.traps import load_user_lists
        lists = load_user_lists(4242, path=self.trap_path)
        self.assertEqual(lists["blocked_employers"][0]["name"], "ООО Рога и Копыта")

        callback = FakeCallback("mvp:cfg:t:rmemp:0")
        await settings_router.trap_remove_employer(callback)
        lists = load_user_lists(4242, path=self.trap_path)
        self.assertEqual(lists["blocked_employers"], [])

    async def test_block_employer_command_uses_last_shown_vacancy(self):
        fake_store = mock.MagicMock()
        fake_store.list_recent.return_value = [{
            "vacancy": {"name": "Test", "employer": {"name": "Плохая Компания", "id": "77"}},
        }]
        with mock.patch("mvp.routers.settings.config.STORE", fake_store):
            message = FakeMessage()
            await settings_router.block_employer_command(message)
        from candidate.traps import load_user_lists
        lists = load_user_lists(4242, path=self.trap_path)
        self.assertEqual(lists["blocked_employers"][0]["name"], "Плохая Компания")
        self.assertEqual(lists["blocked_employers"][0]["id"], "77")

    async def test_block_employer_command_with_no_history(self):
        fake_store = mock.MagicMock()
        fake_store.list_recent.return_value = []
        with mock.patch("mvp.routers.settings.config.STORE", fake_store):
            message = FakeMessage()
            await settings_router.block_employer_command(message)
        self.assertIn("Нет недавно показанных", message.answer.call_args_list[0].args[0])

    # -- letter style ---------------------------------------------------------

    async def test_letter_tone_set(self):
        callback = FakeCallback("mvp:cfg:l:tone:calm")
        await settings_router.letter_tone_set(callback)
        self.assertEqual(store.get_letter_style(4242, path=self.settings_path)["tone"], "спокойный")

    async def test_letter_add_and_remove_word(self):
        state = _fake_state()
        message = FakeMessage(text="синергия")
        await settings_router.letter_add_word_save(message, state)
        style = store.get_letter_style(4242, path=self.settings_path)
        self.assertIn("синергия", style["denylist"])

        callback = FakeCallback("mvp:cfg:l:rmword:0")
        await settings_router.letter_remove_word(callback)
        style = store.get_letter_style(4242, path=self.settings_path)
        self.assertEqual(style["denylist"], [])

    # -- safety: auto-apply gate is untouched by this extension -------------

    async def test_toggle_apply_still_refuses_when_gate_locked(self):
        os.environ.pop("AUTO_APPLY_ARMED", None)  # LOCKED
        callback = FakeCallback("mvp:toggle_apply")
        with mock.patch("mvp.routers.settings.search_setup", mock.AsyncMock()):
            await settings_router.toggle_apply(callback)
        self.assertFalse(self.automation.settings(4242)["auto_apply_enabled"])
        callback.answer.assert_awaited()
        self.assertTrue(callback.answer.call_args.kwargs.get("show_alert"))

    # -- cancel / back navigation --------------------------------------------

    async def test_cfg_cancel_clears_state(self):
        state = _fake_state()
        callback = FakeCallback("mvp:cfg:cancel")
        await settings_router.cfg_cancel(callback, state)
        state.clear.assert_awaited_once()
        callback.message.answer.assert_awaited()


if __name__ == "__main__":
    unittest.main()
