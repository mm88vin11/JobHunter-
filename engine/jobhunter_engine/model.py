"""Единая модель вакансии + конвертация в формат hh.ru (его понимают ranking/traps из dream-job-bot)."""
from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field

EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.I)
TG_RE = re.compile(r"(?:^|[\s(,:;])@([a-z][a-z0-9_]{4,31})\b|t\.me/([a-z][a-z0-9_]{4,31})(?![a-z0-9_/])", re.I)
LINK_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)
IGNORE_TG = {"joinchat", "share", "addstickers", "proxy", "iv"}


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h\d|tr)>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "• ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def detect_lang(text: str) -> str:
    sample = text[:3000]
    cyr = len(re.findall(r"[а-яё]", sample, re.I))
    lat = len(re.findall(r"[a-z]", sample, re.I))
    return "ru" if cyr >= lat * 0.25 else "en"


def extract_contacts(text: str, own: str = "") -> dict:
    emails = sorted({e.lower() for e in EMAIL_RE.findall(text) if not re.search(r"\.(png|jpe?g|gif|webp)$", e, re.I)})
    tg = []
    for m in TG_RE.finditer(text):
        u = (m.group(1) or m.group(2) or "").lower()
        if u and u not in IGNORE_TG and u != own.lower() and u not in tg:
            tg.append(u)
    links = sorted({l for l in LINK_RE.findall(text) if "t.me/" not in l})
    return {"emails": emails, "telegram": tg, "links": links[:10]}


@dataclass
class Vacancy:
    source: str
    external_id: str
    url: str
    title: str
    company: str = ""
    location: str = ""
    remote: bool = False
    salary_from: float | None = None
    salary_to: float | None = None
    currency: str | None = None
    salary_period: str = "month"      # month | year | hour
    description: str = ""
    published_at: str = ""
    lang: str = ""
    contacts: dict = field(default_factory=lambda: {"emails": [], "telegram": [], "links": []})
    skills: list[str] = field(default_factory=list)
    apply_url: str = ""
    hh_raw: dict | None = None        # оригинальный ответ hh.ru, если есть
    # заполняется скорингом
    rule_score: int | None = None
    trap: dict | None = None
    ranking: dict | None = None

    def finalize(self) -> "Vacancy":
        self.description = self.description.strip()
        if not self.lang:
            self.lang = detect_lang(f"{self.title}\n{self.description}")
        if not any(self.contacts.values()):
            self.contacts = extract_contacts(self.description)
        if not self.remote and re.search(r"удал[её]н|remote|из дома|wfh|anywhere", f"{self.title} {self.location} {self.description[:1500]}", re.I):
            self.remote = True
        return self

    def monthly(self) -> tuple[int | None, int | None]:
        k = {"year": 1 / 12, "hour": 168, "week": 4.33, "day": 21}.get(self.salary_period, 1)
        f = int(self.salary_from * k) if self.salary_from else None
        t = int(self.salary_to * k) if self.salary_to else None
        return f, t

    def to_hh_like(self) -> dict:
        """Форма вакансии hh.ru — её читают candidate.traps и candidate.ranking из dream-job-bot."""
        if self.hh_raw:
            return self.hh_raw
        f, t = self.monthly()
        cur = (self.currency or "RUR").upper().replace("RUB", "RUR")
        return {
            "id": self.external_id,
            "name": self.title,
            "description": self.description,
            "employer": {"name": self.company or ""},
            "area": {"name": self.location or ("Удалённо" if self.remote else "")},
            "salary": {"from": f, "to": t, "currency": cur} if (f or t) else None,
            "schedule": {"id": "remote", "name": "Удаленная работа"} if self.remote else None,
            "work_format": [{"id": "REMOTE"}] if self.remote else [],
            "key_skills": [{"name": s} for s in self.skills],
            "alternate_url": self.url,
            "published_at": self.published_at,
        }

    def to_ingest(self) -> dict:
        f, t = self.monthly()
        d = asdict(self)
        d.pop("hh_raw", None)
        d["salary_from"], d["salary_to"] = f, t
        d["description"] = self.description[:12000]
        return d
