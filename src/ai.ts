import Anthropic from "@anthropic-ai/sdk";
import type { Env, Lang, VacancyRow } from "./env";
import type { Profile, Settings } from "./config";
import { clamp, truncate } from "./util";

/**
 * ИИ-слой. Claude (если задан ANTHROPIC_API_KEY) — лучшие, самые «человеческие» письма.
 * Иначе бесплатный Workers AI (Llama 3.3 70B) прямо в Cloudflare.
 */

type Schema = Record<string, unknown>;

async function llmJson<T>(env: Env, opts: { system: string; user: string; schema: Schema; effort?: "low" | "medium" | "high"; maxTokens?: number }): Promise<T> {
  if (env.ANTHROPIC_API_KEY) {
    const client = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY });
    const msg = await client.beta.messages.create({
      model: env.CLAUDE_MODEL || "claude-opus-5-5",
      max_tokens: opts.maxTokens ?? 4000,
      betas: ["server-side-fallback-2026-07-01"],
      fallbacks: "default",
      system: opts.system,
      messages: [{ role: "user", content: opts.user }],
      output_config: { effort: opts.effort ?? "low", format: { type: "json_schema", schema: opts.schema } },
    });
    if (msg.stop_reason === "refusal") throw new Error("Claude отказался обрабатывать запрос");
    const text = msg.content.map((b) => (b.type === "text" ? b.text : "")).join("");
    return JSON.parse(text) as T;
  }
  const res: any = await env.AI.run("@cf/meta/llama-3.3-70b-instruct-fp8-fast" as any, {
    messages: [
      { role: "system", content: opts.system + "\nОтвечай строго одним JSON-объектом по схеме, без пояснений." },
      { role: "user", content: opts.user + "\n\nJSON-схема ответа:\n" + JSON.stringify(opts.schema) },
    ],
    response_format: { type: "json_schema", json_schema: opts.schema },
    max_tokens: opts.maxTokens ?? 1500,
    temperature: 0.6,
  } as any);
  const out = res?.response;
  if (out && typeof out === "object") return out as T;
  const s = String(out ?? "");
  const m = s.match(/\{[\s\S]*\}/);
  if (!m) throw new Error("ИИ вернул не JSON: " + truncate(s, 200));
  return JSON.parse(m[0]) as T;
}

export function aiProviderName(env: Env): string {
  return env.ANTHROPIC_API_KEY ? `Claude (${env.CLAUDE_MODEL || "claude-opus-5-5"})` : "Workers AI · Llama 3.3 70B (бесплатно)";
}

// ─────────────────────────────── Оценка вакансии ───────────────────────────────

export interface Evaluation {
  fit: number;
  conditions: number;
  chance: number;
  verdict: string;
  red_flags: string[];
  is_job: boolean;
  scam: boolean;
}

const EVAL_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["is_job", "scam", "fit", "conditions", "chance", "verdict", "red_flags"],
  properties: {
    is_job: { type: "boolean", description: "это реальная вакансия/заказ, а не реклама, курс или резюме" },
    scam: { type: "boolean", description: "признаки мошенничества: платное обучение, предоплата, серые схемы, «работа в телеграм за 5 минут»" },
    fit: { type: "integer", description: "0-100, насколько кандидат подходит под требования" },
    conditions: { type: "integer", description: "0-100, насколько хороши условия: деньги относительно рынка, удалёнка, гибкость, продукт, рост" },
    chance: { type: "integer", description: "0-100, вероятность получить приглашение с учётом опыта кандидата (~1.5 года, без вуза, сильное портфолио)" },
    verdict: { type: "string", description: "1-2 коротких предложения по-русски: почему стоит/не стоит" },
    red_flags: { type: "array", items: { type: "string" } },
  },
};

