"""Резюме screen: review live hh.ru status, preview a profile->resume diff, upload/edit,
confirm-then-write to hh.ru, and roll back.

Safety contract (do not weaken these when editing this file):

- hh.ru is only ever written to inside ``apply_profile_to_hh``, ``rollback_profile`` and
  ``create_new_hh_resume`` — each fires only from an explicit owner button press on THIS
  screen. Opening a profile, previewing a diff, or editing a field never writes to hh.ru.
- ``apply_profile_to_hh`` always rebuilds the profile/current-resume/payload/diff from disk
  and from a fresh hh.ru GET immediately before writing — nothing computed for an earlier
  "open profile" view is cached and reused for the write. This is what makes edits made
  after opening a diff safe: there is no stale cached confirmation that could go out of
  date, because the write always uses whatever is on disk at the moment of the click.
- A snapshot of the live resume is saved under ``var/resume-backups/`` before every write,
  so ``rollback_profile`` can always undo the last attempt.
- Profile edits (``ResumeProfileEdit`` states) and resume uploads write only into
  ``candidate_data/v1/hh_profiles/<key>.json``, atomically (temp file + ``os.replace``),
  and only after the real ``load_profile`` validator accepts the result.
- The OAuth token is never logged or echoed; only ``HHOfficialError.reason`` /
  ``HHResumeApplyError`` messages (already Russian, already safe) reach the chat.
- An accepted local edit reseals exactly its own manifest entry, so later
  integrity checks continue to protect every other candidate-data file.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from candidate.data import profile_keys, reseal_manifest_entry
from candidate.hh_official import HHOfficialClient, HHOfficialError
from candidate.hh_resume_sync import (
    DictionaryResolutionError,
    HHDictionaries,
    HHResumeApplyError,
    MAX_ABOUT_LENGTH,
    MAX_SKILL_SET_COUNT,
    MAX_TITLE_LENGTH,
    PROFILE_ROOT,
    ProfileValidationError,
    apply_profile,
    build_payload,
    create_resume,
    diff,
    load_profile,
    publish,
    restore,
    snapshot,
)

from mvp import config
from mvp.hh_sync import invalidate_live_resume

log = logging.getLogger(__name__)

router = Router()

_NEW_PROFILE_KEY_RE = re.compile(r"[a-z][a-z0-9_]{2,19}")


class ResumeProfileEdit(StatesGroup):
    """In-chat editing of one field of a hh_profiles/<key>.json profile."""

    headline = State()
    about = State()
    salary = State()
    skills = State()


class ResumeUpload(StatesGroup):
    """Transient state while naming an additional profile from an uploaded resume."""

    choosing_new_key = State()


_EDIT_FIELD_STATES = {
    "headline": ResumeProfileEdit.headline,
    "about": ResumeProfileEdit.about,
    "salary": ResumeProfileEdit.salary,
    "skills": ResumeProfileEdit.skills,
}
_EDIT_FIELD_PROMPTS = {
    "headline": f"Пришлите новый заголовок резюме одним сообщением (до {MAX_TITLE_LENGTH} символов).",
    "about": f"Пришлите новый текст блока «О себе» одним сообщением (до {MAX_ABOUT_LENGTH} символов).",
    "salary": "Пришлите желаемую зарплату числом в рублях, например: 100000.",
    "skills": f"Пришлите ключевые навыки — по одному на строке или через запятую (до {MAX_SKILL_SET_COUNT} штук).",
}


# ---------------------------------------------------------------------------
# HH API client (reuses the construction path from mvp/hh_sync.py: an
# HHOfficialClient built from hh_oauth_accounts, refreshed if near expiry)
# ---------------------------------------------------------------------------

async def _hh_client(user_id: int) -> HHOfficialClient | None:
    account = config.AUTOMATION.hh_account(user_id)
    if not account:
        return None
    client = HHOfficialClient(
        client_id=os.environ.get("HH_CLIENT_ID", ""),
        client_secret=os.environ.get("HH_CLIENT_SECRET", ""),
        access_token=account["access_token"],
    )
    if float(account.get("expires_at", 0)) <= time.time() + 60 and account.get("refresh_token"):
        try:
            tokens = await asyncio.to_thread(client.refresh, account["refresh_token"])
        except HHOfficialError as exc:
            log.warning("HH token refresh failed safely: %s", exc.reason)
            return None
        account.update(tokens)
        await asyncio.to_thread(config.AUTOMATION.save_hh_account, user_id, account, account.get("resumes") or [])
    return client


# ---------------------------------------------------------------------------
# Profile-file discovery and display helpers
# ---------------------------------------------------------------------------

def _discover_profile_keys() -> list[str]:
    declared = profile_keys(PROFILE_ROOT.parent)
    extra = sorted(
        p.stem for p in PROFILE_ROOT.glob("*.json")
        if p.stem not in declared
    )
    return [*declared, *extra]


def _profile_display_name(key: str) -> str:
    if config.ADAPTER is not None:
        master = config.ADAPTER.data.profiles.get(key)
        if master is not None and master.name:
            return master.name
    return key.replace("_", " ").title()


#: Master-profile names come from the H1 of `candidate_data/v1/resumes/*.md`,
#: which reads "Резюме 2 — Менеджер проектов по внедрению AI". On a button that
#: ordinal prefix is pure noise and used to eat 11 of the 24 characters the
#: label was cut to, producing «Резюме 2 — Менеджер прое».
_ORDINAL_PREFIX_RE = re.compile(r"^Резюме\s*\d+\s*[—–-]\s*")
_BUTTON_LABEL_LIMIT = 40


def _button_label(name: str) -> str:
    stripped = _ORDINAL_PREFIX_RE.sub("", name).strip() or name
    if len(stripped) <= _BUTTON_LABEL_LIMIT:
        return stripped
    return stripped[: _BUTTON_LABEL_LIMIT - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Atomic profile-file writes (edit + upload flows share this)
# ---------------------------------------------------------------------------

def _validate_profile_dict(profile_key: str, raw: dict) -> dict:
    """Validate `raw` via the real ``load_profile`` without touching the live file."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / f"{profile_key}.json"
        tmp_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        return load_profile(profile_key, root=Path(tmp))


