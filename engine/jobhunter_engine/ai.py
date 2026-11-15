"""ИИ-слой.

Если задан ANTHROPIC_API_KEY — Claude, экономная схема:
  • оценка сотен вакансий — Claude Haiku 5.5 (≈$0.0005 за вакансию);
  • письма и ответы работодателям — Claude Opus 5.5 (их десятки в день, качество важнее всего).
Без ключа — бесплатные GitHub Models (GITHUB_TOKEN в Actions): gpt-4.1-mini / gpt-4.1.
"""
from __future__ import annotations

import json
import logging
import os
import re

import requests

from candidate.service import extract_apply_instructions, extract_screening_steps, required_letter_openings  # dream-job-bot

log = logging.getLogger("jobhunter")

SCORE_MODEL = os.environ.get("CLAUDE_SCORE_MODEL", "claude-haiku-5-5")
WRITE_MODEL = os.environ.get("CLAUDE_WRITE_MODEL", "claude-opus-5-5")
GH_SCORE_MODEL = os.environ.get("GH_SCORE_MODEL", "openai/gpt-4.1-mini")
GH_WRITE_MODEL = os.environ.get("GH_WRITE_MODEL", "openai/gpt-4.1")

_client = None


def provider() -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return f"Claude: оценка {SCORE_MODEL}, письма {WRITE_MODEL}"
    if os.environ.get("GITHUB_TOKEN"):
        return f"GitHub Models (бесплатно): {GH_SCORE_MODEL} / {GH_WRITE_MODEL}"
    return "ИИ не подключён"


def _claude(system: str, user: str, schema: dict, *, write: bool, max_tokens: int) -> dict:
    global _client
    import anthropic

    if _client is None:
        _client = anthropic.Anthropic()
    fmt = {"type": "json_schema", "schema": schema}
    if write:
        msg = _client.beta.messages.create(
            model=WRITE_MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "medium", "format": fmt},
        )
    else:
        msg = _client.messages.create(
            model=SCORE_MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low", "format": fmt},
        )
    if msg.stop_reason == "refusal":
        raise RuntimeError("Claude отклонил запрос")
    text = "".join(b.text for b in msg.content if b.type == "text")
    return json.loads(text)


