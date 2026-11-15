"""Entry point for Dream Job Bot."""
from __future__ import annotations

import asyncio
import logging
import os
import time  # noqa: F401 -- re-exported so `mock.patch("run_candidate_mvp.time.time")`
            # in tests patches the one shared stdlib `time` module that
            # mvp.hh_sync._search_queries also reads through.

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommand

from candidate.service import CandidateAdapter

from mvp import config
from mvp.automation import _automation_loop, _crm_digest_loop, _inbox_loop, _try_auto_apply
from mvp.bump import _resume_bump_loop
from mvp.config import DEMO_VACANCY
from mvp.followup import _followup_loop
from mvp.formatting import _payload_digest, _quality_checklist
from mvp.generation import _parse_user_vacancy, _provider_fallback_used
from mvp.hh_sync import _hh_vacancy_id, _search_queries
from mvp.keyboards import _preview_keyboard
from mvp.middleware import OwnerOnlyMiddleware, _is_owner
from mvp.routers import routers
from mvp.settings_store import apply_persisted_search_queries
from mvp.webapp import start_webapp

__all__ = [
    "DEMO_VACANCY", "_hh_vacancy_id", "_is_owner", "_parse_user_vacancy",
    "_preview_keyboard", "_payload_digest", "_provider_fallback_used", "_quality_checklist",
    "_search_queries", "_try_auto_apply", "main",
]


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Set OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL", "").strip()
    if not model:
        raise RuntimeError("Set OPENAI_MODEL to a model supported by your API endpoint")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    config.ADAPTER = CandidateAdapter(
        api_key=api_key,
        base_url=base_url,
        model=model,
        letter_model=os.environ.get("OPENAI_LETTER_MODEL") or model,
    )
    fallback_model = os.environ.get("OPENAI_FALLBACK_MODEL", "").strip()
    if fallback_model and fallback_model != config.ADAPTER.model:
        config.FALLBACK_ADAPTER = CandidateAdapter(
            api_key=api_key,
            base_url=base_url,
            model=fallback_model,
        )
    bot = Bot(token=token)
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="profile", description="Мои master-профили"),
            BotCommand(command="facts", description="Поиск по базе фактов"),
            BotCommand(command="hh_code", description="Завершить HH OAuth"),
            BotCommand(command="search_now", description="Искать вакансии сейчас"),
        ])
    except TelegramNetworkError as exc:
        logging.warning("Telegram command menu update failed; polling will still start: %s", exc)
    dp = Dispatcher()
    dp.message.outer_middleware(OwnerOnlyMiddleware())
    dp.callback_query.outer_middleware(OwnerOnlyMiddleware())
    for router in routers:
        dp.include_router(router)
    owner = os.environ.get("MVP_OWNER_TELEGRAM_ID", "")
    if owner.isdigit():
        config.AUTOMATION.settings(int(owner))
        # Search queries edited in Настройки live in var/mvp_settings.json and are
        # read by _search_queries() through HH_SEARCH_QUERIES. Without this they
        # would silently revert to the hardcoded rotation on every restart.
        apply_persisted_search_queries(int(owner))
    background = [
        asyncio.create_task(_automation_loop(bot), name="mvp:auto_search"),
        asyncio.create_task(_inbox_loop(bot), name="mvp:inbox_sync"),
        asyncio.create_task(_crm_digest_loop(bot), name="mvp:crm_digest"),
        # Two levers on "поднять себя в списке кандидатов", deliberately split:
        # bumping a resume touches nobody and runs unattended, while a nudge into
        # a live thread is drafted here but only ever sent by an owner's press.
        asyncio.create_task(_resume_bump_loop(bot), name="mvp:resume_bump"),
        asyncio.create_task(_followup_loop(bot), name="mvp:followup"),
    ]
    # Off unless WEBAPP_PORT is set, so an existing deploy keeps opening no port.
    webapp_runner = await start_webapp(bot)
    try:
        await dp.start_polling(bot)
    finally:
        for task in background:
            task.cancel()
        if webapp_runner is not None:
            await webapp_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
