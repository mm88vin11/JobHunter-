"""Хранилище: SQLite в процессе работы, между запусками — зашифрованный файл в ветке `jobhunter-data`.

Репозиторий публичный, поэтому база (письма, переписка, токены hh.ru) шифруется AES-256-GCM
ключом, производным от секрета (DATA_KEY или TELEGRAM_BOT_TOKEN). Без секрета её не прочитать.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import threading
from contextlib import contextmanager

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("jobhunter")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_BRANCH = "jobhunter-data"


def _key() -> bytes:
    secret = os.environ.get("DATA_KEY") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    if not secret:
        raise RuntimeError("нужен секрет TELEGRAM_BOT_TOKEN (или DATA_KEY) для шифрования базы")
    return hashlib.sha256(b"jobhunter-data-v1:" + secret.encode()).digest()


def encrypt(data: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(_key()).encrypt(nonce, data, None)


def decrypt(blob: bytes) -> bytes:
    return AESGCM(_key()).decrypt(blob[:12], blob[12:], None)


class Store:
    def __init__(self, workdir: str):
        self.workdir = workdir                    # git worktree ветки данных
        self.enc_path = os.path.join(workdir, "state.db.enc")
        self.db_path = os.path.join(workdir, "state.db")
        self.lock = threading.RLock()
        with open(os.path.join(workdir, ".gitignore"), "w") as f:
            f.write("state.db\nstate.db-*\n")  # в git попадает только зашифрованная копия
        if os.path.exists(self.enc_path):
            with open(self.enc_path, "rb") as f:
                raw = decrypt(f.read())
            with open(self.db_path, "wb") as f:
                f.write(raw)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with open(os.path.join(HERE, "schema.sql"), encoding="utf-8") as f:
            self.conn.executescript(f.read())
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(vacancies)")}
        for col, ddl in (("instructions", "TEXT"), ("rule_score", "INTEGER"), ("apply_url", "TEXT"), ("notified", "INTEGER DEFAULT 0")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE vacancies ADD COLUMN {col} {ddl}")
        self.conn.commit()

    # ── доступ ──
    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, *args):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def x(self, sql: str, *args) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.rowcount

    def get(self, key: str, default=None):
        r = self.one("SELECT v FROM kv WHERE k = ?", key)
        return json.loads(r["v"]) if r else default

    def set(self, key: str, value) -> None:
        self.x("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", key, json.dumps(value, ensure_ascii=False))

    def event(self, text: str, level: str = "info") -> None:
        log.info("[%s] %s", level, text)
        self.x("INSERT INTO events (level, text) VALUES (?, ?)", level, text[:2000])

    # ── сохранение в git ──
    def save(self, message: str = "state") -> None:
        with self.lock:
            self.conn.execute("DELETE FROM events WHERE id < (SELECT MAX(id) - 2000 FROM events)")
            self.conn.commit()
            backup = sqlite3.connect(":memory:")
            self.conn.backup(backup)
            raw = backup.serialize()
            backup.close()
        with open(self.enc_path, "wb") as f:
            f.write(encrypt(bytes(raw)))
        git_commit_push(self.workdir, DATA_BRANCH, ["state.db.enc", ".gitignore"], message)


def git(*args: str, cwd: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:400]}")
    return r.stdout.strip()


def prepare_branch_worktree(repo: str, branch: str, path: str) -> str:
    """Готовит worktree ветки (создаёт пустую orphan-ветку, если её ещё нет)."""
    if os.path.exists(os.path.join(path, ".git")):
        return path
    git("config", "user.name", "JobHunter bot", cwd=repo)
    git("config", "user.email", "jobhunter-bot@users.noreply.github.com", cwd=repo)
    exists = git("ls-remote", "--heads", "origin", branch, cwd=repo, check=False)
    if exists:
        git("fetch", "origin", f"{branch}:{branch}", cwd=repo, check=False)
        git("worktree", "add", "-f", path, branch, cwd=repo)
    else:
        git("worktree", "add", "--detach", path, cwd=repo)
        git("checkout", "--orphan", branch, cwd=path)
        git("rm", "-rf", "--quiet", ".", cwd=path, check=False)
    return path


def git_commit_push(path: str, branch: str, files: list[str], message: str) -> None:
    if not os.environ.get("GITHUB_ACTIONS"):
        return  # локально ничего не пушим
    git("add", "-A", *files, cwd=path)
    if not git("status", "--porcelain", cwd=path):
        return
    # Ветка держится в одном коммите (amend + force), чтобы репозиторий не разрастался
    has_head = subprocess.run(["git", "rev-parse", "-q", "--verify", "HEAD"], cwd=path, capture_output=True).returncode == 0
    ident = ["-c", "user.name=JobHunter bot", "-c", "user.email=jobhunter-bot@users.noreply.github.com"]
    git(*ident, "commit", "-q", *(["--amend"] if has_head else []), "-m", message, cwd=path)
    for attempt in range(4):
        r = subprocess.run(["git", "push", "-q", "origin", f"HEAD:{branch}", "--force"], cwd=path, capture_output=True, text=True)
        if r.returncode == 0:
            return
        log.warning("push %s: %s", branch, r.stderr[:200])
    raise RuntimeError(f"не удалось запушить {branch}")