def _commit_profile_dict(profile_key: str, raw: dict) -> dict:
    """Validate `raw`, then atomically replace the live profile file.

    Raises ProfileValidationError (real file untouched) if `raw` does not pass
    ``load_profile``. The write itself is a same-directory temp file + ``os.replace``,
    so a reader never observes a half-written file.
    """
    validated = _validate_profile_dict(profile_key, raw)
    real_path = PROFILE_ROOT / f"{profile_key}.json"
    real_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = real_path.parent / f".{profile_key}.json.tmp-{uuid.uuid4().hex[:8]}"
    tmp_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, real_path)
    # The manifest pins a hash for every profile file and load_candidate_data
    # refuses to start when one drifts. An owner edit here is authorised, so
    # reseal that one entry — otherwise the next restart would not come back up.
    # Derive the root from PROFILE_ROOT so a patched root (tests) reseals its own
    # manifest, or none at all, instead of touching the real one.
    manifest_root = PROFILE_ROOT.parent
    if (manifest_root / "manifest.json").exists():
        try:
            reseal_manifest_entry(f"hh_profiles/{profile_key}.json", root=manifest_root)
        except Exception as exc:  # pragma: no cover - never lose a valid edit over bookkeeping
            log.warning("Manifest reseal failed for %s: %s", profile_key, type(exc).__name__)
    return validated


def _write_profile_field(profile_key: str, mutate) -> dict:
    real_path = PROFILE_ROOT / f"{profile_key}.json"
    raw = json.loads(real_path.read_text(encoding="utf-8"))
    mutate(raw)
    return _commit_profile_dict(profile_key, raw)


def _apply_field(raw: dict, field: str, value) -> None:
    if field == "headline":
        raw["title"] = value
    elif field == "about":
        raw["about"] = value
    elif field == "salary":
        salary = dict(raw.get("salary") or {})
        salary["amount"] = value
        salary.setdefault("currency", "RUR")
        raw["salary"] = salary
    elif field == "skills":
        raw["skill_set"] = value


# ---------------------------------------------------------------------------
# Long-message splitting stays self-contained in this module.
# ---------------------------------------------------------------------------

async def _send_long(message: Message, text: str, **kwargs) -> None:
    if not text or not text.strip():
        reply_markup = kwargs.get("reply_markup")
        if reply_markup:
            await message.answer("(пусто)", reply_markup=reply_markup)
        return
    limit = 4000
    chunks = [text[i : i + limit] for i in range(0, len(text), limit)]
    tail_kwargs = {}
    if "parse_mode" in kwargs:
        tail_kwargs["parse_mode"] = kwargs["parse_mode"]
    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1:
            await message.answer(chunk, **kwargs)
        else:
            await message.answer(chunk, **tail_kwargs)


# ---------------------------------------------------------------------------
# Diff rendering (spec 2: "было -> станет" grouped by field, RU labels)
# ---------------------------------------------------------------------------

def _format_value(value) -> str:
    if isinstance(value, dict):
        if "length" in value and "first_line" in value:
            first = value.get("first_line") or "(пусто)"
            return f"{value['length']} симв. · «{first}»"
        if "count" in value:
            companies = [str(x) for x in (value.get("companies") or []) if x]
            names = ", ".join(companies) if companies else "—"
            return f"{value['count']} записей ({names})"
        if "entries" in value:
            return f"{value.get('level') or '—'}, записей: {value['entries']}"
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return ", ".join(str(x) for x in value) if value else "—"
    return str(value) if value not in (None, "") else "—"


