"""
TON Whale Watcher — Telegram-бот, который следит за крупными покупками
(свопами) на DEX в блокчейне TON (STON.fi, DeDust и др.) и присылает
алерт в Telegram, если сумма сделки превышает заданный порог в USD.

Источник данных: TonAPI (https://tonapi.io) — публичный индексатор блокчейна TON.
Отправка алертов: Telegram Bot API.

Как это работает (логика):
1. Раз в POLL_INTERVAL секунд скрипт запрашивает у TonAPI последние события
   (эндпоинт /v2/accounts/{address}/events) для адресов, которые мы "слушаем"
   (это могут быть роутеры DEX, конкретные пулы или адреса токенов).
2. Каждое событие может содержать несколько "действий" (actions). Нас
   интересуют действия типа "JettonSwap" — то есть покупка/продажа токена.
3. Для каждого свопа скрипт вычисляет сумму сделки в TON, переводит в USD
   по текущему курсу (эндпоинт /v2/rates) и сравнивает с порогом.
4. Если сумма выше порога — отправляет сообщение в Telegram-чат.
5. Чтобы не слать один и тот же алерт дважды, скрипт запоминает lt
   (logical time) последнего обработанного события для каждого адреса.

ВАЖНО ПРО ТОЧНОСТЬ ПОЛЕЙ API:
Структура action.JettonSwap в TonAPI может незначительно отличаться в
деталях (названия полей amount_in/amount_out и т.п.) в зависимости от
версии API. При первом запуске скрипт печатает "сырой" JSON первого
найденного JettonSwap-события в консоль (см. DEBUG_PRINT_FIRST_SWAP) —
это позволяет быстро свериться с реальным ответом и поправить функцию
parse_swap_action(), если что-то называется иначе.
"""

import os
import json
import time
import logging
import requests

from volume_tracker import VolumeTracker, format_spike_alert
import db

# ---------- КОНФИГУРАЦИЯ ----------
# Всё через переменные окружения, чтобы не хранить секреты в коде.

TONAPI_KEY = os.environ.get("TONAPI_KEY", "")  # ключ можно получить бесплатно на tonconsole.com
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Адреса, за которыми следим (роутеры/пулы DEX или конкретные жетоны).
# Пример: роутер STON.fi (mainnet). Можно указать несколько через запятую
# в переменной окружения WATCH_ACCOUNTS, либо прописать здесь напрямую.
DEFAULT_WATCH_ACCOUNTS = [
    "EQB3ncyBUTjZUA5EnFKR5_EnOMI9V1tTEAAPaiU71gc4TiUt",  # STON.fi Router v1 (пример, проверьте актуальный адрес)
]
WATCH_ACCOUNTS = [
    a.strip() for a in os.environ.get("WATCH_ACCOUNTS", "").split(",") if a.strip()
] or DEFAULT_WATCH_ACCOUNTS

THRESHOLD_USD = float(os.environ.get("THRESHOLD_USD", "1000"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "20"))  # секунд между опросами
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

TONAPI_BASE = "https://tonapi.io"
DEBUG_PRINT_FIRST_SWAP = True  # напечатать сырой JSON первого свопа для сверки полей

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ton_whale_watcher")

_printed_debug_sample = False


# ---------- РАБОТА С СОСТОЯНИЕМ (чтобы не дублировать алерты) ----------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("Не удалось прочитать state-файл, начинаем с нуля")
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ---------- ЗАПРОСЫ К TONAPI ----------

def tonapi_headers() -> dict:
    headers = {"Accept": "application/json"}
    if TONAPI_KEY:
        headers["Authorization"] = f"Bearer {TONAPI_KEY}"
    return headers


