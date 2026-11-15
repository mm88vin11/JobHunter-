"""Telegram-бот (long polling внутри GitHub Actions — сервер не нужен)."""
from __future__ import annotations

import html
import json
import logging
import os
import re

import requests

from . import apply as ap

log = logging.getLogger("jobhunter")
h = lambda s: html.escape(str(s or ""), quote=False)  # noqa: E731

SRC = {
    "hh": "hh.ru", "habr": "Хабр Карьера", "getmatch": "getmatch", "trudvsem": "Работа России", "fl": "FL.ru", "telegram": "Telegram", "vk": "VK",
    "linkedin": "LinkedIn", "indeed": "Indeed", "glassdoor": "Glassdoor", "zip_recruiter": "ZipRecruiter", "google": "Google Jobs", "bayt": "Bayt",
    "naukri": "Naukri", "bdjobs": "BDJobs", "hellowork": "HelloWork", "remoteok": "RemoteOK", "remotive": "Remotive", "himalayas": "Himalayas",
    "wwr": "We Work Remotely", "workingnomads": "Working Nomads", "jobicy": "Jobicy", "arbeitnow": "Arbeitnow", "hn": "Hacker News",
}

HELP = (
    "<b>JobHunter — ваш автономный охотник за работой</b>\n\n"
    "/run — запустить сбор по всем 24 площадкам прямо сейчас\n/status — статистика и воронка\n/top — 10 лучших вакансий\n"
    "/hot — кто хочет созвониться\n/waiting — отклики в одно касание\n/site — ссылка и пароль на сайт-панель\n/tglogin — автоотправка откликов HR в Telegram\n"
    "/pause, /resume — пауза автооткликов\n/limit 40 — откликов в сутки\n/threshold 62 — порог автоотклика\n/hh — подключить hh.ru"
)


