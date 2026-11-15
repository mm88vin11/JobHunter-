from __future__ import annotations

import html
import json
from copy import deepcopy
from pathlib import Path


def _resume_document(payload: dict) -> dict:
    if payload.get("resume_document"):
        return payload["resume_document"]
    generated = payload["generated_variant"]
    return {
        "basics": {"headline": generated["title"], "summary": generated["about"], "contacts": {}},
        "experience": [],
        "projects": [{"title": "Ключевые результаты", "bullets": [{"text": x} for x in generated["achievements"]]}],
        "skills": generated["skills"], "education": [],
    }


def enrich_resume_document(payload: dict, hh_resume: dict) -> dict:
    """Merge verified identity/contact/education fields from the user's HH resume."""
    enriched = deepcopy(payload)
    resume = enriched.setdefault("resume_document", _resume_document(payload))
    basics = resume.setdefault("basics", {})
    name = " ".join(
        str(hh_resume.get(key) or "").strip()
        for key in ("first_name", "middle_name", "last_name")
        if hh_resume.get(key)
    )
    contacts = {}
    for item in hh_resume.get("contact") or []:
        kind = str((item.get("type") or {}).get("id") or "contact")
        raw_value = item.get("value")
        if isinstance(raw_value, dict):
            value = str(raw_value.get("formatted") or raw_value.get("number") or "").strip()
        else:
            value = str(raw_value or "").strip()
        if value:
            contacts[kind] = value
    if name:
        basics["name"] = name
    if contacts:
        basics["contacts"] = contacts

    education = []
    for key in ("primary", "additional", "attestation", "elementary"):
        for item in (hh_resume.get("education") or {}).get(key) or []:
            title = str(item.get("name") or item.get("organization") or "").strip()
            if not title:
                continue
            education.append({
                "title": title,
                "organization": str(item.get("organization") or item.get("result") or "").strip(),
                "year": str(item.get("year") or "").strip(),
            })
    if education:
        resume["education"] = education
    enriched["hh_resume_snapshot"] = {
        "resume_id": str(hh_resume.get("id") or ""),
        "title": str(hh_resume.get("title") or ""),
        "updated_at": str(hh_resume.get("updated_at") or ""),
        "contact_types": sorted(contacts),
        "experience_count": len(hh_resume.get("experience") or []),
        "education_count": len(education),
    }
    return enriched


def render_resume_html(payload: dict, output_path: str | Path, *, ats: bool = False) -> Path:
    resume = _resume_document(payload)
    basics = resume["basics"]
    contacts = " · ".join(html.escape(str(value)) for value in basics.get("contacts", {}).values() if value)
    def bullets(items: list[dict]) -> str:
        return "".join(f"<li>{html.escape(item['text'])}</li>" for item in items)

    experience = "".join(
        f"<article><div class='entry-head'><div><h3>{html.escape(entry['title'])}</h3><p class='role'>{html.escape(entry.get('role',''))}</p></div><time>{html.escape(entry.get('dates',''))}</time></div>"
        f"<ul>{bullets(entry.get('bullets', []))}</ul></article>"
        for entry in resume.get("experience", [])
    )
    projects = "".join(
        f"<article><h3>{html.escape(project.get('title','Проекты'))}</h3>"
        f"<ul>{bullets(project.get('bullets', []))}</ul></article>"
        for project in resume.get("projects", [])
    )
    skills = "".join(f"<span>{html.escape(str(skill))}</span>" for skill in resume.get("skills", []))
    education = "".join(
        f"<article><div class='entry-head'><div><h3>{html.escape(item.get('title',''))}</h3>"
        f"<p class='role'>{html.escape(item.get('organization',''))}</p></div>"
        f"<time>{html.escape(item.get('year',''))}</time></div></article>"
        for item in resume.get("education", [])
    )
    style = """
@page{size:A4;margin:16mm 17mm}*{box-sizing:border-box}body{font:14px/1.48 Arial,sans-serif;color:#172033;max-width:820px;margin:32px auto;padding:0 28px}header{border-bottom:2px solid #1d4ed8;padding-bottom:18px;margin-bottom:24px}h1{font-size:30px;line-height:1.12;margin:0 0 8px;letter-spacing:-.02em}h2{font-size:13px;text-transform:uppercase;color:#1d4ed8;margin:28px 0 12px;break-after:avoid-page;font-weight:700}h3{font-size:17px;margin:0 0 3px}.summary{font-size:15px;line-height:1.58;margin:0;white-space:pre-line}.contacts,.role,time{color:#586174}.entry-head{display:block}.entry-head time{display:block;margin-top:2px}article{break-inside:avoid;margin:0 0 18px}ul{margin:9px 0 0;padding-left:20px}li{margin:0 0 6px}.skills{display:flex;flex-wrap:wrap;gap:7px}.skills span{background:#eef3ff;border-radius:999px;padding:5px 9px;font-size:13px}.note{margin-top:30px;color:#6b7280;font-size:11px}@media print{body{margin:0;max-width:none;padding:0}.note{display:none}.skills{gap:0}.skills span{background:none;padding:0}.skills span:not(:last-child)::after{content:'\\00a0·\\00a0';color:#6b7280}}
"""
    if ats:
        style += ".skills{display:block}.skills span{background:none;padding:0}.skills span:not(:last-child)::after{content:', '}"
    name = str(basics.get("name") or "").strip()
    headline_line = f'<p class="summary">{html.escape(basics["headline"])}</p>' if name else ""
    sections = []
    if experience:
        sections.append(f"<section><h2>Опыт работы</h2>{experience}</section>")
    if projects:
        sections.append(f"<section><h2>Ключевые проекты</h2>{projects}</section>")
    if education:
        sections.append(f"<section><h2>Образование</h2>{education}</section>")
    sections.append(f"<section><h2>Навыки</h2><div class='skills'>{skills}</div></section>")
    vacancy = payload.get("vacancy", {})
    document = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(basics['headline'])}</title><style>{style}</style></head>
