"""Мировые удалённые доски с открытыми API: RemoteOK, Remotive, Himalayas, We Work Remotely,
Working Nomads, Jobicy, Arbeitnow, Hacker News «Who is hiring?»."""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

from ..http import get, get_json
from ..model import Vacancy, strip_html

log = logging.getLogger("jobhunter")


def _relevant(cfg: dict, text: str) -> bool:
    t = text.lower()
    return any(k in t for k in cfg["must_have_any"])


def remoteok(cfg: dict) -> list[Vacancy]:
    out = []
    for it in get_json("https://remoteok.com/api")[1:]:
        text = f"{it.get('position')} {' '.join(it.get('tags') or [])}"
        if not _relevant(cfg, text):
            continue
        out.append(Vacancy(
            source="remoteok", external_id=str(it["id"]), url=it.get("url") or f"https://remoteok.com/remote-jobs/{it['id']}",
            apply_url=it.get("apply_url", ""), title=it.get("position", ""), company=it.get("company", ""),
            location=it.get("location") or "Remote", remote=True, salary_from=it.get("salary_min") or None,
            salary_to=it.get("salary_max") or None, currency="USD", salary_period="year",
            description=strip_html(it.get("description")), published_at=it.get("date", ""), skills=it.get("tags") or [], lang="en",
        ).finalize())
    return out


def remotive(cfg: dict) -> list[Vacancy]:
    out = {}
    for q in cfg["queries_en"][:6]:
        try:
            data = get_json(f"https://remotive.com/api/remote-jobs?search={quote(q)}&limit=100")
        except Exception as e:
            log.warning("remotive: %s", e)
            continue
        for it in data.get("jobs", []):
            out[str(it["id"])] = Vacancy(
                source="remotive", external_id=str(it["id"]), url=it["url"], title=it["title"], company=it.get("company_name", ""),
                location=it.get("candidate_required_location", "Remote"), remote=True, description=strip_html(it.get("description")),
                published_at=it.get("publication_date", ""), skills=it.get("tags") or [], lang="en",
            ).finalize()
    return list(out.values())


def himalayas(cfg: dict) -> list[Vacancy]:
    out = {}
    for q in cfg["queries_en"][:6]:
        time.sleep(3)  # Himalayas отдаёт 429 при частых запросах
        try:
            data = get_json(f"https://himalayas.app/jobs/api/search?q={quote(q)}")
        except Exception as e:
            log.warning("himalayas: %s", e)
            continue
        for it in data.get("jobs", []):
            gid = str(it.get("guid") or it.get("applicationLink"))
            out[gid] = Vacancy(
                source="himalayas", external_id=gid, url=it.get("applicationLink") or gid, title=it.get("title", ""),
                company=it.get("companyName", ""), location=", ".join(it.get("locationRestrictions") or []) or "Worldwide",
                remote=True, salary_from=it.get("minSalary"), salary_to=it.get("maxSalary"), currency=it.get("currency") or "USD",
                salary_period="year" if (it.get("salaryPeriod") or "annual") == "annual" else "month",
                description=strip_html(it.get("description")), published_at=str(it.get("pubDate") or ""),
                skills=it.get("categories") or [], lang="en",
            ).finalize()
    return list(out.values())


def wwr(cfg: dict) -> list[Vacancy]:
    out = []
    for cat in ("remote-full-stack-programming-jobs", "remote-front-end-programming-jobs", "remote-back-end-programming-jobs", "all-other-remote-jobs"):
        try:
            root = ET.fromstring(get(f"https://weworkremotely.com/categories/{cat}.rss").content)
        except Exception as e:
            log.warning("wwr: %s", e)
            continue
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            company, _, pos = title.partition(": ")
            desc = strip_html(item.findtext("description"))
            if not _relevant(cfg, f"{title} {desc[:2000]}"):
                continue
            link = item.findtext("link") or ""
            out.append(Vacancy(
                source="wwr", external_id=link, url=link, title=pos or title, company=company, location=item.findtext("region") or "Remote",
                remote=True, description=desc, published_at=item.findtext("pubDate") or "", lang="en",
            ).finalize())
    return out


