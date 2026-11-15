"""Отклики и ответы работодателей.

• hh.ru — официальный API через HHOfficialClient из dream-job-bot (OAuth соискателя).
• Email — с вашей собственной почты (SMTP/IMAP, пароль приложения): письма уходят «от человека», ответы читаются автоматически.
• Telegram — ссылка t.me/<hr>?text=… открывает чат с уже вписанным письмом (бот не может писать людям первым).
"""
from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import json
import logging
import os
import smtplib
import time
from email.mime.text import MIMEText
from urllib.parse import quote

from . import ai, config
from .model import strip_html

log = logging.getLogger("jobhunter")

MAIL_HOSTS = {
    "mail.ru": ("smtp.mail.ru", "imap.mail.ru"), "inbox.ru": ("smtp.mail.ru", "imap.mail.ru"), "list.ru": ("smtp.mail.ru", "imap.mail.ru"),
    "bk.ru": ("smtp.mail.ru", "imap.mail.ru"), "gmail.com": ("smtp.gmail.com", "imap.gmail.com"), "yandex.ru": ("smtp.yandex.ru", "imap.yandex.ru"),
    "ya.ru": ("smtp.yandex.ru", "imap.yandex.ru"),
}


def mail_conf():
    addr = os.environ.get("EMAIL_ADDRESS", "")
    pw = os.environ.get("EMAIL_APP_PASSWORD", "")
    if not addr or not pw:
        return None
    smtp, imap = MAIL_HOSTS.get(addr.split("@")[-1].lower(), (os.environ.get("SMTP_HOST", ""), os.environ.get("IMAP_HOST", "")))
    return {"addr": addr, "pw": pw, "smtp": os.environ.get("SMTP_HOST", smtp), "imap": os.environ.get("IMAP_HOST", imap)}


# ─────────────────────────────── hh.ru ───────────────────────────────

def hh_client(store):
    from candidate.hh_official import HHOfficialClient  # vendor/dreamjob

    os.environ.setdefault("HH_USER_AGENT", "JobHunter/1.0 (vlad_2048@mail.ru)")
    cid, secret = os.environ.get("HH_CLIENT_ID", ""), os.environ.get("HH_CLIENT_SECRET", "")
    if not cid or not secret:
        return None
    c = HHOfficialClient(client_id=cid, client_secret=secret)
    t = store.get("hh_tokens")
    if not t:
        return c
    if time.time() > float(t.get("expires_at", 0)) - 120:
        try:
            nt = c.refresh(t["refresh_token"])
            nt.setdefault("refresh_token", t["refresh_token"])
            store.set("hh_tokens", nt)
            t = nt
        except Exception as e:
            store.event(f"hh.ru: токен не обновился, подключите заново (/hh): {e}", "error")
            return c
    c.access_token = t["access_token"]
    return c


def hh_connected(store) -> bool:
    c = hh_client(store)
    return bool(c and c.access_token)


def hh_connect(store, code: str) -> str:
    c = hh_client(store)
    if not c:
        return "Нет HH_CLIENT_ID / HH_CLIENT_SECRET в секретах GitHub"
    t = c.exchange_code(code.strip())
    store.set("hh_tokens", t)
    resumes = c.resumes()
    if resumes:
        store.set("hh_resume_id", resumes[0]["id"])
    return f"hh.ru подключён ✅ Резюме: {resumes[0].get('title') if resumes else 'не найдено — опубликуйте резюме на hh.ru'}"


def hh_authorize_url() -> str | None:
    from candidate.hh_official import authorize_url

    cid = os.environ.get("HH_CLIENT_ID")
    return authorize_url(cid) if cid else None


def _hh_apply(store, v: dict, letter: str) -> str:
    c = hh_client(store)
    resume = store.get("hh_resume_id")
    detail = c.vacancy(v["external_id"])
    if detail.get("has_test"):
        raise ManualNeeded("на вакансии тест hh.ru — нужен ручной отклик")
    if detail.get("archived"):
        raise Skip("вакансия в архиве")
    try:
        c.apply(vacancy_id=v["external_id"], resume_id=resume, message=letter[:9000])
    except Exception as e:
        text = f"{e} {getattr(e, 'errors', '')}"
        if "already_applied" in text:
            return "already"
        if "limit_exceeded" in text:
            raise Limit("hh.ru: дневной лимит откликов исчерпан")
        if any(k in text for k in ("test_required", "letter_required", "negotiations_forbidden", "resume_not_published")):
            raise ManualNeeded(f"hh.ru не принял автоотклик: {text[:200]}")
        raise
    return "sent"


class ManualNeeded(Exception):
    pass


class Skip(Exception):
    pass


class Limit(Exception):
    pass


# ─────────────────────────────── Email ───────────────────────────────

