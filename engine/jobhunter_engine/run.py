"""Точка входа движка. Запуск: python -m jobhunter_engine.run [--dry-run] [--only hh,habr]

1. Берёт настройки из Cloudflare Worker (запросы, каналы, токен hh) — GET {WORKER_URL}/api/engine/config
2. Параллельно собирает вакансии со всех включённых площадок
3. Склеивает дубли, прогоняет антискам и ранжирование dream-job-bot
4. Отправляет в Worker пачками — POST {WORKER_URL}/api/engine/ingest
   Дальше Worker: ИИ-оценка → письмо на языке вакансии → автоотклик → отчёт в Telegram.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from . import __name__ as _pkg  # noqa: F401  (подключает vendor/ в sys.path)
from .model import Vacancy
from .score import score
from .sources import REGISTRY

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jobhunter")

DEFAULT_CFG = {
    "queries_ru": ["вайб-кодер", "вайбкодер", "vibe coding", "AI-разработчик", "ИИ-разработчик", "prompt engineer", "LLM разработчик",
                   "разработчик Telegram ботов", "AI автоматизация", "fullstack javascript", "no-code разработчик", "AI product builder"],
    "queries_en": ["vibe coder", "vibe coding", "AI engineer", "AI developer", "LLM engineer", "prompt engineer", "AI automation engineer",
                   "AI product engineer", "full stack AI", "telegram bot developer"],
    "must_have_any": ["vibe", "ai ", "ai-", " ai", "llm", "gpt", "prompt", "claude", "cursor", "openai", "agent", "javascript", "typescript",
                      "react", "node", "fullstack", "full-stack", "full stack", "frontend", "telegram", "bot", "automation", "no-code", "webgl"],
    "stop_words": [], "stop_companies": [], "vk_groups": [], "sources": {},
    "telegram_channels": ["forallmedia", "remote_it_jobs", "vacancyit", "jobforjunior", "devjobs", "habr_career", "getitrussia",
                          "remotejobss", "workoo", "nocode_jobs", "it_vakansii_jobs", "jobs_it_remote", "zarubezhom_jobs",
                          "freelancetaverna", "webfrl", "careerspace", "normrabota", "python_jobs_ru"],
    "hours_old": 72, "per_query": 25, "jobspy_queries": 6,
}


def load_config(worker: str | None = None, token: str | None = None) -> dict:
    """Конфиг сборщика из config/settings.json (раньше — из Cloudflare Worker)."""
    from . import config

    cfg = dict(DEFAULT_CFG)
    try:
        cfg.update(config.engine_cfg(config.settings()))
    except Exception as e:
        log.warning("config/settings.json не прочитан (%s), использую значения по умолчанию", e)
    cfg["must_have_any"] = [w.lower() for w in cfg["must_have_any"]]
    for env_key, cfg_key in (("HH_ACCESS_TOKEN", "hh_token"), ("VK_SERVICE_TOKEN", "vk_token"), ("HH_RESUME_ID", "hh_resume_id")):
        if os.environ.get(env_key) and not cfg.get(cfg_key):
            cfg[cfg_key] = os.environ[env_key]
    return cfg


def norm_key(v: Vacancy) -> str:
    n = lambda s: " ".join(sorted(re.sub(r"[^a-zа-яё0-9]+", " ", (s or "").lower()).split()))
    return f"{n(v.title)}|{n(v.company)}"


def collect(cfg: dict, only: set[str] | None) -> list[Vacancy]:
    enabled = {k: f for k, f in REGISTRY.items() if (not only or k in only) and cfg["sources"].get(k, True)}
    results: list[Vacancy] = []
    stats = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(f, cfg): k for k, f in enabled.items()}
        for fut in as_completed(futures):
            k = futures[fut]
            try:
                got = fut.result()
                stats[k] = len(got)
                results += got
            except Exception as e:
                stats[k] = f"ошибка: {e}"[:120]
                log.warning("источник %s упал: %s", k, e)
    log.info("собрано по источникам: %s", json.dumps(stats, ensure_ascii=False))
    cfg["_stats"] = stats
    return results


def filter_and_score(cfg: dict, items: list[Vacancy]) -> list[Vacancy]:
    stop = [s.lower() for s in cfg.get("stop_words") or []]
    stop_comp = [s.lower() for s in cfg.get("stop_companies") or []]
    seen: dict[str, Vacancy] = {}
    for v in items:
        if not v.title or not v.url:
            continue
        t = v.title.lower()
        if any(s in t for s in stop) or any(s and s in (v.company or "").lower() for s in stop_comp):
            continue
        hay = f"{v.title} {v.description[:4000]} {' '.join(v.skills)}".lower()
        if not any(k in hay for k in cfg["must_have_any"]):
            continue
        key = norm_key(v)
        prev = seen.get(key)
        # дубль: оставляем версию с более подробным описанием, контакты объединяем
        if prev:
            for c in ("emails", "telegram", "links"):
                prev.contacts[c] = sorted(set(prev.contacts.get(c, [])) | set(v.contacts.get(c, [])))
            if len(v.description) > len(prev.description):
                v.contacts = prev.contacts
                seen[key] = v
            continue
        seen[key] = v
    out = []
    for v in seen.values():
        score(v, stop, stop_comp)
        if v.ranking and v.ranking.get("hard_reject"):
            continue
        if (v.rule_score or 0) < cfg.get("min_rule_score", 30):
            continue
        out.append(v)
    out.sort(key=lambda v: v.rule_score or 0, reverse=True)
    return out


def push(worker: str, token: str, items: list[Vacancy], stats: dict) -> None:
    batch = 40
    total = {"inserted": 0, "duplicates": 0}
    for i in range(0, len(items), batch):
        payload = {"vacancies": [v.to_ingest() for v in items[i:i + batch]], "stats": stats if i == 0 else None}
        for attempt in range(4):
            try:
                r = requests.post(f"{worker}/api/engine/ingest", json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=60)
                r.raise_for_status()
                res = r.json()
                total["inserted"] += res.get("inserted", 0)
                total["duplicates"] += res.get("duplicates", 0)
                break
            except Exception as e:
                log.warning("ingest попытка %d: %s", attempt + 1, e)
                time.sleep(2 ** attempt)
    log.info("отправлено в Worker: %s", total)
    try:  # сразу запускаем ИИ-оценку и отклики, не дожидаясь cron
        requests.post(f"{worker}/api/engine/kick", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="не отправлять в Worker, сохранить в results.json")
    ap.add_argument("--only", default="", help="через запятую: hh,habr,telegram,jobspy,...")
    args = ap.parse_args(argv)
    worker = (os.environ.get("WORKER_URL") or "").rstrip("/")
    token = os.environ.get("ENGINE_TOKEN") or ""
    cfg = load_config()
    only = {s.strip() for s in args.only.split(",") if s.strip()} or None
    raw = collect(cfg, only)
    items = filter_and_score(cfg, raw)
    log.info("после фильтров и антискама: %d из %d", len(items), len(raw))
    if args.dry_run or not worker:
        with open("results.json", "w", encoding="utf-8") as f:
            json.dump([v.to_ingest() for v in items], f, ensure_ascii=False, indent=1)
        for v in items[:25]:
            print(f"{v.rule_score:>3} [{v.source}] {v.title} — {v.company} {v.url}")
        return 0
    push(worker, token, items, cfg.get("_stats", {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