def _render_diff_text(profile: dict, profile_key: str, context: dict) -> str:
    records = context["records"]
    truncations = context["truncations"]
    parts = [f"🧾 <b>{html.escape(str(profile.get('title') or profile_key))}</b> — разбор изменений перед заливкой"]
    if truncations:
        trunc_lines = ["⚠️ <b>HH урежет эти поля:</b>"]
        for item in truncations:
            trunc_lines.append(
                f"• {html.escape(str(item.get('field', '')))}: "
                f"{item.get('original_length')} → {item.get('truncated_length')} симв."
            )
        parts.append("\n".join(trunc_lines))
    for record in records:
        marker = "🔸 изменится" if record["changed"] else "· без изменений"
        old_text = html.escape(_format_value(record["old"]))
        new_text = html.escape(_format_value(record["new"]))
        parts.append(
            f"<b>{html.escape(record['label'])}</b> ({marker})\n"
            f"Было: {old_text}\n"
            f"Станет: {new_text}"
        )
    parts.append(
        "🛡 Ничего не отправлено в HH. Перед заливкой будет сохранена резервная копия резюме; "
        "после заливки доступен откат."
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Own-file extraction (spec 5): prefer a dependency the repo already has
# (python-docx / PyMuPDF, both in requirements.txt), degrade to a plain-text
# fallback with an explicit warning if the library is not installed in this
# environment or parsing fails, rather than adding a new dependency.
# ---------------------------------------------------------------------------

def _extract_docx_text(file_bytes: bytes) -> tuple[str, str]:
    try:
        import docx  # python-docx — already a repo dependency (requirements.txt)
    except ImportError:
        return file_bytes.decode("utf-8", errors="ignore"), (
            "Библиотека python-docx недоступна в этом окружении — использован грубый "
            "текстовый fallback, качество разбора может быть низким."
        )
    try:
        document = docx.Document(BytesIO(file_bytes))
        paragraphs = [p.text for p in document.paragraphs if p.text and p.text.strip()]
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell.text and cell.text.strip():
                        paragraphs.append(cell.text.strip())
        return "\n".join(paragraphs), ""
    except Exception as exc:
        log.warning("DOCX extraction failed safely: %s", type(exc).__name__)
        return file_bytes.decode("utf-8", errors="ignore"), (
            "Не удалось разобрать DOCX штатной библиотекой — использован грубый текстовый "
            "fallback, качество разбора может быть низким."
        )


def _extract_pdf_text(file_bytes: bytes) -> tuple[str, str]:
    try:
        import fitz  # PyMuPDF — already listed in requirements.txt
    except ImportError:
        return file_bytes.decode("utf-8", errors="ignore"), (
            "Библиотека для чтения PDF (PyMuPDF) не установлена в этом окружении — "
            "использован грубый текстовый fallback, качество разбора может быть низким. "
            "Лучше пришлите DOCX."
        )
    try:
        document = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            text = "\n".join(page.get_text() for page in document)
        finally:
            document.close()
        return text, ""
    except Exception as exc:
        log.warning("PDF extraction failed safely: %s", type(exc).__name__)
        return file_bytes.decode("utf-8", errors="ignore"), (
            "Не удалось разобрать PDF — использован грубый текстовый fallback, качество "
            "разбора может быть низким."
        )


def _extract_text(file_bytes: bytes, ext: str) -> tuple[str, str]:
    if ext == ".docx":
        return _extract_docx_text(file_bytes)
    if ext == ".pdf":
        return _extract_pdf_text(file_bytes)
    return file_bytes.decode("utf-8", errors="ignore"), "Формат не распознан — текст извлечён как есть."


# ---------------------------------------------------------------------------
# AI mapping of extracted resume text -> profile JSON shape (spec 5)
# ---------------------------------------------------------------------------

def _parse_json_response(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("Модель не вернула JSON") from exc
        value = json.loads(match.group())
    if not isinstance(value, dict):
        raise ValueError("Ответ модели — не JSON-объект")
    return value


def _resume_mapping_prompt(resume_text: str, target_key: str) -> str:
    documents = config.ADAPTER.data.documents if config.ADAPTER else {}
    existing_path = PROFILE_ROOT / f"{target_key}.json"
    existing = None
    if existing_path.exists():
        try:
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
    task = (
        "Владелец прислал текст своего резюме. Разбери его и заполни JSON строго по "
        "output_schema для последующей публикации на hh.ru. Пиши по-русски от первого лица, "
        "только по фактам из текста — ничего не придумывай и не усиливай числа. Если поле "
        "не удаётся определить из текста, оставь его пустым (пустая строка/список), но не "
        "выдумывай значение. Соблюдай стиль resume_voice_policy и инструкцию "
        "resume_tailoring_policy. Все даты — формат YYYY-MM-DD (если известен только "
        "месяц/год, возьми первое число месяца)."
    )
    return json.dumps({
        "task": task,
        "resume_text": resume_text[:16000],
        "resume_voice_policy": documents.get("resume_voice.md", ""),
        "resume_tailoring_policy": documents.get("resume_tailoring_prompt.md", ""),
        "existing_profile_for_reference": existing,
        "output_schema": {
            "profile_key": target_key,
            "title": f"строка, заголовок резюме, до {MAX_TITLE_LENGTH} символов",
            "salary": {"amount": "целое число, рубли", "currency": "RUR"},
            "area": "строка, город",
            "relocation": "одно из: no_relocation | relocation_possible | relocation_wanted",
            "employments": ["строки: full | part | project | ..."],
            "schedules": ["строки: fullDay | remote | flexible | ..."],
            "about": f"строка от первого лица, до {MAX_ABOUT_LENGTH} символов",
            "skill_set": ["короткие теги навыков"],
            "experience": [{
                "company": "строка", "position": "строка",
                "start": "YYYY-MM-DD", "end": "YYYY-MM-DD или null",
                "area": "строка", "description": "строка",
            }],
            "education": {
                "level": "строка",
                "primary": [{"name": "строка", "organization": "строка", "result": "строка", "year": "число"}],
            },
            "languages": [{"name": "строка", "level": "строка"}],
            "professional_roles": ["строки"],
        },
    }, ensure_ascii=False)


def _map_resume_to_profile(resume_text: str, target_key: str) -> dict:
    if config.ADAPTER is None:
        raise RuntimeError("AI adapter is not configured")
    prompt = _resume_mapping_prompt(resume_text, target_key)
    raw_response = config.ADAPTER._completion(prompt)
    parsed = _parse_json_response(raw_response)
    parsed["profile_key"] = target_key
    return parsed


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _render_parsed_preview(parsed: dict, target_key: str) -> str:
    salary = _as_dict(parsed.get("salary"))
    about = str(parsed.get("about") or "")
    about_first_line = about.splitlines()[0][:160] if about.strip() else ""
    skills = _as_list(parsed.get("skill_set"))
    experience = _as_list(parsed.get("experience"))
    education = _as_dict(parsed.get("education"))
    languages = _as_list(parsed.get("languages"))
    employments = ", ".join(str(x) for x in _as_list(parsed.get("employments"))) or "—"
    schedules = ", ".join(str(x) for x in _as_list(parsed.get("schedules"))) or "—"
    skills_text = ", ".join(str(s) for s in skills[:12])
    if len(skills) > 12:
        skills_text += "…"
    companies = ", ".join(str(_as_dict(e).get("company", "")) for e in experience[:4])
    experience_line = f"{len(experience)} запис." + (f" — {companies}" if companies else "")
    languages_text = ", ".join(
        f"{_as_dict(lang).get('name', '')} ({_as_dict(lang).get('level', '')})" for lang in languages
    ) or "—"
    lines = [
        f"🧾 <b>Разбор для профиля «{html.escape(target_key)}»</b>",
        f"<b>Заголовок:</b> {html.escape(str(parsed.get('title') or '—'))}",
        f"<b>Зарплата:</b> {html.escape(str(salary.get('amount') or '—'))} {html.escape(str(salary.get('currency') or ''))}",
        f"<b>Город:</b> {html.escape(str(parsed.get('area') or '—'))}",
        f"<b>Готовность к переезду:</b> {html.escape(str(parsed.get('relocation') or '—'))}",
        f"<b>Занятость:</b> {html.escape(employments)}",
        f"<b>График:</b> {html.escape(schedules)}",
        f"<b>О себе</b> ({len(about)} симв.): {html.escape(about_first_line or '—')}",
        f"<b>Навыки</b> ({len(skills)}): {html.escape(skills_text) or '—'}",
        f"<b>Опыт:</b> {html.escape(experience_line)}",
        f"<b>Образование:</b> {html.escape(str(education.get('level') or '—'))}, записей: {len(_as_list(education.get('primary')))}",
        f"<b>Языки:</b> {html.escape(languages_text)}",
        "\n✅ Проверьте и подтвердите — в файл профиля ещё ничего не записано.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _diff_keyboard(profile_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Подтвердить и залить в HH", callback_data=f"mvp:resumes:apply:{profile_key}")],
        [
            InlineKeyboardButton(text="✏️ Заголовок", callback_data=f"mvp:resumes:editfield:headline:{profile_key}"),
            InlineKeyboardButton(text="✏️ О себе", callback_data=f"mvp:resumes:editfield:about:{profile_key}"),
        ],
        [
            InlineKeyboardButton(text="✏️ Зарплата", callback_data=f"mvp:resumes:editfield:salary:{profile_key}"),
            InlineKeyboardButton(text="✏️ Навыки", callback_data=f"mvp:resumes:editfield:skills:{profile_key}"),
        ],
        [InlineKeyboardButton(text="↩️ Откатить последнюю заливку", callback_data=f"mvp:resumes:rollback:{profile_key}")],
        [InlineKeyboardButton(text="⬅️ К списку резюме", callback_data="mvp:resumes")],
    ])


