import type { ApplicationRow, Env, VacancyRow } from "./env";
import type { ApplyResult } from "./apply";
import type { ReplyAnalysis } from "./ai";
import { getSettings, kvGet, kvSet, logEvent } from "./config";
import { escapeHtml as h, formatSalary, truncate } from "./util";

const SRC: Record<string, string> = {
  hh: "hh.ru", habr: "Хабр Карьера", getmatch: "getmatch", trudvsem: "Работа России", fl: "FL.ru", telegram: "Telegram", vk: "VK",
  linkedin: "LinkedIn", indeed: "Indeed", glassdoor: "Glassdoor", zip_recruiter: "ZipRecruiter", google: "Google Jobs", bayt: "Bayt",
  naukri: "Naukri", bdjobs: "BDJobs", hellowork: "HelloWork", remoteok: "RemoteOK", remotive: "Remotive", himalayas: "Himalayas",
  wwr: "We Work Remotely", workingnomads: "Working Nomads", jobicy: "Jobicy", arbeitnow: "Arbeitnow", hn: "Hacker News",
};
export const sourceName = (s: string) => SRC[s] || s;

export async function tg(env: Env, method: string, payload: Record<string, unknown>): Promise<any> {
  if (!env.TELEGRAM_BOT_TOKEN) return null;
  const res = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const j: any = await res.json().catch(() => ({}));
  if (!j.ok) console.log("telegram", method, JSON.stringify(j).slice(0, 300));
  return j.result;
}

async function ownerChat(env: Env): Promise<number | null> {
  return kvGet<number | null>(env, "tg_owner", null);
}

export async function say(env: Env, text: string, extra: Record<string, unknown> = {}): Promise<void> {
  const chat = await ownerChat(env);
  if (!chat) return;
  await tg(env, "sendMessage", { chat_id: chat, text: truncate(text, 4000), parse_mode: "HTML", disable_web_page_preview: true, ...extra });
}

const bar = (n: number | null) => (n == null ? "—" : `${n}`);
const short = (id: string) => id.slice(0, 50); // callback_data ≤ 64 байт

function card(v: VacancyRow): string {
  const sal = formatSalary(v.salary_from, v.salary_to, v.currency);
  return (
    `<b>${h(v.title)}</b>\n${h(v.company || "")} · ${sourceName(v.source)}${v.remote ? " · 🌍 удалённо" : v.location ? ` · ${h(v.location)}` : ""}\n` +
    (sal ? `💰 ${h(sal)}${v.salary_usd ? ` (≈$${v.salary_usd}/мес)` : ""}\n` : "") +
    `⭐ <b>${bar(v.score)}</b>/100 · соответствие ${bar(v.fit)} · условия ${bar(v.conditions)} · шанс ${bar(v.chance)}\n` +
    (v.verdict ? `🧠 ${h(truncate(v.verdict, 300))}\n` : "") +
    `🔗 ${h(v.url)}`
  );
}

export async function notifyTopMatch(env: Env, v: VacancyRow): Promise<void> {
  const s = await getSettings(env);
  const auto = s.autoApply && (v.score ?? 0) >= s.minScoreAuto;
  await say(env, `🎯 <b>Сильное совпадение</b>${auto ? " — откликаюсь автоматически" : ""}\n\n${card(v)}`, {
    reply_markup: { inline_keyboard: [[{ text: "✅ Откликнуться", callback_data: `ap|${short(v.id)}` }, { text: "🚫 Скрыть", callback_data: `sk|${short(v.id)}` }]] },
  });
}

