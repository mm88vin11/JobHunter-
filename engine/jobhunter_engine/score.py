"""Детерминированный скоринг и антискам из dream-job-bot (candidate.traps + candidate.ranking, MIT).
Улучшение: собственный словарь ролей «вайб-кодер / AI-разработчик», работа с вакансиями любых площадок
(через to_hh_like), бонус за удалёнку/зарубеж и штраф за очевидно нерелевантное."""
from __future__ import annotations

import json
import logging
import os
import re

from candidate import ranking, traps  # vendor/dreamjob

from .model import Vacancy

log = logging.getLogger("jobhunter")
PROFILE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "candidate_data", "hh_profiles", "vibe_coder.json")

VIBE_VOCAB = {
    "strong": (
        "вайб", "vibe", "ai-разработ", "ии-разработ", "ai developer", "ai engineer", "llm", "prompt", "промпт", "ai builder",
        "ai product", "ai automation", "нейросет", "gpt", "агент", "agents", "claude", "cursor", "no-code", "nocode", "low-code",
    ),
    "medium": (
        "fullstack", "full-stack", "full stack", "frontend", "фронтенд", "javascript", "typescript", "react", "node", "веб-разработ",
        "web developer", "telegram", "бот", "bot", "mini app", "битрикс", "bitrix", "amocrm", "автоматизац", "automation", "webgl",
        "three.js", "n8n", "интеграц", "разработчик", "developer", "engineer", "программист",
    ),
    "anti": (
        "java ", "c++", "c#", ".net", "golang", "1с", "1c", "android", "ios", "kotlin", "swift", "devops", "sre", "qa ", "тестировщик",
        "data scientist", "аналитик данных", "продаж", "sales", "marketing manager", "бухгалтер", "оператор", "водитель", "курьер",
    ),
}


def _load_profile() -> dict:
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return json.load(f)


PROFILE = _load_profile()
SENIOR_RE = re.compile(r"\b(?:senior|sr\.?|lead|principal|staff|head|director|chief|cto|architect)\b|ведущ|старш|руководител|архитектор|тимлид", re.I)
JUNIOR_RE = re.compile(r"\b(?:junior|jr\.?|middle|intern)\b|стаж[её]р|младш|начинающ|без опыта", re.I)
try:
    ranking._ROLE_VOCABULARY["vibe_coder"] = VIBE_VOCAB  # type: ignore[index]
except TypeError:  # если в оригинале неизменяемый Mapping
    ranking._ROLE_VOCABULARY = {**ranking._ROLE_VOCABULARY, "vibe_coder": VIBE_VOCAB}


def score(v: Vacancy, stop_phrases: list[str], blocked: list[str]) -> Vacancy:
    hh = v.to_hh_like()
    try:
        verdict = traps.evaluate_vacancy(hh, user_lists={"stop_phrases": stop_phrases, "blocked_employers": blocked})
        v.trap = verdict.as_dict() if hasattr(verdict, "as_dict") else dict(verdict.__dict__)
    except Exception as e:
        log.debug("traps: %s", e)
        verdict = None
    try:
        rv = ranking.rank_vacancy(hh, PROFILE, profile_key="vibe_coder", trap_verdict=verdict)
        r = rv.as_dict()
        total = int(r["total"])
        if v.remote:
            total += 5
        if v.lang == "en":
            total += 3  # зарубежный рынок — обычно выше доход
        # Улучшение: у кандидата ~1.5 года коммерческого опыта — senior/lead роли почти не дают ответов
        if SENIOR_RE.search(v.title):
            total -= 18
        if JUNIOR_RE.search(v.title):
            total += 6
        v.rule_score = max(0, min(100, total))
        v.ranking = {k: r[k] for k in ("total", "hard_reject", "hard_reject_reason", "pros", "cons", "fit_reasons", "missing_reasons", "reply_chance", "reply_chance_reason", "explanation")}
    except Exception as e:
        log.debug("ranking: %s", e)
        v.rule_score = 50
    return v
