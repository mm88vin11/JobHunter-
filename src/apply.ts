import type { ApplicationRow, Contacts, Env, VacancyRow } from "./env";
import { getSettings, getProfile, kvGet, kvSet, logEvent, type Settings } from "./config";
import { analyzeReply, writeLetter } from "./ai";
import { getApplicationByVacancy } from "./db";
import { notifyReply } from "./telegram";
import { http, stripHtml } from "./util";

// ─────────────────────────────── hh.ru: OAuth + API ───────────────────────────────

interface HHTokens {
  access_token: string;
  refresh_token: string;
  expires_at: number;
}

function hhHeaders(env: Env): Record<string, string> {
  const ua = env.HH_USER_AGENT || "JobHunter/1.0 (vlad_2048@mail.ru)";
  return { "User-Agent": ua, "HH-User-Agent": ua, Accept: "application/json" };
}

export function hhAuthorizeUrl(env: Env, origin: string): string {
  const p = new URLSearchParams({ response_type: "code", client_id: env.HH_CLIENT_ID || "", redirect_uri: `${origin}/auth/hh/callback` });
  return `https://hh.ru/oauth/authorize?${p}`;
}

async function hhTokenRequest(env: Env, body: Record<string, string>): Promise<HHTokens> {
  const res = await http("https://api.hh.ru/token", {
    method: "POST",
    headers: { ...hhHeaders(env), "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(body),
  });
  const j: any = await res.json();
  if (!res.ok) throw new Error(`hh token: ${JSON.stringify(j)}`);
  const t = { access_token: j.access_token, refresh_token: j.refresh_token, expires_at: Date.now() + (j.expires_in || 1209600) * 1000 };
  await kvSet(env, "hh_tokens", t);
  return t;
}

export async function hhExchangeCode(env: Env, code: string, origin: string): Promise<void> {
  await hhTokenRequest(env, {
    grant_type: "authorization_code", client_id: env.HH_CLIENT_ID || "", client_secret: env.HH_CLIENT_SECRET || "",
    code, redirect_uri: `${origin}/auth/hh/callback`,
  });
  // сразу выбираем первое опубликованное резюме
  const resumes: any = await hhApi(env, "/resumes/mine");
  const s = await getSettings(env);
  if (!s.hhResumeId && resumes?.items?.length) {
    await kvSet(env, "settings", { ...s, hhResumeId: resumes.items[0].id });
  }
}

export async function hhAccessToken(env: Env): Promise<string | null> {
  const t = await kvGet<HHTokens | null>(env, "hh_tokens", null);
  if (!t) return null;
  if (Date.now() < t.expires_at - 60_000) return t.access_token;
  try {
    return (await hhTokenRequest(env, { grant_type: "refresh_token", refresh_token: t.refresh_token })).access_token;
  } catch (e) {
    await logEvent(env, `hh.ru: не удалось обновить токен — переподключите hh.ru в панели (${e})`, "error");
    return null;
  }
}

export async function hhApi(env: Env, path: string, init: RequestInit = {}): Promise<any> {
  const token = await hhAccessToken(env);
  if (!token) throw new Error("hh.ru не подключён");
  const headers = { ...hhHeaders(env), Authorization: `Bearer ${token}`, ...(init.headers as Record<string, string> | undefined) };
  const res = await http(`https://api.hh.ru${path}`, { ...init, headers });
  const text = await res.text();
  const body = text ? (() => { try { return JSON.parse(text); } catch { return text; } })() : {};
  if (!res.ok) {
    const err: any = new Error(`hh ${res.status}: ${typeof body === "string" ? body.slice(0, 200) : JSON.stringify(body).slice(0, 300)}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

async function hhApply(env: Env, v: VacancyRow, letter: string, s: Settings): Promise<string> {
  if (!s.hhResumeId) throw new Error("не выбрано резюме hh.ru");
  const detail = await hhApi(env, `/vacancies/${v.external_id}`);
  if (detail?.has_test) throw Object.assign(new Error("на вакансии тест — нужен ручной отклик"), { manual: true });
  if (detail?.type?.id === "closed" || detail?.archived) throw Object.assign(new Error("вакансия в архиве"), { skip: true });
  const form = new URLSearchParams({ vacancy_id: v.external_id, resume_id: s.hhResumeId, message: letter.slice(0, 9000) });
  try {
    await hhApi(env, "/negotiations", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: form });
  } catch (e: any) {
    const val = JSON.stringify(e.body || "");
    if (/already_applied/.test(val)) return "already";
    if (/test_required|letter_required|negotiations_forbidden|resume_not_published/.test(val)) throw Object.assign(e, { manual: true });
    if (/limit_exceeded/.test(val)) throw Object.assign(new Error("hh.ru: дневной лимит откликов исчерпан"), { limit: true });
    throw e;
  }
  return "sent";
}

/** Синхронизация откликов hh.ru: приглашения, отказы, новые сообщения → ИИ-разбор → Telegram. */
export async function hhSyncNegotiations(env: Env): Promise<number> {
  if (!(await hhAccessToken(env))) return 0;
  const profile = await getProfile(env);
  const data = await hhApi(env, "/negotiations?per_page=50&order=desc");
  let changed = 0;
  for (const item of data?.items || []) {
    const vid = `hh:${item?.vacancy?.id}`;
    const app = await getApplicationByVacancy(env, vid);
    if (!app) continue;
    const state = item?.state?.id as string;
    const mapped = state === "invitation" ? "invited" : state === "discard" ? "rejected" : state === "hired" ? "offer" : item?.viewed_by_opponent ? "viewed" : "sent";
    const rank = ["draft", "waiting_user", "sent", "viewed", "rejected", "invited", "interview", "offer"];
    if (rank.indexOf(mapped) > rank.indexOf(app.status) || (mapped === "rejected" && app.status !== "rejected")) {
      await env.DB.prepare("UPDATE applications SET status = ?, external_ref = ?, updated_at = datetime('now'), hot = CASE WHEN ? IN ('invited','offer') THEN 1 ELSE hot END WHERE id = ?")
        .bind(mapped, String(item.id), mapped, app.id).run();
      changed++;
      if (mapped === "invited" || mapped === "offer") await logEvent(env, `🔥 Приглашение на hh.ru: ${item?.vacancy?.name} (${item?.vacancy?.employer?.name})`, "hot");
    }
    if (item?.has_updates || mapped === "invited") {
      try {
        const msgs = await hhApi(env, `/negotiations/${item.id}/messages`);
        const list: any[] = msgs?.items || [];
        let newest: any = null;
        for (const m of list) {
          if (m?.author?.participant_type !== "employer") continue;
          const r = await env.DB.prepare("INSERT OR IGNORE INTO messages (application_id, direction, body, external_id) VALUES (?, 'in', ?, ?)")
            .bind(app.id, stripHtml(m.text || ""), String(m.id)).run();
          if (r.meta.changes) newest = m;
        }
        if (newest) {
          const v = await env.DB.prepare("SELECT * FROM vacancies WHERE id = ?").bind(vid).first<VacancyRow>();
          const history = list.map((m) => `${m?.author?.participant_type === "employer" ? "Работодатель" : "Я"}: ${stripHtml(m.text || "")}`).join("\n");
          await handleIncomingReply(env, { ...app, external_ref: String(item.id) }, v, history, stripHtml(newest.text || ""), profile);
          changed++;
        }
      } catch (e) {
        // API сообщений hh постепенно переводят на чаты — статусы всё равно синхронизируются
        console.log("hh messages:", e);
      }
    }
  }
  return changed;
}

export async function hhSendMessage(env: Env, negotiationId: string, text: string): Promise<void> {
  await hhApi(env, `/negotiations/${negotiationId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ message: text }),
  });
}