def _rollback_keyboard(profile_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Откатить последнюю заливку", callback_data=f"mvp:resumes:rollback:{profile_key}")],
        [InlineKeyboardButton(text="⬅️ К списку резюме", callback_data="mvp:resumes")],
    ])


def _upload_target_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key in _discover_profile_keys():
        try:
            title = load_profile(key).get("title") or key
        except ProfileValidationError:
            title = key
        rows.append([InlineKeyboardButton(text=f"↺ {str(title)[:32]}", callback_data=f"mvp:resumes:upload_target:{key}")])
    rows.append([InlineKeyboardButton(text="🆕 Новый профиль", callback_data="mvp:resumes:upload_target:new")])
    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data="mvp:resumes:upload_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _upload_confirm_keyboard(target_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить как профиль", callback_data=f"mvp:resumes:upload_save:{target_key}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="mvp:resumes:upload_cancel")],
    ])


# ---------------------------------------------------------------------------
# 1. Список резюме
# ---------------------------------------------------------------------------

async def _render_resume_list(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    account = config.AUTOMATION.hh_account(user_id)
    client = await _hh_client(user_id) if account else None
    lines = ["📄 <b>Резюме по мастер-профилям</b>"]
    rows: list[list[InlineKeyboardButton]] = []
    for key in _discover_profile_keys():
        try:
            profile = load_profile(key)
        except ProfileValidationError as exc:
            lines.append(f"⚠️ <b>{html.escape(_profile_display_name(key))}</b> ({html.escape(key)}) — профиль повреждён: {html.escape(str(exc))}")
            rows.append([InlineKeyboardButton(text=f"⚠️ {key[:30]}", callback_data=f"mvp:resumes:open:{key}")])
            continue
        name = _profile_display_name(key)
        mapping = config.AUTOMATION.resume_mapping(user_id, key)
        if not mapping or not mapping.get("resume_id"):
            lines.append(f"🔴 <b>{html.escape(name)}</b> ({html.escape(key)}) — НЕ привязан к резюме HH")
            rows.append([InlineKeyboardButton(text=f"🔗 Привязать «{_button_label(name)}»", callback_data=f"mvp:resumes:link:{key}")])
            continue
        resume_id = str(mapping["resume_id"])
        live_title = str(mapping.get("title") or "—")
        status_line = "статус неизвестен (HH не подключён)"
        if client is not None:
            try:
                live = await asyncio.to_thread(client.resume, resume_id)
            except HHOfficialError as exc:
                status_line = f"ошибка HH: {html.escape(exc.reason)}"
            else:
                live_title = str(live.get("title") or live_title)
                status_obj = live.get("status") or {}
                status_id = str(status_obj.get("id") if isinstance(status_obj, dict) else status_obj or "").lower()
                published = status_id in {"published", "visible"}
                status_line = "опубликовано ✅" if published else f"не опубликовано ({html.escape(status_id or 'unknown')})"
                total_views = live.get("total_views")
                new_views = live.get("new_views")
                if total_views is not None or new_views is not None:
                    status_line += f" · просмотры: {total_views if total_views is not None else '—'} (новых: {new_views if new_views is not None else '—'})"
        lines.append(
            f"🟢 <b>{html.escape(name)}</b> ({html.escape(key)})\n"
            f"Заголовок на HH: {html.escape(live_title)}\n"
            f"Статус: {status_line}"
        )
        rows.append([InlineKeyboardButton(text=f"📂 «{_button_label(name)}»", callback_data=f"mvp:resumes:open:{key}")])
    if not account:
        lines.append("⚠️ HH не подключён — показаны только локальные профили без живого статуса. Подключите HH в «⚙️ Настройки».")
    rows.append([InlineKeyboardButton(text="📤 Загрузить своё резюме", callback_data="mvp:resumes:upload_hint")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="mvp:resumes")])
    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "mvp:resumes")
