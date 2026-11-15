"""Настройки: файлы config/*.json в репозитории (их можно править прямо на GitHub) + оперативные правки из Telegram (в базе)."""
from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_DIR = os.path.join(ROOT, "config")


def _load(name: str) -> dict:
    with open(os.path.join(CONFIG_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def profile() -> dict:
    return _load("profile.json")


def settings(store=None) -> dict:
    s = _load("settings.json")
    if store is not None:
        s.update(store.get("settings_override", {}) or {})
    return s


def engine_cfg(s: dict, store=None) -> dict:
    """Конфиг для сборщика вакансий (jobhunter_engine.run)."""
    hh = (store.get("hh_tokens") if store else None) or {}
    return {
        "queries_ru": s["queriesRu"], "queries_en": s["queriesEn"], "must_have_any": [w.lower() for w in s["mustHaveAny"]],
        "stop_words": s["stopWords"], "stop_companies": s["stopCompanies"], "telegram_channels": s["telegramChannels"],
        "vk_groups": s["vkGroups"], "vk_token": os.environ.get("VK_SERVICE_TOKEN", ""), "hh_area": s.get("hhArea"),
        "hh_token": hh.get("access_token", ""), "hh_resume_id": (store.get("hh_resume_id") if store else "") or "",
        "hh_user_agent": os.environ.get("HH_USER_AGENT", "JobHunter/1.0 (vlad_2048@mail.ru)"),
        "sources": s["sources"], "hours_old": 72, "per_query": 25, "jobspy_queries": 6,
    }
