"""HH.ru search, resume selection, live enrichment, and browser-login session helpers.

Search queries and profile hints come from ``candidate_data/v1/manifest.json``.
Each query carries hh.ru server-side filters derived from its profile, and
:func:`search_hh_vacancies` is the filtered, relevance-sorted replacement for
``candidate.hh_html.discover_vacancies_official``. Before this, the search
sent only ``text``/``page``/``per_page``/``order_by=publication_time`` and the
bot paid a model to discard what hh.ru filters for free.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from candidate.data import load_candidate_data
from candidate.export import enrich_resume_document
from candidate.hh_html import vacancy_from_api
from candidate.hh_official import ORDER_BY_FRESH, HHOfficialClient, HHOfficialError
from candidate.hh_resume_sync import ProfileValidationError, load_profile
from candidate.ranking import (
    ProfileApplicability,
    hh_search_filters,
    resolve_applicable_profile,
)
from hh.api_client import HHApiClient

from mvp import config

log = logging.getLogger(__name__)

#: Cache of hh search params per profile key. The profile JSONs are read-only
#: at runtime, so one read per key per process is enough.
_SEARCH_PARAMS_CACHE: dict[str, dict] = {}


def _minimum_salary() -> int:
    try:
        return max(0, int(os.environ.get("MIN_SALARY_RUB", "0")))
    except ValueError:
        return 0


def search_params_for_profile(profile_hint: str) -> dict:
    """hh.ru search filters for one master profile (empty hint -> neutral set).

    Derived from the profile's own ``employments`` / ``schedules`` / ``area``
    and the optional ``MIN_SALARY_RUB`` deployment setting — see
    ``candidate.ranking.hh_search_filters`` for what is sent and why.
    """
    key = str(profile_hint or "")
    if key not in _SEARCH_PARAMS_CACHE:
        profile = None
        if key:
            try:
                profile = load_profile(key)
            except (ProfileValidationError, OSError):
                log.warning("hh profile %r unavailable, using neutral search filters", key)
        _SEARCH_PARAMS_CACHE[key] = hh_search_filters(profile, salary_floor=_minimum_salary())
    return dict(_SEARCH_PARAMS_CACHE[key])


def _balanced_window(queries: list[dict], window: int) -> list[dict]:
    """Keep the rotation, but never let one profile eat the whole window.

    A plain time-rotated slice can otherwise let one profile occupy the whole
    window and starve the others for a cycle.
    """
    chosen = queries[:window]
    all_hints = [hint for hint in dict.fromkeys(q.get("profile_hint") or "" for q in queries) if hint]
    for hint in all_hints:
        present = {q.get("profile_hint") or "" for q in chosen}
        if hint in present:
            continue
        replacement = next((q for q in queries if (q.get("profile_hint") or "") == hint), None)
        if replacement is None:
            continue
        victim_hint, count = Counter(
            q.get("profile_hint") or "" for q in chosen
        ).most_common(1)[0]
        if count < 2:
            break
        for index in range(len(chosen) - 1, -1, -1):
            if (chosen[index].get("profile_hint") or "") == victim_hint:
                chosen[index] = replacement
                break
    return chosen


def _manifest_queries(data) -> list[dict]:
    declared = data.manifest.get("profiles") or {}
    queries: list[dict] = []
    for profile in data.profiles.values():
        entries = [
            {"text": profile.desired_title, "search_fields": ["name"]},
            *(declared.get(profile.key, {}).get("search_queries") or []),
        ]
        for entry in entries:
            query = {"text": entry} if isinstance(entry, str) else dict(entry)
            text = str(query.get("text") or "").strip()
            if text:
                query.update(text=text, profile_hint=profile.key)
                query.setdefault("search_fields", ["name"])
                queries.append(query)
    return queries


def _search_queries(*, mapped_profiles: set[str] | None = None) -> list[dict]:
    """Rotating search queries, each carrying its hh.ru server-side filters.

    ``HH_SEARCH_QUERIES`` (``|``-separated) still overrides the list wholesale —
    the settings screen persists the owner's queries through it. Overridden
    queries carry no profile hint and get the neutral filter set.

    ``mapped_profiles`` (profile keys with a linked hh resume) only reorders
    the window: profiles that can actually apply go first. Nothing is dropped,
    so discovery for an unmapped profile still happens — it is the
    applicability check at generation time (:func:`profile_applicability`)
    that stops the wasted variants.
    """
    configured = [x.strip() for x in os.environ.get("HH_SEARCH_QUERIES", "").split("|") if x.strip()]
    if configured:
        queries = [{"text": text, "search_fields": [], "profile_hint": ""} for text in configured]
    else:
        data = config.ADAPTER.data if config.ADAPTER else load_candidate_data()
        queries = _manifest_queries(data)
    if not queries:
        return []
    interval = max(300, int(os.environ.get("AUTO_SEARCH_INTERVAL_SECONDS", "1200")))
    offset = int(time.time() // interval) % len(queries)
    rotated = queries[offset:] + queries[:offset]
    window = max(4, int(os.environ.get("AUTO_SEARCH_QUERY_WINDOW", "6")))
    selected = _balanced_window(rotated, window)
    if mapped_profiles is not None:
        selected = sorted(
            selected,
            key=lambda item: 0 if (item.get("profile_hint") or "") in mapped_profiles else 1,
        )
    for query in selected:
        query["params"] = search_params_for_profile(query.get("profile_hint") or "")
    return selected


def _fresh_ai_queries() -> list[dict]:
    """Freshness pass over the same manifest-declared search lanes."""
    data = config.ADAPTER.data if config.ADAPTER else load_candidate_data()
    result = _manifest_queries(data)
    for query in result:
        params = search_params_for_profile(query["profile_hint"])
        params.update({"order_by": ORDER_BY_FRESH, "period": 7})
        query.update(discovery_bonus=0, params=params)
    return result


def search_hh_vacancies(
    client: HHOfficialClient,
    queries: list[dict],
    *,
    known_ids: set[str] | None = None,
    max_items: int = 10,
    order_by: str = "",
    per_page: int = 0,
) -> list[dict]:
    """Filtered, relevance-sorted replacement for ``discover_vacancies_official``.

    Same contract (interleave query results, dedup against ``known_ids``,
    fetch full details, return normalized vacancies with ``search_query`` and
    ``search_profile_hint`` attached), but each query is sent to hh.ru with
    its own server-side filters: salary floor, employment, experience and
    ``order_by=relevance``. Format stays client-side because HH deprecated the
    old combined ``schedule`` parameter.

    Pass ``order_by=ORDER_BY_FRESH`` to run a freshness pass over the same
    queries — useful for catching new postings before other applicants.
    """
    candidates = search_hh_candidates(
        client, queries, known_ids=known_ids,
        per_page=per_page or max(20, max_items), order_by=order_by,
    )
    return hydrate_hh_vacancies(client, candidates, max_items=max_items)


def search_hh_candidates(
    client: HHOfficialClient,
    queries: list[dict],
    *,
    known_ids: set[str] | None = None,
    per_page: int = 20,
    max_pages: int = 1,
    order_by: str = "",
    force_recommendations: bool = False,
) -> list[dict]:
    """Collect cheap search rows; full vacancy pages are loaded only after ranking.

    Also folds in the recommendations lane (hh.ru's own ``similar_vacancies``
    per resume, see :func:`recommended_hh_candidates`) after the text
    queries, gated by ``HH_RECOMMENDATIONS_ENABLED`` and its own cadence
    timer — this is the one call both of automation.py's discovery lanes
    (the rotating text-query lane and the always-on AI lane) share, so
    hooking in here needs no change to the automation cycle itself; the
    cadence gate, not this function, is what stops it firing twice when
    both callers run back-to-back in the same cycle. ``force_recommendations``
    bypasses that gate — for tests only, production call sites never pass it.
    """
    known = set(known_ids or ())
    search_results: list[tuple[dict, list[dict]]] = []
    for query in queries:
        params = dict(query.get("params") or search_params_for_profile(query.get("profile_hint") or ""))
        if order_by:
            params["order_by"] = order_by
        items: list[dict] = []
        for page in range(max(1, max_pages)):
            try:
                response = client.search_vacancies(
                    query["text"], page=page, per_page=per_page,
                    search_fields=tuple(query.get("search_fields") or ()),
                    **params,
                )
            except Exception as exc:  # network/HH errors must not kill the cycle
                log.warning("HH search failed for %r: %s", query.get("text"), exc)
                break
            page_items = list(response.get("items") or [])
            items.extend(page_items)
            if len(page_items) < per_page:
                break
        search_results.append((query, items))

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
    by_id = {
        str(item.get("id") or ""): item
        for _, items in search_results for item in items if item.get("id")
    }
    for vacancy_id, query in candidates:
        if vacancy_id in seen:
            continue
        seen.add(vacancy_id)
        vacancy = vacancy_from_api(
            by_id[vacancy_id], search_query=query["text"],
            search_profile_hint=str(query.get("profile_hint") or ""),
        )
        vacancy["discovery_bonus"] = int(query.get("discovery_bonus") or 0)
        vacancy["reserve_hydration"] = bool(query.get("reserve_hydration"))
        result.append(vacancy)

    for vacancy in _maybe_recommended_candidates(
        client, known=known | seen, force=force_recommendations,
    ):
        vacancy_id = str(vacancy.get("external_id") or "")
        if vacancy_id and vacancy_id not in seen:
            seen.add(vacancy_id)
            result.append(vacancy)
    return result


def hydrate_hh_vacancies(
    client: HHOfficialClient, candidates: list[dict], *, max_items: int,
) -> list[dict]:
    """Load full descriptions for an already-prioritised candidate slice."""
    result: list[dict] = []
    for candidate in candidates[:max(0, max_items)]:
        vacancy_id = str(candidate.get("external_id") or "")
        if not vacancy_id:
            continue
        try:
            result.append(vacancy_from_api(
                client.vacancy(vacancy_id),
                search_query=str(candidate.get("search_query") or ""),
                search_profile_hint=str(candidate.get("search_profile_hint") or ""),
            ))
        except Exception:
            continue
    return result


def mapped_resume_profiles(user_id: int) -> dict[str, str]:
    """``resume_id -> profile_key`` for every profile with a linked, still-live hh.ru resume.

    A mapping is only counted when the resume still exists on the account, so
    a resume deleted on hh.ru does not keep a profile looking applicable.
    Keyed by resume (not by profile, see :func:`mapped_profile_keys` for that)
    because the recommendations lane calls hh.ru once per resume and only
    needs the profile key to stamp ``search_profile_hint`` on what comes back.
    """
    account = config.AUTOMATION.hh_account(user_id)
    if not account:
        return {}
    live_ids = {str(item.get("id") or "") for item in (account.get("resumes") or [])}
    mapped: dict[str, str] = {}
    for profile_key in _profile_keys():
        mapping = config.AUTOMATION.resume_mapping(user_id, profile_key)
        resume_id = str((mapping or {}).get("resume_id") or "")
        if resume_id and (not live_ids or resume_id in live_ids):
            mapped[resume_id] = profile_key
    return mapped


def mapped_profile_keys(user_id: int) -> set[str]:
    """Profile keys that have a linked hh.ru resume in ``hh_resume_mappings``.

    A mapping is only counted when the resume still exists on the account, so
    a resume deleted on hh.ru does not keep a profile looking applicable.
    """
    return set(mapped_resume_profiles(user_id).values())


def _profile_keys() -> tuple[str, ...]:
    data = config.ADAPTER.data if config.ADAPTER else load_candidate_data()
    return tuple(data.profiles.keys())


def profile_applicability(
    user_id: int, profile_key: str, *, ranked_alternatives: tuple[str, ...] = (),
) -> ProfileApplicability:
    """Can a variant for ``profile_key`` ever be sent for this user?

    Call this before generating a variant so an unmapped profile does not
    consume a model call.

    * ``action == "generate"``         -> generate with ``profile_key``
    * ``action == "fallback"``         -> generate with ``effective_profile_key``
    * ``action == "manual_required"``  -> queue the vacancy as manual-required,
      show ``reason`` to the owner, do not spend a generation on it.
    """
    return resolve_applicable_profile(
        profile_key, mapped_profile_keys(user_id),
        ranked_alternatives=ranked_alternatives,
    )


# ---------------------------------------------------------------------------
# Recommendations lane: hh.ru's own ``similar_vacancies`` per mapped resume,
# a second discovery source next to the manifest text queries.
#
# Wiring note: automation.py is not touched by this change. The lane hooks
# into `search_hh_candidates` below — the one function both of automation.py's
# existing discovery calls (the rotating text-query lane and the always-on
# AI lane) already funnel through — so both callers pick it up for free and
# every recommended vacancy flows through the exact same trap screening /
# ranker / threshold / dedup path as a text-query hit, with no gate skipped.
# ---------------------------------------------------------------------------

#: Marker written into the vacancy's ``search_query`` field so this lane's
#: origin stays visible downstream exactly like a real text query.
RECOMMENDATIONS_SEARCH_QUERY_MARKER = "hh:similar_vacancies"

#: hh.ru's own page-size ceiling for this endpoint, and what we request.
#: One page per mapped resume, not deep pagination — this mirrors the manual
#: probe that found the gap (``per_page=100&page=0``) and keeps request
#: volume trivially small (one call per resume per interval). The cadence
#: gate below re-runs this on a schedule instead of paging deeper in one
#: go, so coverage still widens over time without a burst of calls.
_RECOMMENDATIONS_PER_PAGE = 100

#: Cap on new vacancies kept from one lane fetch. This lane only fetches the
#: recommendation listing; detail reads remain bounded by
#: ``AUTO_SEARCH_HYDRATE_LIMIT``.
#: Override with HH_RECOMMENDATIONS_MAX_PER_CYCLE.
_RECOMMENDATIONS_DEFAULT_CAP = 200

#: Politeness delay between per-resume requests (matches the 0.345s used
#: elsewhere for hh.ru's browser-driven client, see _hh_browser_client).
_RECOMMENDATIONS_PAGE_DELAY_SECONDS = 0.35

#: Retention cap mirrors AutomationStore's own filtered_vacancies cap (see
#: candidate.automation._FILTERED_MAX_ROWS_PER_USER) — large enough that
#: "give me every filtered id" and "give me the last N" are the same query.
_FILTERED_DEDUP_LIMIT = 5000

#: Cadence state is process-lifetime, not per-call: hh.ru gets hit for
#: recommendations at most once per HH_RECOMMENDATIONS_INTERVAL_SECONDS
#: regardless of how many times search_hh_candidates runs in that window
#: (the automation cycle calls it twice per pass — narrow lane, then the
#: always-on AI lane — and both share this one gate).
_recommendations_state: dict[str, float] = {"last_fetch_monotonic": 0.0}


def recommendations_enabled() -> bool:
    """Whether the hh.ru recommendations lane is enabled (default: on)."""
    return os.environ.get("HH_RECOMMENDATIONS_ENABLED", "1").strip().lower() not in {"0", "false", "no"}


def _recommendations_interval_seconds() -> int:
    return max(60, int(os.environ.get("HH_RECOMMENDATIONS_INTERVAL_SECONDS", "1800")))


def _recommendations_cap() -> int:
    return max(1, int(os.environ.get("HH_RECOMMENDATIONS_MAX_PER_CYCLE", str(_RECOMMENDATIONS_DEFAULT_CAP))))


def recommendations_due(*, force: bool = False) -> bool:
    """Cadence gate: at most one hh.ru hit per HH_RECOMMENDATIONS_INTERVAL_SECONDS
    (default 1800s = 30 min), independent of AUTO_SEARCH_INTERVAL_SECONDS
    (default 1200s = 20 min, drives the automation cycle itself) —
    recommendations do not change fast enough to need every cycle. This avoids
    unnecessary bursts of sequential calls.
    """
    if force:
        return True
    elapsed = time.monotonic() - _recommendations_state["last_fetch_monotonic"]
    return elapsed >= _recommendations_interval_seconds()


def _mark_recommendations_fetched() -> None:
    _recommendations_state["last_fetch_monotonic"] = time.monotonic()


def _owner_user_id() -> int | None:
    """Single-owner bot: resolve the one user id this lane ever runs for.

    Same env var and the same "must be all digits" guard used everywhere
    else this project resolves the owner (mvp/automation.py, mvp/bump.py,
    mvp/followup.py) — kept local here instead of imported so this module
    never needs to import from mvp/automation.py.
    """
    owner_raw = os.environ.get("MVP_OWNER_TELEGRAM_ID", "")
    return int(owner_raw) if owner_raw.isdigit() else None


def _filtered_hh_ids(user_id: int) -> set[str]:
    """Vacancy ids already cut into the «Отфильтрованные» bin.

    ``known_hh_ids`` (candidate_variants) alone is not enough dedup for this
    lane: hh.ru keeps recommending the same rejected vacancy every cycle
    even after the trap/ranker gates already cut it once, and re-running
    those gates on a posting the owner already effectively said no to
    re-bills whatever the ranker's model nudge costs for nothing new.
    """
    rows = config.AUTOMATION.filtered_vacancies(user_id, limit=_FILTERED_DEDUP_LIMIT)
    return {str(row.get("vacancy_id") or "") for row in rows if row.get("vacancy_id")}


def recommended_hh_candidates(
    client: HHOfficialClient, user_id: int, *,
    known_ids: set[str] | None = None, cap: int | None = None,
) -> list[dict]:
    """hh.ru's own resume recommendations, normalized like ``search_hh_candidates``.

    Second discovery lane: always dedup'd against both ``known_ids``
    (candidate_variants, passed in by the caller) and the filtered_vacancies
    bin (read here) so a re-run never re-processes or re-bills a posting the
    pipeline already decided about. Every returned dict carries
    ``search_query=RECOMMENDATIONS_SEARCH_QUERY_MARKER`` and the resume's
    mapped ``search_profile_hint`` (via :func:`mapped_resume_profiles`), so
    downstream code — trap screening, the ranker, the threshold gate — treats
    it exactly like a text-query hit; nothing here shortcuts a gate.

    Any hh.ru error for one resume is logged and skipped, never raised: one
    stale/expired resume must not stop recommendations for the others, and
    this function's caller (``search_hh_candidates``) already assumes the
    lane can never break the ordinary text-query cycle it shares this
    function with.
    """
    resume_profiles = mapped_resume_profiles(user_id)
    if not resume_profiles:
        return []
    excluded = set(known_ids or ()) | _filtered_hh_ids(user_id)
    budget = max(1, int(cap if cap is not None else _recommendations_cap()))
    result: list[dict] = []
    seen: set[str] = set()
    fetched = 0
    resumes_queried = 0
    for index, (resume_id, profile_key) in enumerate(resume_profiles.items()):
        if len(result) >= budget:
            break
        if index:
            time.sleep(_RECOMMENDATIONS_PAGE_DELAY_SECONDS)
        try:
            response = client.similar_vacancies(
                resume_id, page=0, per_page=_RECOMMENDATIONS_PER_PAGE,
            )
        except HHOfficialError as exc:
            log.warning("HH similar_vacancies failed for resume %s: %s", resume_id, exc.reason)
            continue
        except Exception as exc:  # network noise must not break the lane
            log.warning("HH similar_vacancies failed for resume %s: %s", resume_id, type(exc).__name__)
            continue
        resumes_queried += 1
        items = list(response.get("items") or [])
        fetched += len(items)
        for item in items:
            vacancy_id = str(item.get("id") or "")
            if not vacancy_id or vacancy_id in seen or vacancy_id in excluded:
                continue
            if len(result) >= budget:
                break
            seen.add(vacancy_id)
            vacancy = vacancy_from_api(
                item, search_query=RECOMMENDATIONS_SEARCH_QUERY_MARKER,
                search_profile_hint=profile_key,
            )
            vacancy["discovery_bonus"] = 0
            vacancy["reserve_hydration"] = False
            result.append(vacancy)
    skipped = fetched - len(result)
    log.info(
        "HH recommendations lane: %d/%d mapped resumes queried, %d items fetched, "
        "%d new vacancies kept, %d skipped (dedup/cap)",
        resumes_queried, len(resume_profiles), fetched, len(result), skipped,
    )
    return result


def _maybe_recommended_candidates(
    client: HHOfficialClient, *, known: set[str], force: bool = False,
) -> list[dict]:
    """The recommendations lane's entry point from ``search_hh_candidates``.

    Cheapest checks first (env flag, then the in-memory cadence timer) so a
    disabled or off-cadence lane never touches the automation store or
    hh.ru. Any failure inside the lane — hh.ru error, store error, anything
    — is caught here as a last resort (``recommended_hh_candidates`` already
    isolates a single resume's hh.ru error) so it can never affect the
    text-query results ``search_hh_candidates`` was already asked for.
    """
    if not force and not (recommendations_enabled() and recommendations_due()):
        return []
    user_id = _owner_user_id()
    if user_id is None:
        return []
    _mark_recommendations_fetched()
    try:
        return recommended_hh_candidates(client, user_id, known_ids=known)
    except Exception as exc:
        log.warning("HH recommendations lane failed safely: %s", type(exc).__name__)
        return []


# ---------------------------------------------------------------------------
# Live resume reads (the card browser never shows a stored snapshot)
# ---------------------------------------------------------------------------

#: Seconds a freshly fetched hh.ru resume may be reused. Arrow-scrolling a
#: 33-card queue back and forth would otherwise fire one API call per press for
#: a document that only changes when the owner edits it. Any write we make to a
#: resume drops its entry immediately via :func:`invalidate_live_resume`, so the
#: window can only ever hide a change made from hh.ru's own UI, and only for a
#: minute.
LIVE_RESUME_TTL_SECONDS = 60.0

_LIVE_RESUME_CACHE: dict[str, tuple[float, dict]] = {}


def invalidate_live_resume(resume_id: str = "") -> None:
    """Drop one resume (or the whole cache) so the next card read hits hh.ru."""
    if resume_id:
        _LIVE_RESUME_CACHE.pop(str(resume_id), None)
    else:
        _LIVE_RESUME_CACHE.clear()


async def live_resume(user_id: int, profile_key: str, *, force: bool = False) -> dict:
    """The profile's resume as hh.ru has it right now.

    Returns ``{"state", "reason", "resume_id", "resume"}`` where ``state`` is
    one of ``ok`` / ``unmapped`` / ``no_hh`` / ``error``. Never raises: a card
    must still render when hh.ru is down, just without live data.
    """
    mapping = config.AUTOMATION.resume_mapping(user_id, profile_key)
    resume_id = str((mapping or {}).get("resume_id") or "")
    if not resume_id:
        return {"state": "unmapped", "reason": "профиль не привязан к резюме на hh.ru",
                "resume_id": "", "resume": None}
    if not force:
        cached = _LIVE_RESUME_CACHE.get(resume_id)
        if cached and (time.monotonic() - cached[0]) < LIVE_RESUME_TTL_SECONDS:
            return {"state": "ok", "reason": "", "resume_id": resume_id, "resume": cached[1]}
    account = config.AUTOMATION.hh_account(user_id)
    if not account:
        return {"state": "no_hh", "reason": "hh.ru не подключён", "resume_id": resume_id, "resume": None}
    client = HHOfficialClient(
        client_id=os.environ.get("HH_CLIENT_ID", ""),
        client_secret=os.environ.get("HH_CLIENT_SECRET", ""),
        access_token=account["access_token"],
    )
    try:
        if float(account.get("expires_at", 0)) <= time.time() + 60 and account.get("refresh_token"):
            tokens = await asyncio.to_thread(client.refresh, account["refresh_token"])
            account.update(tokens)
            await asyncio.to_thread(
                config.AUTOMATION.save_hh_account, user_id, account, account.get("resumes") or [],
            )
        resume = await asyncio.to_thread(client.resume, resume_id)
    except HHOfficialError as exc:
        log.warning("Live resume read failed safely: %s", exc.reason)
        return {"state": "error", "reason": exc.reason, "resume_id": resume_id, "resume": None}
    except Exception as exc:  # pragma: no cover - network stack noise
        log.warning("Live resume read failed safely: %s", type(exc).__name__)
        return {"state": "error", "reason": type(exc).__name__, "resume_id": resume_id, "resume": None}
    _LIVE_RESUME_CACHE[resume_id] = (time.monotonic(), resume)
    return {"state": "ok", "reason": "", "resume_id": resume_id, "resume": resume}


def _resume_status(resume: dict) -> str:
    status = resume.get("status", "")
    return str(status.get("id", "") if isinstance(status, dict) else status).lower()


def _select_hh_resume(resumes: list[dict], profile_key: str, title: str) -> dict | None:
    published = [resume for resume in resumes if _resume_status(resume) in {"published", "visible"}]
    if not published:
        return None
    if len(published) == 1:
        return published[0]
    profile = config.ADAPTER.data.profiles[profile_key]
    target = f"{profile.desired_title} {profile.name} {title}".lower()
    target_tokens = set(re.findall(r"[a-zа-яё]{3,}", target))
    ranked = []
    for resume in published:
        tokens = set(re.findall(r"[a-zа-яё]{3,}", str(resume.get("title", "")).lower()))
        ranked.append((len(target_tokens & tokens), resume))
    score, selected = max(ranked, key=lambda item: item[0])
    return selected if score > 0 else None


def _hh_vacancy_id(vacancy: dict) -> str:
    vacancy_id = str(vacancy.get("external_id") or "").strip()
    if vacancy_id.isdigit():
        return vacancy_id
    match = re.search(r"(?:vacancy/|hh:)(\d+)", " ".join([
        str(vacancy.get("id") or ""), str(vacancy.get("source_url") or ""),
    ]))
    return match.group(1) if match else ""


async def _enrich_payload_from_hh(user_id: int, payload: dict) -> dict:
    account = config.AUTOMATION.hh_account(user_id)
    if not account:
        return payload
    mapping = config.AUTOMATION.resume_mapping(user_id, payload["master_profile"]["key"])
    resume_id = str(mapping["resume_id"]) if mapping else ""
    if not resume_id:
        selected = _select_hh_resume(
            account.get("resumes") or [], payload["master_profile"]["key"],
            payload["generated_variant"]["title"],
        )
        resume_id = str((selected or {}).get("id") or "")
    if not resume_id:
        return payload
    client = HHOfficialClient(
        client_id=os.environ.get("HH_CLIENT_ID", ""),
        client_secret=os.environ.get("HH_CLIENT_SECRET", ""),
        access_token=account["access_token"],
    )
    try:
        if float(account.get("expires_at", 0)) <= time.time() + 60 and account.get("refresh_token"):
            tokens = await asyncio.to_thread(client.refresh, account["refresh_token"])
            account.update(tokens)
            config.AUTOMATION.save_hh_account(user_id, account, account.get("resumes") or [])
        resume = await asyncio.to_thread(client.resume, resume_id)
    except HHOfficialError as exc:
        log.warning("HH resume preview enrichment failed safely: %s", exc.reason)
        return payload
    enriched = enrich_resume_document(payload, resume)
    enriched["hh_resume_snapshot"]["stable_mapping"] = bool(
        mapping and str(mapping.get("resume_id") or "") == resume_id
    )
    enriched["hh_resume_snapshot"]["profile_key"] = payload["master_profile"]["key"]
    return enriched


def _hh_browser_client() -> HHApiClient:
    config = SimpleNamespace(
        hh_client_id=os.environ.get("HH_CLIENT_ID", ""),
        hh_client_secret=os.environ.get("HH_CLIENT_SECRET", ""),
        hh_user_agent=os.environ.get(
            "HH_USER_AGENT", "DreamJobBot/1.0 (contact: you@example.com)"
        ),
        hh_api_url="https://api.hh.ru/",
        hh_oauth_url="https://hh.ru/oauth/",
        hh_api_delay=0.345,
    )
    return HHApiClient(config)


async def _close_hh_browser_session(user_id: int) -> None:
    session = config.HH_BROWSER_SESSIONS.pop(user_id, None)
    if not session:
        return
    auth, client = session
    await auth._cleanup()
    client.close()
    screenshot = getattr(auth, "_screenshot_path", None)
    if screenshot:
        Path(screenshot).unlink(missing_ok=True)
