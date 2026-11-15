"""Публикация сайта-панели в ветку gh-pages. Данные шифруются паролем (PBKDF2 + AES-GCM) и расшифровываются в браузере."""
from __future__ import annotations

import base64
import json
import os
import secrets
import shutil

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import ai, config
from . import apply as ap
from .bot import SRC, stats
from .store import git_commit_push

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SITE_SRC = os.path.join(ROOT, "site")
ITER = 200_000


def site_password(store) -> str:
    pw = store.get("site_password")
    if not pw:
        pw = secrets.token_urlsafe(9)
        store.set("site_password", pw)
    return pw


def encrypt_for_web(password: str, payload: dict) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(password.encode())
    ct = AESGCM(key).encrypt(iv, json.dumps(payload, ensure_ascii=False).encode(), None)
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return {"v": 1, "iter": ITER, "salt": b(salt), "iv": b(iv), "data": b(ct)}


def snapshot(store, bot_username: str) -> dict:
    vac = [dict(r) for r in store.q(
        """SELECT v.rowid AS rid, v.id, v.source, v.url, v.title, v.company, v.location, v.remote, v.salary_from, v.salary_to, v.currency, v.salary_usd,
                  v.lang, v.status, v.fit, v.conditions, v.chance, v.score, v.verdict, v.instructions, v.found_at, a.status AS app_status
           FROM vacancies v LEFT JOIN applications a ON a.vacancy_id = v.id
           WHERE v.status NOT IN ('filtered') ORDER BY COALESCE(v.score, -1) DESC, v.found_at DESC LIMIT 600""")]
    apps = [dict(r) for r in store.q(
        """SELECT a.*, v.rowid AS rid, v.title, v.company, v.url, v.source, v.score FROM applications a JOIN vacancies v ON v.id = a.vacancy_id
           ORDER BY a.hot DESC, a.updated_at DESC LIMIT 400""")]
    for a in apps:
        a["messages"] = [dict(m) for m in store.q("SELECT direction, body, created_at FROM messages WHERE application_id = ? ORDER BY id", a["id"])]
        row = store.one("SELECT contacts FROM vacancies WHERE id = ?", a["vacancy_id"])
        c = ap.contacts_of(dict(row) if row else {})
        a["tg_link"] = ap.tg_link(c["telegram"][0], a["letter"]) if a["channel"] == "telegram" and c["telegram"] else None
    s = config.settings(store)
    repo = os.environ.get("GITHUB_REPOSITORY", "mm88vin11/JobHunter-")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    return {
        "generated": store.one("SELECT datetime('now')")[0], "bot": bot_username, "repo": repo, "branch": branch,
        "stats": stats(store), "vacancies": vac, "applications": apps, "sources": SRC,
        "events": [dict(r) for r in store.q("SELECT at, level, text FROM events ORDER BY id DESC LIMIT 80")],
        "settings": {k: s[k] for k in ("paused", "autoApply", "minScoreAuto", "minScoreNotify", "dailyLimit", "remoteOnly", "sources")},
        "connections": {
            "ai": ai.provider(), "telegram": bool(store.get("tg_owner")), "hh": ap.hh_connected(store), "hh_configured": bool(os.environ.get("HH_CLIENT_ID")),
            "email": bool(ap.mail_conf()), "vk": bool(os.environ.get("VK_SERVICE_TOKEN")),
            "tg_user": __import__("jobhunter_engine.userbot", fromlist=["available"]).available(store),
            "tg_user_configured": __import__("jobhunter_engine.userbot", fromlist=["configured"]).configured(),
        },
        "engine": store.get("engine_last"),
    }


def publish(store, pages_dir: str, bot_username: str) -> None:
    os.makedirs(pages_dir, exist_ok=True)
    for name in os.listdir(SITE_SRC):
        shutil.copy(os.path.join(SITE_SRC, name), os.path.join(pages_dir, name))
    open(os.path.join(pages_dir, ".nojekyll"), "w").close()
    with open(os.path.join(pages_dir, "data.enc.json"), "w") as f:
        json.dump(encrypt_for_web(site_password(store), snapshot(store, bot_username)), f)
    git_commit_push(pages_dir, "gh-pages", ["."], "site")
