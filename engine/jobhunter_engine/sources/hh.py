"""hh.ru через HHOfficialClient из dream-job-bot (vendor/dreamjob/candidate/hh_official.py, MIT).
Улучшения: поиск по всем RU-запросам + свежие (order_by=publication_time) + рекомендации hh по резюме,
полное описание для каждой вакансии, работа с токеном соискателя из панели JobHunter."""
from __future__ import annotations

import logging
import os

from ..model import Vacancy, strip_html

log = logging.getLogger("jobhunter")


def _client(cfg: dict):
    from candidate.hh_official import HHOfficialClient  # vendor/dreamjob

    os.environ.setdefault("HH_USER_AGENT", cfg.get("hh_user_agent") or "JobHunter/1.0 (vlad_2048@mail.ru)")
    return HHOfficialClient(client_id=cfg.get("hh_client_id", ""), client_secret="", access_token=cfg.get("hh_token", ""))


def _to_vacancy(item: dict) -> Vacancy:
    sal = item.get("salary") or item.get("salary_range") or {}
    sched = (item.get("schedule") or {}).get("id")
    snippet = item.get("snippet") or {}
    desc = strip_html(item.get("description")) or "\n".join(filter(None, [strip_html(snippet.get("requirement")), strip_html(snippet.get("responsibility"))]))
    v = Vacancy(
        source="hh",
        external_id=str(item["id"]),
        url=item.get("alternate_url") or f"https://hh.ru/vacancy/{item['id']}",
        title=item.get("name", ""),
        company=(item.get("employer") or {}).get("name", ""),
        location=(item.get("area") or {}).get("name", ""),
        remote=sched == "remote" or any((w or {}).get("id") == "REMOTE" for w in item.get("work_format") or []),
        salary_from=sal.get("from"),
        salary_to=sal.get("to"),
        currency=sal.get("currency") or "RUR",
        description=desc,
        published_at=item.get("published_at", ""),
        skills=[s.get("name", "") for s in item.get("key_skills") or []],
        hh_raw=item,
        lang="ru",
    )
    return v.finalize()


def collect(cfg: dict) -> list[Vacancy]:
    client = _client(cfg)
    seen: dict[str, dict] = {}
    for q in cfg["queries_ru"]:
        for order in ("relevance", "publication_time"):
            try:
                res = client.search_vacancies(q, per_page=50, order_by=order, period=cfg.get("hh_period", 7), area=cfg.get("hh_area") or None)
                for it in res.get("items", []):
                    seen.setdefault(str(it["id"]), it)
            except Exception as e:
                log.warning("hh search '%s': %s", q, e)
                if getattr(e, "status", 0) == 403 or "forbidden" in str(e).lower():
                    return []  # hh не пускает без токена — не тратим время
    if cfg.get("hh_token") and cfg.get("hh_resume_id"):
        try:
            for it in client.similar_vacancies(cfg["hh_resume_id"], per_page=100).get("items", []):
                seen.setdefault(str(it["id"]), it)
        except Exception as e:
            log.warning("hh similar: %s", e)
    out = []
    for vid, it in list(seen.items())[: cfg.get("hh_max_details", 250)]:
        try:
            it = client.vacancy(vid)  # полное описание, has_test, response_letter_required
        except Exception:
            pass
        out.append(_to_vacancy(it))
    return out
