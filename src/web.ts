import type { Env, VacancyRow } from "./env";
import { DEFAULT_PROFILE, DEFAULT_SETTINGS, getProfile, getSettings, kvGet, kvSet, logEvent } from "./config";
import { getVacancy, ingest, stats } from "./db";
import { applyToVacancy, hhAccessToken, hhAuthorizeUrl, hhExchangeCode } from "./apply";
import { aiProviderName } from "./ai";
import { safeTick } from "./pipeline";
import { dispatchEngine } from "./github";
import { setupWebhook } from "./telegram";
import { hmac } from "./util";
import { PAGE } from "./ui";

const json = (d: unknown, status = 200) => new Response(JSON.stringify(d), { status, headers: { "content-type": "application/json; charset=utf-8" } });

async function sessionToken(env: Env): Promise<string> {
  return hmac(env.ADMIN_PASSWORD || "change-me", "jobhunter-session-v1");
}
export async function tgSecret(env: Env): Promise<string> {
  return (await hmac(env.TELEGRAM_BOT_TOKEN || "x", "tg-webhook")).slice(0, 32);
}

async function isAdmin(req: Request, env: Env): Promise<boolean> {
  if (!env.ADMIN_PASSWORD) return false;
  const cookie = req.headers.get("cookie") || "";
  const m = cookie.match(/jh=([^;]+)/);
  return !!m && m[1] === (await sessionToken(env));
}

function engineAuthorized(req: Request, env: Env): boolean {
  const auth = req.headers.get("authorization") || "";
  return !!env.ENGINE_TOKEN && auth === `Bearer ${env.ENGINE_TOKEN}`;
}