async def resumes_list(callback: CallbackQuery) -> None:
    await callback.answer()
    wait = await callback.message.answer("⏳ Собираю статус резюме…")
    text, keyboard = await _render_resume_list(callback.from_user.id)
    try:
        await wait.delete()
    except Exception:
        pass
    await _send_long(callback.message, text, parse_mode="HTML", reply_markup=keyboard)


@router.message(Command("resumes"))
async def resumes_command(message: Message) -> None:
    wait = await message.answer("⏳ Собираю статус резюме…")
    text, keyboard = await _render_resume_list(message.from_user.id)
    try:
        await wait.delete()
    except Exception:
        pass
    await _send_long(message, text, parse_mode="HTML", reply_markup=keyboard)


# ---------------------------------------------------------------------------
# 2 & 3. Разбор изменений и заливка
# ---------------------------------------------------------------------------

async def _build_diff_context(client: HHOfficialClient, profile: dict, resume_id: str) -> dict:
    current_resume = await asyncio.to_thread(client.resume, resume_id)
    dictionaries = HHDictionaries(client=client)
    truncations: list[dict] = []
    payload = await asyncio.to_thread(build_payload, profile, dictionaries, current_resume, truncations=truncations)
    records = await asyncio.to_thread(diff, current_resume, payload, profile=profile)
    return {
        "current_resume": current_resume, "payload": payload,
        "records": records, "truncations": truncations, "resume_id": resume_id,
    }


async def _show_profile_diff(message: Message, state: FSMContext, user_id: int, profile_key: str) -> None:
    try:
        profile = load_profile(profile_key)
    except ProfileValidationError as exc:
        await message.answer(f"⚠️ Профиль повреждён: {html.escape(str(exc))}", parse_mode="HTML")
        return
    mapping = config.AUTOMATION.resume_mapping(user_id, profile_key)
    if not mapping or not mapping.get("resume_id"):
        await _show_link_options(message, state, user_id, profile_key)
        return
    client = await _hh_client(user_id)
    if client is None:
        await message.answer("HH не подключён. Подключите его в «⚙️ Настройки», затем откройте профиль снова.")
        return
    resume_id = str(mapping["resume_id"])
    wait = await message.answer("⏳ Сравниваю профиль с текущим резюме HH…")
    try:
        context = await _build_diff_context(client, profile, resume_id)
    except HHOfficialError as exc:
        await wait.edit_text(f"Не удалось получить резюме HH: {html.escape(exc.reason)}", parse_mode="HTML")
        return
    except DictionaryResolutionError as exc:
        await wait.edit_text(f"⚠️ {html.escape(str(exc))}", parse_mode="HTML")
        return
    try:
        await wait.delete()
    except Exception:
        pass
    text = _render_diff_text(profile, profile_key, context)
    await _send_long(message, text, parse_mode="HTML", reply_markup=_diff_keyboard(profile_key))


@router.callback_query(F.data.startswith("mvp:resumes:open:"))
async def open_profile(callback: CallbackQuery, state: FSMContext) -> None:
    profile_key = callback.data.rsplit(":", 1)[-1]
    await callback.answer()
    await _show_profile_diff(callback.message, state, callback.from_user.id, profile_key)


@router.callback_query(F.data.startswith("mvp:resumes:apply:"))
async def apply_profile_to_hh(callback: CallbackQuery, state: FSMContext) -> None:
    profile_key = callback.data.rsplit(":", 1)[-1]
    user_id = callback.from_user.id
    await callback.answer("Проверяю перед заливкой…")
    try:
        profile = load_profile(profile_key)
    except ProfileValidationError as exc:
        await callback.message.answer(f"⚠️ Профиль повреждён, заливка отменена: {html.escape(str(exc))}", parse_mode="HTML")
        return
    mapping = config.AUTOMATION.resume_mapping(user_id, profile_key)
    if not mapping or not mapping.get("resume_id"):
        await callback.message.answer("Профиль не привязан к резюме HH — сначала привяжите его.")
        await _show_link_options(callback.message, state, user_id, profile_key)
        return
    client = await _hh_client(user_id)
    if client is None:
        await callback.message.answer("HH не подключён — заливка отменена.")
        return
    resume_id = str(mapping["resume_id"])
    # Rebuild everything fresh right now — never reuse a diff computed for an earlier
    # "open profile" view, so an edit made after opening the diff is always what gets sent.
    try:
        context = await _build_diff_context(client, profile, resume_id)
    except HHOfficialError as exc:
        await callback.message.answer(
            f"Не удалось получить свежее резюме HH — заливка отменена: {html.escape(exc.reason)}", parse_mode="HTML",
        )
        return
    except DictionaryResolutionError as exc:
        await callback.message.answer(f"⚠️ Заливка отменена: {html.escape(str(exc))}", parse_mode="HTML")
        return

    backup_dir = config.ROOT / "var" / "resume-backups"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{resume_id}-{timestamp}.json"
    try:
        await asyncio.to_thread(snapshot, client, resume_id, backup_path)
    except Exception as exc:
        log.warning("Resume snapshot failed safely: %s", type(exc).__name__)
        await callback.message.answer("Не удалось сохранить резервную копию — заливка отменена, HH не тронут.")
        return

    try:
        await asyncio.to_thread(apply_profile, client, resume_id, context["payload"])
        # The card browser serves resumes from a short-lived cache; a write we
        # made ourselves must never be the thing it hides.
        invalidate_live_resume(resume_id)
    except HHResumeApplyError as exc:
        await callback.message.answer(
            f"❌ HH отклонил изменения (код {exc.status}): {html.escape(str(exc))}",
            parse_mode="HTML", reply_markup=_rollback_keyboard(profile_key),
        )
        return

    try:
        await asyncio.to_thread(publish, client, resume_id)
    except HHResumeApplyError as exc:
        await callback.message.answer(
            f"⚠️ Резюме обновлено, но HH не подтвердил публикацию (код {exc.status}): {html.escape(str(exc))}. "
            "Можно откатить последнюю резервную копию.",
            parse_mode="HTML", reply_markup=_rollback_keyboard(profile_key),
        )
        return

    changed_labels = [record["label"] for record in context["records"] if record["changed"]]
    summary = ", ".join(changed_labels) if changed_labels else "изменений не было, резюме опубликовано повторно"
    await callback.message.answer(
        f"✅ HH подтвердил обновление и публикацию резюме «{html.escape(str(profile.get('title') or profile_key))}».\n"
        f"Изменено: {html.escape(summary)}.\n"
        f"Резервная копия: <code>{html.escape(backup_path.name)}</code>",
        parse_mode="HTML", reply_markup=_rollback_keyboard(profile_key),
    )


