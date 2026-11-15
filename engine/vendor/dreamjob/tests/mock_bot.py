"""
Mock aiogram objects for testing bot handlers without a real Telegram connection.

Usage:
    msg = MockMessage(user_id=42, text="Hello")
    state = MockFSMContext()
    await state.set_state(InterviewStates.summary)
    await state.update_data({"summary": "test"})
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock


class MockFSMContext:
    """Minimal FSMContext mock that stores state in memory."""

    def __init__(self, initial_data: dict | None = None):
        self._state: str | None = None
        self._data: dict = initial_data or {}
        self.sent_states: list[str | None] = []

    async def get_state(self) -> str | None:
        return self._state

    async def set_state(self, state=None) -> None:
        if state is None:
            self._state = None
        elif hasattr(state, "state"):
            self._state = state.state
        else:
            self._state = str(state)
        self.sent_states.append(self._state)

    async def get_data(self) -> dict:
        return dict(self._data)

    async def update_data(self, data: dict | None = None, **kwargs) -> dict:
        if data:
            self._data.update(data)
        self._data.update(kwargs)
        return dict(self._data)

    async def set_data(self, data: dict) -> None:
        self._data = dict(data)

    async def clear(self) -> None:
        self._state = None
        self._data = {}


class CapturedMessage:
    """Records a single answer/reply sent by a handler."""
    def __init__(self, text: str, reply_markup=None, **kwargs):
        self.text = text
        self.reply_markup = reply_markup
        self.kwargs = kwargs

    def __repr__(self):
        preview = self.text[:60].replace("\n", " ") if self.text else ""
        return f"<Msg: {preview!r}>"


class MockMessage:
    """Fake aiogram Message with captured answers."""

    def __init__(
        self,
        user_id: int = 100001,
        text: str = "",
        username: str | None = "test_user",
        chat_id: int | None = None,
    ):
        self.chat = MagicMock()
        self.chat.id = chat_id or user_id
        self.from_user = MagicMock()
        self.from_user.id = user_id
        self.from_user.username = username
        self.from_user.first_name = "Test"
        self.from_user.last_name = "User"
        self.text = text
        self.document = None
        self.voice = None
        self.message_id = 1
        self.answers: list[CapturedMessage] = []
        self._bot = MagicMock()

    async def answer(self, text: str = "", reply_markup=None, **kwargs) -> "MockMessage":
        captured = CapturedMessage(text, reply_markup, **kwargs)
        self.answers.append(captured)
        return self._make_sent(text, reply_markup)

    async def reply(self, text: str = "", reply_markup=None, **kwargs) -> "MockMessage":
        return await self.answer(text, reply_markup=reply_markup, **kwargs)

    def _make_sent(self, text: str, reply_markup) -> "MockMessage":
        sent = MockMessage(user_id=self.from_user.id, text=text)
        sent._is_sent = True
        return sent

    @property
    def bot(self):
        return self._bot

    def __repr__(self):
        preview = (self.text or "")[:40]
        return f"<MockMessage user={self.from_user.id} text={preview!r}>"


class MockCallbackQuery:
    """Fake aiogram CallbackQuery."""

    def __init__(
        self,
        user_id: int = 100001,
        data: str = "",
        message: MockMessage | None = None,
    ):
        self.from_user = MagicMock()
        self.from_user.id = user_id
        self.from_user.username = "test_user"
        self.data = data
        self.message = message or MockMessage(user_id=user_id)
        self.answers: list[str] = []
        self._answered = False

    async def answer(self, text: str = "", **kwargs) -> None:
        self.answers.append(text)
        self._answered = True

    def __repr__(self):
        return f"<MockCB user={self.from_user.id} data={self.data!r}>"


class BotSimulator:
    """
    Stateful simulator for a single user's bot session.
    Maintains FSM state and a log of all sent messages.
    """

    def __init__(self, user_id: int, username: str = "agent_user"):
        self.user_id = user_id
        self.username = username
        self.state = MockFSMContext()
        self.message_log: list[CapturedMessage] = []
        self._last_message: MockMessage | None = None

    def _msg(self, text: str) -> MockMessage:
        m = MockMessage(user_id=self.user_id, text=text, username=self.username)
        # Intercept .answer() to also log in simulator
        original_answer = m.answer

        async def patched_answer(text="", reply_markup=None, **kwargs):
            result = await original_answer(text, reply_markup=reply_markup, **kwargs)
            self.message_log.append(CapturedMessage(text, reply_markup, **kwargs))
            return result

        m.answer = patched_answer
        self._last_message = m
        return m

    def _cb(self, data: str) -> MockCallbackQuery:
        msg = self._msg("")
        return MockCallbackQuery(user_id=self.user_id, data=data, message=msg)

    def last_answers(self, n: int = 3) -> list[CapturedMessage]:
        return self.message_log[-n:]

    def last_text(self) -> str:
        return self.message_log[-1].text if self.message_log else ""

    def has_button(self, label: str) -> bool:
        """Check if the last keyboard reply contains a button with this label."""
        if not self.message_log:
            return False
        last = self.message_log[-1]
        markup = last.reply_markup
        if markup is None:
            return False
        kb = getattr(markup, "inline_keyboard", None) or getattr(markup, "keyboard", None)
        if not kb:
            return False
        for row in kb:
            for btn in row:
                if getattr(btn, "text", "") == label:
                    return True
        return False