export async function handleWeb(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  const url = new URL(req.url);
  const origin = env.PUBLIC_URL || url.origin;
  const p = url.pathname;

  if (p === "/" || p === "/index.html") return new Response(PAGE, { headers: { "content-type": "text/html; charset=utf-8" } });

  // ── движок (GitHub Actions) ──
  if (p === "/api/engine/config") {
    if (!engineAuthorized(req, env)) return json({ error: "unauthorized" }, 401);
    const s = await getSettings(env);
    return json({
      queries_ru: s.queriesRu, queries_en: s.queriesEn, must_have_any: s.mustHaveAny, stop_words: s.stopWords, stop_companies: s.stopCompanies,
      telegram_channels: s.telegramChannels, vk_groups: s.vkGroups, vk_token: env.VK_SERVICE_TOKEN || "", hh_area: s.hhArea,
      hh_token: (await hhAccessToken(env)) || "", hh_resume_id: s.hhResumeId, hh_user_agent: env.HH_USER_AGENT, sources: s.sources,
    });
  }
  if (p === "/api/engine/ingest" && req.method === "POST") {
    if (!engineAuthorized(req, env)) return json({ error: "unauthorized" }, 401);
    const body: any = await req.json();
    const r = await ingest(env, body.vacancies || []);
    if (body.stats) {
      await kvSet(env, "engine_last", { at: new Date().toISOString(), stats: body.stats });
      await logEvent(env, `Сбор: ${Object.entries(body.stats).map(([k, v]) => `${k} ${v}`).join(", ")}`);
    }
    return json(r);
  }
  if (p === "/api/engine/kick" && req.method === "POST") {
    if (!engineAuthorized(req, env)) return json({ error: "unauthorized" }, 401);
    ctx.waitUntil(safeTick(env, { evaluate: 8, apply: 3 }));
    return json({ ok: true });
  }

  // ── авторизация панели ──
  if (p === "/api/login" && req.method === "POST") {
    const { password } = (await req.json()) as { password: string };
    if (!env.ADMIN_PASSWORD || password !== env.ADMIN_PASSWORD) return json({ error: "Неверный пароль" }, 401);
    return new Response(JSON.stringify({ ok: true }), {
      headers: { "content-type": "application/json", "set-cookie": `jh=${await sessionToken(env)}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=31536000` },
    });
  }
  if (p === "/auth/hh/callback") {
    if (!(await isAdmin(req, env))) return new Response("Сначала войдите в панель", { status: 401 });
    const code = url.searchParams.get("code");
    if (!code) return new Response("Нет code", { status: 400 });
    try {
      await hhExchangeCode(env, code, origin);
      await logEvent(env, "hh.ru подключён: автоотклики включены");
      return Response.redirect(`${origin}/#connections`, 302);
    } catch (e) {
      return new Response(`Ошибка подключения hh.ru: ${e}`, { status: 500 });
    }
  }

  if (!p.startsWith("/api/")) return new Response("Not found", { status: 404 });
  if (!(await isAdmin(req, env))) return json({ error: "unauthorized" }, 401);

  // ── API панели ──
  if (p === "/api/overview") {
    const [st, s, last, engine, events] = await Promise.all([
      stats(env), getSettings(env), kvGet(env, "last_tick", null), kvGet(env, "engine_last", null),
      env.DB.prepare("SELECT * FROM events ORDER BY id DESC LIMIT 40").all(),
    ]);
    return json({ stats: st, paused: s.paused, autoApply: s.autoApply, lastTick: last, engine, events: events.results });
  }
  if (p === "/api/vacancies") {
    const status = url.searchParams.get("status") || "";
    const q = url.searchParams.get("q") || "";
    const src = url.searchParams.get("source") || "";
    const where: string[] = [];
    const binds: unknown[] = [];
    if (status) { where.push("v.status = ?"); binds.push(status); }
    if (src) { where.push("v.source = ?"); binds.push(src); }
    if (q) { where.push("(v.title LIKE ? OR v.company LIKE ?)"); binds.push(`%${q}%`, `%${q}%`); }
    const rows = await env.DB.prepare(
      `SELECT v.id, v.source, v.url, v.title, v.company, v.location, v.remote, v.salary_from, v.salary_to, v.currency, v.salary_usd, v.lang,
              v.status, v.fit, v.conditions, v.chance, v.score, v.verdict, v.found_at, a.status AS app_status
       FROM vacancies v LEFT JOIN applications a ON a.vacancy_id = v.id ${where.length ? "WHERE " + where.join(" AND ") : ""}
       ORDER BY COALESCE(v.score, -1) DESC, v.found_at DESC LIMIT 200`,
    ).bind(...binds).all();
    return json(rows.results);
  }
  if (p === "/api/applications") {
    const rows = await env.DB.prepare(
      `SELECT a.*, v.title, v.company, v.url, v.source, v.score FROM applications a JOIN vacancies v ON v.id = a.vacancy_id
       ORDER BY a.hot DESC, a.updated_at DESC LIMIT 300`,
    ).all();
    return json(rows.results);
  }
  if (p === "/api/apply" && req.method === "POST") {
    const { id } = (await req.json()) as { id: string };
    const v = await getVacancy(env, id);
    if (!v) return json({ error: "нет вакансии" }, 404);
    return json(await applyToVacancy(env, v, { force: true }));
  }
  if (p === "/api/skip" && req.method === "POST") {
    const { id } = (await req.json()) as { id: string };
    await env.DB.prepare("UPDATE vacancies SET status = 'skipped' WHERE id = ?").bind(id).run();
    return json({ ok: true });
  }
  if (p === "/api/app-status" && req.method === "POST") {
    const { id, status } = (await req.json()) as { id: number; status: string };
    await env.DB.prepare("UPDATE applications SET status = ?, hot = CASE WHEN ? IN ('interview','offer','invited') THEN 1 ELSE hot END, sent_at = COALESCE(sent_at, CASE WHEN ? = 'sent' THEN datetime('now') END), updated_at = datetime('now') WHERE id = ?").bind(status, status, status, id).run();
    return json({ ok: true });
  }
  if (p === "/api/run" && req.method === "POST") {
    const engineMsg = await dispatchEngine(env);
    const tickMsg = await safeTick(env, { evaluate: 6, apply: 2, sync: true });
    await logEvent(env, `Запуск из панели: ${engineMsg}`);
    return json({ engine: engineMsg, tick: tickMsg });
  }
  if (p === "/api/settings") {
    if (req.method === "POST") {
      const body: any = await req.json();
      if (body.settings) await kvSet(env, "settings", { ...DEFAULT_SETTINGS, ...body.settings });
      if (body.profile) await kvSet(env, "profile", { ...DEFAULT_PROFILE, ...body.profile });
      return json({ ok: true });
    }
    return json({ settings: await getSettings(env), profile: await getProfile(env) });
  }
  if (p === "/api/connections") {
    const s = await getSettings(env);
    return json({
      ai: aiProviderName(env),
      hh: { configured: !!env.HH_CLIENT_ID, connected: !!(await hhAccessToken(env)), resumeId: s.hhResumeId, authUrl: env.HH_CLIENT_ID ? hhAuthorizeUrl(env, origin) : null },
      telegram: { configured: !!env.TELEGRAM_BOT_TOKEN, owner: await kvGet(env, "tg_owner", null) },
      email: { configured: !!env.RESEND_API_KEY && !!s.emailFrom },
      github: { configured: !!env.GITHUB_TOKEN && !!env.GITHUB_REPO, repo: env.GITHUB_REPO || null },
      vk: { configured: !!env.VK_SERVICE_TOKEN },
      engine: await kvGet(env, "engine_last", null),
    });
  }
  if (p === "/api/telegram/setup" && req.method === "POST") {
    return json(await setupWebhook(env, origin, await tgSecret(env)));
  }
  if (p === "/api/hh/resumes") {
    const { hhApi } = await import("./apply");
    return json(await hhApi(env, "/resumes/mine"));
  }
  return json({ error: "not found" }, 404);
}
