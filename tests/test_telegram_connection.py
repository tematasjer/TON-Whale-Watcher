"""
test_telegram_connection.py — ручная проверка, что бот может отправить
сообщение в указанный чат. Не часть автоматических тестов (обращается
к реальному Telegram API), запускать вручную после настройки .env.

Использование:
    export TELEGRAM_BOT_TOKEN="..."
    export TELEGRAM_CHAT_ID="..."
    python tests/test_telegram_connection.py
"""

import os
import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

if not TOKEN or not CHAT_ID:
    raise SystemExit("Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в окружении")

url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
resp = requests.post(url, json={
    "chat_id": CHAT_ID,
    "text": "✅ TON Whale Watcher: тестовое сообщение, связь настроена корректно.",
})
resp.raise_for_status()
print("Сообщение отправлено успешно:", resp.json()["result"]["message_id"])
