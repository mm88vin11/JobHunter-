"""HH.ru API client with OAuth, rate limiting, and automatic token refresh."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlencode, urljoin

import httpx

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)


def parse_expires_at(raw) -> float:
    """Parse an ISO string or float timestamp."""
    try:
        return float(raw)
    except (ValueError, TypeError):
        pass
    from dateutil.parser import isoparse
    return isoparse(str(raw)).timestamp()


HH_ANDROID_SCHEME = "hhandroid"


# ── Errors ──────────────────────────────────────────────────────

class HHApiError(Exception):
    def __init__(self, status_code: int, body: Any = None, message: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(message or f"HH API error {status_code}: {body}")


class HHForbidden(HHApiError):
    pass


class HHBadRequest(HHApiError):
    pass


class HHLimitExceeded(HHApiError):
    pass


class HHRedirect(HHApiError):
    pass


def _raise_for_status(response: httpx.Response, body: Any) -> None:
    code = response.status_code
    if 200 <= code < 300:
        return
    errors_list = body.get("errors", []) if isinstance(body, dict) else []
    error_types = [e.get("type", "") for e in errors_list]

    if code == 429 or "limit_exceeded" in error_types:
        raise HHLimitExceeded(code, body, "Rate limit exceeded")
    if code == 403:
        raise HHForbidden(code, body)
    if code == 400:
        raise HHBadRequest(code, body)
    if code in (301, 302, 303):
        location = response.headers.get("location", "")
        raise HHRedirect(code, body, f"Redirect to {location}")
    raise HHApiError(code, body)


# ── OAuth Client ────────────────────────────────────────────────

@dataclass
class OAuthClient:
    """Handles OAuth token exchange with HH."""
    config: Config
    _http: httpx.Client = field(init=False, repr=False)

    def __post_init__(self):
        self._http = httpx.Client(
            base_url=self.config.hh_oauth_url,
            headers={
                "User-Agent": self.config.hh_user_agent,
            },
            follow_redirects=False,
            timeout=30.0,
            transport=httpx.HTTPTransport(retries=3),
        )

    @property
    def authorize_url(self) -> str:
        params = urlencode({
            "client_id": self.config.hh_client_id,
            "response_type": "code",
            "role": "applicant",
        })
        return f"{self.config.hh_oauth_url}authorize?{params}"

    def exchange_code(self, code: str) -> dict:
        """Exchange authorization code for tokens."""
        resp = self._http.post("/token", data={
            "client_id": self.config.hh_client_id,
            "client_secret": self.config.hh_client_secret,
            "code": code,
            "grant_type": "authorization_code",
        })
        data = resp.json()
        if resp.status_code != 200:
            raise HHApiError(resp.status_code, data, "Token exchange failed")
        return {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_in": data.get("expires_in", 0),
        }

    def refresh_token(self, refresh_token: str) -> dict:
        """Refresh expired access token."""
        resp = self._http.post("/token", data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
        data = resp.json()
        if resp.status_code != 200:
            raise HHApiError(resp.status_code, data, "Token refresh failed")
        return {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_in": data.get("expires_in", 0),
        }

    def close(self):
        self._http.close()


# ── API Client ──────────────────────────────────────────────────

AllowedMethods = Literal["GET", "POST", "PUT", "DELETE"]


@dataclass
class HHApiClient:
    """
    HH.ru REST API client.
    Thread-safe rate limiting, automatic token refresh on 403.
    """
    config: Config
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0

    _http: httpx.Client = field(init=False, repr=False)
    _oauth: OAuthClient = field(init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _prev_request_time: float = field(default=0.0, init=False, repr=False)
    _user_agent: str = field(init=False)

    def __post_init__(self):
        self._user_agent = self.config.hh_user_agent
        self._http = httpx.Client(
            base_url=self.config.hh_api_url,
            follow_redirects=False,
            timeout=30.0,
            transport=httpx.HTTPTransport(retries=3),
        )
        self._oauth = OAuthClient(self.config)

    @property
    def oauth(self) -> OAuthClient:
        return self._oauth

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def is_authenticated(self) -> bool:
        return bool(self.access_token)

    def set_tokens(self, access_token: str, refresh_token: str, expires_at: float):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self._user_agent,
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def request(
        self,
        method: AllowedMethods,
        endpoint: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        delay: float | None = None,
    ) -> Any:
        """Make rate-limited request with auto token refresh."""
        def _do():
            return self._raw_request(method, endpoint, params, data, delay)

        try:
            return _do()
        except HHForbidden:
            if not self.refresh_token:
                raise
            logger.info("Access token rejected (403), refreshing...")
            self._refresh()
            return _do()

    def _raw_request(
        self,
        method: AllowedMethods,
        endpoint: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        delay: float | None = None,
    ) -> Any:
        with self._lock:
            # Rate limiting
            wait = (delay or self.config.hh_api_delay) - (
                time.monotonic() - self._prev_request_time
            )
            if wait > 0:
                time.sleep(wait)

            url = endpoint.lstrip("/")
            kwargs: dict[str, Any] = {"headers": self._headers()}

            if method in ("GET", "DELETE"):
                kwargs["params"] = params
            else:
                kwargs["data"] = data or params

            response = self._http.request(method, url, **kwargs)
            self._prev_request_time = time.monotonic()

        body = response.json() if response.text else {}
        logger.debug("%d %s %s", response.status_code, method, url)
        _raise_for_status(response, body)
        return body

    def _refresh(self):
        tokens = self._oauth.refresh_token(self.refresh_token)
        self.access_token = tokens["access_token"]
        self.refresh_token = tokens["refresh_token"]
        self.expires_at = time.time() + tokens.get("expires_in", 0)
        logger.info("Token refreshed successfully")

    # ── Convenience methods ─────────────────────────────────────

    def get(self, endpoint: str, params: dict[str, Any] | None = None, **kw) -> Any:
        return self.request("GET", endpoint, params=params, **kw)

    def post(self, endpoint: str, data: dict[str, Any] | None = None, **kw) -> Any:
        return self.request("POST", endpoint, data=data, **kw)

    def put(self, endpoint: str, data: dict[str, Any] | None = None, **kw) -> Any:
        return self.request("PUT", endpoint, data=data, **kw)

    def delete(self, endpoint: str, params: dict[str, Any] | None = None, **kw) -> Any:
        return self.request("DELETE", endpoint, params=params, **kw)

    def get_me(self) -> dict:
        return self.get("/me")

    def get_resumes(self) -> list[dict]:
        data = self.get("/resumes/mine")
        return data.get("items", [])

    def get_vacancy(self, vacancy_id: str) -> dict:
        """Fetch full vacancy details by ID."""
        return self.get(f"/vacancies/{vacancy_id}")

    def search_vacancies(
        self,
        query: str,
        filters: dict | None = None,
        max_pages: int = 3,
        per_page: int = 100,
    ) -> list[dict]:
        """Search vacancies via HH API. Returns list of vacancy dicts."""
        filters = filters or {}
        params: dict = {
            "text": query,
            "search_field": "name",
            "order_by": "publication_time",
            "per_page": per_page,
            "page": 0,
        }
        if filters.get("salary_from"):
            params["salary"] = filters["salary_from"]
            params["only_with_salary"] = "false"
        if filters.get("experience"):
            params["experience"] = filters["experience"]
        if filters.get("employment"):
            params["employment"] = filters["employment"]
        if filters.get("schedule"):
            params["schedule"] = filters["schedule"]
        if filters.get("area"):
            params["area"] = filters["area"]

        all_items: list[dict] = []
        for page in range(max_pages):
            params["page"] = page
            try:
                data = self.get("/vacancies", params=params)
            except Exception as e:
                logger.warning("search_vacancies page %d failed: %s", page, e)
                break
            items = data.get("items", [])
            all_items.extend(items)
            if page >= data.get("pages", 1) - 1:
                break
            time.sleep(0.5)
        return all_items

    def close(self):
        self._http.close()
        self._oauth.close()