def send_email(to: str, subject: str, body: str) -> str:
    mc = mail_conf()
    if not mc:
        raise ManualNeeded("почта не подключена (EMAIL_ADDRESS + EMAIL_APP_PASSWORD)")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = email.utils.formataddr(("Влад", mc["addr"]))
    msg["To"] = to
    msg["Message-ID"] = email.utils.make_msgid(domain=mc["addr"].split("@")[-1])
    with smtplib.SMTP_SSL(mc["smtp"], 465, timeout=40) as s:
        s.login(mc["addr"], mc["pw"])
        s.sendmail(mc["addr"], [to], msg.as_string())
    return msg["Message-ID"]


def _decode(h: str | None) -> str:
    return "".join(p.decode(enc or "utf-8", "replace") if isinstance(p, bytes) else p for p, enc in email.header.decode_header(h or ""))


def _plain(msg) -> str:
    part = next((p for p in msg.walk() if p.get_content_type() == "text/plain"), None) or next((p for p in msg.walk() if p.get_content_type() == "text/html"), None)
    if not part:
        return ""
    raw = part.get_payload(decode=True) or b""
    text = raw.decode(part.get_content_charset() or "utf-8", "replace")
    if part.get_content_type() == "text/html":
        text = strip_html(text)
    lines = []
    for line in text.splitlines():
        if line.startswith(">") or line.strip().endswith("wrote:") or "пишет:" in line or line.startswith("-----Original"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def check_inbox(store, notify) -> int:
    """Читает новые письма от работодателей, которым мы писали."""
    mc = mail_conf()
    if not mc:
        return 0
    contacted = {}
    for r in store.q("SELECT a.id, v.contacts FROM applications a JOIN vacancies v ON v.id = a.vacancy_id WHERE a.channel = 'email'"):
        for e in json.loads(r["contacts"] or "{}").get("emails", []):
            contacted[e.lower()] = r["id"]
    if not contacted:
        return 0
    n = 0
    with imaplib.IMAP4_SSL(mc["imap"], 993) as im:
        im.login(mc["addr"], mc["pw"])
        im.select("INBOX")
        since = time.strftime("%d-%b-%Y", time.gmtime(time.time() - 14 * 86400))
        _, data = im.search(None, f'(SINCE "{since}")')
        for num in data[0].split()[-200:]:
            _, msgdata = im.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(msgdata[0][1])
            sender = email.utils.parseaddr(msg.get("From"))[1].lower()
            app_id = contacted.get(sender)
            if not app_id:
                continue
            mid = msg.get("Message-ID") or f"{sender}-{msg.get('Date')}"
            body = _plain(msg)
            if not body:
                continue
            if store.x("INSERT OR IGNORE INTO messages (application_id, direction, body, external_id) VALUES (?, 'in', ?, ?)", app_id, body[:8000], mid):
                handle_reply(store, app_id, body, notify)
                n += 1
    return n


# ─────────────────────────────── Отклик ───────────────────────────────

def contacts_of(v: dict) -> dict:
    try:
        return {"emails": [], "telegram": [], "links": [], **json.loads(v.get("contacts") or "{}")}
    except Exception:
        return {"emails": [], "telegram": [], "links": []}


def tg_link(username: str, text: str) -> str:
    return f"https://t.me/{username}?text={quote(text[:3500])}"


def pick_channel(store, v: dict) -> str:
    c = contacts_of(v)
    if v["source"] == "hh" and store.get("hh_resume_id") and hh_connected(store):
        return "hh"
    if c["emails"] and mail_conf():
        return "email"
    if c["telegram"]:
        return "telegram"
    return "link"


def apply(store, v: dict, *, force: bool = False, extra: str = "") -> dict:
    prof = config.profile()
    existing = store.one("SELECT * FROM applications WHERE vacancy_id = ?", v["id"])
    if existing and existing["status"] not in ("draft", "waiting_user") and not force:
        return {"status": "skipped", "channel": existing["channel"], "note": "уже откликались"}
    ins = json.loads(v.get("instructions") or "{}")
    channel = pick_channel(store, v)
    if ins.get("manual_only") and channel in ("hh", "email"):
        channel = "link"
    out = ai.write_letter(v, prof, channel=channel, instructions=ins, extra=extra)
    letter = out["letter"]
    c = contacts_of(v)
    status, ref, note = "waiting_user", None, ""
    try:
        if channel == "hh":
            r = _hh_apply(store, v, letter)
            status, note = "sent", ("отклик уже был" if r == "already" else "отправлено через hh.ru")
        elif channel == "email":
            ref = send_email(c["emails"][0], out["subject"], letter)
            status, note = "sent", f"письмо отправлено на {c['emails'][0]}"
        elif channel == "telegram":
            note = f"готово сообщение для @{c['telegram'][0]} — отправка в одно касание"
        else:
            note = "отклик на сайте работодателя — письмо готово" + (" (там анкета/тест)" if ins.get("manual_only") else "")
    except Limit as e:
        return {"status": "limit", "channel": channel, "note": str(e)}
    except Skip as e:
        store.x("UPDATE vacancies SET status = 'skipped', error = ? WHERE id = ?", str(e), v["id"])
        return {"status": "skipped", "channel": channel, "note": str(e)}
    except Exception as e:
        status, note = "waiting_user", f"нужно одно касание: {e}"[:300]
    if out["problems"]:
        note += " · ⚠️ проверьте: " + "; ".join(out["problems"][:2])
    store.x(
        """INSERT INTO applications (vacancy_id, channel, letter, lang, status, external_ref, sent_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, CASE WHEN ? = 'sent' THEN datetime('now') END, datetime('now'))
           ON CONFLICT(vacancy_id) DO UPDATE SET channel = excluded.channel, letter = excluded.letter, status = excluded.status,
             external_ref = COALESCE(excluded.external_ref, external_ref), sent_at = COALESCE(excluded.sent_at, sent_at), updated_at = datetime('now')""",
        v["id"], channel, letter, v.get("lang") or "ru", status, ref, status,
    )
    store.x("UPDATE vacancies SET status = ?, apply_channel = ? WHERE id = ?", "applied" if status == "sent" else "manual", channel, v["id"])
    if status == "sent":
        store.x("INSERT INTO messages (application_id, direction, body) SELECT id, 'out', ? FROM applications WHERE vacancy_id = ?", letter, v["id"])
    store.event(f"{'✅ Отклик' if status == 'sent' else '✍️ Готов отклик'}: {v['title']} ({v.get('company') or v['source']}) — {note}")
    return {"status": status, "channel": channel, "letter": letter, "note": note, "subject": out["subject"]}


# ─────────────────────────────── Ответы ───────────────────────────────

RANK = ["draft", "waiting_user", "sent", "viewed", "rejected", "invited", "interview", "offer"]


def handle_reply(store, app_id: int, reply: str, notify) -> None:
    app = store.one("SELECT * FROM applications WHERE id = ?", app_id)
    v = store.one("SELECT * FROM vacancies WHERE id = ?", app["vacancy_id"])
    hist = "\n".join(f"{'Работодатель' if m['direction'] == 'in' else 'Я'}: {m['body']}" for m in store.q("SELECT * FROM messages WHERE application_id = ? ORDER BY id", app_id))
    a = ai.analyze_reply(dict(v) if v else None, hist, reply, config.profile())
    status = {"reject": "rejected", "offer": "offer", "invite": "interview"}.get(a["kind"], "viewed" if app["status"] == "sent" else app["status"])
    store.x("UPDATE applications SET last_reply = ?, reply_kind = ?, suggested_answer = ?, status = ?, hot = CASE WHEN ? THEN 1 ELSE hot END, updated_at = datetime('now') WHERE id = ?",
            reply[:4000], a["kind"], a["suggested_answer"], status, 1 if a.get("hot") else 0, app_id)
    store.event(f"{'🔥' if a.get('hot') else '💬'} Ответ по «{v['title'] if v else app['vacancy_id']}»: {a['summary']}", "hot" if a.get("hot") else "info")
    notify(app_id, a)


def hh_sync(store, notify) -> int:
    if not hh_connected(store):
        return 0
    c = hh_client(store)
    data = c.negotiations(page=0, per_page=50)
    changed = 0
    for item in data.get("items", []):
        vid = f"hh:{(item.get('vacancy') or {}).get('id')}"
        app = store.one("SELECT * FROM applications WHERE vacancy_id = ?", vid)
        if not app:
            continue
        state = (item.get("state") or {}).get("id")
        mapped = {"invitation": "invited", "discard": "rejected", "hired": "offer"}.get(state) or ("viewed" if item.get("viewed_by_opponent") else "sent")
        if RANK.index(mapped) > RANK.index(app["status"] if app["status"] in RANK else "sent"):
            store.x("UPDATE applications SET status = ?, external_ref = ?, hot = CASE WHEN ? IN ('invited','offer') THEN 1 ELSE hot END, updated_at = datetime('now') WHERE id = ?",
                    mapped, str(item["id"]), mapped, app["id"])
            changed += 1
            if mapped in ("invited", "offer"):
                store.event(f"🔥 Приглашение на hh.ru: {item['vacancy'].get('name')}", "hot")
                notify(app["id"], {"kind": "invite", "hot": True, "summary": "Приглашение на hh.ru", "suggested_answer": ""})
        if item.get("has_updates") or mapped == "invited":
            try:
                msgs = c._request("GET", f"https://api.hh.ru/negotiations/{item['id']}/messages")
                newest = None
                for m in msgs.get("items", []):
                    if (m.get("author") or {}).get("participant_type") != "employer":
                        continue
                    if store.x("INSERT OR IGNORE INTO messages (application_id, direction, body, external_id) VALUES (?, 'in', ?, ?)", app["id"], strip_html(m.get("text") or ""), str(m["id"])):
                        newest = m
                if newest:
                    store.x("UPDATE applications SET external_ref = ? WHERE id = ?", str(item["id"]), app["id"])
                    handle_reply(store, app["id"], strip_html(newest.get("text") or ""), notify)
                    changed += 1
            except Exception as e:
                log.info("hh messages: %s", e)
    return changed


def hh_send_message(store, negotiation_id: str, text: str) -> None:
    c = hh_client(store)
    c._request("POST", f"https://api.hh.ru/negotiations/{negotiation_id}/messages", {"message": text})