@router.callback_query(F.data.startswith("mvp:resumes:rollback:"))
async def rollback_profile(callback: CallbackQuery) -> None:
    profile_key = callback.data.rsplit(":", 1)[-1]
    user_id = callback.from_user.id
    mapping = config.AUTOMATION.resume_mapping(user_id, profile_key)
    if not mapping or not mapping.get("resume_id"):
        await callback.answer("Резюме не привязано", show_alert=True)
        return
    resume_id = str(mapping["resume_id"])
    backup_dir = config.ROOT / "var" / "resume-backups"
    backups = sorted(backup_dir.glob(f"{resume_id}-*.json"), reverse=True)
    if not backups:
        await callback.answer("Резервная копия не найдена", show_alert=True)
        return
    client = await _hh_client(user_id)
    if client is None:
        await callback.answer("HH не подключён", show_alert=True)
        return
    await callback.answer("Восстанавливаю…")
    latest = backups[0]
    try:
        await asyncio.to_thread(restore, client, resume_id, latest)
        invalidate_live_resume(resume_id)
    except HHResumeApplyError as exc:
        await callback.message.answer(f"❌ HH не подтвердил откат: {html.escape(str(exc))}", parse_mode="HTML")
        return
    await callback.message.answer(
        f"↩️ HH подтвердил восстановление из резервной копии <code>{html.escape(latest.name)}</code>.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# 6. Профиль без привязки: линковка к существующему резюме или создание нового
# ---------------------------------------------------------------------------

async def _show_link_options(message: Message, state: FSMContext, user_id: int, profile_key: str) -> None:
    client = await _hh_client(user_id)
    if client is None:
        await message.answer(
            f"Профиль «{html.escape(profile_key)}» не привязан к резюме HH, а HH-аккаунт не подключён. "
            "Подключите HH в «⚙️ Настройки», затем вернитесь сюда.",
            parse_mode="HTML",
        )
        return
    try:
        resumes = await asyncio.to_thread(client.resumes)
    except HHOfficialError as exc:
        await message.answer(f"Не удалось получить список резюме HH: {html.escape(exc.reason)}", parse_mode="HTML")
        return
    rows: list[list[InlineKeyboardButton]] = []
    if resumes:
        candidates = [{"id": str(r.get("id") or ""), "title": str(r.get("title") or "")} for r in resumes[:15]]
        await state.update_data(link_profile_key=profile_key, link_candidates=candidates)
        for index, candidate in enumerate(candidates):
            label = candidate["title"][:36] or candidate["id"]
            rows.append([InlineKeyboardButton(text=label, callback_data=f"mvp:resumes:link_pick:{index}")])
    rows.append([InlineKeyboardButton(text="🆕 Создать новое резюме на HH", callback_data=f"mvp:resumes:link_new:{profile_key}")])
    intro = f"Профиль «{html.escape(profile_key)}» пока не привязан ни к одному резюме HH.\n"
    intro += "Выберите существующее резюме для привязки или создайте новое:" if resumes else "На аккаунте нет резюме — можно только создать новое:"
    await message.answer(intro, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("mvp:resumes:link:"))
async def start_link(callback: CallbackQuery, state: FSMContext) -> None:
    profile_key = callback.data.rsplit(":", 1)[-1]
    await callback.answer()
    await _show_link_options(callback.message, state, callback.from_user.id, profile_key)


@router.callback_query(F.data.startswith("mvp:resumes:link_pick:"))
async def pick_link_candidate(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        index = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректный выбор", show_alert=True)
        return
    data = await state.get_data()
    candidates = data.get("link_candidates") or []
    profile_key = data.get("link_profile_key")
    if not profile_key or index < 0 or index >= len(candidates):
        await callback.answer("Список устарел — откройте привязку заново", show_alert=True)
        return
    candidate = candidates[index]
    resume_id = str(candidate.get("id") or "")
    title = str(candidate.get("title") or "")
    if not resume_id:
        await callback.answer("У резюме нет id", show_alert=True)
        return
    await asyncio.to_thread(config.AUTOMATION.save_resume_mapping, callback.from_user.id, profile_key, resume_id, title)
    await callback.answer("Привязано")
    await callback.message.answer(
        f"🔗 Профиль «{html.escape(profile_key)}» привязан к резюме «{html.escape(title)}» "
        f"(id <code>{html.escape(resume_id)}</code>). Откройте профиль в списке резюме, чтобы посмотреть "
        "разбор изменений перед заливкой.",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("mvp:resumes:link_new:"))
async def create_new_hh_resume(callback: CallbackQuery) -> None:
    profile_key = callback.data.rsplit(":", 1)[-1]
    user_id = callback.from_user.id
    await callback.answer("Создаю резюме на HH…")
    try:
        profile = load_profile(profile_key)
    except ProfileValidationError as exc:
        await callback.message.answer(f"⚠️ Профиль повреждён: {html.escape(str(exc))}", parse_mode="HTML")
        return
    client = await _hh_client(user_id)
    if client is None:
        await callback.message.answer("HH не подключён.")
        return
    dictionaries = HHDictionaries(client=client)
    truncations: list[dict] = []
    try:
        payload = await asyncio.to_thread(build_payload, profile, dictionaries, {}, truncations=truncations)
    except DictionaryResolutionError as exc:
        await callback.message.answer(f"⚠️ Не удалось подготовить резюме: {html.escape(str(exc))}", parse_mode="HTML")
        return
    try:
        resume_id = await asyncio.to_thread(create_resume, client, payload)
    except HHResumeApplyError as exc:
        await callback.message.answer(
            f"❌ HH отклонил создание резюме (код {exc.status}): {html.escape(str(exc))}", parse_mode="HTML",
        )
        return
    await asyncio.to_thread(config.AUTOMATION.save_resume_mapping, user_id, profile_key, resume_id, str(payload.get("title") or ""))
    message_lines = [
        f"✅ HH подтвердил создание резюме «{html.escape(str(payload.get('title') or ''))}» "
        f"(id <code>{html.escape(resume_id)}</code>) и оно привязано к профилю «{html.escape(profile_key)}».",
    ]
    try:
        await asyncio.to_thread(publish, client, resume_id)
        message_lines.append("Резюме также опубликовано.")
    except HHResumeApplyError as exc:
        message_lines.append(f"⚠️ Публикация не подтверждена HH: {html.escape(str(exc))}. Опубликуйте вручную позже.")
    await callback.message.answer("\n".join(message_lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# 4. Правка текста прямо в чате
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("mvp:resumes:editfield:"))
async def start_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, _, field, profile_key = callback.data.split(":", 4)
    if field not in _EDIT_FIELD_STATES:
        await callback.answer("Неизвестное поле", show_alert=True)
        return
    await state.set_state(_EDIT_FIELD_STATES[field])
    await state.update_data(edit_profile_key=profile_key, edit_field=field)
    await callback.answer()
    await callback.message.answer(
        _EDIT_FIELD_PROMPTS[field],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Отмена", callback_data="mvp:resumes:edit_cancel"),
        ]]),
    )


