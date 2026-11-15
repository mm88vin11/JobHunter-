from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping, Sequence

#: hh.ru vacancy sort orders used by this project.
#: ``relevance`` is the default for the automation loop: it returns the best
#: matches for the query instead of the newest postings. ``publication_time``
#: stays available for a deliberate freshness pass.
ORDER_BY_RELEVANCE = "relevance"
ORDER_BY_FRESH = "publication_time"


def _as_values(value) -> tuple:
    """Normalize a filter into a tuple of query values (hh repeats params)."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes, int)):
        return (value,) if str(value) != "" else ()
    if isinstance(value, Sequence):
        return tuple(item for item in value if str(item) != "")
    return (value,)


class HHOfficialError(RuntimeError):
    def __init__(self, status: int, reason: str, errors: list[dict] | None = None):
        self.status = status
        self.reason = reason
        self.errors = errors or []
        super().__init__(f"HH API {status}: {reason}")


def authorize_url(client_id: str) -> str:
    return "https://hh.ru/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "role": "applicant",
    })


def _reason(body: dict) -> str:
    errors = body.get("errors") if isinstance(body, dict) else None
    if errors and isinstance(errors[0], dict):
        return str(errors[0].get("value") or errors[0].get("type") or "api_error")
    return "api_error"


class HHOfficialClient:
    def __init__(self, *, client_id: str, client_secret: str, access_token: str = ""):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.last_response_headers: dict[str, str] = {}
        self.last_response_status: int = 0

    def _request(
        self, method: str, url: str, data: dict | None = None, *,
        auth: bool = True, json_data: dict | None = None,
    ) -> dict:
        headers = {
            "User-Agent": os.environ.get(
                "HH_USER_AGENT", "DreamJobBot/1.0 (contact: you@example.com)"
            ),
            "Accept": "application/json",
        }
        if auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if json_data is not None:
            headers["Content-Type"] = "application/json"
            encoded = json.dumps(json_data, ensure_ascii=False).encode()
        else:
            encoded = urllib.parse.urlencode(data).encode() if data is not None else None
        request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                self.last_response_headers = dict(response.headers.items())
                self.last_response_status = response.status
                raw = response.read()
                try:
                    return json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    return {}
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except Exception:
                body = {}
            self.last_response_status = exc.code
            errors = body.get("errors") if isinstance(body, dict) else None
            raise HHOfficialError(exc.code, _reason(body), errors) from exc

    def exchange_code(self, code: str) -> dict:
        tokens = self._request("POST", "https://api.hh.ru/token", {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
        }, auth=False)
        tokens["expires_at"] = time.time() + int(tokens.get("expires_in", 0))
        self.access_token = str(tokens["access_token"])
        return tokens

    def refresh(self, refresh_token: str) -> dict:
        tokens = self._request("POST", "https://api.hh.ru/token", {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, auth=False)
        tokens["expires_at"] = time.time() + int(tokens.get("expires_in", 0))
        self.access_token = str(tokens["access_token"])
        return tokens

    def me(self) -> dict:
        return self._request("GET", "https://api.hh.ru/me")

    def resumes(self) -> list[dict]:
        return list(self._request("GET", "https://api.hh.ru/resumes/mine").get("items", []))

    def resume(self, resume_id: str) -> dict:
        return self._request("GET", f"https://api.hh.ru/resumes/{resume_id}")

    def clone_resume(self, source_resume_id: str, patch: dict) -> str:
        body = self._request("POST", "https://api.hh.ru/resume_profile", json_data={
            "clone_resume_id": source_resume_id,
            "entry_point": "default",
            "update_profile": False,
        })
        resume_id = str((body.get("resume") or {}).get("id", ""))
        if not resume_id:
            raise HHOfficialError(502, "resume_id_missing")
        return resume_id

    def update_resume(self, resume_id: str, patch: dict) -> None:
        self._request(
            "PUT",
            f"https://api.hh.ru/resume_profile/{resume_id}",
            json_data={"resume": patch},
        )

    def publish_resume(self, resume_id: str) -> None:
        self._request("POST", f"https://api.hh.ru/resumes/{resume_id}/publish")

    def vacancy(self, vacancy_id: str) -> dict:
        return self._request("GET", f"https://api.hh.ru/vacancies/{vacancy_id}")

    def search_vacancies(
        self, text: str, *, page: int = 0, per_page: int = 20,
        search_fields: Sequence[str] = (),
        salary: int | None = None,
        only_with_salary: bool | None = None,
        area: str | int | Sequence[str | int] | None = None,
        schedule: str | Sequence[str] | None = None,
        employment: str | Sequence[str] | None = None,
        experience: str | Sequence[str] | None = None,
        professional_role: str | int | Sequence[str | int] | None = None,
        order_by: str = ORDER_BY_RELEVANCE,
        period: int = 30,
        no_magic: bool = True,
        extra_params: Mapping[str, object] | None = None,
    ) -> dict:
        """Search vacancies, pushing the filters hh.ru already supports server-side.

        Backward compatible: ``search_vacancies(text, per_page=..., search_fields=...)``
        keeps working. The only default that changed is ``order_by`` — it is
        now ``relevance`` instead of ``publication_time``, because the old
        default returned the 20 *newest* vacancies rather than the 20 best
        matches. Pass ``order_by=ORDER_BY_FRESH`` for an explicit freshness
        pass (useful for catching new postings before other applicants).

        Every filter accepts either a single value or a sequence; hh.ru
        expects repeated query parameters for multi-value filters, which is
        what this builds.

        ``salary`` + ``only_with_salary=False`` is the important combination:
        hh.ru returns vacancies at or above ``salary`` **plus** vacancies
        with no salary stated. Setting ``only_with_salary=True`` throws away
        every salary-less posting — a deliberate choice, never a default
        here. ``None`` omits the parameter entirely.
        """
        params: list[tuple[str, object]] = [
            ("text", text), ("page", page), ("per_page", per_page),
            ("order_by", order_by), ("period", period),
        ]
        if no_magic:
            params.append(("no_magic", "true"))
        params.extend(("search_field", field) for field in search_fields)
        if salary is not None:
            params.append(("salary", int(salary)))
        if only_with_salary is not None:
            params.append(("only_with_salary", "true" if only_with_salary else "false"))
        for name, value in (
            ("area", area), ("schedule", schedule), ("employment", employment),
            ("experience", experience), ("professional_role", professional_role),
        ):
            params.extend((name, item) for item in _as_values(value))
        for name, value in (extra_params or {}).items():
            params.extend((name, item) for item in _as_values(value))
        query = urllib.parse.urlencode(params)
        return self._request("GET", f"https://api.hh.ru/vacancies?{query}")

    def similar_vacancies(self, resume_id: str, *, page: int = 0, per_page: int = 100) -> dict:
        """hh.ru's own recommendations for one resume.

        Same response shape as :meth:`search_vacancies` (``items``/``pages``/
        ``found``), but the ranking comes from hh reading the resume itself
        instead of a text query. See ``mvp.hh_sync.recommended_hh_candidates``.
        """
        query = urllib.parse.urlencode({"page": page, "per_page": per_page})
        return self._request(
            "GET", f"https://api.hh.ru/resumes/{resume_id}/similar_vacancies?{query}",
        )

    def negotiations(self, *, page: int = 0, per_page: int = 100) -> dict:
        query = urllib.parse.urlencode({"page": page, "per_page": per_page, "order": "desc"})
        return self._request("GET", f"https://api.hh.ru/negotiations?{query}")

    def find_negotiation(
        self, *, vacancy_id: str, resume_id: str, max_pages: int = 5,
    ) -> dict | None:
        for page in range(max_pages):
            response = self.negotiations(page=page)
            for item in response.get("items", []):
                vacancy = item.get("vacancy") or {}
                resume = item.get("resume") or {}
                if str(vacancy.get("id")) == vacancy_id and str(resume.get("id")) == resume_id:
                    return item
            if page + 1 >= int(response.get("pages") or 0):
                break
        return None

    def apply(self, *, vacancy_id: str, resume_id: str, message: str) -> dict:
        return self._request("POST", "https://api.hh.ru/negotiations", {
            "vacancy_id": vacancy_id,
            "resume_id": resume_id,
            "message": message,
        })
