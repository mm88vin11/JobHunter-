import type { Env } from "./env";

export interface Profile {
  name: string;
  telegram: string;
  email: string;
  portfolioRu: string;
  portfolioEn: string;
  resumeRu: string;
  resumeEn: string;
  /** Чего хочет кандидат — используется при оценке условий. */
  wishes: string;
}

export interface Settings {
  paused: boolean;
  autoApply: boolean;
  /** Минимальный итоговый рейтинг для автоотклика. */
  minScoreAuto: number;
  /** Минимальный рейтинг, чтобы вакансия попала в дайджест. */
  minScoreNotify: number;
  dailyLimit: number;
  /** Минимальная зарплата в USD/мес (0 — не фильтровать; вакансии без зарплаты не отсекаются). */
  minSalaryUsd: number;
  remoteOnly: boolean;
  queriesRu: string[];
  queriesEn: string[];
  /** Хотя бы одно из этих слов должно встретиться — иначе вакансия не тратит ИИ-квоту. */
  mustHaveAny: string[];
  /** Вакансии с этими словами в заголовке отбрасываются сразу. */
  stopWords: string[];
  stopCompanies: string[];
  sources: Record<string, boolean>;
  telegramChannels: string[];
  vkGroups: string[];
  hhResumeId: string;
  hhArea: string;
  /** Канал автоотклика: включён ли email-отклик (нужен RESEND_API_KEY). */
  emailFrom: string;
  emailReplyTo: string;
}

export const DEFAULT_PROFILE: Profile = {
  name: "Влад",
  telegram: "@lllvlad",
  email: "vlad_2048@mail.ru",
  portfolioRu: "https://bazadigital.ru",
  portfolioEn: "https://bazadigital.ru/en/",
  resumeRu: `Fullstack-разработчик, вайб-кодер. В 18 лет вместо вуза открыл студию разработки «БАЗА» (bazadigital.ru), с мая 2025 — основатель и fullstack-разработчик.
Делаю продукты от идеи до запуска один, там где обычно собирают команду: сайты и веб-приложения (фронт и бэк), боты для Telegram, MAX и VK, мини-приложения, приём оплат, автоматизация и CRM (Битрикс24, amoCRM), интеграции, аналитика, внедрение ИИ. Больше года каждый день работаю с нейросетями (вайб-кодинг, LLM, prompt engineering). Дизайн интерфейсов, 3D, анимации.
Проекты и факты:
- сайт студии сделал сам: 3D, физика, интерактив, быстрая загрузка даже на слабых телефонах — это и есть портфолио;
- собираю бизнесу под ключ связку «сайт + бот + CRM + ИИ», чтобы ни одна заявка не терялась;
- написал больше 120 статей для блога студии;
- сам веду клиентов от первого созвона до запуска;
- есть пройденные курсы и сертификаты;
- обо мне вышла статья на vc.ru в рубрике «Кейсы роста»;
- предприниматель со школы, был президентом школы (выбрали голосованием учеников) — опыт руководства;
- призёр олимпиад и конкурсов, в том числе по английскому; мои статьи опубликованы в вузах России;
- не пью, не курю, без кофе и сахара, спорт — ясная голова каждый день.
Навыки: JavaScript, HTML5, CSS3, WebGL, REST API, Telegram Bot API, Telegram Mini Apps, Битрикс24, amoCRM, интеграции с CRM, нейросети, LLM, вайб-кодинг, prompt engineering, автоматизация бизнес-процессов, UX/UI, SEO, Яндекс.Метрика, управление проектами, переговоры, английский язык.`,
  resumeEn: `Full-stack developer and vibe coder. At 18 I chose to start my own dev studio, BAZA (bazadigital.ru/en), instead of university; founder and full-stack developer since May 2025.
I ship products solo from idea to launch, where teams are usually needed: websites and web apps (front and back end), Telegram / VK / MAX bots and mini apps with payments, business automation and CRM (Bitrix24, amoCRM), integrations, analytics, AI integration. I've used AI tools daily for over a year (vibe coding, LLMs, prompt engineering). UI design, 3D, animation.
Facts:
- built the studio website myself: 3D, physics, interactivity, loads fast even on weak phones — it is my portfolio;
- build turnkey "website + bot + CRM + AI" setups for businesses so no lead gets lost;
- wrote 120+ articles for the studio blog;
- handle clients myself from the first call to launch;
- completed courses with certificates;
- featured on vc.ru ("Growth cases" section);
- entrepreneur since school; elected school president by student vote;
- olympiad and competition prize-winner, including English; my articles were published by Russian universities.
Skills: JavaScript, HTML5, CSS3, WebGL, REST APIs, Telegram Bot API, Telegram Mini Apps, Bitrix24, amoCRM, CRM integrations, LLMs, vibe coding (Cursor / Claude Code style workflows), prompt engineering, business process automation, UX/UI, SEO, analytics, project management, negotiation, English.`,
  wishes: "Ищу работу вайб-кодером / AI-разработчиком / fullstack с упором на ИИ. Предпочтительно удалёнка, гибкий график, сильная команда или интересный продукт, рост дохода. Готов к фрилансу и проектной работе, но приоритет — постоянная работа с хорошими условиями, в том числе в зарубежных компаниях.",
};

