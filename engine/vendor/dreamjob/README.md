# Dream Job Bot

Личный Telegram-бот для поиска работы: собирает произвольные вакансии с hh.ru, ранжирует их по настраиваемым профилям кандидата, готовит фактологичные сопроводительные письма и показывает карточки перед любым действием. Проект не привязан к конкретной вакансии, работодателю или человеку.

Бот рассчитан на одного владельца. Доступ ограничен Telegram ID из `MVP_OWNER_TELEGRAM_ID`, данные хранятся локально в SQLite, а автоматические отклики в примере выключены.

> В репозитории лежат только вымышленные шаблоны. Не подключайте запись в hh.ru, пока не замените содержимое `candidate_data/v1/` своими данными и не проверите локальный diff.

## Что умеет

- искать вакансии и рекомендации hh.ru;
- оценивать соответствие вакансии нескольким профилям;
- готовить сопроводительное письмо только из подтверждённых фактов;
- вести журнал откликов и простую CRM переписок;
- поднимать связанные резюме по расписанию;
- работать через обычный Telegram long polling — домен и webhook не нужны.

## 1. Создайте Telegram-бота

1. Откройте [@BotFather](https://t.me/BotFather) в Telegram.
2. Отправьте `/newbot`.
3. Укажите отображаемое имя и username, который заканчивается на `bot`.
4. Скопируйте выданный токен. Он выглядит как `123456789:AA...`.
5. Не отправляйте токен в чаты и не добавляйте его в git. Если токен утёк, выполните `/revoke` у BotFather и выпустите новый.

Опционально у BotFather можно настроить `/setdescription`, `/setabouttext` и `/setuserpic`. Команды `/start`, `/profile`, `/facts`, `/hh_code` и `/search_now` бот регистрирует сам при запуске.

## 2. Узнайте свой Telegram ID

Отправьте созданному боту любое сообщение, затем в терминале временно задайте токен и запросите накопившееся обновление:

```bash
export TELEGRAM_BOT_TOKEN='токен_от_BotFather'
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" | python3 -m json.tool
unset TELEGRAM_BOT_TOKEN
```

Нужное число находится в `result → message → from → id`. Его следует записать в `MVP_OWNER_TELEGRAM_ID`. После запуска бот будет игнорировать всех остальных пользователей.

## 3. Установите проект

Нужны Git, Python 3.11+ и Chromium, который установит Playwright.

```bash
git clone https://github.com/YOUR_GITHUB_USER/YOUR_REPOSITORY.git dream-job-bot
cd dream-job-bot

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

На Linux без системных библиотек выполните вместо последней команды:

```bash
python -m playwright install --with-deps chromium
```

## 4. Настройте переменные окружения

```bash
cp .env.example .env
```

Минимально заполните только значения, отмеченные как обязательные:

```dotenv
TELEGRAM_BOT_TOKEN=токен_от_BotFather
MVP_OWNER_TELEGRAM_ID=ваш_числовой_Telegram_ID

OPENAI_API_KEY=ключ_вашего_LLM_провайдера
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=id_поддерживаемой_модели
```

Подойдёт любой OpenAI-совместимый API. Если провайдер использует другой адрес, измените `OPENAI_BASE_URL`; идентификаторы основной, отдельной модели для писем и fallback задаются через `OPENAI_MODEL`, `OPENAI_LETTER_MODEL` и `OPENAI_FALLBACK_MODEL`.

Персональные фильтры по умолчанию отключены. При необходимости настройте их самостоятельно:

```dotenv
MIN_SALARY_RUB=0
CANDIDATE_HOME_TERMS=
```

`MIN_SALARY_RUB=0` не фильтрует вакансии по доходу. `CANDIDATE_HOME_TERMS` — необязательный список подходящих вам локаций через запятую; пустое значение не задаёт домашнюю локацию.

Оставьте защитные флаги выключенными на первом запуске:

```dotenv
HH_APPLICATION_WRITES_ENABLED=0
AUTO_APPLY_ARMED=0
FOLLOWUP_ENABLED=0
FOLLOWUP_AUTO_SEND=0
RESUME_BUMP_ENABLED=0
```

## 5. Замените демонстрационные данные кандидата

В `candidate_data/v1/` находятся примеры, а не готовое резюме:

- `career_facts.md` — подтверждённые факты и результаты;
- `resumes/*.md` — читаемые master-профили;
- `hh_profiles/*.json` — структурированные версии для hh.ru;
- `cover_letter_voice.md` и `resume_voice.md` — правила тона;
- `manifest.json` — контрольные суммы файлов.

Для публичного форка держите личную копию этих файлов вне репозитория. Например:

```bash
mkdir -p "$HOME/.local/share/dream-job-bot"
cp -R candidate_data/v1 "$HOME/.local/share/dream-job-bot/candidate-data"
```

Укажите абсолютный путь в `.env`:

```dotenv
CANDIDATE_DATA_ROOT=/home/YOUR_LINUX_USER/.local/share/dream-job-bot/candidate-data
```

Отредактируйте внешнюю копию под свои задачи. Не добавляйте приватные ключи, клиентские документы, точные домашние адреса и непубликуемые факты. После изменений пересчитайте её манифест — скрипт использует тот же `CANDIDATE_DATA_ROOT`:

```bash
python scripts/reseal_candidate_data.py
python scripts/reseal_candidate_data.py --check
```

До замены демо-данных не включайте запись в hh.ru.

## 6. Запустите бота

Файл `.env` намеренно не читается автоматически: так секреты не смешиваются с кодом. Экспортируйте его в окружение процесса и запустите polling:

```bash
set -a
source .env
set +a
python run_candidate_mvp.py
```

Откройте бота в Telegram и отправьте `/start`. Для первого теста:

1. Проверьте, что открылось главное меню.
2. Откройте профили и убедитесь, что там ваши локальные данные, а не demo-шаблоны.
3. Отправьте название, ссылку и полное вымышленное описание тестовой вакансии одним сообщением.
4. Убедитесь, что бот создаёт только preview и не отправляет отклик.

Остановить локальный процесс можно через `Ctrl+C`.

## 7. Подключите hh.ru — опционально

Telegram-часть и разбор присланного полного описания работают без HH OAuth. Для загрузки вакансии только по ссылке, поиска, резюме, CRM и откликов нужен отдельный доступ:

1. Войдите на [dev.hh.ru](https://dev.hh.ru/) и зарегистрируйте приложение.
2. Скопируйте ID и секрет приложения в `HH_CLIENT_ID` и `HH_CLIENT_SECRET`.
3. Укажите настоящий контакт разработчика в обязательном User-Agent:

   ```dotenv
   HH_USER_AGENT='DreamJobBot/1.0 (contact: you@example.com)'
   ```

4. Перезапустите бота.
5. В Telegram откройте `Настройки → Автопоиск / автоотклик` и выберите способ подключения HH. Для browser/OTP-сценария нужен установленный Chromium.

HH использует OAuth2, требует идентифицирующий User-Agent и может менять требования к redirect URI/PKCE; сверяйтесь с [официальной документацией API](https://api.hh.ru/openapi/redoc) при регистрации нового приложения.

### Как безопасно включать действия в hh.ru

Включайте возможности по одной после ручного теста:

```dotenv
# Разрешить подтверждённые вручную записи через API
HH_APPLICATION_WRITES_ENABLED=1

# Разрешить именно автоматический отклик; это отдельный второй предохранитель
AUTO_APPLY_ARMED=1

# Автоматически поднимать связанные резюме
RESUME_BUMP_ENABLED=1

# Автоматически отправлять follow-up сообщения
FOLLOWUP_ENABLED=1
FOLLOWUP_AUTO_SEND=1
```

Если нужен только поиск и preview, все четыре значения должны оставаться `0`.

## 8. Постоянный запуск на VPS через systemd

Long polling не требует публичного порта, домена, TLS-сертификата или webhook. На Ubuntu/Debian создайте отдельного непривилегированного пользователя, затем установите проект, зависимости и Chromium так же, как в шагах выше, например в `/opt/dream-job-bot`.

Сохраните заполненный `.env` в каталоге проекта и ограничьте доступ:

```bash
chmod 600 /opt/dream-job-bot/.env
```

Создайте `/etc/systemd/system/dream-job-bot.service`:

```ini
[Unit]
Description=Dream Job Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_LINUX_USER
WorkingDirectory=/opt/dream-job-bot
EnvironmentFile=/opt/dream-job-bot/.env
ExecStart=/opt/dream-job-bot/.venv/bin/python /opt/dream-job-bot/run_candidate_mvp.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Замените `YOUR_LINUX_USER`, затем включите сервис:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dream-job-bot
sudo systemctl status dream-job-bot
sudo journalctl -u dream-job-bot -f
```

Для обновления:

```bash
cd /opt/dream-job-bot
git pull --ff-only
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python scripts/reseal_candidate_data.py --check
sudo systemctl restart dream-job-bot
```

## Тесты

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Частые проблемы

- `TelegramUnauthorizedError` — неверный или отозванный токен BotFather.
- `TelegramConflictError` / HTTP 409 — с тем же токеном уже работает другой polling-процесс или установлен webhook. Остановите второй экземпляр; polling и webhook одновременно не работают.
- Бот не отвечает — проверьте `MVP_OWNER_TELEGRAM_ID` и `journalctl`/вывод терминала.
- `Executable doesn't exist` от Playwright — выполните `python -m playwright install chromium`.
- `Candidate data manifest mismatch` — после осознанного редактирования профилей выполните `python scripts/reseal_candidate_data.py`; при неожиданном изменении сначала проверьте diff.
- HH возвращает ошибку авторизации — проверьте `HH_CLIENT_ID`, `HH_CLIENT_SECRET`, `HH_USER_AGENT` и настройки OAuth-приложения.
- Письмо не генерируется — проверьте `OPENAI_BASE_URL`, `OPENAI_MODEL`, ключ и поддержку OpenAI-compatible Chat Completions вашим провайдером.

## Безопасность

- Никогда не коммитьте `.env`, SQLite из `var/`, OAuth-токены и реальные резюме, если репозиторий публичный.
- Перед публикацией форка запустите `git grep` по своему имени, email, телефону, Telegram ID, названиям работодателей и ID вакансий/резюме.
- Запускайте только один экземпляр polling-бота на один Telegram-токен.
- Бот удаляет сообщения с логином и OTP для HH, но сервер и его логи всё равно должны быть под вашим контролем.
- Перед включением автооткликов проверьте условия и ограничения сервисов, которыми пользуется бот.

## Структура

```text
candidate/          база фактов, ранжирование и генерация
candidate_data/v1/  demo-профили и правила кандидата
hh/                 OAuth и HH API-клиент
mvp/                Telegram-экраны, автоматизация и CRM
scripts/             обслуживание манифеста
tests/               тесты активного кода
run_candidate_mvp.py точка входа
```

Лицензия: MIT.