class Bot:
    def __init__(self, store, submit, request_collect):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.store = store
        self.submit = submit                    # запуск тяжёлых задач в фоне
        self.request_collect = request_collect
        self.offset = store.get("tg_offset", 0)
        self.username = ""
        if self.token:
            me = self.api("getMe") or {}
            self.username = me.get("username", "")
            self.api("deleteWebhook", drop_pending_updates=False)
            self.api("setMyCommands", commands=[
                {"command": c, "description": d} for c, d in [
                    ("run", "Запустить поиск и отклики"), ("status", "Статистика"), ("top", "Лучшие вакансии"), ("hot", "Кто хочет созвониться"),
                    ("waiting", "Отклики в одно касание"), ("site", "Сайт-панель"), ("pause", "Пауза"), ("resume", "Продолжить"), ("hh", "Подключить hh.ru")]])

    # ── API ──
    def api(self, method: str, **payload):
        if not self.token:
            return None
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=40)
            j = r.json()
            if not j.get("ok"):
                log.warning("telegram %s: %s", method, str(j)[:300])
            return j.get("result")
        except Exception as e:
            log.warning("telegram %s: %s", method, e)
            return None

    @property
    def owner(self):
        return self.store.get("tg_owner")

    def say(self, text: str, buttons=None) -> None:
        if not self.owner:
            return
        payload = {"chat_id": self.owner, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        self.api("sendMessage", **payload)

    def site_url(self) -> str:
        return site_url()

    # ── карточки ──
    def card(self, v) -> str:
        sal = ""
        if v["salary_from"] or v["salary_to"]:
            cur = (v["currency"] or "").replace("RUR", "₽").replace("RUB", "₽").replace("USD", "$").replace("EUR", "€")
            sal = f"💰 {v['salary_from'] or ''}{'–' + str(v['salary_to']) if v['salary_to'] else ''} {cur}\n"
        ins = json.loads(v["instructions"] or "{}") if "instructions" in v.keys() else {}
        extra = ""
        if ins.get("must_include") or ins.get("must_start_with"):
            extra = "🔑 Инструкции работодателя учтены: " + h(", ".join([*([ins["must_start_with"]] if ins.get("must_start_with") else []), *ins.get("must_include", [])])[:200]) + "\n"
        return (
            f"<b>{h(v['title'])}</b>\n{h(v['company'])} · {SRC.get(v['source'], v['source'])}{' · 🌍 удалённо' if v['remote'] else (' · ' + h(v['location']) if v['location'] else '')}\n"
            f"{sal}⭐ <b>{v['score'] if v['score'] is not None else '—'}</b>/100 · соответствие {v['fit'] or '—'} · условия {v['conditions'] or '—'} · шанс {v['chance'] or '—'}\n"
            + (f"🧠 {h(v['verdict'][:300])}\n" if v["verdict"] else "") + extra + f"🔗 {h(v['url'])}"
        )

    def notify_top(self, v, auto: bool) -> None:
        self.say(f"🎯 <b>Сильное совпадение</b>{' — откликаюсь сам' if auto else ''}\n\n{self.card(v)}",
                 [[{"text": "✅ Откликнуться", "callback_data": f"ap_{v['rowid']}"}, {"text": "🚫 Скрыть", "callback_data": f"sk_{v['rowid']}"}]])

    def notify_applied(self, v, r: dict) -> None:
        if r["status"] == "sent":
            self.say(f"✅ <b>Отклик отправлен</b> ({ {'hh': 'hh.ru', 'email': 'email', 'telegram': 'Telegram'}.get(r['channel'], r['channel']) })\n\n{self.card(v)}\n\n<i>{h(r.get('letter', '')[:1200])}</i>")
            return
        if r["status"] != "waiting_user":
            return
        c = ap.contacts_of(dict(v))
        buttons = []
        if r["channel"] == "telegram" and c["telegram"]:
            buttons.append([{"text": f"✈️ Отправить @{c['telegram'][0]} (1 касание)", "url": ap.tg_link(c["telegram"][0], r.get("letter", ""))}])
        else:
            buttons.append([{"text": "🌐 Открыть вакансию", "url": (v["apply_url"] if "apply_url" in v.keys() and v["apply_url"] else None) or (c["links"][0] if c["links"] else v["url"])}])
        buttons.append([{"text": "✔️ Я отправил", "callback_data": f"mk_{v['rowid']}"}, {"text": "🔁 Переписать", "callback_data": f"rw_{v['rowid']}"}])
        self.say(f"✍️ <b>Отклик готов — нужно одно касание</b>\n{h(r.get('note', ''))}\n\n{self.card(v)}\n\n<code>{h(r.get('letter', '')[:2500])}</code>", buttons)

    def notify_reply(self, app_id: int, a: dict) -> None:
        app = self.store.one("SELECT a.*, v.title, v.company, v.url FROM applications a JOIN vacancies v ON v.id = a.vacancy_id WHERE a.id = ?", app_id)
        head = "🔥🔥 <b>С вами хотят поговорить!</b>" if a.get("hot") else ("❌ <b>Отказ</b>" if a.get("kind") == "reject" else "💬 <b>Ответ работодателя</b>")
        buttons = []
        if a.get("suggested_answer") and app["channel"] == "hh" and app["external_ref"]:
            buttons.append([{"text": "📤 Отправить предложенный ответ", "callback_data": f"sa_{app_id}"}])
        buttons.append([{"text": "🗓 Созвон назначен", "callback_data": f"ms_{app_id}_interview"}, {"text": "🏆 Оффер", "callback_data": f"ms_{app_id}_offer"}])
        text = f"{head}\n<b>{h(app['title'])}</b> · {h(app['company'])}\n{h(app['url'])}\n\n📝 {h(a.get('summary', ''))}"
        if app["last_reply"]:
            text += f"\n\n<b>Они написали:</b>\n{h(app['last_reply'][:1200])}"
        if a.get("suggested_answer"):
            text += f"\n\n<b>Предлагаю ответить:</b>\n<code>{h(a['suggested_answer'][:1500])}</code>"
        self.say(text, buttons)

    # ── входящие ──
    def poll(self, timeout: int = 20) -> None:
        if not self.token:
            return
        ups = self._get_updates(timeout)
        for u in ups or []:
            self.offset = u["update_id"] + 1
            try:
                self.handle(u)
            except Exception as e:
                log.exception("update")
                self.store.event(f"Ошибка бота: {e}", "error")
        self.store.set("tg_offset", self.offset)

    def _get_updates(self, timeout: int):
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.token}/getUpdates",
                              json={"offset": self.offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]}, timeout=timeout + 15)
            return r.json().get("result") or []
        except Exception as e:
            log.warning("getUpdates: %s", e)
            return []

    def vac(self, rowid: str):
        return self.store.one("SELECT rowid, * FROM vacancies WHERE rowid = ?", int(rowid))

    def handle(self, u: dict) -> None:
        msg = u.get("message") or {}
        cb = u.get("callback_query")
        chat = (msg.get("chat") or {}).get("id") or ((cb or {}).get("message") or {}).get("chat", {}).get("id")
        if not chat:
            return
        text = (msg.get("text") or "").strip()
        if not self.owner and text.startswith("/start"):
            self.store.set("tg_owner", chat)
            self.store.event("Telegram подключён")
            self.say("✅ Бот привязан к вам — теперь он работает только с вами.\n\n" + HELP)
            self.cmd_site()
        if chat != self.owner:
            self.api("sendMessage", chat_id=chat, text="Это личный бот JobHunter.")
            return
        if cb:
            self.api("answerCallbackQuery", callback_query_id=cb["id"], text="Принято")
            self.action(cb.get("data", ""))
            return
        if text.startswith("/start "):
            self.action(text.split(" ", 1)[1])  # действия с сайта: t.me/<bot>?start=ap_123
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0]
        ov = self.store.get("settings_override", {}) or {}
        if cmd == "/run":
            self.request_collect()
            self.say("🚀 Запускаю сбор по всем площадкам. Новые вакансии оценю и откликнусь на лучшие, результаты пришлю сюда.")
        elif cmd == "/status":
            self.cmd_status()
        elif cmd == "/top":
            rows = self.store.q("SELECT * FROM vacancies WHERE score IS NOT NULL AND fit IS NOT NULL AND status IN ('scored','manual','applied') ORDER BY score DESC LIMIT 10")
            self.say("🏆 <b>Топ-10</b>\n\n" + ("\n".join(f"{i + 1}. <b>{v['score']}</b> <a href=\"{h(v['url'])}\">{h(v['title'])}</a> — {h(v['company'])}" for i, v in enumerate(rows)) or "Пока пусто — /run"))
        elif cmd == "/hot":
            rows = self.store.q("SELECT a.*, v.title, v.company, v.url FROM applications a JOIN vacancies v ON v.id = a.vacancy_id WHERE a.hot = 1 ORDER BY a.updated_at DESC LIMIT 15")
            self.say("🔥 <b>Хотят с вами говорить</b>\n\n" + ("\n".join(f"• <a href=\"{h(a['url'])}\">{h(a['title'])}</a> — {h(a['company'])} · {a['status']}" for a in rows) or "Пока нет. Как только кто-то позовёт — пришлю сразу."))
        elif cmd == "/waiting":
            rows = self.store.q("SELECT v.rowid, v.*, a.letter, a.channel FROM applications a JOIN vacancies v ON v.id = a.vacancy_id WHERE a.status = 'waiting_user' ORDER BY v.score DESC LIMIT 5")
            if not rows:
                self.say("Все отклики отправлены 👌")
            for v in rows:
                self.notify_applied(v, {"status": "waiting_user", "channel": v["channel"], "letter": v["letter"], "note": "ждёт отправки"})
        elif cmd == "/site":
            self.cmd_site()
        elif cmd in ("/pause", "/resume"):
            ov["paused"] = cmd == "/pause"
            self.store.set("settings_override", ov)
            self.say("⏸ Пауза: новые отклики не отправляются." if ov["paused"] else "▶️ Продолжаю охоту.")
        elif cmd in ("/limit", "/threshold") and arg.strip().isdigit():
            ov["dailyLimit" if cmd == "/limit" else "minScoreAuto"] = int(arg)
            self.store.set("settings_override", ov)
            self.say(f"Готово: {'откликов в сутки' if cmd == '/limit' else 'порог автоотклика'} = {arg}")
        elif cmd == "/tglogin":
            from . import userbot

            self.say(h(userbot.start_login(self.store)) if userbot.configured() else
                     "Для автоотправки в Telegram добавьте секреты TELEGRAM_API_ID, TELEGRAM_API_HASH (my.telegram.org → API development tools) и TELEGRAM_PHONE.")
        elif cmd in ("/code", "/password"):
            from . import userbot

            if msg.get("message_id"):
                self.api("deleteMessage", chat_id=chat, message_id=msg["message_id"])
            self.say(h(userbot.finish_login(self.store, code=arg if cmd == "/code" else "", password=arg if cmd == "/password" else "")))
        elif cmd == "/hh":
            if arg.strip():
                self.say(h(ap.hh_connect(self.store, arg.strip())))
            else:
                url = ap.hh_authorize_url()
                self.say(f"Откройте ссылку, войдите в hh.ru и разрешите доступ. После этого вернитесь сюда — код подставится сам.\n\n{h(url)}" if url
                         else "Для автооткликов на hh.ru нужны секреты HH_CLIENT_ID и HH_CLIENT_SECRET (приложение на dev.hh.ru). Инструкция — в README.")
        else:
            self.say(HELP + f"\n\n🌐 Панель: {self.site_url()}")

    def action(self, data: str) -> None:
        if data == "run":
            self.request_collect()
            self.say("🚀 Запускаю сбор по всем площадкам.")
            return
        if data.startswith("hh-"):
            self.say(h(ap.hh_connect(self.store, data[3:])))
            return
        parts = data.split("_")
        op = parts[0]
        if op in ("ap", "rw"):
            v = self.vac(parts[1])
            if v:
                self.say(f"⏳ Пишу письмо под «{h(v['title'])}» и откликаюсь…")
                extra = "Сделай письмо заметно другим по структуре и первой фразе." if op == "rw" else ""
                self.submit(lambda: self.notify_applied(v, ap.apply(self.store, dict(v), force=True, extra=extra)))
        elif op == "sk":
            self.store.x("UPDATE vacancies SET status = 'skipped' WHERE rowid = ?", int(parts[1]))
        elif op == "mk":
            v = self.vac(parts[1])
            self.store.x("UPDATE applications SET status = 'sent', sent_at = COALESCE(sent_at, datetime('now')), updated_at = datetime('now') WHERE vacancy_id = ?", v["id"])
            self.store.x("UPDATE vacancies SET status = 'applied' WHERE id = ?", v["id"])
            self.say("Отметил как отправленный ✔️ Слежу за ответом.")
        elif op == "ms":
            self.store.x("UPDATE applications SET status = ?, hot = 1, updated_at = datetime('now') WHERE id = ?", parts[2], int(parts[1]))
            self.say("🏆 Поздравляю с оффером!" if parts[2] == "offer" else "🗓 Отметил созвон. Удачи!")
        elif op == "sa":
            app = self.store.one("SELECT * FROM applications WHERE id = ?", int(parts[1]))
            try:
                ap.hh_send_message(self.store, app["external_ref"], app["suggested_answer"])
                self.store.x("INSERT INTO messages (application_id, direction, body) VALUES (?, 'out', ?)", app["id"], app["suggested_answer"])
                self.say("📤 Ответ отправлен работодателю на hh.ru")
            except Exception as e:
                self.say(f"Не получилось через API ({h(e)}). Скопируйте текст выше и ответьте в чате hh.ru.")

    def cmd_status(self) -> None:
        st = stats(self.store)
        self.say(
            f"📊 <b>Статистика</b>\nВакансий: {st['vacancies']} (за сутки +{st['newToday']})\nОценено ИИ: {st['scored']} · в очереди: {st['queue']}\n"
            f"Откликов: {st['applied']} (за сутки {st['appliedToday']}) · ждут касания: {st['waitingUser']}\n"
            f"Ответили: {st['replies']} · приглашений: {st['invited']} · 🔥 {st['hot']}\n\n"
            + "<b>Источники:</b> " + ", ".join(f"{SRC.get(r['source'], r['source'])} {r['n']}" for r in st["bySource"])
        )

    def cmd_site(self) -> None:
        pw = self.store.get("site_password")
        self.say(f"🌐 <b>Ваша панель:</b> {self.site_url()}\n🔑 Пароль: <code>{h(pw)}</code>\n\nДанные на сайте зашифрованы, без пароля их никто не увидит. Кнопки на сайте отправляют команды сюда, в бота.")


