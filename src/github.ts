import type { Env } from "./env";

/** Запуск движка сбора (GitHub Actions) по кнопке на сайте / команде /run в Telegram. */
export async function dispatchEngine(env: Env & { GITHUB_TOKEN?: string; GITHUB_REPO?: string; GITHUB_REF?: string }): Promise<string> {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) return "GitHub не подключён: движок запустится по расписанию (каждые 2 часа)";
  const res = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/engine.yml/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "JobHunter",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ ref: env.GITHUB_REF || "main" }),
  });
  if (res.status === 204) return "Сбор вакансий запущен на всех площадках (≈5–10 минут)";
  return `GitHub ответил ${res.status}: ${(await res.text()).slice(0, 200)}`;
}
