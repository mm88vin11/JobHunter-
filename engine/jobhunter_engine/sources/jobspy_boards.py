"""9 мировых площадок через JobSpy (speedyapply/JobSpy, MIT): LinkedIn, Indeed, Glassdoor, ZipRecruiter,
Google Jobs, Bayt, Naukri, BDJobs, HelloWork. Улучшения поверх оригинала: несколько запросов подряд,
удалёнка по умолчанию, нормализация в общую модель, извлечение email/telegram из описаний, защита от падения одной площадки."""
from __future__ import annotations

import logging
import math

from ..model import Vacancy

log = logging.getLogger("jobhunter")

# Площадка JobSpy → (страна Indeed/Glassdoor, удалёнка)
JOBSPY_SITES = ["linkedin", "indeed", "glassdoor", "zip_recruiter", "google", "bayt", "naukri", "bdjobs", "hellowork"]


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def collect(cfg: dict) -> list[Vacancy]:
    from jobspy import scrape_jobs  # vendor/jobspy_repo

    sites = [s for s in JOBSPY_SITES if cfg["sources"].get(s, True)]
    out: list[Vacancy] = []
    for query in cfg["queries_en"][: cfg.get("jobspy_queries", 6)]:
        for site in sites:
            try:
                df = scrape_jobs(
                    site_name=[site],
                    search_term=query,
                    google_search_term=f"{query} remote jobs since yesterday",
                    location=None if site in ("linkedin", "google") else ("USA" if site in ("indeed", "glassdoor", "zip_recruiter") else None),
                    is_remote=True,
                    results_wanted=cfg.get("per_query", 25),
                    hours_old=cfg.get("hours_old", 72),
                    country_indeed="usa",
                    description_format="markdown",
                    fetch_description=site == "linkedin",
                    verbose=0,
                )
            except Exception as e:  # одна площадка не должна валить остальные
                log.warning("jobspy %s '%s': %s", site, query, e)
                continue
            for row in df.to_dict("records"):
                desc = str(row.get("description") or "")
                if desc == "nan":
                    desc = ""
                interval = str(row.get("interval") or "yearly")
                v = Vacancy(
                    source=str(row.get("site") or site),
                    external_id=str(row.get("id") or row.get("job_url")),
                    url=str(row.get("job_url") or ""),
                    apply_url=str(row.get("job_url_direct") or "") if str(row.get("job_url_direct")) != "nan" else "",
                    title=str(row.get("title") or ""),
                    company=str(row.get("company") or "") if str(row.get("company")) != "nan" else "",
                    location=str(row.get("location") or "") if str(row.get("location")) != "nan" else "",
                    remote=bool(row.get("is_remote")) if str(row.get("is_remote")) != "nan" else False,
                    salary_from=_num(row.get("min_amount")),
                    salary_to=_num(row.get("max_amount")),
                    currency=str(row.get("currency") or "USD") if str(row.get("currency")) != "nan" else "USD",
                    salary_period={"yearly": "year", "monthly": "month", "hourly": "hour", "weekly": "week", "daily": "day"}.get(interval, "year"),
                    description=desc,
                    published_at=str(row.get("date_posted") or ""),
                    lang="en",
                )
                emails = row.get("emails")
                v.finalize()
                if isinstance(emails, list):
                    v.contacts["emails"] = sorted(set(v.contacts["emails"]) | {e.lower() for e in emails})
                out.append(v)
    return out