<body><header><h1>{html.escape(name or basics['headline'])}</h1>
{headline_line}<div class="contacts">{contacts}</div></header>
<main><section><h2>О себе</h2><p class="summary">{html.escape(basics['summary'])}</p></section>{''.join(sections)}</main>
<p class="note">Адаптировано для: {html.escape(vacancy.get('name',''))}</p></body></html>"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    return path


def render_variant_html(payload: dict, output_path: str | Path) -> Path:
    return render_resume_html(payload, output_path)


def render_ats_resume_html(payload: dict, output_path: str | Path) -> Path:
    return render_resume_html(payload, output_path, ats=True)


def render_cover_letter_html(payload: dict, output_path: str | Path) -> Path:
    vacancy = payload.get("vacancy", {})
    letter = payload["generated_variant"]["cover_letter"]
    document = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Сопроводительное письмо</title>
<style>@page{{size:A4;margin:22mm}}body{{font:16px/1.65 Arial,sans-serif;color:#172033;max-width:760px;margin:48px auto;padding:0 28px}}h1{{font-size:24px}}p{{white-space:pre-wrap}}small{{color:#6b7280}}@media print{{body{{margin:0;padding:0}}}}</style></head>
<body><h1>Сопроводительное письмо</h1><small>{html.escape(vacancy.get('name',''))}</small><p>{html.escape(letter)}</p></body></html>"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    return path


def render_vacancy_html(payload: dict, output_path: str | Path) -> Path:
    vacancy = payload.get("vacancy", payload)
    source_url = vacancy.get("source_url") or ""
    details = {
        "Источник": vacancy.get("source") or "manual",
        "Компания": vacancy.get("employer_name") or vacancy.get("employer") or "Не указана",
        "Тип": vacancy.get("vacancy_type") or "unknown",
        "География": vacancy.get("geography") or "Не указана",
        "Зарплата": vacancy.get("salary") or "Не указана",
        "Получено": vacancy.get("fetched_at") or "Не указано",
    }
    rows = "".join(
        f"<dt>{html.escape(label)}</dt><dd>{html.escape(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))}</dd>"
        for label, value in details.items()
    )
    raw_text = vacancy.get("raw_text") or vacancy.get("description") or vacancy.get("normalized_text") or ""
    link = (
        f'<p><a href="{html.escape(source_url, quote=True)}">Открыть исходную страницу</a></p>'
        if source_url else ""
    )
    document = f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<title>{html.escape(vacancy.get('name') or 'Вакансия')}</title>
<style>body{{font:16px/1.55 system-ui;max-width:900px;margin:40px auto;padding:0 24px;color:#171717}}h1{{font-size:30px}}dl{{display:grid;grid-template-columns:140px 1fr;gap:8px 20px}}dt{{font-weight:650}}dd{{margin:0}}pre{{white-space:pre-wrap;font:inherit;background:#f5f5f5;padding:20px;border-radius:12px}}</style>
<body><h1>{html.escape(vacancy.get('name') or 'Вакансия')}</h1><dl>{rows}</dl>{link}
<h2>Исходное описание</h2><pre>{html.escape(str(raw_text))}</pre>
<hr><small>Локальный snapshot; сохранён до адаптации резюме.</small></body></html>"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    return path