def get_ton_usd_rate() -> float:
    """Текущий курс TON/USD через /v2/rates."""
    url = f"{TONAPI_BASE}/v2/rates"
    params = {"tokens": "ton", "currencies": "usd"}
    resp = requests.get(url, params=params, headers=tonapi_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    # Ожидаемая форма: {"rates": {"TON": {"prices": {"USD": 5.42}}}}
    try:
        return float(data["rates"]["TON"]["prices"]["USD"])
    except (KeyError, TypeError, ValueError):
        log.error("Не удалось распарсить курс TON/USD, ответ: %s", data)
        raise


def get_account_events(account_id: str, after_lt: int | None, limit: int = 50) -> dict:
    """Получить последние события аккаунта через /v2/accounts/{id}/events."""
    url = f"{TONAPI_BASE}/v2/accounts/{account_id}/events"
    params = {"limit": limit, "sort_order": "asc"}
    if after_lt:
        params["after_lt"] = after_lt
    resp = requests.get(url, params=params, headers=tonapi_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


# ---------- ПАРСИНГ СВОПОВ ----------

def parse_swap_action(action: dict) -> dict | None:
    """
    Достаёт из action данные о свопе: сколько TON потрачено/получено.
    Возвращает dict с полями: ton_amount (float, в TON), description (str)
    либо None, если это не JettonSwap или сумму не удалось определить.

    ПРИМЕЧАНИЕ: если структура не совпадёт с реальным ответом API,
    здесь нужно будет поправить пути к полям — см. DEBUG_PRINT_FIRST_SWAP.
    """
    if action.get("type") != "JettonSwap":
        return None

    global _printed_debug_sample
    if DEBUG_PRINT_FIRST_SWAP and not _printed_debug_sample:
        log.info("Пример JettonSwap action (для сверки полей):\n%s",
                  json.dumps(action, indent=2, ensure_ascii=False))
        _printed_debug_sample = True

    details = action.get("JettonSwap", {})

    # TonAPI обычно отдаёт суммы в нанотонах (1 TON = 1e9), если сторона TON,
    # либо в минимальных единицах жеттона, если своп jetton->jetton.
    ton_nano = details.get("ton_in") or details.get("ton_out")
    if ton_nano is None:
        # Если свопа TON нет напрямую (jetton->jetton), пропускаем —
        # для оценки в USD нам проще смотреть именно TON-номинированные свопы.
        return None

    try:
        ton_amount = float(ton_nano) / 1e9
    except (TypeError, ValueError):
        return None

    dex = details.get("dex", "unknown DEX")

    # Пытаемся определить, какой именно жеттон участвует в свопе — нужно
    # для группировки объёма по токену в volume-alerts. Название поля может
    # отличаться в реальном ответе API (см. DEBUG_PRINT_FIRST_SWAP выше) —
    # пробуем несколько вариантов и в худшем случае помечаем как "unknown".
    jetton_info = details.get("jetton_master_in") or details.get("jetton_master_out") or {}
    jetton_symbol = (
        jetton_info.get("symbol")
        if isinstance(jetton_info, dict)
        else None
    ) or details.get("jetton_symbol") or "unknown"

    return {"ton_amount": ton_amount, "dex": dex, "jetton": jetton_symbol}


# ---------- TELEGRAM ----------

def send_telegram_alert(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram не настроен, алерт не отправлен:\n%s", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Ошибка отправки в Telegram: %s", e)


def format_alert(account_id: str, ton_amount: float, usd_amount: float, dex: str, event_id: str) -> str:
    return (
        f"🐋 <b>Крупная покупка на TON</b>\n\n"
        f"DEX: {dex}\n"
        f"Сумма: {ton_amount:,.2f} TON (~${usd_amount:,.0f})\n"
        f"Аккаунт: <code>{account_id}</code>\n"
        f"Событие: <code>{event_id}</code>"
    )


# ---------- ОСНОВНОЙ ЦИКЛ ----------

def main() -> None:
    log.info("Запуск TON Whale Watcher")
    log.info("Порог алерта: $%s | Слежу за: %s", THRESHOLD_USD, WATCH_ACCOUNTS)
    db.init_db()

    state = load_state()
    ton_usd_rate = None
    rate_last_updated = 0.0
    volume_tracker = VolumeTracker()

    while True:
        try:
            # Обновляем курс TON/USD не чаще раза в 60 секунд
            if time.time() - rate_last_updated > 60:
                ton_usd_rate = get_ton_usd_rate()
                rate_last_updated = time.time()
                log.info("Курс TON/USD обновлён: %.4f", ton_usd_rate)

            for account in WATCH_ACCOUNTS:
                after_lt = state.get(account)
                data = get_account_events(account, after_lt)
                events = data.get("events", [])

                for event in events:
                    event_lt = event.get("lt")
                    for action in event.get("actions", []):
                        swap = parse_swap_action(action)
                        if not swap:
                            continue
                        usd_amount = swap["ton_amount"] * ton_usd_rate

                        # 1) алерт на разовую крупную покупку
                        if usd_amount >= THRESHOLD_USD:
                            text = format_alert(
                                account, swap["ton_amount"], usd_amount,
                                swap["dex"], event.get("event_id", "?"),
                            )
                            log.info("АЛЕРТ (крупная покупка): %s", text.replace("\n", " | "))
                            send_telegram_alert(text)

                        # 2) записываем сделку для отслеживания объёма по токену
                        volume_tracker.add_trade(swap["jetton"], usd_amount)
                        db.save_trade(swap["jetton"], swap["dex"], swap["ton_amount"], usd_amount)

                    if event_lt:
                        state[account] = event_lt

            # 3) проверяем, не случился ли аномальный всплеск объёма по какому-то токену
            for spike in volume_tracker.check_spikes():
                text = format_spike_alert(spike)
                log.info("АЛЕРТ (объём): %s", text.replace("\n", " | "))
                send_telegram_alert(text)
                db.save_trade(spike.jetton, "aggregate", 0, spike.recent_volume_usd, is_spike_alert=True)

            save_state(state)

        except requests.RequestException as e:
            log.error("Ошибка запроса к TonAPI: %s", e)
        except Exception as e:
            log.exception("Неожиданная ошибка: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