// ─────────────────────────────── Email (Resend) ───────────────────────────────

async function sendEmail(env: Env, s: Settings, to: string, subject: string, text: string): Promise<string> {
  if (!env.RESEND_API_KEY || !s.emailFrom) throw Object.assign(new Error("email не настроен"), { manual: true });
  const res = await http("https://api.resend.com/emails", {
    method: "POST",
    headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ from: s.emailFrom, to: [to], subject, text, reply_to: s.emailReplyTo || undefined }),
  });
  const j: any = await res.json();
  if (!res.ok) throw new Error(`resend: ${JSON.stringify(j)}`);
  return j.id;
}

// ─────────────────────────────── Выбор канала и отклик ───────────────────────────────

export function contactsOf(v: VacancyRow): Contacts {
  try {
    return { emails: [], telegram: [], links: [], ...JSON.parse(v.contacts || "{}") };
  } catch {
    return { emails: [], telegram: [], links: [] };
  }
}

export async function pickChannel(env: Env, v: VacancyRow, s: Settings): Promise<"hh" | "email" | "telegram" | "link"> {
  const c = contactsOf(v);
  if (v.source === "hh" && s.hhResumeId && (await hhAccessToken(env))) return "hh";
  if (c.emails.length && env.RESEND_API_KEY && s.emailFrom) return "email";
  if (c.telegram.length) return "telegram";
  return "link";
}

/** Ссылка, открывающая чат с готовым текстом: одно касание — и отклик отправлен. */
export function telegramDeepLink(username: string, text: string): string {
  return `https://t.me/${username}?text=${encodeURIComponent(text.slice(0, 3500))}`;
}