export const DEFAULT_SETTINGS: Settings = {
  paused: false,
  autoApply: true,
  minScoreAuto: 62,
  minScoreNotify: 55,
  dailyLimit: 40,
  minSalaryUsd: 0,
  remoteOnly: false,
  queriesRu: [
    "вайб-кодер", "вайбкодер", "вайб кодинг", "vibe coding", "AI-разработчик", "ИИ-разработчик", "разработчик нейросети",
    "prompt engineer", "промпт-инженер", "LLM разработчик", "AI engineer", "разработчик ботов telegram", "AI автоматизация",
    "no-code разработчик", "fullstack javascript", "AI product builder", "cursor разработчик",
  ],
  queriesEn: [
    "vibe coder", "vibe coding", "AI engineer", "AI developer", "LLM engineer", "prompt engineer", "AI automation engineer",
    "full stack AI", "AI product engineer", "Claude Code", "Cursor AI developer", "telegram bot developer", "AI builder",
  ],
  mustHaveAny: [
    "vibe", "вайб", " ai", "ai ", "ai-", "-ai", "ии-", " ии ", "llm", "gpt", "нейросет", "prompt", "промпт", "claude", "cursor", "openai",
    "агент", "agent", "javascript", "typescript", "react", "node", "fullstack", "full-stack", "full stack", "frontend", "фронтенд",
    "telegram", "телеграм", "бот", "bot", "mini app", "битрикс", "bitrix", "amocrm", "автоматизац", "automation", "no-code", "nocode",
    "webgl", "three.js", "веб-разработ", "web developer", "разработчик сайт", "n8n", "make.com", "zapier",
  ],
  stopWords: [
    "senior java", "1с", "1c:", "c++", "c#", "golang developer", "android developer", "ios developer", "devops", "sre",
    "data scientist", "ml researcher", "qa manual", "тестировщик", "менеджер по продажам", "оператор", "курьер", "водитель",
    "грузчик", "call-центр", "колл-центр", "администратор", "бухгалтер", "lead ", "head of", "principal", "staff engineer",
  ],
  stopCompanies: [],
  sources: {
    // Россия и СНГ
    hh: true, habr: true, getmatch: true, trudvsem: true, fl: true, telegram: true, vk: true,
    // мир через JobSpy (speedyapply/JobSpy)
    linkedin: true, indeed: true, glassdoor: true, zip_recruiter: true, google: true, bayt: false, naukri: false, bdjobs: false, hellowork: false,
    // мировые удалённые доски
    remoteok: true, remotive: true, himalayas: true, wwr: true, workingnomads: true, jobicy: true, arbeitnow: true, hn: true,
  },
  telegramChannels: [
    "forallmedia", "remote_it_jobs", "vacancyit", "jobforjunior", "devjobs", "habr_career", "getitrussia", "remotejobss",
    "workoo", "nocode_jobs", "it_vakansii_jobs", "jobs_it_remote", "zarubezhom_jobs", "freelancetaverna", "webfrl",
    "careerspace", "normrabota", "python_jobs_ru",
  ],
  vkGroups: [],
  hhResumeId: "",
  hhArea: "113",
  emailFrom: "",
  emailReplyTo: "",
};

export async function kvGet<T>(env: Env, key: string, fallback: T): Promise<T> {
  const row = await env.DB.prepare("SELECT v FROM kv WHERE k = ?").bind(key).first<{ v: string }>();
  if (!row) return fallback;
  try {
    return JSON.parse(row.v) as T;
  } catch {
    return fallback;
  }
}

export async function kvSet(env: Env, key: string, value: unknown): Promise<void> {
  await env.DB.prepare("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v")
    .bind(key, JSON.stringify(value))
    .run();
}

export async function getSettings(env: Env): Promise<Settings> {
  const s = await kvGet<Partial<Settings>>(env, "settings", {});
  return { ...DEFAULT_SETTINGS, ...s, sources: { ...DEFAULT_SETTINGS.sources, ...(s.sources || {}) } };
}

export async function getProfile(env: Env): Promise<Profile> {
  const p = await kvGet<Partial<Profile>>(env, "profile", {});
  return { ...DEFAULT_PROFILE, ...p };
}

export async function logEvent(env: Env, text: string, level: "info" | "warn" | "error" | "hot" = "info"): Promise<void> {
  console.log(`[${level}] ${text}`);
  await env.DB.prepare("INSERT INTO events (level, text) VALUES (?, ?)").bind(level, text.slice(0, 2000)).run();
}
