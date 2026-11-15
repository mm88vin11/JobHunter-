"""Assembles the MVP bot's per-screen routers in a safety-critical order.

The original single-file bot registered handlers in one flat sequence.
Splitting into one router per screen means the file-to-file order is now
the only lever left to control cross-file handler priority — aiogram
resolves the FIRST matching handler per event type (message vs
callback_query are independent chains).

In practice only one ordering constraint is load-bearing: `vacancies`
contains the catch-all `@router.message(F.text | F.caption)` handler
(`vacancy_text`), which matches *any* text/caption message with no state or
command filter. Every command handler (`/start`, `/profile`, `/facts`,
`/hh_code`, `/search_now`) and every FSM-state handler (`HHEasyLogin.*`,
`ManualLetterEdit.letter`) must get first refusal, or the catch-all would
swallow their messages instead. That is only guaranteed if `vacancies` is
the LAST router in this list — so it is, deliberately, even though the
task's suggested file listing shows it second. Every other router's
callback_query filters are mutually exclusive exact-match/prefix strings
(no overlaps), so their relative order does not affect behaviour.
"""
from __future__ import annotations

from aiogram import Router

from mvp.routers.main_menu import router as main_menu_router
from mvp.routers.hh_auth import router as hh_auth_router
from mvp.routers.crm import router as crm_router
from mvp.routers.followup import router as followup_router
from mvp.routers.resumes import router as resumes_router
from mvp.routers.cards import router as cards_router
from mvp.routers.history import router as history_router
from mvp.routers.stats import router as stats_router
from mvp.routers.settings import router as settings_router
from mvp.routers.filtered import router as filtered_router
from mvp.routers.vacancies import router as vacancies_router

routers: list[Router] = [
    main_menu_router,
    hh_auth_router,
    crm_router,
    followup_router,
    resumes_router,
    cards_router,
    history_router,
    stats_router,
    settings_router,
    filtered_router,
    vacancies_router,
]

__all__ = ["routers"]