@router.callback_query(F.data == "mvp:resumes:edit_cancel")
async def cancel_edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def _finish_field_edit(message: Message, state: FSMContext, field: str, value) -> None:
    data = await state.get_data()
    profile_key = str(data.get("edit_profile_key") or "")
    if not profile_key:
        await state.clear()
        await message.answer("Сессия редактирования устарела. Откройте профиль заново.")
        return
    try:
        await asyncio.to_thread(_write_profile_field, profile_key, lambda raw: _apply_field(raw, field, value))
    except ProfileValidationError as exc:
        await message.answer(f"⚠️ Значение не прошло проверку, профиль не изменён: {html.escape(str(exc))}", parse_mode="HTML")
        return
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Profile edit write failed safely: %s", type(exc).__name__)
        await message.answer("Не удалось сохранить изменение файла профиля. Профиль не изменён.")
        return
    await state.clear()
    await message.answer(
        "✅ Профиль обновлён локально. HH ничего не отправлено. Любой открытый ранее разбор изменений устарел — "
        "откройте профиль заново, чтобы увидеть свежее «было → станет» перед заливкой."
    )


@router.message(ResumeProfileEdit.headline, F.text)
async def save_headline_edit(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value or len(value) > MAX_TITLE_LENGTH:
        await message.answer(f"Нужен непустой заголовок до {MAX_TITLE_LENGTH} символов. Попробуйте ещё раз.")
        return
    await _finish_field_edit(message, state, "headline", value)


@router.message(ResumeProfileEdit.about, F.text)
async def save_about_edit(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value or len(value) > MAX_ABOUT_LENGTH:
        await message.answer(f"Нужен непустой текст до {MAX_ABOUT_LENGTH} символов. Попробуйте ещё раз.")
        return
    await _finish_field_edit(message, state, "about", value)


@router.message(ResumeProfileEdit.salary, F.text)
async def save_salary_edit(message: Message, state: FSMContext) -> None:
    raw = re.sub(r"[^\d]", "", message.text or "")
    if not raw:
        await message.answer("Нужно число, например 100000. Попробуйте ещё раз.")
        return
    amount = int(raw)
    if not (10_000 <= amount <= 10_000_000):
        await message.answer("Похоже на опечатку — ожидаю сумму в рублях от 10 000 до 10 000 000. Попробуйте ещё раз.")
        return
    await _finish_field_edit(message, state, "salary", amount)


@router.message(ResumeProfileEdit.skills, F.text)
async def save_skills_edit(message: Message, state: FSMContext) -> None:
    raw_items = re.split(r"[\n,]", message.text or "")
    skills = [item.strip() for item in raw_items if item.strip()]
    if not skills:
        await message.answer("Нужен хотя бы один навык. Попробуйте ещё раз.")
        return
    skills = skills[:MAX_SKILL_SET_COUNT]
    await _finish_field_edit(message, state, "skills", skills)


# ---------------------------------------------------------------------------
# 5. Загрузка своего резюме (PDF/DOCX)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "mvp:resumes:upload_hint")
async def upload_hint(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(
        "Пришлите файл резюме (PDF или DOCX) одним сообщением — прочитаю текст и предложу разбор по профилям."
    )


@router.message(F.document)
async def receive_uploaded_resume(message: Message, state: FSMContext) -> None:
    document = message.document
    if document is None:
        return
    file_name = document.file_name or "resume"
    ext = Path(file_name).suffix.lower()
    if ext not in {".pdf", ".docx"}:
        await message.answer("Понимаю только файлы PDF и DOCX. Пришлите резюме в одном из этих форматов.")
        return
    wait = await message.answer("⏳ Читаю файл…")
    try:
        file_info = await message.bot.get_file(document.file_id)
        buffer = await message.bot.download_file(file_info.file_path)
        raw_bytes = buffer.read()
    except Exception as exc:
        log.warning("Resume file download failed safely: %s", type(exc).__name__)
        await wait.edit_text("Не удалось скачать файл из Telegram. Попробуйте прислать ещё раз.")
        return
    text, note = await asyncio.to_thread(_extract_text, raw_bytes, ext)
    if not text.strip():
        await wait.edit_text(
            "Файл прочитан, но текста внутри нет — возможно, это скан без текстового слоя. "
            "Файл не сохранён, профиль не создан."
        )
        return
    upload_dir = config.ROOT / "var" / "resume-uploads"
    saved_path = upload_dir / f"{uuid.uuid4().hex[:12]}{ext}"

    def _save() -> None:
        upload_dir.mkdir(parents=True, exist_ok=True)
        saved_path.write_bytes(raw_bytes)

    await asyncio.to_thread(_save)
    await state.update_data(upload_text=text[:20000], upload_path=str(saved_path))
    note_text = f"\n\n⚠️ {html.escape(note)}" if note else ""
    await wait.edit_text(
        f"📄 Файл сохранён (<code>{html.escape(saved_path.name)}</code>) и прочитан, {len(text)} симв.{note_text}\n\n"
        "На место какого профиля использовать этот текст?",
        parse_mode="HTML", reply_markup=_upload_target_keyboard(),
    )


@router.callback_query(F.data.startswith("mvp:resumes:upload_target:"))
async def choose_upload_target(callback: CallbackQuery, state: FSMContext) -> None:
    target = callback.data.rsplit(":", 1)[-1]
    data = await state.get_data()
    if not data.get("upload_text"):
        await callback.answer("Сессия загрузки устарела — пришлите файл ещё раз", show_alert=True)
        return
    if target == "new":
        await state.set_state(ResumeUpload.choosing_new_key)
        await callback.answer()
        await callback.message.answer(
            "Придумайте короткий ключ латиницей для нового профиля (буквы, цифры, подчёркивания, "
            "начинается с буквы, 3–20 символов), например: sales_manager."
        )
        return
    await callback.answer()
    await state.update_data(upload_target_key=target)
    await _run_ai_mapping(callback.message, state, target)


@router.message(ResumeUpload.choosing_new_key, F.text)
async def name_new_profile(message: Message, state: FSMContext) -> None:
    raw_key = (message.text or "").strip().lower().replace(" ", "_").replace("-", "_")
    key = re.sub(r"[^a-z0-9_]", "", raw_key)
    if not _NEW_PROFILE_KEY_RE.fullmatch(key):
        await message.answer(
            "Нужен ключ латиницей: буквы/цифры/подчёркивания, начинается с буквы, 3–20 символов. "
            "Попробуйте ещё раз."
        )
        return
    if (PROFILE_ROOT / f"{key}.json").exists():
        await message.answer(f"Профиль «{html.escape(key)}» уже существует. Пришлите другой ключ.", parse_mode="HTML")
        return
    await state.set_state(None)
    await state.update_data(upload_target_key=key)
    await _run_ai_mapping(message, state, key)


async def _run_ai_mapping(message: Message, state: FSMContext, target_key: str) -> None:
    if config.ADAPTER is None:
        await message.answer(
            "AI-провайдер сейчас не настроен, поэтому автоматически разобрать резюме нельзя. "
            "Загруженный файл остаётся сохранён на сервере; профиль не создан и не изменён — ничего не "
            "придумываю. Попробуйте через это же меню, когда провайдер снова будет доступен."
        )
        return
    data = await state.get_data()
    text = data.get("upload_text") or ""
    wait = await message.answer("🤖 Прошу модель разобрать резюме в формат профиля…")
    try:
        parsed = await asyncio.to_thread(_map_resume_to_profile, text, target_key)
    except Exception as exc:
        log.warning("Resume upload AI mapping failed safely: %s", type(exc).__name__)
        await wait.edit_text(
            "AI-провайдер не смог разобрать резюме (недоступен или вернул ошибку). Загруженный файл остаётся "
            "сохранён; профиль не создан и не изменён — ничего не придумываю. Попробуйте ещё раз позже."
        )
        return
    await state.update_data(upload_parsed=parsed)
    try:
        await wait.delete()
    except Exception:
        pass
    try:
        preview_text = _render_parsed_preview(parsed, target_key)
    except Exception:
        log.exception("Resume preview rendering failed")
        raw_json = json.dumps(parsed, ensure_ascii=False, indent=2)[:3500]
        preview_text = f"🧾 Разбор для профиля «{html.escape(target_key)}»:\n<pre>{html.escape(raw_json)}</pre>"
    await _send_long(message, preview_text, parse_mode="HTML", reply_markup=_upload_confirm_keyboard(target_key))


@router.callback_query(F.data.startswith("mvp:resumes:upload_save:"))
async def save_uploaded_profile(callback: CallbackQuery, state: FSMContext) -> None:
    target_key = callback.data.rsplit(":", 1)[-1]
    data = await state.get_data()
    parsed = data.get("upload_parsed")
    if not parsed or data.get("upload_target_key") != target_key:
        await callback.answer("Сессия устарела — начните загрузку заново", show_alert=True)
        return
    parsed = dict(parsed)
    parsed["profile_key"] = target_key
    try:
        await asyncio.to_thread(_commit_profile_dict, target_key, parsed)
    except ProfileValidationError as exc:
        await callback.answer()
        await callback.message.answer(
            f"⚠️ Модель вернула данные не по схеме — профиль не сохранён: {html.escape(str(exc))}", parse_mode="HTML",
        )
        return
    await state.clear()
    await callback.answer("Сохранено")
    await callback.message.answer(
        f"✅ Профиль «{html.escape(target_key)}» сохранён из загруженного резюме. На HH ничего не отправлено — "
        "откройте «📄 Резюме» в списке, чтобы посмотреть разбор изменений перед заливкой.",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "mvp:resumes:upload_cancel")
async def cancel_upload(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
