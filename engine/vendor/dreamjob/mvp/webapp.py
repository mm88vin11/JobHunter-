"""Telegram Mini App surface over the CRM the bot already keeps.

This adds no CRM logic. Every number and every bucket comes from the same
`AutomationStore.crm_*` methods and the same `candidate.automation.crm_bucket`
that `mvp/routers/crm.py` renders in chat, so the two screens can never
disagree about what needs an answer. The Mini App exists for the one thing a
chat screen is bad at: the whole pipeline at once, scrolled with a thumb,
instead of ten rows per group behind a callback round-trip.

Reading is most of it, with exactly one write: sending a reply to an employer.
That one exists because seeing "Расскажите про ваш опыт" and having to switch
to the bot to answer it defeats the point of the screen. It reuses the bot's
own send path (`hh.ru POST /negotiations/{id}/messages`, then
`record_outgoing_message` + `recompute_thread_state`), so there is one way a
message reaches an employer, not two. Everything else -- archiving, outcome
marking, follow-up drafting -- stays in the bot.

That makes proving the request came from the owner's Telegram client the whole
ballgame: a forged one could write to real employers, so init data is checked
three independent ways before any handler runs.

Never queries the CRM tables with raw SQL. Going through the store methods
keeps the Mini App consistent with the bot.

Off unless `WEBAPP_PORT` is set. Binds loopback by default, because
the Mini App should sit behind an HTTPS reverse proxy. Point
`WEBAPP_PUBLIC_URL` at that public origin.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import MenuButtonWebApp, WebAppInfo
from aiogram.utils.web_app import WebAppInitData, safe_parse_webapp_init_data
from aiohttp import web

from candidate.automation import AutomationStore, crm_bucket
from candidate.hh_official import HHOfficialError

from mvp import config
from mvp.automation import _hh_client_for, _hh_thread_url, _hh_vacancy_url
from mvp.settings_store import get_crm_silence_days

log = logging.getLogger(__name__)

# aiogram validates the init-data HMAC but not its age, so a leaked init-data
# string would otherwise stay a valid credential forever. Telegram refreshes
# it on every launch; a day is generous for a session left open.
_DEFAULT_MAX_AGE_SECONDS = 86_400

_THREAD_MESSAGE_LIMIT = 50

# Typed keys rather than bare strings: aiohttp warns on the latter, and these
# are the app's entire configuration.
BOT_TOKEN: web.AppKey[str] = web.AppKey("bot_token", str)
OWNER_ID: web.AppKey[int] = web.AppKey("owner_id", int)
MAX_AGE: web.AppKey[int] = web.AppKey("max_age", int)
# The store and the settings file are injected rather than read from
# `mvp.config` inside the handlers, so a test can point this app at a temp
# database without reassigning the module-level singleton the rest of the bot
# shares. Swapping that global leaks across test files: it made two unrelated
# ranking tests fail in a full-suite run and pass in isolation.
STORE: web.AppKey[AutomationStore] = web.AppKey("store", AutomationStore)
SETTINGS_PATH: web.AppKey[object] = web.AppKey("settings_path", object)

GROUPS = (
    ("reply", "⚡ Ответить"),
    ("task", "📋 Выполнить"),
    ("scheduled", "📅 Запланировано"),
    ("waiting", "⏳ Ждём"),
    ("rejected", "⛔ Отказы"),
)


class AuthError(Exception):
    """Init data missing, forged, stale, or belonging to somebody else."""


def _parse_init_data(raw: str, token: str, *, max_age: int, owner_id: int) -> WebAppInitData:
    """Validate one request's init data, or raise `AuthError`.

    Three independent checks, all of which must pass: the HMAC proves Telegram
    issued it, `auth_date` proves it is recent, and the user id proves it is
    this bot's owner rather than any other Telegram account that happened to
    find the URL.
    """
    if not raw:
        raise AuthError("no init data")
    try:
        data = safe_parse_webapp_init_data(token, raw)
    except ValueError as exc:
        raise AuthError("bad signature") from exc

    age = (datetime.now(timezone.utc) - data.auth_date).total_seconds()
    if age > max_age:
        raise AuthError(f"init data is {int(age)}s old")
    # A clock-skewed client must not be able to mint a far-future auth_date and
    # keep it usable indefinitely.
    if age < -max_age:
        raise AuthError("init data is dated in the future")

    if not data.user or data.user.id != owner_id:
        raise AuthError("not the owner")
    return data


def _authorize(request: web.Request) -> int:
    """Return the authorized user id, or raise `web.HTTPUnauthorized`."""
    raw = request.headers.get("Authorization", "")
    prefix = "tma "
    raw = raw[len(prefix):] if raw.startswith(prefix) else ""
    try:
        data = _parse_init_data(
            raw,
            request.app[BOT_TOKEN],
            max_age=request.app[MAX_AGE],
            owner_id=request.app[OWNER_ID],
        )
    except AuthError as exc:
        log.warning("mini app request rejected: %s", exc)
        raise web.HTTPUnauthorized(text="unauthorized") from exc
    assert data.user is not None  # _parse_init_data rejects init data without one
    return data.user.id


def _thread_payload(thread: dict, *, silence_days: int) -> dict:
    days = int(thread.get("days_silent") or 0)
    bucket = crm_bucket(thread)
    return {
        "id": thread.get("negotiation_id"),
        "employer": thread.get("employer_name") or "Работодатель не указан",
        "vacancy": thread.get("vacancy_name") or "Вакансия",
        "preview": " ".join(str(thread.get("last_message_preview") or "").split()),
        "days_silent": days,
        "messages": int(thread.get("message_count") or 0),
        "group": bucket,
        # The red marker means the same thing here as in the chat screen: this
        # thread has been quiet longer than the owner's own threshold.
        "overdue": bucket == "waiting" and bool(silence_days) and days >= silence_days,
        "hh_url": _hh_thread_url(chat_id=str(thread.get("chat_id") or "")),
        "vacancy_url": _hh_vacancy_url(str(thread.get("vacancy_id") or "")),
    }


def _silence_days(request: web.Request, user_id: int) -> int:
    return get_crm_silence_days(user_id, path=request.app[SETTINGS_PATH])


async def _handle_crm(request: web.Request) -> web.Response:
    user_id = _authorize(request)
    store = request.app[STORE]
    silence_days = await asyncio.to_thread(_silence_days, request, user_id)
    threads = await asyncio.to_thread(store.crm_threads, user_id)
    counters = await asyncio.to_thread(store.crm_counters, user_id, silence_days=silence_days)

    groups: dict[str, list[dict]] = {key: [] for key, _ in GROUPS}
    for thread in threads:
        groups[crm_bucket(thread)].append(_thread_payload(thread, silence_days=silence_days))
    # Same ordering rule as the chat screen: work queues by how long they have
    # been waiting, the date-driven ones by when they last moved.
    for key in ("reply", "task", "waiting"):
        groups[key].sort(key=lambda item: item["days_silent"], reverse=True)

    return web.json_response({
        "counters": counters,
        "silence_days": silence_days,
        "groups": groups,
    })


async def _handle_thread(request: web.Request) -> web.Response:
    user_id = _authorize(request)
    return await _thread_response(request, user_id, request.match_info["negotiation_id"])


async def _thread_response(request: web.Request, user_id: int, negotiation_id: str) -> web.Response:
    store = request.app[STORE]
    thread = await asyncio.to_thread(store.crm_thread, user_id, negotiation_id)
    if thread is None:
        raise web.HTTPNotFound(text="no such thread")
    silence_days = await asyncio.to_thread(_silence_days, request, user_id)
    messages = await asyncio.to_thread(
        store.crm_thread_messages, user_id, negotiation_id, limit=_THREAD_MESSAGE_LIMIT,
    )
    return web.json_response({
        "thread": _thread_payload(thread, silence_days=silence_days),
        "messages": [
            {
                "direction": message.get("direction"),
                "author": message.get("author_name") or "",
                "text": " ".join(str(message.get("text") or "").split()),
                "at": message.get("created_at") or "",
            }
            for message in messages
        ],
    })


async def _handle_reply(request: web.Request) -> web.Response:
    """Send one message to an employer, exactly the way the bot does.

    Every failure path answers with the reason and, like the bot's flow, is
    explicit that nothing was sent -- a reply that silently vanishes is worse
    than one that visibly fails, because the owner would think an employer was
    ignoring them.
    """
    user_id = _authorize(request)
    negotiation_id = request.match_info["negotiation_id"]
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 -- any malformed body is one error
        raise web.HTTPBadRequest(text="ожидался JSON с полем text") from exc
    text = str(payload.get("text") or "").strip()
    if not text:
        raise web.HTTPBadRequest(text="пустое сообщение не отправляется")

    store = request.app[STORE]
    if await asyncio.to_thread(store.crm_thread, user_id, negotiation_id) is None:
        raise web.HTTPNotFound(text="no such thread")

    try:
        client, _account = await _hh_client_for(user_id)
    except HHOfficialError as exc:
        log.warning("HH token refresh failed safely: %s", exc.reason)
        client = None
    if client is None:
        raise web.HTTPBadGateway(
            text="hh.ru не подключён или токен недействителен — сообщение НЕ отправлено",
        )

    try:
        await asyncio.to_thread(
            client._request, "POST",
            f"https://api.hh.ru/negotiations/{negotiation_id}/messages",
            {"message": text},
        )
    except HHOfficialError as exc:
        log.warning("HH reply send failed safely: %s", exc.reason)
        raise web.HTTPBadGateway(
            text=f"hh.ru не подтвердил отправку ({exc.reason}) — сообщение НЕ отправлено",
        ) from exc
    except Exception as exc:  # noqa: BLE001 -- the owner must still learn it failed
        log.exception("Unexpected error sending HH reply from the mini app")
        raise web.HTTPBadGateway(
            text="не удалось отправить — сообщение НЕ отправлено",
        ) from exc

    await asyncio.to_thread(store.record_outgoing_message, user_id, negotiation_id, text)
    await asyncio.to_thread(store.recompute_thread_state, user_id, negotiation_id)
    return await _thread_response(request, user_id, negotiation_id)


async def _handle_index(request: web.Request) -> web.Response:
    # No auth here on purpose: this response is a static shell with no data in
    # it. Everything it renders arrives from /api/*, which does authorize.
    return web.Response(text=INDEX_HTML, content_type="text/html")


def build_app(
    *, bot_token: str, owner_id: int, max_age: int = _DEFAULT_MAX_AGE_SECONDS,
    store: AutomationStore | None = None, settings_path: object = None,
) -> web.Application:
    app = web.Application()
    app[BOT_TOKEN] = bot_token
    app[OWNER_ID] = owner_id
    app[MAX_AGE] = max_age
    # `mvp.config` documents STORE/AUTOMATION as never rebound, so binding the
    # singleton once at build time is safe; tests pass their own instead.
    app[STORE] = store if store is not None else config.AUTOMATION
    app[SETTINGS_PATH] = settings_path
    app.router.add_get("/", _handle_index)
    app.router.add_get("/api/crm", _handle_crm)
    app.router.add_get("/api/thread/{negotiation_id}", _handle_thread)
    app.router.add_post("/api/thread/{negotiation_id}/reply", _handle_reply)
    return app


async def start_webapp(bot: Bot) -> web.AppRunner | None:
    """Serve the Mini App if `WEBAPP_PORT` is set; otherwise do nothing.

    Also publishes the chat menu button when `WEBAPP_PUBLIC_URL` is set, so the
    button follows a tunnel whose hostname changes between restarts instead of
    going stale in BotFather.
    """
    port = os.environ.get("WEBAPP_PORT", "").strip()
    if not port.isdigit():
        return None
    owner = os.environ.get("MVP_OWNER_TELEGRAM_ID", "").strip()
    if not owner.isdigit():
        log.warning("WEBAPP_PORT is set but MVP_OWNER_TELEGRAM_ID is not; mini app stays off")
        return None

    max_age = os.environ.get("WEBAPP_MAX_AGE_SECONDS", "").strip()
    app = build_app(
        bot_token=bot.token,
        owner_id=int(owner),
        max_age=int(max_age) if max_age.isdigit() else _DEFAULT_MAX_AGE_SECONDS,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    host = os.environ.get("WEBAPP_HOST", "127.0.0.1")
    await web.TCPSite(runner, host, int(port)).start()
    log.info("mini app listening on http://%s:%s", host, port)

    public_url = config.webapp_public_url()
    if public_url:
        try:
            await bot.set_chat_menu_button(
                chat_id=int(owner),
                menu_button=MenuButtonWebApp(text="CRM", web_app=WebAppInfo(url=public_url)),
            )
            log.info("mini app menu button published: %s", public_url)
        except Exception as exc:  # noqa: BLE001 -- a failed button must not stop the bot
            log.warning("could not publish the mini app menu button: %s", exc)
    elif os.environ.get("WEBAPP_PUBLIC_URL", "").strip():
        log.warning("WEBAPP_PUBLIC_URL must be https:// for Telegram to open it; button not set")
    return runner


INDEX_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>CRM</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root { color-scheme: light dark; }
  body {
    margin: 0; padding: 0 0 24px;
    font: 15px/1.4 -apple-system, "Segoe UI", Roboto, sans-serif;
    background: var(--tg-theme-bg-color, #fff);
    color: var(--tg-theme-text-color, #000);
  }
  header { position: sticky; top: 0; z-index: 2; background: inherit;
           padding: 10px 12px 8px; border-bottom: 1px solid var(--tg-theme-hint-color, #ccc); }
  #tabs { display: flex; gap: 6px; overflow-x: auto; scrollbar-width: none; }
  #tabs::-webkit-scrollbar { display: none; }
  .tab { flex: 0 0 auto; padding: 6px 11px; border-radius: 16px; border: 0; cursor: pointer;
         font-size: 14px; white-space: nowrap;
         background: var(--tg-theme-secondary-bg-color, #eee);
         color: var(--tg-theme-text-color, #000); }
  .tab[aria-selected="true"] { background: var(--tg-theme-button-color, #2481cc);
                               color: var(--tg-theme-button-text-color, #fff); }
  .card { padding: 11px 12px; border-bottom: 1px solid var(--tg-theme-secondary-bg-color, #eee);
          cursor: pointer; }
  .card b { display: block; }
  .meta { font-size: 13px; color: var(--tg-theme-hint-color, #777); margin-top: 2px; }
  .preview { font-size: 13px; color: var(--tg-theme-hint-color, #777); margin-top: 4px;
             overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
  .overdue b::before { content: "🔴 "; }
  .msg { padding: 8px 12px; }
  .msg .who { font-size: 12px; color: var(--tg-theme-hint-color, #777); }
  .msg.incoming { background: var(--tg-theme-secondary-bg-color, #f2f2f2); }
  .empty, .error { padding: 32px 16px; text-align: center; color: var(--tg-theme-hint-color, #777); }
  .compose { padding: 12px; display: flex; flex-direction: column; gap: 8px; }
  .compose textarea { width: 100%; box-sizing: border-box; padding: 9px; font: inherit; border-radius: 8px;
                      border: 1px solid var(--tg-theme-hint-color, #ccc); resize: vertical;
                      background: var(--tg-theme-bg-color, #fff); color: var(--tg-theme-text-color, #000); }
  .compose button { padding: 11px; border: 0; border-radius: 8px; font: inherit; cursor: pointer;
                    background: var(--tg-theme-button-color, #2481cc);
                    color: var(--tg-theme-button-text-color, #fff); }
  .compose button:disabled { opacity: .5; }
  .note { font-size: 13px; color: var(--tg-theme-hint-color, #777); min-height: 1.2em; }
  a.hh { display: block; padding: 12px; text-align: center;
         color: var(--tg-theme-link-color, #2481cc); }
</style>
</head>
<body>
<header><div id="tabs"></div></header>
<main id="list"><div class="empty">Загружаю…</div></main>
<script>
const tg = window.Telegram.WebApp;
tg.ready(); tg.expand();

const TITLES = {reply:"⚡ Ответить", task:"📋 Выполнить", scheduled:"📅 Запланировано",
                waiting:"⏳ Ждём", rejected:"⛔ Отказы"};
const ORDER = ["reply","task","scheduled","waiting","rejected"];
let state = {data:null, tab:"reply"};

async function api(path) {
  const res = await fetch(path, {headers:{"Authorization":"tma " + tg.initData}});
  if (!res.ok) throw new Error(res.status === 401 ? "Телеграм не подтвердил вход" : "Ошибка " + res.status);
  return res.json();
}

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function renderTabs() {
  document.getElementById("tabs").innerHTML = ORDER.map((key) => {
    const n = (state.data.groups[key] || []).length;
    return `<button class="tab" data-tab="${key}" aria-selected="${key === state.tab}">` +
           `${TITLES[key]} ${n}</button>`;
  }).join("");
}

function renderList() {
  const items = state.data.groups[state.tab] || [];
  const main = document.getElementById("list");
  if (!items.length) { main.innerHTML = '<div class="empty">Здесь пусто — и это хорошо.</div>'; return; }
  main.innerHTML = items.map((t) =>
    `<div class="card ${t.overdue ? "overdue" : ""}" data-id="${esc(t.id)}">` +
    `<b>${esc(t.employer)}</b>` +
    `<div class="meta">${esc(t.vacancy)} · ${t.days_silent} дн. · ${t.messages} сообщ.</div>` +
    `<div class="preview">${esc(t.preview) || "(нет сообщений)"}</div></div>`
  ).join("");
}

function renderThread(id, data) {
  const t = data.thread;
  document.getElementById("list").innerHTML =
    `<div class="card"><b>${esc(t.employer)}</b>` +
    `<div class="meta">${esc(t.vacancy)} · тишина ${t.days_silent} дн.</div></div>` +
    (data.messages.length
      ? data.messages.map((m) =>
          `<div class="msg ${m.direction === "incoming" ? "incoming" : "outgoing"}">` +
          `<div class="who">${m.direction === "incoming" ? "Работодатель" : "Я"} · ${esc(m.at).slice(0,10)}</div>` +
          `<div>${esc(m.text)}</div></div>`).join("")
      : '<div class="empty">Сообщений пока нет.</div>') +
    `<div class="compose">` +
    `<textarea id="reply" rows="3" placeholder="Ответ рекрутёру…"></textarea>` +
    `<button id="send" data-id="${esc(id)}">Отправить в hh.ru</button>` +
    `<div id="sendnote" class="note"></div></div>` +
    `<a class="hh" href="${esc(t.hh_url)}" target="_blank" rel="noopener">Открыть переписку на hh.ru →</a>` +
    (t.vacancy_url
      ? `<a class="hh" href="${esc(t.vacancy_url)}" target="_blank" rel="noopener">Вакансия и компания →</a>`
      : "");
}

async function openThread(id) {
  document.getElementById("list").innerHTML = '<div class="empty">Загружаю…</div>';
  // The back button lives outside the try: on clients where BackButton is
  // missing it throws, and inside the try that exception replaced a thread
  // that had already rendered with an error screen — the thread looked like
  // it refused to open when it had in fact loaded fine.
  try {
    renderThread(id, await api("/api/thread/" + encodeURIComponent(id)));
  } catch (err) {
    document.getElementById("list").innerHTML = `<div class="error">${esc(err.message)}</div>`;
    return;
  }
  showBack();
}

function showBack() {
  try { tg.BackButton.show(); tg.BackButton.onClick(back); } catch (e) { /* старый клиент */ }
}

function back() {
  try { tg.BackButton.hide(); tg.BackButton.offClick(back); } catch (e) { /* старый клиент */ }
  renderList();
}

async function sendReply(id, button) {
  const field = document.getElementById("reply");
  const note = document.getElementById("sendnote");
  const text = (field.value || "").trim();
  if (!text) { note.textContent = "Пустое сообщение не отправляется."; return; }
  button.disabled = true; field.disabled = true;
  note.textContent = "Отправляю…";
  try {
    const res = await fetch("/api/thread/" + encodeURIComponent(id) + "/reply", {
      method: "POST",
      headers: {"Authorization": "tma " + tg.initData, "Content-Type": "application/json"},
      body: JSON.stringify({text: text}),
    });
    if (!res.ok) throw new Error(await res.text() || "Ошибка " + res.status);
    renderThread(id, await res.json());
    document.getElementById("sendnote").textContent = "✅ hh.ru подтвердил отправку.";
  } catch (err) {
    // Never clear the field on failure: the drafted text is the expensive part.
    button.disabled = false; field.disabled = false;
    note.textContent = "❌ " + err.message;
  }
}

document.getElementById("tabs").addEventListener("click", (e) => {
  const button = e.target.closest(".tab");
  if (!button) return;
  state.tab = button.dataset.tab;
  renderTabs(); renderList();
});
document.getElementById("list").addEventListener("click", (e) => {
  const send = e.target.closest("#send");
  if (send) { sendReply(send.dataset.id, send); return; }
  const card = e.target.closest(".card[data-id]");
  if (card) openThread(card.dataset.id);
});

api("/api/crm").then((data) => {
  state.data = data;
  // Land on the first tab that actually has something in it, so opening the app
  // never shows an empty screen while work waits one tab over.
  state.tab = ORDER.find((key) => (data.groups[key] || []).length) || "reply";
  renderTabs(); renderList();
}).catch((err) => {
  document.getElementById("list").innerHTML =
    '<div class="error">' + esc(err.message) + "</div>";
});
</script>
</body>
</html>
"""
