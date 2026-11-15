import type { ApplicationRow, Env, VacancyRow } from "./env";
import { handleWeb, tgSecret } from "./web";
import { handleUpdate, sendDigest } from "./telegram";
import { safeTick } from "./pipeline";
import { handleIncomingReply } from "./apply";
import { logEvent } from "./config";

export default {
  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(req.url);
    if (url.pathname.startsWith("/tg/") && req.method === "POST") {
      if (url.pathname !== `/tg/${await tgSecret(env)}`) return new Response("forbidden", { status: 403 });
      const update = await req.json();
      // отвечаем Telegram сразу, работа продолжается в фоне
      ctx.waitUntil(handleUpdate(env, update, env.PUBLIC_URL || url.origin, ctx).catch((e) => logEvent(env, `Telegram: ${e}`, "error")));
      return new Response("ok");
    }
    try {
      return await handleWeb(req, env, ctx);
    } catch (e) {
      console.error(e);
      return new Response(JSON.stringify({ error: String(e) }), { status: 500, headers: { "content-type": "application/json" } });
    }
  },

  async scheduled(event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    if (event.cron === "0 6 * * *") {
      ctx.waitUntil(sendDigest(env));
      return;
    }
    ctx.waitUntil(safeTick(env));
  },

  /** Cloudflare Email Routing → ответы работодателей на email-отклики попадают сюда. */
  async email(message: ForwardableEmailMessage, env: Env, ctx: ExecutionContext): Promise<void> {
    const from = (message.from || "").toLowerCase();
    const raw = await new Response(message.raw).text();
    const text = extractPlainText(raw);
    const app = await env.DB.prepare(
      `SELECT a.* FROM applications a JOIN vacancies v ON v.id = a.vacancy_id
       WHERE a.channel = 'email' AND v.contacts LIKE ? ORDER BY a.sent_at DESC LIMIT 1`,
    ).bind(`%${from}%`).first<ApplicationRow>();
    if (!app) {
      await logEvent(env, `Письмо от ${from} не сопоставлено с откликом: ${message.headers.get("subject") || ""}`);
      return;
    }
    const v = await env.DB.prepare("SELECT * FROM vacancies WHERE id = ?").bind(app.vacancy_id).first<VacancyRow>();
    await env.DB.prepare("INSERT INTO messages (application_id, direction, body, external_id) VALUES (?, 'in', ?, ?)")
      .bind(app.id, text.slice(0, 8000), message.headers.get("message-id") || null).run();
    ctx.waitUntil(handleIncomingReply(env, app, v, `Я: ${app.letter}`, text));
  },
};

/** Достаём text/plain из MIME (quoted-printable / base64), отрезаем цитату. */
export function extractPlainText(raw: string): string {
  const boundary = raw.match(/boundary="?([^";\r\n]+)"?/i)?.[1];
  let part = raw;
  if (boundary) {
    const parts = raw.split("--" + boundary);
    part = parts.find((p) => /content-type:\s*text\/plain/i.test(p)) || parts[1] || raw;
  }
  const [head, ...rest] = part.split(/\r?\n\r?\n/);
  let body = rest.join("\n\n");
  if (/content-transfer-encoding:\s*base64/i.test(head)) {
    try {
      body = new TextDecoder().decode(Uint8Array.from(atob(body.replace(/\s+/g, "")), (c) => c.charCodeAt(0)));
    } catch { /* оставляем как есть */ }
  } else if (/quoted-printable/i.test(head)) {
    const bytes = body.replace(/=\r?\n/g, "").replace(/=([0-9A-F]{2})/gi, (_, h) => String.fromCharCode(parseInt(h, 16)));
    body = new TextDecoder().decode(Uint8Array.from(bytes, (c) => c.charCodeAt(0)));
  }
  const cut = body.search(/\n(>|On .+wrote:|.*пишет:|-----Original)/);
  return (cut > 0 ? body.slice(0, cut) : body).replace(/<[^>]+>/g, " ").trim();
}
