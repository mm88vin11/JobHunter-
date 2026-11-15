# 🎯 JobHunter: автономный ИИ-охотник за работой

Сам ищет вакансии на **24 площадках** (Россия и весь мир), оценивает их ИИ под ваше резюме, **выполняет инструкции
работодателя** (кодовые слова, обязательная первая фраза, вопросы в отклике), пишет живое письмо на языке вакансии,
**сам откликается**, следит за ответами и присылает в Telegram готовые результаты: кто ответил, кто зовёт на созвон
и что ему ответить.

Всё работает **бесплатно внутри GitHub**: бот и движок живут в GitHub Actions, сайт-панель на GitHub Pages,
база хранится в зашифрованном виде в ветке `jobhunter-data`.

- 🌐 Панель: **https://raw.githack.com/mm88vin11/JobHunter-/gh-pages/index.html** (пароль присылает бот по команде `/site`; после включения GitHub Pages также https://mm88vin11.github.io/JobHunter-/)
- 🤖 Бот: **[@DreamJobAgent](https://t.me/DreamJobAgent)**

> Проект собран поверх лучших открытых репозиториев: [JobSpy](https://github.com/speedyapply/JobSpy) (4.7k★, MIT)
> и [dream-job-bot](https://github.com/ev-mironov/dream-job-bot-open-source) (MIT). Их код лежит целиком в `engine/vendor/`,
> улучшения описаны ниже. Лицензии: [NOTICE.md](NOTICE.md).

## Запуск: одно действие

Откройте **[Settings → Secrets → New secret](https://github.com/mm88vin11/JobHunter-/settings/secrets/actions/new)**
и добавьте `TELEGRAM_BOT_TOKEN` (токен @DreamJobAgent от BotFather). Затем напишите боту `/start`: первый написавший
становится владельцем. Бот пришлёт ссылку на панель и пароль.

Дальше всё идёт само: workflow **JobHunter** запускается каждые 30 минут, держит бота онлайн и работает круглосуточно.

### Нейросеть: бесплатно по умолчанию

Ключи не нужны: письма и оценку делает бесплатный **Pollinations** (GPT-OSS). Цепочка переключается сама:
если один сервис недоступен или упёрся в лимит, задачу подхватывает следующий.

| Секрет (необязательно) | Качество | Где взять (бесплатно, без карты) |
|---|---|---|
| `GEMINI_API_KEY` | лучшее из бесплатных, отлично пишет по-русски | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `GROQ_API_KEY` | очень быстро, GPT-OSS 120B | [console.groq.com/keys](https://console.groq.com/keys) |
| `OPENROUTER_API_KEY` | бесплатные модели OpenRouter | [openrouter.ai/keys](https://openrouter.ai/keys) |
| `ANTHROPIC_API_KEY` | платно: Claude Haiku 5.5 оценка, Opus 5.5 письма | [console.anthropic.com](https://console.anthropic.com) |

Порядок: Claude (если есть ключ) → Gemini → Groq → OpenRouter → Pollinations. Если недоступны все, система
оценивает вакансии по правилам и готовит шаблонные черновики, но сама их не отправляет.

### Другие необязательные секреты

| Секрет | Что даёт | Где взять |
|---|---|---|
| `HH_CLIENT_ID`, `HH_CLIENT_SECRET` | **автоотклики на hh.ru** и слежение за приглашениями | [dev.hh.ru](https://dev.hh.ru) → зарегистрировать приложение, Redirect URI: `https://raw.githack.com/mm88vin11/JobHunter-/gh-pages/hh.html`. Потом команда `/hh` в боте |
| `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD` | отклики письмом **с вашей почты** и чтение ответов работодателей | mail.ru → Безопасность → Пароли для внешних приложений |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE` | **автоотправка откликов HR в Telegram** с вашего аккаунта (до 15 в сутки), чтение их ответов. После добавления: `/tglogin` в боте | [my.telegram.org](https://my.telegram.org) → API development tools; телефон в формате +7… |
| `VK_SERVICE_TOKEN` | VK-сообщества с вакансиями | vk.com/apps?act=manage → приложение → сервисный ключ |
| `DATA_KEY` | отдельный ключ шифрования базы (иначе используется токен бота) | любая длинная случайная строка |

## Что умеет

| | |
|---|---|
| **24 площадки** | **Россия:** hh.ru, Хабр Карьера, getmatch, «Работа России», FL.ru, 18 Telegram-каналов, VK. **Мир (JobSpy):** LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs, Bayt, Naukri, BDJobs, HelloWork. **Удалёнка:** RemoteOK, Remotive, Himalayas, We Work Remotely, Working Nomads, Jobicy, Arbeitnow, Hacker News |
| **Инструкции работодателя** | правила dream-job-bot (русские формулировки) + английские шаблоны + извлечение ИИ: кодовые слова, первая фраза, тема письма, вопросы, анкеты и тесты. Письмо не уйдёт, пока проверка не найдёт в нём кодовое слово дословно |
| **Человеческие письма** | только факты из резюме; цикл «написал → проверил → исправил» убирает признаки ИИ: длинные тире, «не X, а Y», пересказ вакансии, шаблонные фразы |
| **Язык и ссылки** | английская вакансия → письмо на английском и **bazadigital.ru/en/**, русская → по-русски и **bazadigital.ru** |
| **Оценка** | правила dream-job-bot + ИИ (соответствие, условия, шанс на оффер), антискам, штраф за Senior/Lead, склейка дублей между площадками |
| **Отклики** | hh.ru (API), email с вашей почты, Telegram в одно касание (чат с HR открывается с уже вписанным письмом) |
| **Ответы** | hh.ru и почта читаются автоматически; ИИ определяет «зовут на созвон / оффер / вопрос / отказ» и готовит ответ, бот присылает его с кнопкой «Отправить» |
| **Бот** | 🔥 мгновенно о приглашениях, утренняя сводка в 9:00 МСК, `/run /status /top /hot /waiting /site /pause /resume /limit /threshold /hh` |
| **Сайт** | воронка, рейтинг вакансий с вердиктом ИИ, все отклики и переписка. Данные зашифрованы паролем (AES-256), кнопки на сайте отправляют команды боту |

## Что полностью автоматически, а что в одно касание

- ✅ **Само**: hh.ru (после `/hh`) и вакансии с email (если подключена почта).
- 👆 **Одно касание**: Telegram-контакты. Боты не могут писать людям первыми, поэтому бот присылает кнопку, которая открывает чат с HR с готовым письмом. Массовая рассылка с вашего личного аккаунта быстро заканчивается баном, поэтому так не делаю.
- 🌐 **Сайт работодателя**: LinkedIn, Indeed и другие западные доски ведут на свои формы (ATS). Бот присылает готовое английское письмо и ссылку.

## Настройка

Резюме, запросы, Telegram-каналы, стоп-слова, площадки и пороги лежат в [`config/settings.json`](config/settings.json)
и [`config/profile.json`](config/profile.json). Их можно править прямо на GitHub (кнопки во вкладке «Настройки» на сайте).
Быстрые правки из бота: `/limit 40`, `/threshold 62`, `/pause`, `/resume`.

## Архитектура

```
GitHub Actions: workflow «JobHunter» (каждые 30 мин, один запуск живёт ~5ч40м)
 ├─ Telegram-бот: long polling, сервер не нужен
 ├─ сбор: engine/vendor/jobspy_repo (JobSpy) + jobhunter_engine/sources (15 площадок сверху)
 ├─ антискам и ранжирование: engine/vendor/dreamjob (dream-job-bot)
 ├─ ИИ: Gemini / Groq / OpenRouter / Pollinations (бесплатно) или Claude
 ├─ отклики: hh.ru API · SMTP · ссылки t.me
 ├─ ответы: hh.ru negotiations · IMAP
 ├─ база SQLite → AES-GCM → ветка jobhunter-data
 └─ сайт → ветка gh-pages (данные зашифрованы паролем)
```

Самопроверка без секретов: Actions → JobHunter → Run workflow → `selftest`. Она соберёт вакансии, оценит их ИИ
и напишет пробные письма в лог (включая тест с кодовым словом).

## Разработка

```bash
pip install -r engine/requirements.txt pytest && python -m pytest engine/tests
cd engine && python -m jobhunter_engine.run --dry-run --only habr,telegram,remoteok   # сбор → results.json
```