def stats(store) -> dict:
    one = lambda sql: store.one(sql)[0]  # noqa: E731
    return {
        "vacancies": one("SELECT COUNT(*) FROM vacancies"),
        "newToday": one("SELECT COUNT(*) FROM vacancies WHERE found_at >= datetime('now','-1 day')"),
        "scored": one("SELECT COUNT(*) FROM vacancies WHERE fit IS NOT NULL"),
        "queue": one("SELECT COUNT(*) FROM vacancies WHERE status = 'new'"),
        "applied": one("SELECT COUNT(*) FROM applications WHERE status NOT IN ('draft','waiting_user')"),
        "appliedToday": one("SELECT COUNT(*) FROM applications WHERE sent_at >= datetime('now','-1 day')"),
        "waitingUser": one("SELECT COUNT(*) FROM applications WHERE status = 'waiting_user'"),
        "replies": one("SELECT COUNT(*) FROM applications WHERE last_reply IS NOT NULL"),
        "hot": one("SELECT COUNT(*) FROM applications WHERE hot = 1"),
        "invited": one("SELECT COUNT(*) FROM applications WHERE status IN ('invited','interview','offer')"),
        "offers": one("SELECT COUNT(*) FROM applications WHERE status = 'offer'"),
        "bySource": [dict(r) for r in store.q("SELECT source, COUNT(*) n FROM vacancies GROUP BY source ORDER BY n DESC")],
    }


_SITE = {"url": None, "checked": 0.0}


def site_url() -> str:
    """GitHub Pages, если включены; иначе тот же сайт из ветки gh-pages через raw.githack (работает сразу)."""
    import time as _t

    repo = os.environ.get("GITHUB_REPOSITORY", "mm88vin11/JobHunter-")
    owner, name = repo.split("/")
    pages = f"https://{owner.lower()}.github.io/{name}/"
    if _t.time() - _SITE["checked"] > 900:
        _SITE["checked"] = _t.time()
        try:
            _SITE["url"] = pages if requests.get(pages, timeout=10).status_code == 200 else None
        except Exception:
            _SITE["url"] = None
    return _SITE["url"] or f"https://raw.githack.com/{repo}/gh-pages/index.html"
