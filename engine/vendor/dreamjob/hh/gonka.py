"""
Client for the Gonka network (DeepSeek/Kimi/MiniMax gateway) — used for internal,
non-recruiter-facing model work (scoring, matching, extraction, resume/interview prose).

Gonka speaks the Anthropic Messages API shape (`POST /v1/messages`), not OpenAI
chat.completions — a plain OpenAI-compatible client will not work against it.
Streaming is mandatory: without `"stream": true` Cloudflare kills the connection
at ~100s with a 524, even on short prompts.

Reachable directly with no proxy from most locations, but Cloudflare geo-blocks
Russian IPs — set GONKA_PROXY_URL (unset by default) to route through an egress
proxy when running on a RU host.

Env vars (all optional; an empty/missing GONKA_API_KEYS makes this module inert):
  GONKA_API_KEYS            comma-separated list of jg-... keys
  GONKA_BASE_URL             default: https://gate.joingonka.ai
  GONKA_PROXY_URL            optional outbound proxy (e.g. socks5://host:1080)
  GONKA_INTERNAL_MODEL       default: deepseek-ai/DeepSeek-V4-Flash-0731
  GONKA_TIMEOUT_SECONDS       default: 90
  GONKA_KEY_COOLDOWN_SECONDS  default: 60

This module never enables itself — callers must check their own
`gonka_internal_enabled` config flag before calling `complete`/`complete_json`.
It only decides *how* to talk to Gonka, not *whether* to.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_GONKA_MODEL = "deepseek-ai/DeepSeek-V4-Flash-0731"

_AUTH_FAILURE_RE = re.compile(r"\b(401|403)\b|invalid.?api.?key|unauthorized|authentication", re.I)


class GonkaError(RuntimeError):
    """Raised when Gonka is unreachable, unauthorized, or returns no usable content.

    Callers doing internal (non-letter) work should catch this and fall back to
    the existing AIGate route — never let this crash a handler.
    """


def _base_url() -> str:
    return os.environ.get("GONKA_BASE_URL", "https://gate.joingonka.ai").rstrip("/")


def _messages_url() -> str:
    return f"{_base_url()}/v1/messages"


def _timeout_seconds() -> float:
    try:
        return float(os.environ.get("GONKA_TIMEOUT_SECONDS", "90") or 90)
    except ValueError:
        return 90.0


def _cooldown_seconds() -> float:
    try:
        return float(os.environ.get("GONKA_KEY_COOLDOWN_SECONDS", "60") or 60)
    except ValueError:
        return 60.0


def _default_model() -> str:
    return os.environ.get("GONKA_INTERNAL_MODEL", "").strip() or DEFAULT_GONKA_MODEL


def _mask(key: str) -> str:
    return f"{key[:5]}…{key[-4:]}" if len(key) > 10 else "jg-***"


@dataclass
class _KeyState:
    key: str
    # Whole-key quarantine (auth failures) — epoch seconds; 0 = not quarantined.
    quarantined_until: float = 0.0
    # Per-model quarantine (timeout/429/5xx/empty response) — epoch seconds per model id.
    model_quarantine: dict[str, float] = field(default_factory=dict)


_pool: list[_KeyState] | None = None
_rotor = 0


def _load_pool() -> list[_KeyState]:
    global _pool
    if _pool is None:
        raw = os.environ.get("GONKA_API_KEYS", "")
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        _pool = [_KeyState(key=k) for k in keys]
        if not _pool:
            logger.info("gonka: GONKA_API_KEYS not set — Gonka routing is inert")
    return _pool


def reset_pool_for_tests() -> None:
    """Test-only hook: forces the key pool to reload from env on next use."""
    global _pool, _rotor
    _pool = None
    _rotor = 0


def has_keys() -> bool:
    return bool(_load_pool())


def _available_keys(model: str, now: float) -> list[_KeyState]:
    return [
        state
        for state in _load_pool()
        if state.quarantined_until <= now and state.model_quarantine.get(model, 0.0) <= now
    ]


def _quarantine(state: _KeyState, model: str, reason: str) -> None:
    until = time.time() + _cooldown_seconds()
    if _AUTH_FAILURE_RE.search(reason):
        state.quarantined_until = until
        logger.warning(
            "gonka: key %s quarantined for %.0fs (auth failure: %s)",
            _mask(state.key), _cooldown_seconds(), reason,
        )
    else:
        state.model_quarantine[model] = until
        logger.warning(
            "gonka: key %s + model %s quarantined for %.0fs (%s)",
            _mask(state.key), model, _cooldown_seconds(), reason,
        )


def _client_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"timeout": _timeout_seconds()}
    proxy = os.environ.get("GONKA_PROXY_URL", "").strip()
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def _stream_once(
    key: str,
    *,
    messages: list[dict[str, str]],
    system: str | None,
    model: str,
    max_tokens: int,
    temperature: float,
) -> str:
    """Single streaming call to Gonka's Anthropic-shaped /v1/messages. Raises GonkaError
    on any non-200, transport, or in-stream error. Returns "" if the stream completed
    with no text content — the caller treats that as a failure, not a valid answer."""
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "messages": messages,
    }
    if system:
        payload["system"] = system

    headers = {
        "authorization": f"Bearer {key}",
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    text_parts: list[str] = []
    try:
        with httpx.Client(**_client_kwargs()) as client:
            with client.stream("POST", _messages_url(), headers=headers, json=payload) as response:
                if response.status_code != 200:
                    body = response.read().decode("utf-8", "replace")[:300]
                    raise GonkaError(f"HTTP {response.status_code}: {body}")
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[len("data:"):].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    event_type = event.get("type")
                    if event_type == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text_parts.append(delta.get("text", ""))
                    elif event_type == "error":
                        err = event.get("error") or {}
                        raise GonkaError(f"stream error {err.get('type')}: {err.get('message')}")
    except httpx.TimeoutException as exc:
        raise GonkaError(f"timeout: {exc}") from exc
    except httpx.HTTPError as exc:
        raise GonkaError(f"network error: {exc}") from exc

    return "".join(text_parts).strip()


def complete(
    messages: list[dict[str, str]] | None = None,
    *,
    prompt: str | None = None,
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.0,
) -> str:
    """Complete a prompt via Gonka (Anthropic Messages shape, DeepSeek by default).

    Accepts either `messages` (Anthropic-style `[{"role": "user"|"assistant", "content": str}]`
    — no "system" role in the list, pass that separately) or a single `prompt` string
    which is wrapped as one user message.

    Streams internally and returns the concatenated text. Rotates across
    GONKA_API_KEYS; quarantines a key entirely on auth errors (401/403) and
    quarantines just the failing key+model pair on timeout/429/5xx/empty response.

    Raises GonkaError if no key is configured, all are quarantined, or every
    attempt failed. Callers doing internal work should catch this and fall back
    to the AIGate route rather than let it propagate.
    """
    if messages is None:
        if prompt is None:
            raise ValueError("either 'messages' or 'prompt' is required")
        messages = [{"role": "user", "content": prompt}]

    use_model = model or _default_model()
    now = time.time()
    candidates = _available_keys(use_model, now)
    if not candidates:
        raise GonkaError(f"no available Gonka keys for model={use_model} (unset, exhausted, or quarantined)")

    global _rotor
    offset = _rotor % len(candidates)
    ordered = candidates[offset:] + candidates[:offset]
    _rotor += 1

    errors: list[str] = []
    for state in ordered:
        try:
            text = _stream_once(
                state.key,
                messages=messages,
                system=system,
                model=use_model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except GonkaError as exc:
            errors.append(str(exc))
            _quarantine(state, use_model, str(exc))
            continue

        if not text:
            errors.append("empty response content")
            _quarantine(state, use_model, "empty response content")
            continue

        return text

    raise GonkaError(f"all Gonka keys failed for model={use_model}: {'; '.join(errors[:3])}")


def complete_json(
    messages: list[dict[str, str]] | None = None,
    *,
    prompt: str | None = None,
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.0,
) -> dict:
    """Like complete(), but instructs the model to answer with strict JSON and parses it.

    Tolerates markdown code fences around the JSON. Raises GonkaError on transport
    failure (same as complete()) and ValueError if the response isn't valid JSON.
    """
    instruction = "Respond with ONLY a valid JSON object. No markdown, no commentary."
    json_system = f"{system}\n\n{instruction}" if system else instruction

    raw = complete(
        messages=messages,
        prompt=prompt,
        system=json_system,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gonka did not return valid JSON: {exc}") from exc


def make_gonka_completion_with_fallback(
    fallback: Callable[[str], str],
    *,
    model: str | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.1,
) -> Callable[[str], str]:
    """Wrap a plain-prompt completion callable with a Gonka-first, AIGate-fallback route.

    Returns `call(prompt: str) -> str` matching the `Callable[[str], str]` shape
    candidate/service.py already uses for its internal-work completion. Any Gonka
    failure (no keys, quarantined, network, empty content) transparently falls back
    to `fallback` — this never raises on Gonka's behalf.
    """

    def call(prompt: str) -> str:
        try:
            return complete(prompt=prompt, model=model, max_tokens=max_tokens, temperature=temperature)
        except GonkaError as exc:
            logger.warning("gonka: completion failed, falling back: %s", exc)
            return fallback(prompt)

    return call