def _github_models(system: str, user: str, schema: dict, *, write: bool, max_tokens: int) -> dict:
    r = requests.post(
        "https://models.github.ai/inference/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Content-Type": "application/json"},
        json={
            "model": GH_WRITE_MODEL if write else GH_SCORE_MODEL,
            "messages": [
                {"role": "system", "content": system + "\nОтветь строго одним JSON-объектом по схеме:\n" + json.dumps(schema, ensure_ascii=False)},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.7 if write else 0.2,
            "max_tokens": max_tokens,
        },
        timeout=120,
    )
    if r.status_code == 429:
        raise RuntimeError("лимит GitHub Models на сегодня исчерпан")
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub Models {r.status_code}: {r.text[:300]}")
    try:
        text = r.json()["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError(f"GitHub Models вернул не JSON: {r.text[:200]}")
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


def llm_json(system: str, user: str, schema: dict, *, write: bool = False, max_tokens: int = 3000) -> dict:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _claude(system, user, schema, write=write, max_tokens=max_tokens)
    if os.environ.get("GITHUB_TOKEN"):
        return _github_models(system, user, schema, write=write, max_tokens=min(max_tokens, 4000))
    raise RuntimeError("ИИ не подключён: добавьте ANTHROPIC_API_KEY или запускайте в GitHub Actions")


def clamp(v, lo=0, hi=100) -> int:
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return lo


# ─────────────────────────────── Инструкции работодателя (кодовые слова и т.п.) ───────────────────────────────

EN_MARKERS = re.compile(
    r"(include|mention|use|write|put|add|start|begin|type)\b.{0,60}\b(word|phrase|code|keyword|password|subject|message|application|cover letter|email|dm)"
    r"|code ?word|secret word|subject line|to show (that )?you('ve)? read|prove you read|answer (the following|these) questions",
    re.I,
)
QUOTED = re.compile(r"[\"“«'‘`]([^\"”»'’`]{1,80})[\"”»'’`]")
CAPS_WORD = re.compile(r"\b(?:word|keyword|code ?word|password)\s*[:\-]?\s*([A-Z][A-Z0-9_\-]{2,30})\b")


def regex_instructions(title: str, description: str) -> dict:
    """Правила dream-job-bot (русские формулировки) + английские шаблоны (наше улучшение)."""
    vac = {"name": title, "description": description}
    lines = list(extract_apply_instructions(vac))
    openings = list(required_letter_openings(vac))
    phrases: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", description):
        s = sentence.strip()
        if not s or len(s) > 400:
            continue
        if EN_MARKERS.search(s):
            if s not in lines:
                lines.append(s)
            phrases += [p.strip() for p in QUOTED.findall(s)]
            phrases += CAPS_WORD.findall(s)
        if re.search(r"кодов|слово|фраз", s, re.I) and re.search(r"напиш|укаж|добав|отклик|письм", s, re.I):
            phrases += [p.strip() for p in QUOTED.findall(s)]
    phrases = [p for p in dict.fromkeys(phrases) if p and p not in openings]
    return {"lines": lines[:8], "openings": openings, "phrases": phrases[:6], "screening": list(extract_screening_steps(vac))}


# ─────────────────────────────── Оценка вакансии ───────────────────────────────

EVAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["is_job", "scam", "fit", "conditions", "chance", "verdict", "red_flags", "must_include", "must_start_with", "subject", "questions", "other_rules", "manual_only"],
    "properties": {
        "is_job": {"type": "boolean"},
        "scam": {"type": "boolean"},
        "fit": {"type": "integer"},
        "conditions": {"type": "integer"},
        "chance": {"type": "integer"},
        "verdict": {"type": "string"},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "must_include": {"type": "array", "items": {"type": "string"}, "description": "кодовые слова/фразы, которые работодатель просит дословно написать в отклике"},
        "must_start_with": {"type": "string", "description": "фраза, с которой просят начать отклик, или пустая строка"},
        "subject": {"type": "string", "description": "что просят указать в теме письма, или пустая строка"},
        "questions": {"type": "array", "items": {"type": "string"}, "description": "вопросы, на которые просят ответить в отклике"},
        "other_rules": {"type": "array", "items": {"type": "string"}, "description": "другие требования к отклику (язык, формат, приложить ссылку на GitHub и т.п.)"},
        "manual_only": {"type": "boolean", "description": "откликнуться можно только заполнив анкету/тест/форму"},
    },
}


