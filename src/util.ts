import type { Contacts, Lang } from "./env";

/** Грубые курсы для сравнения зарплат между рынками (месяц, USD). */
const USD_RATE: Record<string, number> = {
  USD: 1, RUR: 1 / 95, RUB: 1 / 95, EUR: 1.08, GBP: 1.27, KZT: 1 / 500, UAH: 1 / 41, BYR: 0.3, BYN: 0.3, UZS: 1 / 12800,
};

export function salaryToUsdMonthly(from?: number | null, to?: number | null, currency?: string | null, period: "month" | "year" = "month"): number | null {
  const base = to && from ? (from + to) / 2 : (from || to);
  if (!base) return null;
  const rate = USD_RATE[(currency || "USD").toUpperCase()] ?? 1;
  let v = base * rate;
  if (period === "year" || v > 40000) v = v / 12; // годовые суммы в западных вакансиях
  return Math.round(v);
}

/** Удаляет HTML, декодирует сущности, схлопывает пробелы. */
export function stripHtml(html: string | null | undefined): string {
  if (!html) return "";
  return decodeEntities(
    html
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<\/(p|div|li|h\d|tr)>/gi, "\n")
      .replace(/<li[^>]*>/gi, "• ")
      .replace(/<[^>]+>/g, " "),
  )
    .replace(/[ \t ]+/g, " ")
    .replace(/\n\s*\n\s*\n+/g, "\n\n")
    .trim();
}

export function decodeEntities(s: string): string {
  return s
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;|&#039;|&apos;/g, "'")
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Number(d)))
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)));
}

/** Язык текста по доле кириллицы. */
export function detectLang(text: string): Lang {
  const sample = text.slice(0, 3000);
  const cyr = (sample.match(/[а-яё]/gi) || []).length;
  const lat = (sample.match(/[a-z]/gi) || []).length;
  return cyr >= lat * 0.25 ? "ru" : "en";
}

const EMAIL_RE = /[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/gi;
const TG_RE = /(?:^|[\s(,:;])@([a-z][a-z0-9_]{4,31})\b|t\.me\/([a-z][a-z0-9_]{4,31})(?![a-z0-9_/])/gi;
const LINK_RE = /https?:\/\/[^\s<>"')]+/gi;
const IGNORE_TG = new Set(["joinchat", "share", "addstickers", "proxy", "iv", "s"]);

export function extractContacts(text: string, ownChannel?: string): Contacts {
  const emails = uniq((text.match(EMAIL_RE) || []).map((e) => e.toLowerCase()).filter((e) => !/\.(png|jpe?g|gif|webp)$/.test(e)));
  const telegram: string[] = [];
  for (const m of text.matchAll(TG_RE)) {
    const u = (m[1] || m[2] || "").toLowerCase();
    if (!u || IGNORE_TG.has(u) || u === ownChannel?.toLowerCase() || u.endsWith("bot") && u.length < 6) continue;
    telegram.push(u);
  }
  const links = uniq((text.match(LINK_RE) || []).filter((l) => !/t\.me\//i.test(l)));
  return { emails, telegram: uniq(telegram), links };
}

export function uniq<T>(a: T[]): T[] {
  return [...new Set(a)];
}

/** Отпечаток для склейки одной и той же вакансии из разных источников. */
export async function fingerprint(title: string, company?: string | null): Promise<string> {
  const norm = (s: string) => s.toLowerCase().replace(/[^a-zа-яё0-9]+/gi, " ").trim().split(" ").sort().join(" ");
  return sha1(`${norm(title)}|${norm(company || "")}`);
}

export async function sha1(s: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function hmac(secret: string, data: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(data));
  return btoa(String.fromCharCode(...new Uint8Array(sig))).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
}

export function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n - 1) + "…";
}

export function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** fetch с таймаутом и единым User-Agent. */
export async function http(url: string, init: RequestInit & { timeoutMs?: number } = {}): Promise<Response> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort("timeout"), init.timeoutMs ?? 15000);
  try {
    const headers = new Headers(init.headers);
    if (!headers.has("user-agent")) headers.set("user-agent", "Mozilla/5.0 (compatible; JobHunter/1.0; +https://bazadigital.ru)");
    return await fetch(url, { ...init, headers, signal: ctrl.signal });
  } finally {
    clearTimeout(t);
  }
}

export function clamp(n: unknown, lo = 0, hi = 100): number {
  const v = Math.round(Number(n));
  return Number.isFinite(v) ? Math.max(lo, Math.min(hi, v)) : lo;
}

export function formatSalary(from: number | null, to: number | null, cur: string | null): string {
  if (!from && !to) return "";
  const c = (cur || "").toUpperCase().replace("RUR", "₽").replace("RUB", "₽").replace("USD", "$").replace("EUR", "€");
  const f = (n: number) => n.toLocaleString("ru-RU");
  if (from && to) return `${f(from)}–${f(to)} ${c}`;
  return from ? `от ${f(from)} ${c}` : `до ${f(to!)} ${c}`;
}
