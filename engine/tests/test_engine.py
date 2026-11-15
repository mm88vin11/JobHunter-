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


# ── новые модули (GitHub-версия) ──
import base64  # noqa: E402
import json  # noqa: E402

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC  # noqa: E402

from jobhunter_engine import ai, pipeline, site  # noqa: E402
from jobhunter_engine.store import Store, decrypt, encrypt  # noqa: E402


def test_codewords_ru_en():
    ru = ai.regex_instructions("X", "В отклике обязательно напишите кодовое слово «Банан». Начните письмо с фразы \"Я читал вакансию\".")
    assert ru["phrases"] == ["Банан"] and ru["openings"] == ["Я читал вакансию"]
    en = ai.regex_instructions("X", "To show you read this, include the word PINEAPPLE in your application.")
    assert "PINEAPPLE" in en["phrases"]


def test_letter_checker_requires_codeword():
    letter = "Здравствуйте! Я Влад, делаю продукты с ИИ от идеи до запуска. " * 8
    probs = ai.letter_problems(letter, "ru", {"must_include": ["Банан"], "must_start_with": "Я читал вакансию"})
    assert any("Банан" in p for p in probs) and any("начинаться" in p for p in probs)
    assert not ai.letter_problems("Я читал вакансию. Банан. " + letter, "ru", {"must_include": ["Банан"], "must_start_with": "Я читал вакансию"})


def test_store_encryption_and_ingest(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test")
    assert decrypt(encrypt(b"secret")) == b"secret"
    st = Store(str(tmp_path))
    item = {"source": "habr", "external_id": "1", "url": "u", "title": "Вайб-кодер", "company": "Acme", "rule_score": 80, "lang": "ru"}
    assert pipeline.ingest(st, [item, dict(item, source="hh", external_id="9")]) == 1  # дубль по отпечатку
    st.set("k", {"a": 1})
    assert st.get("k") == {"a": 1}


def test_web_encryption_matches_browser_format():
    blob = site.encrypt_for_web("pw", {"hello": "мир"})
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=base64.b64decode(blob["salt"]), iterations=blob["iter"]).derive(b"pw")
    data = AESGCM(key).decrypt(base64.b64decode(blob["iv"]), base64.b64decode(blob["data"]), None)
    assert json.loads(data) == {"hello": "мир"}