export async function evaluateVacancy(env: Env, v: VacancyRow, profile: Profile): Promise<Evaluation> {
  const r = await llmJson<Evaluation>(env, {
    system:
      "Ты опытный IT-рекрутер и карьерный консультант. Оцениваешь вакансию для конкретного кандидата честно и строго. " +
      "Кандидат ищет работу вайб-кодером / AI-разработчиком / fullstack с ИИ. Senior/Lead-требования с 5+ годами опыта резко снижают chance. " +
      "Вакансии, где нужен в основном Java/C++/1С/мобильная нативная разработка/ML-исследования, дают низкий fit.",
    user:
      `КАНДИДАТ:\n${profile.resumeRu}\n\nПОЖЕЛАНИЯ:\n${profile.wishes}\n\nВАКАНСИЯ (${v.source}):\n` +
      `Название: ${v.title}\nКомпания: ${v.company || "?"}\nЛокация: ${v.location || "?"}${v.remote ? " (удалённо)" : ""}\n` +
      `Зарплата: ${v.salary_from || "?"}–${v.salary_to || "?"} ${v.currency || ""} (≈${v.salary_usd ?? "?"} USD/мес)\n\n${truncate(v.description || "", 6000)}`,
    schema: EVAL_SCHEMA,
    effort: "low",
    maxTokens: 1200,
  });
  return {
    is_job: r.is_job !== false,
    scam: !!r.scam,
    fit: clamp(r.fit),
    conditions: clamp(r.conditions),
    chance: clamp(r.chance),
    verdict: String(r.verdict || "").slice(0, 400),
    red_flags: Array.isArray(r.red_flags) ? r.red_flags.slice(0, 6).map(String) : [],
  };
}

/** Итоговый рейтинг: правила dream-job-bot (из движка) + оценка ИИ. */
export function finalScore(rule: number | null, e: Evaluation): number {
  if (!e.is_job || e.scam) return 0;
  const base = 0.35 * e.fit + 0.25 * e.conditions + 0.25 * e.chance + 0.15 * (rule ?? 50);
  return clamp(base - e.red_flags.length * 2);
}

// ─────────────────────────────── Сопроводительное письмо ───────────────────────────────

/** Признаки «нейросетевого» текста. Перенесено из dream-job-bot (candidate/service.py) и расширено для английского. */
const RU_BANNED = [
  "очень нужна работа", "готов на всё", "компания моей мечты", "восхищён вашей компанией", "я в отчаянии", "24/7",
  "не просто ", "нечто большее", "больше чем ", "в современном мире", "динамично развивающ", "команде профессионалов",
  "хотел бы выразить", "с большим интересом ознакомился", "уверен, что мой опыт", "буду рад стать частью", "идеально подхожу",
];
const RU_PARAPHRASE_OPENERS = [
  "вам нужно", "вам предстоит", "вам требуется", "вам важно", "вы ищете", "вижу, что вы", "судя по вакансии", "в вашей вакансии", "ваша задача",
];
const EN_BANNED = [
  "i am excited", "i'm excited", "thrilled", "passionate about", "i am writing to express", "i'm writing to express", "dynamic team",
  "fast-paced", "leverage my", "i believe i would be a great fit", "perfect fit", "dear hiring manager", "delve", "tapestry",
  "embark", "in today's", "furthermore", "moreover", "synergy", "i am confident that", "look no further",
];
const ANTITHESIS_RE = /(?:^|[\s«(])не\s+[^,.;:!?]{2,45},\s*а\s+/iu;

export function letterProblems(letter: string, lang: Lang): string[] {
  const p: string[] = [];
  const low = letter.toLowerCase();
  if (letter.includes("—")) p.push("убери длинное тире «—», замени запятой или точкой");
  if (lang === "ru") {
    for (const b of RU_BANNED) if (low.includes(b)) p.push(`убери шаблонную фразу «${b.trim()}»`);
    if (ANTITHESIS_RE.test(letter)) p.push("убери конструкцию «не X, а Y», это выдаёт ИИ");
    const firstSentence = low.replace(/^здравствуйте[!,.]?\s*/, "").slice(0, 80);
    for (const o of RU_PARAPHRASE_OPENERS) if (firstSentence.startsWith(o)) p.push("не начинай с пересказа вакансии, начни с себя и дела");
  } else {
    for (const b of EN_BANNED) if (low.includes(b)) p.push(`remove the cliché "${b}"`);
  }
  if (letter.length < 300) p.push("письмо слишком короткое, нужно 350–850 знаков");
  if (letter.length > 1100) p.push("письмо слишком длинное, сократи до 850 знаков");
  if ((letter.match(/!/g) || []).length > 2) p.push("слишком много восклицательных знаков");
  if (/\[[^\]]+\]|\{[^}]+\}|<[^>]+>/.test(letter)) p.push("в тексте остались плейсхолдеры в скобках");
  return p;
}

