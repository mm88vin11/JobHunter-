"""Vacancy parsing and the generate-then-show flow, with model failover."""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram.types import Message

from candidate.hh_resume_sync import load_profile
from candidate.ranking import choose_profile, rank_vacancy
from candidate.service import UnsafeVariantError
from candidate.vacancy import normalize_vacancy_input

from mvp import config
from mvp.formatting import _preview_text
from mvp.hh_sync import _enrich_payload_from_hh
from mvp.keyboards import _preview_keyboard
from mvp.settings_store import get_letter_style

log = logging.getLogger(__name__)


def _parse_user_vacancy(text: str) -> dict:
    return normalize_vacancy_input(text)


def _provider_fallback_used(variant) -> bool:
    return variant.score == 50 and any("llm selection json failed" in reason.lower() for reason in variant.internal_rationale)


def _apply_owner_letter_style(user_id: int) -> None:
    """Push the owner's tone and personal denylist from the settings hub into the adapters.

    Read fresh on every generation rather than cached at startup, so a change made
    in Настройки affects the very next letter instead of waiting for a restart.
    """
    try:
        style = get_letter_style(user_id)
    except Exception as exc:  # pragma: no cover - a preference must never block generation
        log.warning("Letter style unavailable, using defaults: %s", type(exc).__name__)
        return
    for adapter in (config.ADAPTER, config.FALLBACK_ADAPTER):
        if adapter is not None:
            adapter.set_owner_preferences(tone=style.get("tone"), denylist=style.get("denylist"))


def _generate_with_failover(vacancy: dict, forced_profile: str | None = None):
    if config.ADAPTER is None:
        raise RuntimeError("Primary adapter is not configured")
    if config.FALLBACK_ADAPTER is not None and time.time() < config.MODEL_DEGRADED_UNTIL:
        return config.FALLBACK_ADAPTER.generate(vacancy, forced_profile=forced_profile)
    variant = config.ADAPTER.generate(vacancy, forced_profile=forced_profile)
    if config.FALLBACK_ADAPTER is not None and _provider_fallback_used(variant):
        config.MODEL_DEGRADED_UNTIL = time.time() + 900
        log.warning("Primary model unavailable; using secondary model for 15 minutes")
        return config.FALLBACK_ADAPTER.generate(vacancy, forced_profile=forced_profile)
    return variant


async def _generate_and_show(message: Message, vacancy: dict, forced_profile: str | None = None) -> None:
    if config.ADAPTER is None:
        await message.answer("AIGate пока не настроен.")
        return
    profiles = await asyncio.to_thread(
        lambda: {key: load_profile(key) for key in config.ADAPTER.data.profiles},
    )
    selected_profile = forced_profile
    if selected_profile is None:
        ranking, _ = await asyncio.to_thread(
            choose_profile, vacancy, profiles,
            profile_hint=str(vacancy.get("search_profile_hint") or ""),
        )
        selected_profile = ranking.profile_key

    existing = await asyncio.to_thread(
        config.STORE.get_by_fingerprints, message.chat.id,
        vacancy.get("fingerprint", ""), vacancy.get("content_fingerprint", ""),
    )
    if existing and (existing.get("master_profile") or {}).get("key") == selected_profile:
        await message.answer(
            "♻️ Эта вакансия уже есть в журнале. Показываю сохранённый пакет.\n\n" + _preview_text(existing),
            parse_mode="HTML", reply_markup=_preview_keyboard(existing["id"], existing["vacancy"].get("source_url", ""), existing),
        )
        return
    _apply_owner_letter_style(message.chat.id)
    loading = await message.answer("⏳ Модель выбирает профиль и проверяет факты…")
    try:
        variant = await asyncio.to_thread(_generate_with_failover, vacancy, selected_profile)
        master = config.ADAPTER.data.profiles[variant.profile_key]
        payload = variant.as_payload(
            user_id=message.chat.id, vacancy=vacancy,
            data_version=config.ADAPTER.data.version, master=master,
        )
        payload = await _enrich_payload_from_hh(message.chat.id, payload)
        final = await asyncio.to_thread(
            rank_vacancy, vacancy, profiles[selected_profile],
            profile_key=selected_profile,
            profile_hint=str(vacancy.get("search_profile_hint") or ""),
        )
        score = final.as_dict()
        score["reasons"] = list(final.pros) or list((payload.get("score") or {}).get("reasons") or [])
        payload["score"] = score
        payload.setdefault("analysis_internal", {})["ranking"] = score
        await asyncio.to_thread(config.STORE.save, payload)
        await loading.edit_text(
            _preview_text(payload), parse_mode="HTML",
            reply_markup=_preview_keyboard(variant.id, vacancy["source_url"], payload),
        )
    except UnsafeVariantError as exc:
        log.warning("Rejected unsafe variant: %s", exc)
        await loading.edit_text("🛡 Вариант отклонён факт-проверкой. Отклик не отправлен; попробуйте другую вакансию.")
    except Exception:
        log.exception("Candidate adaptation failed")
        await loading.edit_text("Не удалось подготовить вариант. Отклик не отправлен; попробуйте ещё раз.")