def workingnomads(cfg: dict) -> list[Vacancy]:
    out = []
    for it in get_json("https://www.workingnomads.com/api/exposed_jobs/"):
        text = f"{it.get('title')} {it.get('tags')} {it.get('category_name')}"
        if not _relevant(cfg, text):
            continue
        out.append(Vacancy(
            source="workingnomads", external_id=it["url"], url=it["url"], title=it["title"], company=it.get("company_name", ""),
            location=it.get("location") or "Remote", remote=True, description=strip_html(it.get("description")),
            published_at=it.get("pub_date", ""), lang="en",
        ).finalize())
    return out


def jobicy(cfg: dict) -> list[Vacancy]:
    out = {}
    for tag in ("ai", "llm", "javascript", "full-stack", "automation", "prompt"):
        if len(tag) < 3:
            continue
        try:
            data = get_json(f"https://jobicy.com/api/v2/remote-jobs?count=50&tag={tag}")
        except Exception as e:
            log.warning("jobicy %s: %s", tag, e)
            continue
        for it in data.get("jobs", []):
            out[str(it["id"])] = Vacancy(
                source="jobicy", external_id=str(it["id"]), url=it["url"], title=it.get("jobTitle", ""), company=it.get("companyName", ""),
                location=it.get("jobGeo", "Remote"), remote=True, salary_from=it.get("annualSalaryMin"), salary_to=it.get("annualSalaryMax"),
                currency=it.get("salaryCurrency") or "USD", salary_period="year", description=strip_html(it.get("jobDescription")),
                published_at=it.get("pubDate", ""), lang="en",
            ).finalize()
    return list(out.values())


def arbeitnow(cfg: dict) -> list[Vacancy]:
    out = []
    data = get_json("https://www.arbeitnow.com/api/job-board-api")
    for it in data.get("data", []):
        if not it.get("remote"):
            continue
        desc = strip_html(it.get("description"))
        if not _relevant(cfg, f"{it.get('title')} {' '.join(it.get('tags') or [])}"):
            continue
        out.append(Vacancy(
            source="arbeitnow", external_id=it["slug"], url=it["url"], title=it["title"], company=it.get("company_name", ""),
            location=it.get("location", ""), remote=True, description=desc, skills=it.get("tags") or [], lang="",
        ).finalize())
    return out


def hackernews(cfg: dict) -> list[Vacancy]:
    """Свежий тред «Ask HN: Who is hiring?». Многие посты содержат email → автоотклик письмом."""
    stories = get_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=5").get("hits", [])
    story = next((s for s in stories if "who is hiring" in (s.get("title") or "").lower()), None)
    if not story:
        return []
    out = {}
    for q in ("remote", "AI", "LLM", "full stack", "javascript", "agents"):
        hits = get_json(f"https://hn.algolia.com/api/v1/search?tags=comment,story_{story['objectID']}&query={quote(q)}&hitsPerPage=100").get("hits", [])
        for h in hits:
            if h.get("parent_id") != int(story["objectID"]):
                continue  # только верхнеуровневые посты работодателей
            text = strip_html(h.get("comment_text"))
            first = text.split("\n", 1)[0]
            parts = [p.strip() for p in first.split("|")]
            if not _relevant(cfg, text[:3000]):
                continue
            out[h["objectID"]] = Vacancy(
                source="hn", external_id=h["objectID"], url=f"https://news.ycombinator.com/item?id={h['objectID']}",
                title=(parts[1] if len(parts) > 1 else first)[:140], company=parts[0][:80], location=" | ".join(parts[2:4])[:120],
                remote=bool(re.search(r"remote", first, re.I)), description=text, published_at=h.get("created_at", ""), lang="en",
            ).finalize()
    return list(out.values())