def evaluate(v: dict, profile: dict) -> dict:
    rx = regex_instructions(v["title"], v.get("description") or "")
    hint = ("\n\nПохоже на инструкции к отклику (проверь и извлеки точно):\n- " + "\n- ".join(rx["lines"])) if rx["lines"] else ""
    r = llm_json(
        "Ты опытный IT-рекрутер. Оцени вакансию для кандидата честно и строго (0-100): fit — соответствие требованиям; "
        "conditions — деньги относительно рынка, удалёнка, гибкость, продукт, рост; chance — вероятность приглашения "
        "(у кандидата ~1.5 года своей студии, без вуза, сильное портфолио; Senior/Lead с 5+ годами — низкий шанс). "
        "scam — платное обучение, предоплата, серые схемы. Отдельно и ДОСЛОВНО извлеки инструкции к отклику: кодовые слова, "
        "фразу для начала письма, тему письма, вопросы, на которые нужно ответить. verdict — 1-2 предложения по-русски.",
        f"КАНДИДАТ:\n{profile['resumeRu']}\n\nПОЖЕЛАНИЯ: {profile['wishes']}\n\nВАКАНСИЯ ({v['source']}): {v['title']}\n"
        f"Компания: {v.get('company') or '?'} · {v.get('location') or ''}{' · удалённо' if v.get('remote') else ''}\n"
        f"Зарплата: {v.get('salary_from') or '?'}–{v.get('salary_to') or '?'} {v.get('currency') or ''}\n\n{(v.get('description') or '')[:7000]}{hint}",
        EVAL_SCHEMA, write=False, max_tokens=2500,
    )
    must = list(dict.fromkeys([*(r.get("must_include") or []), *rx["phrases"]]))
    start = (r.get("must_start_with") or "").strip() or (rx["openings"][0] if rx["openings"] else "")
    return {
        "is_job": r.get("is_job", True) is not False, "scam": bool(r.get("scam")),
        "fit": clamp(r.get("fit")), "conditions": clamp(r.get("conditions")), "chance": clamp(r.get("chance")),
        "verdict": str(r.get("verdict") or "")[:400], "red_flags": [str(x) for x in (r.get("red_flags") or [])][:6],
        "instructions": {
            "must_include": [m for m in must if m][:6], "must_start_with": start, "subject": (r.get("subject") or "").strip(),
            "questions": [str(q) for q in (r.get("questions") or [])][:5], "other_rules": [str(q) for q in (r.get("other_rules") or [])][:5],
            "manual_only": bool(r.get("manual_only")), "screening": rx["screening"], "raw": rx["lines"],
        },
    }


def final_score(rule: int | None, e: dict) -> int:
    if not e["is_job"] or e["scam"]:
        return 0
    base = 0.35 * e["fit"] + 0.25 * e["conditions"] + 0.25 * e["chance"] + 0.15 * (rule if rule is not None else 50)
    return clamp(base - 2 * len(e["red_flags"]))


# ─────────────────────────────── Письмо ───────────────────────────────

RU_BANNED = ["очень нужна работа", "готов на всё", "компания моей мечты", "восхищён вашей компанией", "в современном мире", "динамично развивающ",
             "команде профессионалов", "хотел бы выразить", "с большим интересом ознакомился", "уверен, что мой опыт", "буду рад стать частью",
             "идеально подхожу", "нечто большее", "не просто "]
RU_OPENERS = ["вам нужно", "вам предстоит", "вам требуется", "вам важно", "вы ищете", "вижу, что вы", "судя по вакансии", "в вашей вакансии", "ваша задача"]
EN_BANNED = ["i am excited", "i'm excited", "thrilled", "passionate about", "i am writing to express", "i'm writing to express", "dynamic team", "fast-paced",
             "leverage my", "perfect fit", "dear hiring manager", "delve", "tapestry", "embark", "in today's", "furthermore", "moreover", "synergy",
             "i am confident that", "look no further", "i believe i would be a great fit"]
