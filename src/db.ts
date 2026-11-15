import type { ApplicationRow, Env, VacancyRow } from "./env";
import { detectLang, fingerprint, salaryToUsdMonthly } from "./util";

export interface IngestVacancy {
  source: string;
  external_id: string;
  url: string;
  apply_url?: string;
  title: string;
  company?: string;
  location?: string;
  remote?: boolean;
  salary_from?: number | null;
  salary_to?: number | null;
  currency?: string | null;
  description?: string;
  published_at?: string;
  lang?: string;
  contacts?: { emails?: string[]; telegram?: string[]; links?: string[] };
  rule_score?: number | null;
  ranking?: { pros?: string[]; cons?: string[]; explanation?: string; reply_chance?: string } | null;
}

/** Приём вакансий от движка: дедупликация по id и по отпечатку «название+компания». */
export async function ingest(env: Env, items: IngestVacancy[]): Promise<{ inserted: number; duplicates: number }> {
  let inserted = 0;
  let duplicates = 0;
  for (const it of items) {
    if (!it?.title || !it?.url || !it?.source) continue;
    const id = `${it.source}:${it.external_id || it.url}`.slice(0, 300);
    const fp = await fingerprint(it.title, it.company);
    const dup = await env.DB.prepare("SELECT id FROM vacancies WHERE id = ? OR fingerprint = ? LIMIT 1").bind(id, fp).first();
    if (dup) {
      duplicates++;
      continue;
    }
    const lang = it.lang === "en" || it.lang === "ru" ? it.lang : detectLang(`${it.title}\n${it.description || ""}`);
    const contacts = { emails: it.contacts?.emails || [], telegram: it.contacts?.telegram || [], links: it.contacts?.links || [] };
    if (it.apply_url) contacts.links = [it.apply_url, ...contacts.links];
    await env.DB.prepare(
      `INSERT OR IGNORE INTO vacancies (id, source, external_id, url, title, company, location, remote, salary_from, salary_to, currency,
        salary_usd, lang, description, contacts, published_at, fingerprint, status, score, verdict)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)`,
    )
      .bind(
        id, it.source, String(it.external_id || it.url), it.url, it.title.slice(0, 300), it.company || null, it.location || null,
        it.remote ? 1 : 0, it.salary_from ?? null, it.salary_to ?? null, it.currency || null,
        salaryToUsdMonthly(it.salary_from, it.salary_to, it.currency), lang, (it.description || "").slice(0, 12000),
        JSON.stringify(contacts), it.published_at || null, fp, it.rule_score ?? null,
        it.ranking?.explanation ? `Правила: ${it.ranking.explanation}`.slice(0, 600) : null,
      )
      .run();
    inserted++;
  }
  return { inserted, duplicates };
}

export async function getVacancy(env: Env, id: string): Promise<VacancyRow | null> {
  return env.DB.prepare("SELECT * FROM vacancies WHERE id = ?").bind(id).first<VacancyRow>();
}

export async function getApplication(env: Env, id: number): Promise<ApplicationRow | null> {
  return env.DB.prepare("SELECT * FROM applications WHERE id = ?").bind(id).first<ApplicationRow>();
}

export async function getApplicationByVacancy(env: Env, vacancyId: string): Promise<ApplicationRow | null> {
  return env.DB.prepare("SELECT * FROM applications WHERE vacancy_id = ?").bind(vacancyId).first<ApplicationRow>();
}

export async function stats(env: Env) {
  const one = async (sql: string, ...b: unknown[]) => ((await env.DB.prepare(sql).bind(...b).first<{ n: number }>())?.n ?? 0);
  const bySource = await env.DB.prepare("SELECT source, COUNT(*) AS n FROM vacancies GROUP BY source ORDER BY n DESC").all<{ source: string; n: number }>();
  const byStatus = await env.DB.prepare("SELECT status, COUNT(*) AS n FROM applications GROUP BY status").all<{ status: string; n: number }>();
  return {
    vacancies: await one("SELECT COUNT(*) AS n FROM vacancies"),
    newToday: await one("SELECT COUNT(*) AS n FROM vacancies WHERE found_at >= datetime('now','-1 day')"),
    scored: await one("SELECT COUNT(*) AS n FROM vacancies WHERE fit IS NOT NULL"),
    queue: await one("SELECT COUNT(*) AS n FROM vacancies WHERE status = 'new'"),
    applied: await one("SELECT COUNT(*) AS n FROM applications WHERE status NOT IN ('draft','waiting_user')"),
    appliedToday: await sentToday(env),
    waitingUser: await one("SELECT COUNT(*) AS n FROM applications WHERE status = 'waiting_user'"),
    replies: await one("SELECT COUNT(*) AS n FROM applications WHERE last_reply IS NOT NULL"),
    hot: await one("SELECT COUNT(*) AS n FROM applications WHERE hot = 1"),
    invited: await one("SELECT COUNT(*) AS n FROM applications WHERE status IN ('invited','interview','offer')"),
    bySource: bySource.results,
    byStatus: byStatus.results,
  };
}

export async function sentToday(env: Env): Promise<number> {
  const r = await env.DB.prepare("SELECT COUNT(*) AS n FROM applications WHERE sent_at >= datetime('now','-1 day')").first<{ n: number }>();
  return r?.n ?? 0;
}
