"""Telegram-каналы (публичные превью t.me/s/…, без аккаунта и API-ключей) и VK-сообщества (wall.get)."""
from __future__ import annotations

import html
import logging
import re

from ..http import get, get_json
from ..model import Vacancy, extract_contacts, strip_html

log = logging.getLogger("jobhunter")

JOB_MARKERS = re.compile(
    r"#ваканси|ваканси[яи]|ищем|ищу разработ|требуется|в команду|hiring|we.?re looking|job|зарплат|з/п|зп[: ]|оклад|вилка|"
    r"#работа|#job|#remote|удал[её]нк|откликнуться|резюме|cv\b",
    re.I,
)
MSG_RE = re.compile(r'<div class="tgme_widget_message_wrap.*?data-post="([^"]+)".*?(?=<div class="tgme_widget_message_wrap|\Z)', re.S)
TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
DATE_RE = re.compile(r'<time datetime="([^"]+)"')
HREF_RE = re.compile(r'href="(https?://[^"]+)"')
SALARY_RE = re.compile(r"(?:от|from)?\s*(\d[\d\s]{2,})\s*(?:-|–|до|to)?\s*(\d[\d\s]{2,})?\s*(₽|руб|rub|\$|usd|€|eur)", re.I)


def parse_channel_html(channel: str, page: str) -> list[Vacancy]:
    out = []
    for m in MSG_RE.finditer(page):
        post, block = m.group(1), m.group(0)
        tm = TEXT_RE.search(block)
        if not tm:
            continue
        raw = tm.group(1)
        text = strip_html(raw)
        if len(text) < 120 or not JOB_MARKERS.search(text):
            continue
        hrefs = [html.unescape(h) for h in HREF_RE.findall(raw)]
        contacts = extract_contacts(text + "\n" + "\n".join(hrefs), own=channel)
        first_line = next((l.strip(" •#*-—") for l in text.split("\n") if len(l.strip()) > 8), text[:80])
        cur = sf = st = None
        sm = SALARY_RE.search(text)
        if sm:
            sf = float(sm.group(1).replace(" ", ""))
            st = float(sm.group(2).replace(" ", "")) if sm.group(2) else None
            c = sm.group(3).lower()
            cur = "USD" if c in ("$", "usd") else "EUR" if c in ("€", "eur") else "RUR"
        dm = DATE_RE.search(block)
        out.append(Vacancy(
            source="telegram", external_id=post, url=f"https://t.me/{post}", title=first_line[:140], company=f"t.me/{channel}",
            salary_from=sf, salary_to=st, currency=cur, description=text, published_at=dm.group(1) if dm else "", contacts=contacts,
        ).finalize())
    return out


def telegram(cfg: dict) -> list[Vacancy]:
    out = []
    for ch in cfg["telegram_channels"]:
        ch = ch.strip().lstrip("@").replace("https://t.me/", "")
        if not ch:
            continue
        try:
            out += parse_channel_html(ch, get(f"https://t.me/s/{ch}").text)
        except Exception as e:
            log.warning("telegram %s: %s", ch, e)
    return out


def vk(cfg: dict) -> list[Vacancy]:
    token = cfg.get("vk_token")
    if not token or not cfg.get("vk_groups"):
        return []
    out = []
    for g in cfg["vk_groups"]:
        g = g.strip().replace("https://vk.com/", "")
        try:
            data = get_json(f"https://api.vk.com/method/wall.get?domain={g}&count=50&access_token={token}&v=5.199")
        except Exception as e:
            log.warning("vk %s: %s", g, e)
            continue
        for p in (data.get("response") or {}).get("items", []):
            text = p.get("text") or ""
            if len(text) < 120 or not JOB_MARKERS.search(text):
                continue
            first = next((l for l in text.split("\n") if len(l.strip()) > 8), text[:80])
            out.append(Vacancy(
                source="vk", external_id=f"{p['owner_id']}_{p['id']}", url=f"https://vk.com/wall{p['owner_id']}_{p['id']}",
                title=first[:140], company=f"vk.com/{g}", description=text, contacts=extract_contacts(text),
            ).finalize())
    return out