export interface ApplyResult {
  status: "sent" | "waiting_user" | "skipped" | "error" | "limit";
  channel: string;
  letter?: string;
  note?: string;
}

export async function applyToVacancy(env: Env, v: VacancyRow, opts: { force?: boolean; extra?: string } = {}): Promise<ApplyResult> {
  const s = await getSettings(env);
  const profile = await getProfile(env);
  const existing = await getApplicationByVacancy(env, v.id);
  if (existing && existing.status !== "draft" && existing.status !== "waiting_user" && !opts.force) {
    return { status: "skipped", channel: existing.channel, note: "уже откликались" };
  }
  const channel = await pickChannel(env, v, s);
  const letter = await writeLetter(env, v, profile, { channel, extra: opts.extra });
  const c = contactsOf(v);
  let status: ApplyResult["status"] = "waiting_user";
  let ref: string | null = null;
  let note = "";
  try {
    if (channel === "hh") {
      const r = await hhApply(env, v, letter, s);
      status = "sent";
      note = r === "already" ? "отклик уже был на hh.ru" : "отправлено через hh.ru";
    } else if (channel === "email") {
      const subj = v.lang === "en" ? `${v.title}: application from Vlad (full-stack / vibe coder)` : `Отклик на вакансию «${v.title}»`;
      ref = await sendEmail(env, s, c.emails[0], subj, letter);
      status = "sent";
      note = `письмо отправлено на ${c.emails[0]}`;
    } else if (channel === "telegram") {
      note = `готово сообщение для @${c.telegram[0]} — отправка в одно касание`;
    } else {
      note = "отклик через сайт работодателя — письмо готово";
    }
  } catch (e: any) {
    if (e.limit) return { status: "limit", channel, note: e.message };
    if (e.skip) {
      await env.DB.prepare("UPDATE vacancies SET status = 'skipped', error = ? WHERE id = ?").bind(String(e.message), v.id).run();
      return { status: "skipped", channel, note: e.message };
    }
    status = "waiting_user";
    note = `нужен ручной отклик: ${e.message}`;
  }
  await env.DB.prepare(
    `INSERT INTO applications (vacancy_id, channel, letter, lang, status, external_ref, sent_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, CASE WHEN ? = 'sent' THEN datetime('now') END, datetime('now'))
     ON CONFLICT(vacancy_id) DO UPDATE SET channel = excluded.channel, letter = excluded.letter, status = excluded.status,
       external_ref = COALESCE(excluded.external_ref, external_ref), sent_at = COALESCE(excluded.sent_at, sent_at), updated_at = datetime('now')`,
  ).bind(v.id, channel, letter, v.lang, status, ref, status).run();
  await env.DB.prepare("UPDATE vacancies SET status = ?, apply_channel = ? WHERE id = ?")
    .bind(status === "sent" ? "applied" : "manual", channel, v.id).run();
  if (status === "sent") {
    await env.DB.prepare("INSERT INTO messages (application_id, direction, body) SELECT id, 'out', ? FROM applications WHERE vacancy_id = ?").bind(letter, v.id).run();
  }
  await logEvent(env, `${status === "sent" ? "✅ Отклик" : "✍️ Черновик"}: ${v.title} (${v.company || v.source}) — ${note}`);
  return { status, channel, letter, note };
}

/** Общий обработчик входящего ответа (hh, email): статус, ИИ-разбор, черновик ответа, уведомление. */
export async function handleIncomingReply(env: Env, app: ApplicationRow, v: VacancyRow | null, history: string, reply: string, profile = null as any) {
  profile = profile || (await getProfile(env));
  const a = await analyzeReply(env, v, history, reply, profile);
  const status = a.kind === "reject" ? "rejected" : a.kind === "offer" ? "offer" : a.kind === "invite" ? "interview" : app.status === "sent" ? "viewed" : app.status;
  await env.DB.prepare(
    "UPDATE applications SET last_reply = ?, reply_kind = ?, suggested_answer = ?, status = ?, hot = CASE WHEN ? THEN 1 ELSE hot END, updated_at = datetime('now') WHERE id = ?",
  ).bind(reply.slice(0, 4000), a.kind, a.suggested_answer, status, a.hot ? 1 : 0, app.id).run();
  await logEvent(env, `${a.hot ? "🔥" : "💬"} Ответ по «${v?.title || app.vacancy_id}»: ${a.summary}`, a.hot ? "hot" : "info");
  await notifyReply(env, { ...app, status, last_reply: reply, reply_kind: a.kind, suggested_answer: a.suggested_answer, hot: a.hot ? 1 : app.hot }, v, a);
}
