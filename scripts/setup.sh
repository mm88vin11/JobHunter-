#!/usr/bin/env bash
# Установка JobHunter на Cloudflare в одну команду: bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "▶ Устанавливаю зависимости"; npm install --silent
echo "▶ Вход в Cloudflare (откроется браузер)"; npx wrangler whoami >/dev/null 2>&1 || npx wrangler login

if grep -q REPLACE_WITH_D1_ID wrangler.toml; then
  echo "▶ Создаю базу D1"
  ID=$(npx wrangler d1 create jobhunter 2>&1 | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)
  [ -z "$ID" ] && ID=$(npx wrangler d1 list --json | node -e 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{const x=JSON.parse(d).find(b=>b.name==="jobhunter");console.log(x?x.uuid:"")})')
  sed -i.bak "s/REPLACE_WITH_D1_ID/$ID/" wrangler.toml && rm -f wrangler.toml.bak
fi
echo "▶ Применяю миграции"; npx wrangler d1 migrations apply jobhunter --remote

ask() { local name=$1 prompt=$2; read -r -s -p "$prompt (Enter — пропустить): " val; echo; [ -n "$val" ] && echo "$val" | npx wrangler secret put "$name" >/dev/null && echo "  ✓ $name"; }
ENGINE_TOKEN=$(node -e 'console.log(require("crypto").randomBytes(24).toString("hex"))')
echo "$ENGINE_TOKEN" | npx wrangler secret put ENGINE_TOKEN >/dev/null
ask ADMIN_PASSWORD "Придумайте пароль для панели"
ask TELEGRAM_BOT_TOKEN "Токен бота от @BotFather"
ask ANTHROPIC_API_KEY "Ключ Claude API (опционально, иначе бесплатный Workers AI)"
ask HH_CLIENT_ID "HH_CLIENT_ID с dev.hh.ru (опционально)"
ask HH_CLIENT_SECRET "HH_CLIENT_SECRET с dev.hh.ru (опционально)"
ask RESEND_API_KEY "Ключ Resend для email-откликов (опционально)"
ask VK_SERVICE_TOKEN "Сервисный ключ VK (опционально)"
ask GITHUB_TOKEN "GitHub fine-grained token с правом Actions: write (для кнопки «Запустить»)"

echo "▶ Деплой"; OUT=$(npx wrangler deploy 2>&1); echo "$OUT" | tail -5
URL=$(echo "$OUT" | grep -oE 'https://[a-z0-9.-]+\.workers\.dev' | head -1)
REPO=$(git config --get remote.origin.url | sed -E 's#.*github.com[:/]##; s#\.git$##')
echo "$REPO" | npx wrangler secret put GITHUB_REPO >/dev/null || true

cat <<MSG

✅ Готово! Панель: $URL

Осталось добавить в GitHub → Settings → Secrets and variables → Actions:
  WORKER_URL   = $URL
  ENGINE_TOKEN = $ENGINE_TOKEN
(или выполните:  gh secret set WORKER_URL -b "$URL" && gh secret set ENGINE_TOKEN -b "$ENGINE_TOKEN")

Затем: откройте панель → «Подключения» → «Настроить вебхук» → отправьте боту /start <пароль панели>.
MSG