export async function notifyApplied(env: Env, v: VacancyRow, r: ApplyResult): Promise<void> {
  const { contactsOf, telegramDeepLink } = await import("./apply");
  if (r.status === "sent") {
    await say(env, `✅ <b>Отклик отправлен</b> (${r.channel === "hh" ? "hh.ru" : "email"})\n\n${card(v)}\n\n<i>${h(truncate(r.letter || "", 900))}</i>`);
    return;
  }
  if (r.status !== "waiting_user") return;
  const c = contactsOf(v);
  const buttons: any[][] = [];
  if (r.channel === "telegram" && c.telegram[0]) {
    buttons.push([{ text: `✈️ Отправить @${c.telegram[0]} (1 касание)`, url: telegramDeepLink(c.telegram[0], r.letter || "") }]);
  } else {
    buttons.push([{ text: "🌐 Открыть вакансию", url: c.links[0] || v.url }]);
  }
  buttons.push([{ text: "✔️ Я отправил", callback_data: `mk|${short(v.id)}|sent` }, { text: "🔁 Переписать", callback_data: `rw|${short(v.id)}` }]);
  await say(env, `✍️ <b>Отклик готов — нужно одно касание</b>\n${h(r.note || "")}\n\n${card(v)}\n\n<code>${h(truncate(r.letter || "", 2500))}</code>`, {
    reply_markup: { inline_keyboard: buttons },
  });
}

export async function notifyReply(env: Env, app: ApplicationRow, v: VacancyRow | null, a: ReplyAnalysis): Promise<void> {
  const head = a.hot ? "🔥🔥 <b>С вами хотят поговорить!</b>" : a.kind === "reject" ? "❌ <b>Отказ</b>" : "💬 <b>Новый ответ работодателя</b>";
  const buttons: any[][] = [];
  if (a.suggested_answer && app.channel === "hh" && app.external_ref) buttons.push([{ text: "📤 Отправить предложенный ответ", callback_data: `sa|${app.id}` }]);
  buttons.push([{ text: "🗓 Созвон назначен", callback_data: `ms|${app.id}|interview` }, { text: "🏆 Оффер", callback_data: `ms|${app.id}|offer` }]);
  await say(
    env,
    `${head}\n${v ? `<b>${h(v.title)}</b> · ${h(v.company || "")}\n` : ""}\n📝 ${h(a.summary)}\n\n<b>Они написали:</b>\n${h(truncate(app.last_reply || "", 1200))}` +
      (a.suggested_answer ? `\n\n<b>Предлагаю ответить:</b>\n<code>${h(truncate(a.suggested_answer, 1500))}</code>` : ""),
    { reply_markup: { inline_keyboard: buttons } },
  );
}

export async function sendDigest(env: Env): Promise<void> {
  const { stats } = await import("./db");
  const st = await stats(env);
  const top = await env.DB.prepare("SELECT * FROM vacancies WHERE score IS NOT NULL AND found_at >= datetime('now','-1 day') ORDER BY score DESC LIMIT 5").all<VacancyRow>();
  const hot = await env.DB.prepare("SELECT a.*, v.title, v.company FROM applications a JOIN vacancies v ON v.id = a.vacancy_id WHERE a.hot = 1 AND a.status NOT IN ('rejected') ORDER BY a.updated_at DESC LIMIT 5").all<any>();
  let text =
    `☀️ <b>Утренняя сводка JobHunter</b>\n\nЗа сутки найдено: <b>${st.newToday}</b> · отправлено откликов: <b>${st.appliedToday}</b>\n` +
    `Всего откликов: ${st.applied} · ответили: ${st.replies} · 🔥 горячих: <b>${st.hot}</b> · ждут вашего касания: ${st.waitingUser}\n`;
  if (hot.results.length) text += `\n<b>🔥 Кто хочет с вами говорить:</b>\n` + hot.results.map((a) => `• ${h(a.title)} — ${h(a.company || "")} (${a.status})`).join("\n") + "\n";
  if (top.results.length) text += `\n<b>🏆 Лучшие новые вакансии:</b>\n` + top.results.map((v) => `• <b>${v.score}</b> ${h(v.title)} — ${h(v.company || "")} · <a href="${h(v.url)}">${sourceName(v.source)}</a>`).join("\n");
  await say(env, text);
}

// ─────────────────────────────── Входящие апдейты ───────────────────────────────

