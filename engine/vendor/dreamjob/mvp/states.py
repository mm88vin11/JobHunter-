"""FSM state groups used across the MVP bot's routers."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class HHEasyLogin(StatesGroup):
    login = State()
    otp = State()


class ManualLetterEdit(StatesGroup):
    letter = State()


class CrmReply(StatesGroup):
    """Reply-to-recruiter flow on the CRM thread screen (`mvp/routers/crm.py`)."""

    text = State()
