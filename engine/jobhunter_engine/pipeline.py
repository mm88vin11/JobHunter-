"""Конвейер: приём вакансий → ИИ-оценка → автоотклик. Порт логики из Cloudflare-версии на Python."""
from __future__ import annotations

import hashlib
import json
import logging
import re

from . import ai, config
from . import apply as ap

log = logging.getLogger("jobhunter")
USD = {"USD": 1, "RUR": 1 / 95, "RUB": 1 / 95, "EUR": 1.08, "GBP": 1.27, "KZT": 1 / 500, "UAH": 1 / 41, "BYN": 0.3, "INR": 1 / 84}


def usd_month(f, t, cur):
    base = (f + t) / 2 if f and t else (f or t)
    if not base:
        return None
    v = base * USD.get((cur or "USD").upper(), 1)
    return round(v / 12 if v > 40000 else v)


def fingerprint(title: str, company: str) -> str:
    n = lambda s: " ".join(sorted(re.sub(r"[^a-zа-яё0-9]+", " ", (s or "").lower()).split()))  # noqa: E731
    return hashlib.sha1(f"{n(title)}|{n(company)}".encode()).hexdigest()


def ingest(store, items: list[dict]) -> int:
    added = 0
    for it in items:
        vid = f"{it['source']}:{it.get('external_id') or it['url']}"[:300]
        fp = fingerprint(it["title"], it.get("company") or "")
        if store.one("SELECT 1 FROM vacancies WHERE id = ? OR fingerprint = ?", vid, fp):
            continue
        rk = it.get("ranking") or {}
        store.x(
            """INSERT OR IGNORE INTO vacancies (id, source, external_id, url, apply_url, title, company, location, remote, salary_from, salary_to,
               currency, salary_usd, lang, description, contacts, published_at, fingerprint, status, rule_score, score, verdict)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)""",
            vid, it["source"], str(it.get("external_id") or it["url"]), it["url"], it.get("apply_url") or None, it["title"][:300], it.get("company") or None,
            it.get("location") or None, 1 if it.get("remote") else 0, it.get("salary_from"), it.get("salary_to"), it.get("currency"),
            usd_month(it.get("salary_from"), it.get("salary_to"), it.get("currency")), it.get("lang") or "ru", (it.get("description") or "")[:12000],
            json.dumps(it.get("contacts") or {}, ensure_ascii=False), it.get("published_at") or None, fp, it.get("rule_score"), it.get("rule_score"),
            ("Правила: " + rk["explanation"])[:600] if rk.get("explanation") else None,
        )
        added += 1
    return added


def sent_today(store) -> int:
    return store.one("SELECT COUNT(*) FROM applications WHERE sent_at >= datetime('now','-1 day')")[0]


def tick(store, bot) -> str:
    s = config.settings(store)
    if s.get("paused"):
        return "пауза"
    prof = config.profile()
    report = []
    evaluated = 0
    for v in store.q("SELECT rowid, * FROM vacancies WHERE status = 'new' ORDER BY COALESCE(rule_score, 50) DESC, found_at DESC LIMIT ?", s.get("evaluatePerTick", 12)):
        if s.get("remoteOnly") and not v["remote"]:
            store.x("UPDATE vacancies SET status = 'filtered', verdict = 'не удалёнка' WHERE id = ?", v["id"])
            continue
        if s.get("minSalaryUsd") and v["salary_usd"] and v["salary_usd"] < s["minSalaryUsd"]:
            store.x("UPDATE vacancies SET status = 'filtered', verdict = 'зарплата ниже порога' WHERE id = ?", v["id"])
            continue
        try:
            e = ai.evaluate(dict(v), prof)
        except Exception as err:
            store.x("UPDATE vacancies SET error = ? WHERE id = ?", str(err)[:300], v["id"])
            report.append(f"ИИ: {err}"[:200])
            break
        score = ai.final_score(v["rule_score"], e)
        store.x("UPDATE vacancies SET fit = ?, conditions = ?, chance = ?, score = ?, verdict = ?, red_flags = ?, instructions = ?, status = ? WHERE id = ?",
                e["fit"], e["conditions"], e["chance"], score, e["verdict"], json.dumps(e["red_flags"], ensure_ascii=False),
                json.dumps(e["instructions"], ensure_ascii=False), "scored" if score >= s["minScoreNotify"] else "skipped", v["id"])
        evaluated += 1
        if score >= 85 and bot:
            bot.notify_top(store.one("SELECT rowid, * FROM vacancies WHERE id = ?", v["id"]), s["autoApply"] and score >= s["minScoreAuto"])
    report.append(f"оценено {evaluated}")
    if s.get("autoApply") and not ai.enabled():
        report.append("автоотклики ждут ключ Claude")
    if s.get("autoApply") and ai.enabled():
        left = s["dailyLimit"] - sent_today(store)
        n = max(0, min(s.get("applyPerTick", 3), left))
        done = 0
        for v in store.q("SELECT rowid, * FROM vacancies WHERE status = 'scored' AND score >= ? ORDER BY score DESC LIMIT ?", s["minScoreAuto"], n):
            try:
                r = ap.apply(store, dict(v))
            except Exception as err:
                store.x("UPDATE vacancies SET status = 'error', error = ? WHERE id = ?", str(err)[:300], v["id"])
                report.append(f"отклик: {err}"[:200])
                continue
            if r["status"] == "limit":
                break
            done += 1
            if bot:
                bot.notify_applied(store.one("SELECT rowid, * FROM vacancies WHERE id = ?", v["id"]), r)
        report.append(f"откликов {done}")
    notify = bot.notify_reply if bot else (lambda *a: None)
    from . import userbot

    tg_check = lambda st, nt: userbot.check_replies(st, nt, ap.handle_reply)  # noqa: E731
    for name, fn in (("hh", ap.hh_sync), ("почта", ap.check_inbox), ("telegram", tg_check)):
        try:
            n = fn(store, notify)
            if n:
                report.append(f"{name}: {n} обновл.")
        except Exception as err:
            report.append(f"{name}: {err}"[:150])
    store.set("last_tick", {"report": report})
    return " · ".join(report)
