"""Главный процесс JobHunter внутри GitHub Actions.

Один долгоживущий запуск (~5 ч 40 мин), затем расписание поднимает следующий:
  • Telegram-бот (long polling) — постоянно;
  • сбор вакансий со всех площадок — раз в 2 часа и по /run;
  • ИИ-оценка, автоотклики, проверка ответов (hh.ru, почта) — каждые 5 минут;
  • сохранение зашифрованной базы и публикация сайта — каждые 10 минут;
  • утренняя сводка в 09:00 МСК.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import config, pipeline, site
from .bot import Bot, SRC, stats
from .run import collect, filter_and_score
from .store import DATA_BRANCH, Store, prepare_branch_worktree

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jobhunter")
ROOT = config.ROOT


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=float, default=340)
    ap.add_argument("--workdir", default=os.environ.get("RUNNER_TEMP", "/tmp"))
    args = ap.parse_args(argv)
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("::warning::Нет секрета TELEGRAM_BOT_TOKEN — добавьте его: Settings → Secrets and variables → Actions → New repository secret")
        return 0

    data_dir = prepare_branch_worktree(ROOT, DATA_BRANCH, os.path.join(args.workdir, "jh-data"))
    pages_dir = prepare_branch_worktree(ROOT, "gh-pages", os.path.join(args.workdir, "jh-pages"))
    store = Store(data_dir)
    pool = ThreadPoolExecutor(max_workers=2)
    busy = {"collect": None, "tick": None}
    collect_now = threading.Event()
    bot = Bot(store, submit=lambda fn: pool.submit(_safe, store, fn), request_collect=collect_now.set)
    site.site_password(store)
    store.event(f"Запуск JobHunter · ИИ: {__import__('jobhunter_engine.ai', fromlist=['provider']).provider()}")
    if bot.owner and not store.get("welcomed_v3"):
        bot.say("🤖 JobHunter перезапущен на GitHub и работает 24/7.")
        bot.cmd_site()
        store.set("welcomed_v3", True)

    import signal

    def _on_stop(*_):  # отмена запуска в GitHub: успеваем сохранить базу
        log.info("получен сигнал остановки — сохраняюсь")
        _persist(store, pages_dir, bot)
        os._exit(0)
    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)

    deadline = time.time() + args.max_minutes * 60
    last = {"tick": 0.0, "save": 0.0}

    def do_collect():
        s = config.settings(store)
        cfg = config.engine_cfg(s, store)
        only = {x for x in os.environ.get('JH_ONLY_SOURCES', '').split(',') if x} or None
        raw = collect(cfg, only)
        items = filter_and_score(cfg, raw)
        added = pipeline.ingest(store, [v.to_ingest() for v in items])
        store.set("engine_last", {"at": datetime.now(timezone.utc).isoformat(), "stats": cfg.get("_stats", {})})
        store.set("last_collect_at", time.time())
        store.event(f"Сбор: найдено {len(raw)}, подходит {len(items)}, новых {added} · " + ", ".join(f"{SRC.get(k, k)} {v}" for k, v in cfg.get("_stats", {}).items()))
        if added and bot.owner:
            bot.say(f"🔎 Сбор завершён: новых подходящих вакансий <b>{added}</b>. Оцениваю и откликаюсь на лучшие.")
        collect_now.clear()

    def do_tick():
        r = pipeline.tick(store, bot)
        log.info("tick: %s", r)

    while time.time() < deadline:
        bot.poll(timeout=20)
        now = time.time()
        s = config.settings(store)
        every = s.get("collectEveryMinutes", 120) * 60
        if (collect_now.is_set() or now - (store.get("last_collect_at", 0) or 0) > every) and not _running(busy["collect"]):
            busy["collect"] = pool.submit(_safe, store, do_collect)
        if now - last["tick"] > 300 and not _running(busy["tick"]):
            last["tick"] = now
            busy["tick"] = pool.submit(_safe, store, do_tick)
        if now - last["save"] > 600:
            last["save"] = now
            _persist(store, pages_dir, bot)
        _digest(store, bot)

    log.info("Время запуска истекает — сохраняюсь")
    for f in busy.values():
        if f:
            try:
                f.result(timeout=600)
            except Exception:
                pass
    _persist(store, pages_dir, bot)
    return 0


def _running(f) -> bool:
    return f is not None and not f.done()


def _safe(store, fn):
    try:
        return fn()
    except Exception as e:
        log.exception("task")
        store.event(f"Ошибка: {e}"[:500], "error")


def _persist(store, pages_dir, bot) -> None:
    try:
        store.save("state")
    except Exception as e:
        log.warning("save: %s", e)
    try:
        site.publish(store, pages_dir, bot.username)
    except Exception as e:
        log.warning("site: %s", e)


def _digest(store, bot) -> None:
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    if now.hour != 6 or store.get("digest_day") == day:
        return
    store.set("digest_day", day)
    st = stats(store)
    top = store.q("SELECT * FROM vacancies WHERE score IS NOT NULL AND fit IS NOT NULL AND found_at >= datetime('now','-1 day') ORDER BY score DESC LIMIT 5")
    hot = store.q("SELECT a.status, v.title, v.company FROM applications a JOIN vacancies v ON v.id = a.vacancy_id WHERE a.hot = 1 AND a.status != 'rejected' ORDER BY a.updated_at DESC LIMIT 5")
    t = (f"☀️ <b>Утренняя сводка</b>\n\nЗа сутки найдено: <b>{st['newToday']}</b> · отправлено откликов: <b>{st['appliedToday']}</b>\n"
         f"Всего откликов: {st['applied']} · ответили: {st['replies']} · 🔥 горячих: <b>{st['hot']}</b> · ждут касания: {st['waitingUser']}\n")
    if hot:
        t += "\n<b>🔥 Кто хочет с вами говорить:</b>\n" + "\n".join(f"• {r['title']} — {r['company'] or ''} ({r['status']})" for r in hot) + "\n"
    if top:
        t += "\n<b>🏆 Лучшие новые вакансии:</b>\n" + "\n".join(f"• <b>{v['score']}</b> <a href=\"{v['url']}\">{v['title']}</a> — {v['company'] or ''}" for v in top)
    bot.say(t + f"\n\n🌐 {bot.site_url()}")


if __name__ == "__main__":
    sys.exit(main())
