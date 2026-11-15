import { describe, expect, it } from "vitest";
import { detectLang, extractContacts, salaryToUsdMonthly, stripHtml } from "../src/util";
import { finalScore, humanize, letterProblems } from "../src/ai";
import { extractPlainText } from "../src/index";
import { telegramDeepLink } from "../src/apply";

describe("util", () => {
  it("detects language", () => {
    expect(detectLang("Ищем вайб-кодера в команду, удалёнка")).toBe("ru");
    expect(detectLang("We are hiring an AI engineer, fully remote")).toBe("en");
  });
  it("extracts contacts", () => {
    const c = extractContacts("Пишите @hr_anna или на jobs@acme.io, подробнее https://acme.io/jobs и t.me/acme_hr", "vacancyit");
    expect(c.emails).toEqual(["jobs@acme.io"]);
    expect(c.telegram).toEqual(["hr_anna", "acme_hr"]);
    expect(c.links).toEqual(["https://acme.io/jobs"]);
  });
  it("normalizes salary", () => {
    expect(salaryToUsdMonthly(190000, null, "RUR")).toBe(2000);
    expect(salaryToUsdMonthly(120000, 144000, "USD")).toBe(11000);
  });
  it("strips html", () => {
    expect(stripHtml("<p>Привет&nbsp;мир</p><ul><li>a</li></ul>")).toContain("Привет мир");
  });
});

describe("letters", () => {
  it("flags AI tells", () => {
    const p = letterProblems("Здравствуйте! Вам нужно не просто разработчик, а партнёр — и я идеально подхожу.", "ru");
    expect(p.some((x) => x.includes("тире"))).toBe(true);
    expect(p.some((x) => x.includes("не X, а Y"))).toBe(true);
    expect(p.some((x) => x.includes("пересказа"))).toBe(true);
    expect(letterProblems("I am excited and passionate about this dynamic team", "en").length).toBeGreaterThan(2);
  });
  it("humanizes punctuation", () => {
    expect(humanize("Сделал сайт — быстро")).toBe("Сделал сайт, быстро");
  });
  it("scores", () => {
    expect(finalScore(80, { fit: 90, conditions: 80, chance: 70, verdict: "", red_flags: [], is_job: true, scam: false })).toBe(81);
    expect(finalScore(80, { fit: 90, conditions: 80, chance: 70, verdict: "", red_flags: [], is_job: true, scam: true })).toBe(0);
  });
  it("builds telegram deep link", () => {
    expect(telegramDeepLink("hr_anna", "Привет")).toBe("https://t.me/hr_anna?text=%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82");
  });
});

describe("email", () => {
  it("extracts text/plain and drops quote", () => {
    const raw = 'Content-Type: multipart/alternative; boundary="b1"\r\n\r\n--b1\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n' +
      btoa(unescape(encodeURIComponent("Влад, давайте созвонимся завтра в 15:00\n> старое письмо"))) + "\r\n--b1--";
    expect(extractPlainText(raw)).toBe("Влад, давайте созвонимся завтра в 15:00");
  });
});

describe("letter repair loop (мок ИИ)", () => {
  it("переписывает письмо, пока не исчезнут признаки ИИ, и добавляет подпись по языку", async () => {
    const { writeLetter } = await import("../src/ai");
    const { DEFAULT_PROFILE } = await import("../src/config");
    const drafts = [
      "I am excited to apply — I'm passionate about AI and would be a perfect fit for your dynamic team. ".repeat(3),
      "Hi! I'm Vlad, I run a small dev studio and ship web apps, Telegram bots and AI automations solo. You need someone who can take a feature from idea to production with Cursor and Claude; that's how I work every day. I recently built a turnkey site + bot + CRM setup so no lead gets lost. Happy to do a 15 minute call this week and show the projects.",
    ];
    let calls = 0;
    const env: any = { AI: { run: async () => ({ response: { letter: drafts[Math.min(calls++, 1)] } }) } };
    const v: any = { lang: "en", title: "AI Engineer", company: "Acme", description: "Build LLM features with Cursor" };
    const letter = await writeLetter(env, v, DEFAULT_PROFILE, { channel: "email" });
    expect(calls).toBe(2);
    expect(letter).toContain("https://bazadigital.ru/en/");
    expect(letter).not.toContain("—");
  });
});
