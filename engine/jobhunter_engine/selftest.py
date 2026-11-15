"""Самопроверка в GitHub Actions без секретов: сбор с нескольких площадок + ИИ-оценка + письма (вывод в лог)."""
from __future__ import annotations

import json
import sys

from . import ai, config
from .run import collect, filter_and_score


def main() -> int:
    print("ИИ:", ai.provider())
    s = config.settings()
    cfg = config.engine_cfg(s)
    items = filter_and_score(cfg, collect(cfg, {"habr", "remoteok", "hn", "telegram"}))
    print("подходящих вакансий:", len(items), json.dumps(cfg.get("_stats"), ensure_ascii=False))
    prof = config.profile()
    test = {"title": "Vibe Coder (Cursor / Claude Code)", "company": "Acme AI", "source": "hn", "lang": "en",
            "description": "Remote startup building AI agents. Ship features fast with Cursor and Claude Code, build Telegram bots. "
                           "To show you read this, include the word PINEAPPLE in your application and tell us which AI tool you use most."}
    ru = next((v.to_ingest() for v in items if v.lang == "ru"), None)
    en = next((v.to_ingest() for v in items if v.lang == "en"), None)
    ok = True
    for v in [test, ru, en]:
        if not v:
            continue
        print("\n" + "=" * 80 + f"\n{v['title']} — {v.get('company')} [{v['source']}, {v.get('lang')}]")
        e = ai.evaluate(v, prof)
        print("оценка:", {k: e[k] for k in ("fit", "conditions", "chance")}, "итог", ai.final_score(v.get("rule_score"), e), "|", e["verdict"])
        print("инструкции работодателя:", json.dumps(e["instructions"], ensure_ascii=False))
        out = ai.write_letter(v, prof, channel="email", instructions=e["instructions"])
        print("тема:", out["subject"], "\n" + out["letter"], "\nпроблемы:", out["problems"])
        if v is test and "pineapple" not in out["letter"].lower():  # кодовое слово обязано попасть в письмо
            ok = False
            print("❌ кодовое слово не вставлено")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
