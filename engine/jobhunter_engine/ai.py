"""ИИ-слой.

Если задан ANTHROPIC_API_KEY — Claude, экономная схема:
  • оценка сотен вакансий — Claude Haiku 5.5 (≈$0.0005 за вакансию);
  • письма и ответы работодателям — Claude Opus 5.5 (их десятки в день, качество важнее всего).
Без ключа Claude — цепочка бесплатных моделей: Google Gemini → Groq → OpenRouter (если заданы бесплатные ключи) → Pollinations (без ключа).
Если все недоступны — оценка по правилам dream-job-bot и черновик по шаблону.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

from candidate.service import extract_apply_instructions, extract_screening_steps, required_letter_openings  # dream-job-bot

log = logging.getLogger("jobhunter")

SCORE_MODEL = os.environ.get("CLAUDE_SCORE_MODEL", "claude-haiku-5-5")
WRITE_MODEL = os.environ.get("CLAUDE_WRITE_MODEL", "claude-opus-5-5")

_client = None


# Бесплатные провайдеры с OpenAI-совместимым API. Порядок = приоритет качества.
# Ключи Gemini / Groq / OpenRouter бесплатные (без карты); Pollinations работает вообще без ключа.
FREE_PROVIDERS = [
    ("Google Gemini", "GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
     os.environ.get("GEMINI_MODEL", "gemini-flash-latest")),
    ("Groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1/chat/completions", os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")),
    ("OpenRouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/chat/completions",
     os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")),
    ("Pollinations", None, "https://text.pollinations.ai/openai", os.environ.get("POLLINATIONS_MODEL", "openai")),
]
_last_call = {"t": 0.0}


def _free_chain() -> list[tuple]:
    return [p for p in FREE_PROVIDERS if p[1] is None or os.environ.get(p[1])]


def provider() -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return f"Claude: оценка {SCORE_MODEL}, письма {WRITE_MODEL}"
    chain = _free_chain()
    return "Бесплатно: " + " → ".join(f"{name} ({model})" for name, _, _, model in chain)


def enabled() -> bool:
    return os.environ.get("JH_DISABLE_AI") != "1"


def _openai_compatible(name: str, key_env: str | None, url: str, model: str, system: str, user: str, schema: dict, max_tokens: int, write: bool) -> dict:
    import time

    import requests

    if key_env is None:  # анонимный тариф Pollinations: не чаще раза в несколько секунд
        wait = 4 - (time.time() - _last_call["t"])
        if wait > 0:
            time.sleep(wait)
        _last_call["t"] = time.time()
    headers = {"Content-Type": "application/json"}
    if key_env:
        headers["Authorization"] = f"Bearer {os.environ[key_env]}"
    r = requests.post(url, headers=headers, timeout=180, json={
        "model": model,
        "messages": [
            {"role": "system", "content": system + "\nОтветь строго одним JSON-объектом по этой схеме (все числа — целые 0..100, где это оценки):\n"
                                         + json.dumps(schema, ensure_ascii=False)},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.7 if write else 0.2,
        "max_tokens": max_tokens,
    })
    if r.status_code >= 400:
        raise RuntimeError(f"{name} {r.status_code}: {r.text[:200]}")
    msg = r.json()["choices"][0]["message"]
    text = msg.get("content") or ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"{name}: ответ не JSON: {text[:150]}")
    return json.loads(m.group(0))


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


def llm_json(system: str, user: str, schema: dict, *, write: bool = False, max_tokens: int = 3000) -> dict:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _claude(system, user, schema, write=write, max_tokens=max_tokens)
    errors = []
    for name, key_env, url, model in _free_chain():
        for attempt in range(3):
            try:
                return _openai_compatible(name, key_env, url, model, system, user, schema, max_tokens, write)
            except Exception as e:  # лимит/сбой — ждём и пробуем ещё раз, затем следующий провайдер
                errors.append(str(e)[:160])
                log.info("ИИ %s: %s", name, e)
                time.sleep(6 * (attempt + 1))
    raise RuntimeError("все бесплатные ИИ недоступны: " + " | ".join(errors[-3:]))


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
    if not enabled():
        return _rule_eval(v, rx)
    hint = ("\n\nПохоже на инструкции к отклику (проверь и извлеки точно):\n- " + "\n- ".join(rx["lines"])) if rx["lines"] else ""
    try:
        r = _eval_llm(v, profile, rx)
    except Exception as err:
        log.info("оценка ИИ недоступна, использую правила: %s", err)
        return _rule_eval(v, rx)
    return _merge_eval(r, rx)


def _rule_eval(v: dict, rx: dict) -> dict:
    rule = v.get("rule_score") if v.get("rule_score") is not None else 50
    return {"is_job": True, "scam": False, "fit": rule, "conditions": rule, "chance": rule, "red_flags": [],
            "verdict": "Оценка по правилам (ИИ временно недоступен)",
            "instructions": {"must_include": rx["phrases"], "must_start_with": rx["openings"][0] if rx["openings"] else "", "subject": "",
                             "questions": [], "other_rules": [], "manual_only": bool(rx["screening"]), "screening": rx["screening"], "raw": rx["lines"]}}


def _eval_llm(v: dict, profile: dict, rx: dict) -> dict:
    hint = ("\n\nПохоже на инструкции к отклику (проверь и извлеки точно):\n- " + "\n- ".join(rx["lines"])) if rx["lines"] else ""
    return llm_json(
        "Ты опытный IT-рекрутер. Оцени вакансию для кандидата честно и строго (0-100): fit — соответствие требованиям; "
        "conditions — деньги относительно рынка, удалёнка, гибкость, продукт, рост; chance — вероятность приглашения "
        "(у кандидата ~1.5 года своей студии, без вуза, сильное портфолио; Senior/Lead с 5+ годами — низкий шанс). "
        "ГЛАВНАЯ ЦЕЛЬ кандидата — работа вайб-кодером, который создаёт сайты, веб-приложения, мини-приложения и ботов с помощью ИИ: "
        "такие вакансии получают максимальные fit и conditions; чистый ML, Java/Go/C++ бэкенд, QA и продажи — низкие. "
        "scam — платное обучение, предоплата, серые схемы. Отдельно и ДОСЛОВНО извлеки инструкции к отклику: кодовые слова, "
        "фразу для начала письма, тему письма, вопросы, на которые нужно ответить. verdict — 1-2 предложения по-русски.",
        f"КАНДИДАТ:\n{profile['resumeRu']}\n\nПОЖЕЛАНИЯ: {profile['wishes']}\n\nВАКАНСИЯ ({v['source']}): {v['title']}\n"
        f"Компания: {v.get('company') or '?'} · {v.get('location') or ''}{' · удалённо' if v.get('remote') else ''}\n"
        f"Зарплата: {v.get('salary_from') or '?'}–{v.get('salary_to') or '?'} {v.get('currency') or ''}\n\n{(v.get('description') or '')[:7000]}{hint}",
        EVAL_SCHEMA, write=False, max_tokens=2500,
    )


def _merge_eval(r: dict, rx: dict) -> dict:
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
            "Open with \"Hi, I'm Vlad\" (or the employer's required opening, if any), then get to the point. The studio is called BAZA. Never restate the job post back to them.",
            "Put any required code word naturally, e.g. \"Code word: …\".",
            "Pick the 2–3 most important requirements and answer each with a REAL fact from the resume.",
            "If the role is about building websites, apps or bots, lead with that: the studio site with 3D and physics, turnkey site + bot + CRM + AI setups, bots and mini apps with payments, daily work with Cursor/Claude.",
            "Use ONLY facts from the resume. Never invent numbers, employers, years or technologies.",
            "Mention the studio website is the portfolio and worth clicking around (the link is added in the signature).",
            "End with a concrete next step: a 15–20 minute call or a quick project walkthrough.",
            "No em dashes, no clichés (excited, passionate, thrilled, perfect fit, leverage, dynamic team), no emojis.",
            "350–600 characters, chat tone." if chat else "500–1000 characters, 2–4 short paragraphs.",
            "Do not add a signature, name or links at the end: they are appended automatically.",
        ]
    else:
        rules = [
            "Пиши как живой человек: просто, конкретно, уверенно, без канцелярита и пафоса. Обращение на «вы».",
            "Начни так: «Здравствуйте! Меня зовут Влад» (или с обязательной фразы работодателя, если она задана), дальше сразу суть. Студия называется «БАЗА». Не пересказывай работодателю его же вакансию.",
            "Кодовое слово вставляй естественно, отдельной фразой вида «Кодовое слово: …».",
            "Возьми 2–3 главных требования вакансии и на каждое ответь реальным фактом из резюме.",
            "Если вакансия про сайты, приложения или ботов, опирайся на это в первую очередь: сайт студии с 3D и физикой, связки «сайт + бот + CRM + ИИ», боты и мини-приложения с оплатой, работа с Cursor/Claude каждый день.",
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
    if not enabled():
        return template_letter(v, profile, ins)
    letter, subject, problems = "", "", []
    for attempt in range(3):
        user = base if attempt == 0 else f"{base}\n\nПРЕДЫДУЩИЙ ВАРИАНТ:\n{letter}\n\nИСПРАВЬ ТОЛЬКО ЭТО, остальное сохрани:\n- " + "\n- ".join(problems)
        try:
            r = llm_json(system, user, schema, write=True, max_tokens=4000)
        except Exception:
            if letter:
                break
            return template_letter(v, profile, ins)
        letter, subject = humanize(str(r.get("letter") or "")), str(r.get("subject") or "").strip()
        problems = letter_problems(letter, lang, ins, chat)
        if not problems:
            break
    if ins.get("subject"):
        subject = ins["subject"] if _norm(ins["subject"]) not in _norm(subject) else subject
    if not subject:
        subject = f"{v['title']}: application from Vlad" if lang == "en" else f"Отклик на вакансию «{v['title']}»"
    return {"letter": f"{letter}\n\n{signature(profile, lang)}", "subject": subject, "problems": problems}


SKILL_PHRASES_RU = [
    (("сайт", "лендинг", "landing", "web", "веб"), "делаю сайты и веб-приложения целиком, фронт и бэк; сайт студии с 3D и физикой и есть портфолио"),
    (("бот", "bot", "telegram", "телеграм", "mini app", "мини-прил"), "собираю Telegram/VK/MAX-ботов и мини-приложения, в том числе с приёмом оплат"),
    (("crm", "битрикс", "amocrm", "автоматизац", "интеграц"), "автоматизирую бизнес: CRM (Битрикс24, amoCRM), интеграции, аналитика"),
    (("ai", "ии", "llm", "gpt", "нейросет", "cursor", "claude", "вайб", "vibe"), "больше года каждый день работаю с нейросетями: вайб-кодинг, LLM, промпты"),
    (("дизайн", "ui", "ux", "figma", "3d", "анимац"), "рисую интерфейсы, делаю 3D и анимации"),
]
SKILL_PHRASES_EN = [
    (("site", "web", "landing", "frontend", "full"), "I build websites and web apps end to end; my studio site with 3D and physics is my portfolio"),
    (("bot", "telegram", "mini app", "chat"), "I build Telegram and VK bots and mini apps, including payments"),
    (("crm", "automation", "integration", "zapier", "n8n"), "I automate businesses: CRM, integrations, analytics"),
    (("ai", "llm", "gpt", "cursor", "claude", "vibe", "agent"), "I have used AI tools every day for over a year: vibe coding, LLMs, prompt engineering"),
    (("design", "ui", "ux", "figma", "3d"), "I design interfaces, 3D and animations"),
]


def template_letter(v: dict, profile: dict, ins: dict) -> dict:
    """Черновик без ИИ: факты из резюме, подобранные под слова вакансии, + обязательные фразы работодателя."""
    lang = "en" if v.get("lang") == "en" else "ru"
    hay = f"{v['title']} {v.get('description') or ''}".lower()
    table = SKILL_PHRASES_EN if lang == "en" else SKILL_PHRASES_RU
    facts = [p for keys, p in table if any(k in hay for k in keys)][:3] or [table[0][1], table[3][1]]
    start = (ins.get("must_start_with") + ". ") if ins.get("must_start_with") else ""
    words = ", ".join(ins.get("must_include") or [])
    if lang == "en":
        body = (f"{start}Hi! I'm Vlad, founder and full-stack developer at the BAZA studio. I saw the {v['title']} role and it matches what I do every day. "
                + " ".join(f[0].upper() + f[1:] + "." for f in facts)
                + (f" P.S. {words}" if words else "") + " Happy to jump on a 15 minute call and walk you through my projects.")
        subject = f"{v['title']}: application from Vlad"
    else:
        body = (f"{start}Здравствуйте! Я Влад, основатель и fullstack-разработчик студии «БАЗА». Увидел вакансию «{v['title']}», это ровно то, чем я занимаюсь каждый день. "
                + " ".join(f[0].upper() + f[1:] + "." for f in facts)
                + (f" Кодовое слово: {words}." if words else "") + " Буду рад созвониться на 15 минут и показать проекты.")
        subject = f"Отклик на вакансию «{v['title']}»"
    return {"letter": f"{body}\n\n{signature(profile, lang)}", "subject": ins.get("subject") or subject, "problems": ["черновик без ИИ — проверьте перед отправкой"]}


# ─────────────────────────────── Ответ работодателя ───────────────────────────────

def analyze_reply(v: dict | None, history: str, reply: str, profile: dict) -> dict:
    if not enabled():
        low = reply.lower()
        hot = any(k in low for k in ("созвон", "собеседован", "интервью", "встреч", "call", "interview", "приглаша", "invite", "schedule"))
        rej = any(k in low for k in ("к сожалению", "отказ", "unfortunately", "not moving forward", "другого кандидата"))
        return {"kind": "invite" if hot else "reject" if rej else "other", "hot": hot, "summary": reply[:200], "suggested_answer": ""}
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
