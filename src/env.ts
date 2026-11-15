export interface Env {
  DB: D1Database;
  AI: Ai;
  PUBLIC_URL?: string;
  HH_USER_AGENT?: string;
  CLAUDE_MODEL?: string;
  ADMIN_PASSWORD?: string;
  TELEGRAM_BOT_TOKEN?: string;
  ANTHROPIC_API_KEY?: string;
  HH_CLIENT_ID?: string;
  HH_CLIENT_SECRET?: string;
  RESEND_API_KEY?: string;
  VK_SERVICE_TOKEN?: string;
  ENGINE_TOKEN?: string;
  GITHUB_TOKEN?: string;
  GITHUB_REPO?: string;
  GITHUB_REF?: string;
}

export type Lang = "ru" | "en";

export interface Contacts {
  emails: string[];
  telegram: string[];
  links: string[];
}

/** Нормализованная вакансия из любого источника. */
export interface RawVacancy {
  source: string;
  externalId: string;
  url: string;
  title: string;
  company?: string;
  location?: string;
  remote?: boolean;
  salaryFrom?: number | null;
  salaryTo?: number | null;
  currency?: string | null;
  description?: string;
  publishedAt?: string;
  contacts?: Contacts;
  lang?: Lang;
}

export interface VacancyRow {
  id: string;
  source: string;
  external_id: string;
  url: string;
  title: string;
  company: string | null;
  location: string | null;
  remote: number;
  salary_from: number | null;
  salary_to: number | null;
  currency: string | null;
  salary_usd: number | null;
  lang: Lang;
  description: string | null;
  contacts: string | null;
  published_at: string | null;
  found_at: string;
  fingerprint: string | null;
  status: string;
  fit: number | null;
  conditions: number | null;
  chance: number | null;
  score: number | null;
  verdict: string | null;
  red_flags: string | null;
  apply_channel: string | null;
  error: string | null;
}

export interface ApplicationRow {
  id: number;
  vacancy_id: string;
  channel: string;
  letter: string;
  lang: Lang;
  status: string;
  external_ref: string | null;
  sent_at: string | null;
  updated_at: string;
  last_reply: string | null;
  reply_kind: string | null;
  suggested_answer: string | null;
  hot: number;
}