const HELP =
  "<b>JobHunter — ваш автономный охотник за работой</b>\n\n" +
  "/run — запустить поиск по всем площадкам и отклики прямо сейчас\n/status — статистика и воронка\n/top — 10 лучших вакансий\n" +
  "/hot — кто хочет созвониться\n/waiting — отклики, которым нужно одно касание\n/pause, /resume — пауза автооткликов\n/help — помощь";

export async function handleUpdate(env: Env, update: any, origin: string, ctx: ExecutionContext): Promise<void> {
  const owner = await ownerChat(env);
  const msg = update.message;
  const cb = update.callback_query;
  const chatId: number | undefined = msg?.chat?.id ?? cb?.message?.chat?.id;
  if (!chatId) return;

  if (msg?.text?.startsWith("/start")) {
    const arg = msg.text.split(" ")[1] || "";
    if (!owner && env.ADMIN_PASSWORD && arg === env.ADMIN_PASSWORD) {
      await kvSet(env, "tg_owner", chatId);
      await logEvent(env, "Telegram подключён");
      await tg(env, "sendMessage", { chat_id: chatId, text: "✅ Бот привязан к вам.\n\n" + HELP, parse_mode: "HTML" });
      return;
    }
    if (owner !== chatId) {
      await tg(env, "sendMessage", { chat_id: chatId, text: "Это личный бот. Владелец подключает его командой /start <пароль панели>." });
      return;
    }
  }
  if (owner !== chatId) return;

  const { stats, getVacancy, getApplication } = await import("./db");
  const { applyToVacancy, hhSendMessage } = await import("./apply");
  const { safeTick } = await import("./pipeline");
  const { dispatchEngine } = await import("./github");

  if (cb) {
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Принято" });
    const [op, a, b] = String(cb.data).split("|");
    const findVacancy = async (prefix: string) =>
      (await getVacancy(env, prefix)) || (await env.DB.prepare("SELECT * FROM vacancies WHERE id LIKE ? LIMIT 1").bind(prefix + "%").first<VacancyRow>());
    if (op === "ap" || op === "rw") {
      const v = await findVacancy(a);
      if (!v) return;
      ctx.waitUntil((async () => {
        const r = await applyToVacancy(env, v, { force: true, extra: op === "rw" ? "Сделай письмо заметно другим по структуре и первой фразе." : undefined });
        await notifyApplied(env, v, r);
      })());
      await say(env, "⏳ Пишу письмо и откликаюсь…");
    } else if (op === "sk") {
      await env.DB.prepare("UPDATE vacancies SET status = 'skipped' WHERE id LIKE ?").bind(a + "%").run();
    } else if (op === "mk") {
      await env.DB.prepare("UPDATE applications SET status = ?, sent_at = COALESCE(sent_at, datetime('now')), updated_at = datetime('now') WHERE vacancy_id LIKE ?").bind(b, a + "%").run();
      await env.DB.prepare("UPDATE vacancies SET status = 'applied' WHERE id LIKE ?").bind(a + "%").run();
      await say(env, "Отметил как отправленный ✔️ Буду следить за ответом.");
    } else if (op === "ms") {
      await env.DB.prepare("UPDATE applications SET status = ?, hot = 1, updated_at = datetime('now') WHERE id = ?").bind(b, Number(a)).run();
      await say(env, b === "offer" ? "🏆 Поздравляю с оффером!" : "🗓 Отметил созвон. Удачи!");
    } else if (op === "sa") {
      const app = await getApplication(env, Number(a));
      if (app?.external_ref && app.suggested_answer) {
        try {
          await hhSendMessage(env, app.external_ref, app.suggested_answer);
          await env.DB.prepare("INSERT INTO messages (application_id, direction, body) VALUES (?, 'out', ?)").bind(app.id, app.suggested_answer).run();
          await say(env, "📤 Ответ отправлен работодателю на hh.ru");
        } catch (e) {
          await say(env, `Не получилось отправить через API: ${h(String(e))}\nСкопируйте текст выше и ответьте в чате hh.ru.`);
        }
      }
    }
    return;
  }

  const cmd = (msg?.text || "").split(/[\s@]/)[0];
  if (cmd === "/run") {
    const r = await dispatchEngine(env);
    ctx.waitUntil(safeTick(env, { sync: true }).then((t) => say(env, `⚙️ Цикл ИИ-оценки и откликов: ${h(t)}`)));
    await say(env, `🚀 ${h(r)}`);
  } else if (cmd === "/status") {
    const st = await stats(env);
    await say(env,
      `📊 <b>Статистика</b>\nВакансий в базе: ${st.vacancies} (за сутки +${st.newToday})\nОценено ИИ: ${st.scored} · в очереди: ${st.queue}\n` +
      `Откликов: ${st.applied} (за сутки ${st.appliedToday}) · ждут касания: ${st.waitingUser}\nОтветили: ${st.replies} · приглашений: ${st.invited} · 🔥 ${st.hot}\n\n` +
      `<b>Источники:</b> ${st.bySource.map((s) => `${sourceName(s.source)} ${s.n}`).join(", ")}`);
  } else if (cmd === "/top") {
    const top = await env.DB.prepare("SELECT * FROM vacancies WHERE score IS NOT NULL AND status IN ('scored','manual','applied') ORDER BY score DESC LIMIT 10").all<VacancyRow>();
    await say(env, "🏆 <b>Топ-10 вакансий</b>\n\n" + (top.results.map((v, i) => `${i + 1}. <b>${v.score}</b> <a href="${h(v.url)}">${h(v.title)}</a> — ${h(v.company || "")} [${v.status}]`).join("\n") || "Пока пусто — нажмите /run"));
  } else if (cmd === "/hot") {
    const hot = await env.DB.prepare("SELECT a.*, v.title, v.company, v.url FROM applications a JOIN vacancies v ON v.id = a.vacancy_id WHERE a.hot = 1 ORDER BY a.updated_at DESC LIMIT 15").all<any>();
    await say(env, "🔥 <b>Хотят с вами говорить</b>\n\n" + (hot.results.map((a) => `• <a href="${h(a.url)}">${h(a.title)}</a> — ${h(a.company || "")} · ${a.status}\n  ${h(truncate(a.last_reply || "", 200))}`).join("\n") || "Пока нет — как только кто-то позовёт, пришлю сразу."));
  } else if (cmd === "/waiting") {
    const w = await env.DB.prepare("SELECT v.* FROM applications a JOIN vacancies v ON v.id = a.vacancy_id WHERE a.status = 'waiting_user' ORDER BY v.score DESC LIMIT 5").all<VacancyRow>();
    if (!w.results.length) await say(env, "Все отклики отправлены 👌");
    for (const v of w.results) {
      const app = await env.DB.prepare("SELECT * FROM applications WHERE vacancy_id = ?").bind(v.id).first<ApplicationRow>();
      await notifyApplied(env, v, { status: "waiting_user", channel: app?.channel || "link", letter: app?.letter, note: "ждёт отправки" });
    }
  } else if (cmd === "/pause" || cmd === "/resume") {
    const s = await getSettings(env);
    await kvSet(env, "settings", { ...s, paused: cmd === "/pause" });
    await say(env, cmd === "/pause" ? "⏸ Пауза. Новые отклики не отправляются." : "▶️ Продолжаю охоту.");
  } else {
    await say(env, HELP + (origin ? `\n\nПанель: ${origin}` : ""));
  }
}

export async function setupWebhook(env: Env, origin: string, secret: string): Promise<any> {
  await tg(env, "setMyCommands", {
    commands: [
      { command: "run", description: "Запустить поиск и отклики" },
      { command: "status", description: "Статистика" },
      { command: "top", description: "Лучшие вакансии" },
      { command: "hot", description: "Кто хочет созвониться" },
      { command: "waiting", description: "Отклики в одно касание" },
      { command: "pause", description: "Пауза" },
      { command: "resume", description: "Продолжить" },
    ],
  });
  return tg(env, "setWebhook", { url: `${origin}/tg/${secret}`, allowed_updates: ["message", "callback_query"], drop_pending_updates: true });
}