export function humanize(s: string): string {
  return s
    .replace(/\s*—\s*/g, ", ")
    .replace(/,\s*,/g, ",")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/\*\*/g, "")
    .trim();
}

function signature(profile: Profile, lang: Lang): string {
  return lang === "en"
    ? `Vlad\nPortfolio: ${profile.portfolioEn}\nTelegram: ${profile.telegram} · ${profile.email}`
    : `Влад\nПортфолио: ${profile.portfolioRu}\nTelegram: ${profile.telegram} · ${profile.email}`;
}

export async function writeLetter(env: Env, v: VacancyRow, profile: Profile, opts: { channel: string; extra?: string } = { channel: "hh" }): Promise<string> {
  const lang: Lang = v.lang === "en" ? "en" : "ru";
  const isChat = opts.channel === "telegram" || opts.channel === "vk";
  const rulesRu = [
    "Пиши как живой человек в мессенджере или почте: просто, конкретно, уверенно, без канцелярита и без пафоса.",
    "Первая фраза: приветствие и сразу суть, кто ты и почему пишешь именно сюда. Не пересказывай вакансию работодателю.",
    "Возьми 2–3 конкретных требования из вакансии и на каждое ответь реальным фактом из резюме (проект, навык, результат).",
    "Используй ТОЛЬКО факты из резюме. Ничего не выдумывай: ни цифр, ни компаний, ни технологий, которых нет в резюме.",
    "Если чего-то из требований нет в опыте, не ври; можно коротко сказать, что быстро осваиваешь с ИИ-инструментами.",
    "Упомяни, что сайт студии и есть портфолио, его стоит покрутить руками (ссылка будет в подписи).",
    "Заверши конкретным следующим шагом: предложи созвон на 15–20 минут или показать проекты.",
    "Без длинных тире, без конструкции «не X, а Y», без слов «уверен, что», «идеально подхожу», без смайликов.",
    isChat ? "Это сообщение в Telegram: 350–600 знаков, можно на «вы», дружелюбно." : "Длина 450–850 знаков, 2–4 коротких абзаца.",
    "Не добавляй подпись, имя и ссылки в конце: подпись будет добавлена автоматически.",
  ];
  const rulesEn = [
    "Write like a real person, plain and specific, confident but not salesy. Natural American English.",
    "Open with a short greeting and who you are in one line. Do not restate the job post back to them.",
    "Pick 2–3 concrete requirements from the posting and answer each with a real fact from the resume.",
    "Use ONLY facts from the resume. Never invent numbers, employers or technologies.",
    "Mention that the studio website is the portfolio and worth clicking around (the link is added in the signature).",
    "End with a concrete next step: a 15–20 minute call or a quick walkthrough of projects.",
    "No em dashes, no clichés (excited, passionate, thrilled, perfect fit, leverage, dynamic team), no emojis.",
    isChat ? "This is a chat message: 350–600 characters." : "Length 450–850 characters, 2–4 short paragraphs.",
    "Do not add a signature, name or links at the end; they are appended automatically.",
  ];
  const schema = {
    type: "object",
    additionalProperties: false,
    required: ["letter"],
    properties: { letter: { type: "string" } },
  };
  const base =
    `${lang === "en" ? "RESUME" : "РЕЗЮМЕ"}:\n${lang === "en" ? profile.resumeEn : profile.resumeRu}\n\n` +
    `${lang === "en" ? "JOB POSTING" : "ВАКАНСИЯ"}: ${v.title} — ${v.company || ""}\n${truncate(v.description || "", 5000)}\n\n` +
    `${lang === "en" ? "RULES" : "ПРАВИЛА"}:\n- ${(lang === "en" ? rulesEn : rulesRu).join("\n- ")}` +
    (opts.extra ? `\n\n${lang === "en" ? "EXTRA INSTRUCTION" : "ДОПОЛНИТЕЛЬНО"}: ${opts.extra}` : "");
  const system =
    lang === "en"
      ? "You write short, human cover letters for a job seeker. They must not read as AI-generated. Return JSON."
      : "Ты пишешь короткие человеческие сопроводительные письма от лица соискателя. Текст не должен выглядеть сгенерированным. Ответ в JSON.";

  let letter = "";
  let problems: string[] = [];
  for (let attempt = 0; attempt < 3; attempt++) {
    const user =
      attempt === 0
        ? base
        : `${base}\n\n${lang === "en" ? "PREVIOUS DRAFT" : "ПРЕДЫДУЩИЙ ВАРИАНТ"}:\n${letter}\n\n${lang === "en" ? "FIX ONLY THESE ISSUES, keep the rest" : "ИСПРАВЬ ТОЛЬКО ЭТО, остальное сохрани"}:\n- ${problems.join("\n- ")}`;
    const r = await llmJson<{ letter: string }>(env, { system, user, schema, effort: "medium", maxTokens: 3000 });
    letter = humanize(String(r.letter || ""));
    problems = letterProblems(letter, lang);
    if (!problems.length) break;
  }
  return `${letter}\n\n${signature(profile, lang)}`;
}

