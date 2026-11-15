"""Российские площадки: Хабр Карьера, getmatch, «Работа России» (trudvsem), FL.ru."""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

from ..http import get, get_json
from ..model import Vacancy, strip_html

log = logging.getLogger("jobhunter")


def habr(cfg: dict) -> list[Vacancy]:
    out = {}
    for q in cfg["queries_ru"] + cfg["queries_en"][:5]:
        try:
            data = get_json(f"https://career.habr.com/api/frontend/vacancies?q={quote(q)}&sort=date&type=all&per_page=50")
        except Exception as e:
            log.warning("habr '%s': %s", q, e)
            continue
        for it in data.get("list", []):
            sal = it.get("salary") or {}
            skills = [s["title"] for s in it.get("skills") or []]
            divisions = [d["title"] for d in it.get("divisions") or []]
            out[str(it["id"])] = Vacancy(
                source="habr",
                external_id=str(it["id"]),
                url="https://career.habr.com" + it["href"],
                title=it["title"],
                company=(it.get("company") or {}).get("title", ""),
                location=", ".join(l["title"] for l in it.get("locations") or []),
                remote=bool(it.get("remoteWork")),
                salary_from=sal.get("from"),
                salary_to=sal.get("to"),
                currency=(sal.get("currency") or "rur").upper(),
                description=f"{it['title']}. Квалификация: {it.get('qualification') or ''}. Специализации: {', '.join(divisions)}. Навыки: {', '.join(skills)}.",
                skills=skills,
                published_at=(it.get("publishedDate") or {}).get("date", ""),
                lang="ru",
            )
    # Подтягиваем полное описание для свежих вакансий
    for v in list(out.values())[: cfg.get("habr_details", 60)]:
        try:
            page = get(v.url).text
            m = re.search(r'<div class="vacancy-description__text">(.*?)</div>\s*</div>', page, re.S)
            if m:
                v.description = strip_html(m.group(1))[:10000] + "\n\n" + v.description
        except Exception:
            pass
    return [v.finalize() for v in out.values()]


def getmatch(cfg: dict) -> list[Vacancy]:
    out = {}
    for q in cfg["queries_ru"][:8] + cfg["queries_en"][:4]:
        try:
            data = get_json(f"https://getmatch.ru/api/offers?q={quote(q)}&limit=50&sa=0")
        except Exception as e:
            log.warning("getmatch '%s': %s", q, e)
            continue
        for it in data.get("offers", []):
            fmt = " ".join(str(l.get("format", "")) for l in it.get("location_requirements") or [])
            out[str(it["id"])] = Vacancy(
                source="getmatch",
                external_id=str(it["id"]),
                url="https://getmatch.ru" + it.get("url", ""),
                title=it.get("position", ""),
                company=(it.get("company") or {}).get("name", ""),
                location=", ".join(l.get("label", "") for l in it.get("location_items") or []),
                remote="remote" in fmt,
                salary_from=it.get("salary_display_from"),
                salary_to=it.get("salary_display_to"),
                currency=(it.get("salary_currency") or "RUR").upper(),
                description=strip_html(it.get("offer_description") or it.get("description_html") or ""),
                published_at=it.get("published_at", ""),
                skills=[s.get("title", "") for s in it.get("skills_objects") or [] if isinstance(s, dict)],
            )
    return [v.finalize() for v in out.values()]


def trudvsem(cfg: dict) -> list[Vacancy]:
    """Официальный открытый API портала «Работа России». Часто содержит email работодателя → автоотклик письмом."""
    out = {}
    for q in cfg["queries_ru"][:10]:
        try:
            data = get_json(f"http://opendata.trudvsem.ru/api/v1/vacancies?text={quote(q)}&limit=50")
        except Exception as e:
            log.warning("trudvsem '%s': %s", q, e)
            continue
        for wrap in (data.get("results") or {}).get("vacancies") or []:
            it = wrap.get("vacancy") or {}
            comp = it.get("company") or {}
            desc = "\n".join(filter(None, [strip_html(it.get("duty")), strip_html(it.get("requirements")), strip_html((it.get("requirement") or {}).get("qualification"))]))
            v = Vacancy(
                source="trudvsem",
                external_id=str(it.get("id")),
                url=it.get("vac_url", ""),
                title=it.get("job-name", ""),
                company=comp.get("name", ""),
                location=(it.get("region") or {}).get("name", ""),
                remote="дистанц" in str(it.get("schedule", "")).lower() or "удал" in str(it.get("schedule", "")).lower(),
                salary_from=it.get("salary_min") or None,
                salary_to=it.get("salary_max") or None,
                currency="RUR",
                description=desc,
                published_at=str(it.get("date_modify") or it.get("creation-date") or ""),
                lang="ru",
            )
            if comp.get("email"):
                v.contacts["emails"] = [comp["email"].lower()]
            out[v.external_id] = v
    return [v.finalize() for v in out.values()]


def fl(cfg: dict) -> list[Vacancy]:
    """FL.ru — проектная работа/фриланс по ИИ, ботам и сайтам."""
    try:
        root = ET.fromstring(get("https://www.fl.ru/rss/all.xml").content)
    except Exception as e:
        log.warning("fl.ru: %s", e)
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        m = re.search(r"Бюджет:\s*([\d\s]+)", title)
        out.append(Vacancy(
            source="fl", external_id=link.rstrip("/").split("/")[-2] if "/projects/" in link else link, url=link,
            title=re.sub(r"\s*\(Бюджет:.*?\)\s*$", "", title), company="FL.ru (заказчик)", remote=True,
            salary_from=float(m.group(1).replace(" ", "")) if m else None, currency="RUR",
            description=strip_html(item.findtext("description")) + "\nКатегория: " + (item.findtext("category") or ""),
            published_at=item.findtext("pubDate") or "", lang="ru",
        ).finalize())
    return out
