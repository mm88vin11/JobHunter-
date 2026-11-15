from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from .service import classify_vacancy_type


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_url(url: str) -> str:
    parts = urlsplit(url.rstrip(".,)"))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def normalize_vacancy_input(text: str, *, forwarded_from: str | None = None) -> dict:
    raw_text = text.strip()
    url_match = re.search(r"https?://\S+", raw_text)
    source_url = _canonical_url(url_match.group(0)) if url_match else ""
    host = re.sub(r"^www\.", "", urlsplit(source_url).netloc.lower()) if source_url else ""
    source = "hh" if host == "hh.ru" else (host or ("telegram" if forwarded_from else "manual"))

    body = re.sub(r"https?://\S+", " ", raw_text)
    normalized_text = re.sub(r"\s+", " ", body).strip()
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    title = lines[0][:180] if lines else "Вакансия"

    hh_id = re.search(r"(?:hh\.ru/vacancy/|^)(\d{5,})", source_url)
    path_id = urlsplit(source_url).path.rstrip("/").rsplit("/", 1)[-1] if source_url else ""
    external_id = hh_id.group(1) if hh_id else (path_id if re.fullmatch(r"[\w-]{3,}", path_id) else "")
    content_fingerprint = _digest(normalized_text.casefold()) if normalized_text else ""
    fingerprint_key = f"{source}:{external_id}" if external_id else (source_url or normalized_text.casefold())
    fingerprint = _digest(fingerprint_key)

    vacancy = {
        "id": f"{source}:{external_id}" if external_id else fingerprint[:20],
        "source": source,
        "source_url": source_url,
        "external_id": external_id,
        "employer_name": "",
        "name": title,
        "raw_text": raw_text[:20000],
        "normalized_text": normalized_text[:20000],
        "description": normalized_text[:20000],
        "salary": None,
        "geography": "",
        "fingerprint": fingerprint,
        "content_fingerprint": content_fingerprint,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "forwarded_from": forwarded_from or "",
    }
    vacancy["vacancy_type"] = classify_vacancy_type(vacancy)
    return vacancy


def has_meaningful_description(vacancy: dict) -> bool:
    text = str(vacancy.get("normalized_text") or vacancy.get("description") or "")
    return len(text) >= 80