// ─────────────────────────────── Ответы работодателей ───────────────────────────────

export interface ReplyAnalysis {
  kind: "invite" | "offer" | "question" | "test" | "reject" | "other";
  hot: boolean;
  summary: string;
  suggested_answer: string;
}

export async function analyzeReply(env: Env, v: VacancyRow | null, history: string, reply: string, profile: Profile): Promise<ReplyAnalysis> {
  const lang: Lang = v?.lang === "en" ? "en" : "ru";
  const r = await llmJson<ReplyAnalysis>(env, {
    system:
      "Ты ассистент соискателя. Разбираешь ответ работодателя и готовишь короткий, вежливый, человеческий ответ от лица кандидата. " +
      "Если зовут на созвон, предложи 2–3 варианта времени в ближайшие рабочие дни и спроси удобный формат. Не выдумывай факты.",
    user:
      `Кандидат:\n${lang === "en" ? profile.resumeEn : profile.resumeRu}\n\nВакансия: ${v?.title || "?"} — ${v?.company || "?"}\n\n` +
      `История переписки:\n${truncate(history, 3000)}\n\nНОВЫЙ ОТВЕТ РАБОТОДАТЕЛЯ:\n${truncate(reply, 3000)}\n\n` +
      `Ответ кандидата пиши на языке переписки (${lang === "en" ? "English" : "русский"}), без длинных тире, 200–600 знаков.`,
    schema: {
      type: "object",
      additionalProperties: false,
      required: ["kind", "hot", "summary", "suggested_answer"],
      properties: {
        kind: { type: "string", enum: ["invite", "offer", "question", "test", "reject", "other"] },
        hot: { type: "boolean", description: "работодатель явно заинтересован: зовёт на созвон/собеседование, делает оффер" },
        summary: { type: "string", description: "1 предложение по-русски: что ответили" },
        suggested_answer: { type: "string" },
      },
    },
    effort: "medium",
    maxTokens: 2500,
  });
  return { ...r, suggested_answer: humanize(r.suggested_answer || "") };
}
