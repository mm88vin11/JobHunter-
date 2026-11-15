from __future__ import annotations

import html as html_module
import json
import os
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from .vacancy import normalize_vacancy_input


USER_AGENT = os.environ.get(
    "HH_USER_AGENT", "DreamJobBot/1.0 (contact: you@example.com)"
)


class HHSourceError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"br", "p", "li", "div", "h1", "h2", "h3", "h4", "ul", "ol"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "li", "div", "h1", "h2", "h3", "h4", "ul", "ol"}:
            self.parts.append("\n")


def _fetch(url: str, timeout: float = 30.0) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise HHSourceError(type(exc).__name__) from exc


def search_vacancy_ids(query: str, *, max_items: int = 25) -> list[str]:
    url = "https://hh.ru/search/vacancy?" + urllib.parse.urlencode({
        "text": query,
        "search_field": "name",
        "order_by": "publication_time",
        "hhtmFrom": "vacancy_search_list",
    })
    page = _fetch(url)
    ids = re.findall(r"https?://hh\.ru/vacancy/(\d{5,})", html_module.unescape(page))
    return list(dict.fromkeys(ids))[:max_items]


def _job_posting(page: str) -> dict:
    scripts = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page,
        flags=re.DOTALL | re.IGNORECASE,
    )
    for raw in scripts:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidates = value.get("@graph", []) if isinstance(value, dict) else []
        if isinstance(value, dict):
            candidates = [value, *candidates]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    raise HHSourceError("job_posting_json_ld_missing")


def _plain_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value or "")
    lines = [re.sub(r"\s+", " ", line).strip() for line in " ".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


def fetch_vacancy(vacancy_id: str) -> dict:
    vacancy_id = str(vacancy_id)
    source_url = f"https://hh.ru/vacancy/{vacancy_id}"
    job = _job_posting(_fetch(source_url))
    title = _plain_text(str(job.get("title", ""))) or "Вакансия"
    description = _plain_text(str(job.get("description", "")))
    employer = job.get("hiringOrganization") if isinstance(job.get("hiringOrganization"), dict) else {}
    location = job.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if location else {}
    address = location.get("address", {}) if isinstance(location, dict) else {}
    geography = ", ".join(
        str(address.get(key, "")).strip()
        for key in ("addressCountry", "addressRegion", "addressLocality")
        if address.get(key)
    )
    vacancy = normalize_vacancy_input(f"{title}\n{description}\n{source_url}")
    vacancy.update({
        "id": f"hh:{vacancy_id}",
        "source": "hh",
        "source_url": source_url,
        "external_id": vacancy_id,
        "name": title,
        "employer_name": _plain_text(str(employer.get("name", ""))),
        "description": description[:20000],
        "normalized_text": description[:20000],
        "geography": geography,
        "published_at": str(job.get("datePosted", "")),
        "valid_through": str(job.get("validThrough", "")),
    })
    return vacancy


def vacancy_from_api(
    detail: dict, *, search_query: str = "", search_profile_hint: str = "",
) -> dict:
    vacancy_id = str(detail.get("id") or "")
    source_url = str(detail.get("alternate_url") or f"https://hh.ru/vacancy/{vacancy_id}")
    title = _plain_text(str(detail.get("name") or "")) or "Вакансия"
    snippet = detail.get("snippet") or {}
    description = _plain_text(str(
        detail.get("description")
        or " ".join(str(snippet.get(key) or "") for key in ("responsibility", "requirement"))
    ))
    employment = str((detail.get("employment") or {}).get("id") or "")
    vacancy_type = {"full": "full_time", "project": "project"}.get(employment, "unknown")
    area = detail.get("area") or {}
    address = detail.get("address") or {}
    geography = ", ".join(dict.fromkeys(
        str(value).strip()
        for value in (area.get("name"), address.get("city"), address.get("street"))
        if value
    ))
    vacancy = normalize_vacancy_input(f"{title}\n{description}\n{source_url}")
    vacancy.update({
        "id": f"hh:{vacancy_id}", "external_id": vacancy_id,
        "source": "hh", "source_url": source_url, "name": title,
        "employer_name": str((detail.get("employer") or {}).get("name") or ""),
        "description": description[:20000], "normalized_text": description[:20000],
        "raw_text": description[:20000], "geography": geography,
        "salary": detail.get("salary"), "vacancy_type": vacancy_type,
        "employer": detail.get("employer") or {},
        "area": area, "address": address,
        "employment": detail.get("employment") or {},
        "schedule": detail.get("schedule") or {},
        "work_format": detail.get("work_format") or [],
        "experience": detail.get("experience") or {},
        "professional_roles": detail.get("professional_roles") or [],
        "has_test": bool(detail.get("has_test")),
        "archived": bool(detail.get("archived")),
        "published_at": str(detail.get("published_at") or ""),
        "search_query": search_query,
        "search_profile_hint": search_profile_hint,
        # The employer's own tag list: the sharpest vacancy vocabulary there is, already
        # normalised by hh.ru, and free. Feeds `vacancy_markers`.
        "key_skills": [
            str((item or {}).get("name") or "") for item in (detail.get("key_skills") or [])
        ],
    })
    return vacancy


def discover_vacancies_official(
    client,
    queries: list[dict],
    *,
    known_ids: set[str] | None = None,
    max_items: int = 10,
) -> list[dict]:
    known = known_ids or set()
    search_results: list[tuple[dict, list[dict]]] = []
    for query in queries:
        try:
            response = client.search_vacancies(
                query["text"], per_page=max(5, max_items),
                search_fields=tuple(query.get("search_fields") or ()),
            )
        except Exception:
            continue
        search_results.append((query, list(response.get("items") or [])))

    candidates: list[tuple[str, dict]] = []
    depth = max((len(items) for _, items in search_results), default=0)
    for index in range(depth):
        for query, items in search_results:
            if index >= len(items):
                continue
            vacancy_id = str(items[index].get("id") or "")
            if vacancy_id and vacancy_id not in known:
                candidates.append((vacancy_id, query))

    result: list[dict] = []
    seen: set[str] = set()
    for vacancy_id, query in candidates:
        if vacancy_id in seen:
            continue
        seen.add(vacancy_id)
        try:
            result.append(vacancy_from_api(
                client.vacancy(vacancy_id), search_query=query["text"],
                search_profile_hint=str(query.get("profile_hint") or ""),
            ))
        except Exception:
            continue
        if len(result) >= max_items:
            break
    return result


def discover_vacancies(
    queries: list[str],
    *,
    known_ids: set[str] | None = None,
    max_items: int = 10,
) -> list[dict]:
    known = known_ids or set()
    ids: list[str] = []
    for query in queries:
        ids.extend(search_vacancy_ids(query, max_items=max_items))
    result: list[dict] = []
    for vacancy_id in dict.fromkeys(ids):
        if vacancy_id in known:
            continue
        try:
            result.append(fetch_vacancy(vacancy_id))
        except HHSourceError:
            continue
        if len(result) >= max_items:
            break
    return result
