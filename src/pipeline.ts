import type { Env, VacancyRow } from "./env";
import { getProfile, getSettings, kvGet, kvSet, logEvent } from "./config";
import { evaluateVacancy, finalScore } from "./ai";
import { applyToVacancy, hhSyncNegotiations } from "./apply";
import { sentToday } from "./db";
import { notifyApplied, notifyTopMatch } from "./telegram";

/**
 * Один «тик» конвейера. Работает маленькими порциями, чтобы укладываться в лимиты бесплатного Cloudflare
 * (50 подзапросов на вызов): оценка → отклики → синхронизация ответов.
 */
export async function tick(env: Env, opts: { evaluate?: number; apply?: number; sync?: boolean } = {}): Promise<string> {
  const s = await getSettings(env);
  if (s.paused) return "на паузе";
  const report: string[] = [];
  const profile = await getProfile(env);

  // 1. ИИ-оценка новых вакансий (самые перспективные по правилам — первыми)
  const fresh = await env.DB.prepare(
    "SELECT * FROM vacancies WHERE status = 'new' ORDER BY COALESCE(score, 50) DESC, found_at DESC LIMIT ?",
  ).bind(opts.evaluate ?? 6).all<VacancyRow>();
  let evaluated = 0;
  for (const v of fresh.results) {
    try {
      if (s.remoteOnly && !v.remote) {
        await env.DB.prepare("UPDATE vacancies SET status = 'filtered', verdict = 'не удалёнка' WHERE id = ?").bind(v.id).run();
        continue;
      }
      if (s.minSalaryUsd && v.salary_usd && v.salary_usd < s.minSalaryUsd) {
        await env.DB.prepare("UPDATE vacancies SET status = 'filtered', verdict = 'зарплата ниже порога' WHERE id = ?").bind(v.id).run();
        continue;
      }
      const e = await evaluateVacancy(env, v, profile);
      const score = finalScore(v.score, e);
      const status = score >= s.minScoreNotify ? "scored" : "skipped";
      await env.DB.prepare("UPDATE vacancies SET fit = ?, conditions = ?, chance = ?, score = ?, verdict = ?, red_flags = ?, status = ? WHERE id = ?")
        .bind(e.fit, e.conditions, e.chance, score, e.verdict, JSON.stringify(e.red_flags), status, v.id).run();
      evaluated++;
      if (score >= 85) await notifyTopMatch(env, { ...v, fit: e.fit, conditions: e.conditions, chance: e.chance, score, verdict: e.verdict });
    } catch (err) {
      await env.DB.prepare("UPDATE vacancies SET error = ? WHERE id = ?").bind(String(err).slice(0, 300), v.id).run();
      report.push(`оценка ${v.id}: ${err}`);
      break; // скорее всего лимит ИИ — продолжим в следующем тике
    }
  }
  report.push(`оценено: ${evaluated}`);

  // 2. Автоотклики на лучшие
  if (s.autoApply) {
    const left = s.dailyLimit - (await sentToday(env));
    const n = Math.max(0, Math.min(opts.apply ?? 2, left));
    const best = await env.DB.prepare(
      "SELECT * FROM vacancies WHERE status = 'scored' AND score >= ? ORDER BY score DESC LIMIT ?",
    ).bind(s.minScoreAuto, n).all<VacancyRow>();
    for (const v of best.results) {
      try {
        const r = await applyToVacancy(env, v);
        if (r.status === "limit") break;
        await notifyApplied(env, v, r);
      } catch (err) {
        await env.DB.prepare("UPDATE vacancies SET status = 'error', error = ? WHERE id = ?").bind(String(err).slice(0, 300), v.id).run();
        report.push(`отклик ${v.id}: ${err}`);
      }
    }
    report.push(`откликов: ${best.results.length} (осталось на сегодня ${Math.max(0, left - best.results.length)})`);
  }

  // 3. Ответы работодателей (каждый 3-й тик)
  const n = (await kvGet<number>(env, "tick_n", 0)) + 1;
  await kvSet(env, "tick_n", n);
  if (opts.sync || n % 3 === 0) {
    try {
      const changed = await hhSyncNegotiations(env);
      report.push(`hh.ru обновлений: ${changed}`);
    } catch (err) {
      report.push(`hh sync: ${err}`);
    }
  }
  await kvSet(env, "last_tick", { at: new Date().toISOString(), report });
  return report.join(" · ");
}

export async function safeTick(env: Env, opts?: Parameters<typeof tick>[1]): Promise<string> {
  try {
    return await tick(env, opts);
  } catch (e) {
    await logEvent(env, `Ошибка конвейера: ${e}`, "error");
    return String(e);
  }
}