ANTITHESIS = re.compile(r"(?:^|[\s«(])не\s+[^,.;:!?]{2,45},\s*а\s+", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", " ", s.lower()).strip()


def letter_problems(letter: str, lang: str, ins: dict | None = None, chat: bool = False) -> list[str]:
    p = []
    low = letter.lower()
    if "—" in letter:
        p.append("убери длинное тире «—»")
    if lang == "ru":
        p += [f"убери шаблон «{b.strip()}»" for b in RU_BANNED if b in low]
        if ANTITHESIS.search(letter):
            p.append("убери конструкцию «не X, а Y»")
        first = re.sub(r"^(здравствуйте|добрый день|привет)[!,.]?\s*", "", low)[:80]
        if any(first.startswith(o) for o in RU_OPENERS):
            p.append("не начинай с пересказа вакансии")
    else:
        p += [f'remove the cliché "{b}"' for b in EN_BANNED if b in low]
    lo, hi = (250, 900) if chat else (350, 1300)
    if len(letter) < lo:
        p.append(f"слишком коротко, нужно {lo}–{hi} знаков")
    if len(letter) > hi + 300:
        p.append(f"слишком длинно, сократи до {hi} знаков")
    if re.search(r"\[[^\]]+\]|\{[^}]+\}", letter):
        p.append("остались плейсхолдеры в скобках")
    if ins:
        for w in ins.get("must_include") or []:
            if _norm(w) and _norm(w) not in _norm(letter):
                p.append(f"работодатель просил дословно написать «{w}», вставь это слово/фразу дословно")
        start = ins.get("must_start_with")
        if start and not _norm(letter).startswith(_norm(start)):
            p.append(f"письмо обязано начинаться дословно с «{start}»")
    return p


def humanize(s: str) -> str:
    s = re.sub(r"\s*—\s*", ", ", s)
    s = re.sub(r",\s*,", ",", s).replace("**", "")
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def signature(profile: dict, lang: str) -> str:
    if lang == "en":
        return f"Vlad\nPortfolio: {profile['portfolioEn']}\nTelegram: {profile['telegram']} · {profile['email']}"
    return f"Влад\nПортфолио: {profile['portfolioRu']}\nTelegram: {profile['telegram']} · {profile['email']}"


def write_letter(v: dict, profile: dict, *, channel: str, instructions: dict | None = None, extra: str = "") -> dict:
    lang = "en" if v.get("lang") == "en" else "ru"
    chat = channel in ("telegram", "vk")
    ins = instructions or {}
    if lang == "en":
        rules = [
            "Write like a real person: plain, specific, confident, not salesy. Natural American English.",
            "Open with a short greeting and who you are in one line. Never restate the job post back to them.",
            "Pick the 2–3 most important requirements and answer each with a REAL fact from the resume.",
            "Use ONLY facts from the resume. Never invent numbers, employers, years or technologies.",
            "Mention the studio website is the portfolio and worth clicking around (the link is added in the signature).",
            "End with a concrete next step: a 15–20 minute call or a quick project walkthrough.",
            "No em dashes, no clichés (excited, passionate, thrilled, perfect fit, leverage, dynamic team), no emojis.",
            "350–600 characters, chat tone." if chat else "500–1000 characters, 2–4 short paragraphs.",
            "Do not add a signature, name or links at the end: they are appended automatically.",
        ]
    else:
        rules = [
            "Пиши как живой человек: просто, конкретно, уверенно, без канцелярита и пафоса.",
            "Первая фраза: приветствие и сразу суть, кто ты. Не пересказывай работодателю его же вакансию.",
            "Возьми 2–3 главных требования вакансии и на каждое ответь реальным фактом из резюме.",
            "Только факты из резюме. Ничего не выдумывай: ни цифр, ни компаний, ни сроков, ни технологий.",
            "Если чего-то из требований нет в опыте, не ври; можно сказать, что быстро осваиваешь с ИИ-инструментами.",
            "Скажи, что сайт студии и есть портфолио, его стоит покрутить руками (ссылка будет в подписи).",
            "Заверши конкретным шагом: созвон на 15–20 минут или показать проекты.",
            "Без длинных тире, без «не X, а Y», без «уверен, что», «идеально подхожу», без смайликов.",
            "350–600 знаков, тон мессенджера, на «вы»." if chat else "500–1000 знаков, 2–4 коротких абзаца.",
            "Не добавляй подпись, имя и ссылки в конце: подпись добавится автоматически.",
        ]
    must = []
    if ins.get("must_start_with"):
        must.append(f"НАЧНИ письмо ДОСЛОВНО с: «{ins['must_start_with']}»")
    for w in ins.get("must_include") or []:
        must.append(f"Обязательно вставь ДОСЛОВНО (кодовое слово/фраза от работодателя): «{w}»")
    for qq in ins.get("questions") or []:
        must.append(f"Коротко и честно ответь на вопрос работодателя: {qq}")
    for o in ins.get("other_rules") or []:
        must.append(f"Требование работодателя к отклику: {o}")
    resume = profile["resumeEn"] if lang == "en" else profile["resumeRu"]
    base = (
        f"РЕЗЮМЕ:\n{resume}\n\nВАКАНСИЯ: {v['title']} — {v.get('company') or ''}\n{(v.get('description') or '')[:6000]}\n\n"
        + ("ТРЕБОВАНИЯ РАБОТОДАТЕЛЯ К ОТКЛИКУ (выполнить важнее всего остального):\n- " + "\n- ".join(must) + "\n\n" if must else "")
        + "ПРАВИЛА:\n- " + "\n- ".join(rules) + (f"\n\nДОПОЛНИТЕЛЬНО: {extra}" if extra else "")
    )
    system = (
        "You write short, human cover letters on behalf of a job seeker. They must never read as AI-generated, and they must follow "
        "every instruction the employer put in the job post (code words, opening phrase, questions) exactly. Return JSON."
        if lang == "en" else
        "Ты пишешь короткие человеческие сопроводительные письма от лица соискателя. Текст не должен выглядеть сгенерированным. "
        "Все инструкции работодателя (кодовые слова, первая фраза, вопросы) выполняются дословно. Ответ в JSON."
    )
    schema = {"type": "object", "additionalProperties": False, "required": ["letter", "subject"],
              "properties": {"letter": {"type": "string"}, "subject": {"type": "string", "description": "тема письма для email; если работодатель задал тему, она дословно"}}}
    letter, subject, problems = "", "", []
    for attempt in range(3):
        user = base if attempt == 0 else f"{base}\n\nПРЕДЫДУЩИЙ ВАРИАНТ:\n{letter}\n\nИСПРАВЬ ТОЛЬКО ЭТО, остальное сохрани:\n- " + "\n- ".join(problems)
        r = llm_json(system, user, schema, write=True, max_tokens=4000)
        letter, subject = humanize(str(r.get("letter") or "")), str(r.get("subject") or "").strip()
        problems = letter_problems(letter, lang, ins, chat)
        if not problems:
            break
    if ins.get("subject"):
        subject = ins["subject"] if _norm(ins["subject"]) not in _norm(subject) else subject
    if not subject:
        subject = f"{v['title']}: application from Vlad" if lang == "en" else f"Отклик на вакансию «{v['title']}»"
    return {"letter": f"{letter}\n\n{signature(profile, lang)}", "subject": subject, "problems": problems}


# ─────────────────────────────── Ответ работодателя ───────────────────────────────

def analyze_reply(v: dict | None, history: str, reply: str, profile: dict) -> dict:
    lang = "en" if (v or {}).get("lang") == "en" else "ru"
    r = llm_json(
        "Ты ассистент соискателя. Разбери ответ работодателя и подготовь короткий, вежливый, человеческий ответ от лица кандидата. "
        "Если зовут на созвон — предложи 2–3 слота в ближайшие рабочие дни (10:00–19:00 МСК) и спроси удобный формат. "
        "Если просят тестовое или анкету — согласись и уточни сроки. Не выдумывай факты, только из резюме.",
        f"Кандидат:\n{profile['resumeEn'] if lang == 'en' else profile['resumeRu']}\n\nВакансия: {(v or {}).get('title', '?')} — {(v or {}).get('company', '?')}\n\n"
        f"История переписки:\n{history[-3000:]}\n\nНОВЫЙ ОТВЕТ РАБОТОДАТЕЛЯ:\n{reply[:3000]}\n\n"
        f"Ответ кандидата пиши на языке переписки ({'English' if lang == 'en' else 'русский'}), без длинных тире, 200–600 знаков.",
        {"type": "object", "additionalProperties": False, "required": ["kind", "hot", "summary", "suggested_answer"],
         "properties": {"kind": {"type": "string", "enum": ["invite", "offer", "question", "test", "reject", "other"]},
                        "hot": {"type": "boolean"}, "summary": {"type": "string"}, "suggested_answer": {"type": "string"}}},
        write=True, max_tokens=3000,
    )
    r["suggested_answer"] = humanize(r.get("suggested_answer") or "")
    return r
