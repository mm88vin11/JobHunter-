import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jobhunter_engine  # noqa: E402,F401
from jobhunter_engine.model import Vacancy, extract_contacts  # noqa: E402
from jobhunter_engine.run import filter_and_score  # noqa: E402
from jobhunter_engine.score import score  # noqa: E402
from jobhunter_engine.sources.social import parse_channel_html  # noqa: E402

TG_HTML = """
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="vacancyit/101">
<div class="tgme_widget_message_text js-message_text" dir="auto"><b>#вакансия Вайб-кодер (Cursor/Claude)</b><br/>Ищем разработчика, который
собирает продукты с ИИ. Удалёнка, зарплата от 150 000 до 200 000 ₽. Стек: JavaScript, Telegram-боты, LLM.<br/>Писать: @hr_anna</div>
<time datetime="2026-11-15T10:00:00+00:00"></time></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="vacancyit/102">
<div class="tgme_widget_message_text js-message_text" dir="auto">Розыгрыш призов среди подписчиков!</div></div></div>
"""


def test_telegram_parser():
    items = parse_channel_html("vacancyit", TG_HTML)
    assert len(items) == 1
    v = items[0]
    assert v.url == "https://t.me/vacancyit/101"
    assert v.contacts["telegram"] == ["hr_anna"]
    assert v.salary_from == 150000 and v.salary_to == 200000 and v.currency == "RUR"
    assert v.remote and v.lang == "ru"


def test_contacts():
    c = extract_contacts("mail me: Jobs@Acme.io or @acme_hr https://acme.io/apply")
    assert c["emails"] == ["jobs@acme.io"] and c["telegram"] == ["acme_hr"]


def test_ranking_prefers_vibe_coding():
    good = score(Vacancy(source="t", external_id="1", url="u", title="Вайб-кодер", description="LLM, JavaScript, удалёнка").finalize(), [], [])
    bad = score(Vacancy(source="t", external_id="2", url="u", title="Senior Java Developer", description="Spring, Kafka").finalize(), [], [])
    assert good.rule_score > bad.rule_score


def test_dedupe_and_filters():
    cfg = {"stop_words": ["водитель"], "stop_companies": [], "must_have_any": ["vibe", "ai ", "javascript"], "min_rule_score": 0}
    a = Vacancy(source="hh", external_id="1", url="u1", title="Vibe coder", company="Acme", description="short").finalize()
    b = Vacancy(source="habr", external_id="2", url="u2", title="Vibe Coder", company="ACME", description="much longer description with javascript").finalize()
    c = Vacancy(source="hh", external_id="3", url="u3", title="Водитель", company="X", description="javascript").finalize()
    out = filter_and_score(cfg, [a, b, c])
    assert len(out) == 1 and out[0].source == "habr"
