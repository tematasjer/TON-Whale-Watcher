"""
db.py — простое хранение сделок в SQLite (без внешних зависимостей,
sqlite3 есть в стандартной библиотеке Python).

Зачем: чтобы не терять историю сделок и алертов при перезапуске бота,
и чтобы можно было делать простые SQL-запросы поверх реальных данных
(топ токенов по объёму, сколько было спайков за день и т.п.) —
а не только учебные примеры.

Специально сделано максимально просто: одна таблица, несколько функций.
"""

import sqlite3
import time

DB_PATH = "trades.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Создаёт таблицу trades, если её ещё нет."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            jetton TEXT NOT NULL,
            dex TEXT,
            ton_amount REAL NOT NULL,
            usd_amount REAL NOT NULL,
            is_spike_alert INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def save_trade(jetton: str, dex: str, ton_amount: float, usd_amount: float,
               ts: float | None = None, is_spike_alert: bool = False) -> None:
    """Сохраняет одну сделку в базу."""
    ts = ts if ts is not None else time.time()
    conn = get_connection()
    conn.execute(
        "INSERT INTO trades (ts, jetton, dex, ton_amount, usd_amount, is_spike_alert) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ts, jetton, dex, ton_amount, usd_amount, int(is_spike_alert)),
    )
    conn.commit()
    conn.close()


# ---------- Несколько простых запросов для аналитики ----------

def top_tokens_by_volume(hours: int = 24, limit: int = 10) -> list[sqlite3.Row]:
    """Топ токенов по суммарному объёму в USD за последние N часов."""
    cutoff = time.time() - hours * 3600
    conn = get_connection()
    rows = conn.execute("""
        SELECT jetton, COUNT(*) AS trades_count, SUM(usd_amount) AS total_usd
        FROM trades
        WHERE ts >= ?
        GROUP BY jetton
        ORDER BY total_usd DESC
        LIMIT ?
    """, (cutoff, limit)).fetchall()
    conn.close()
    return rows


def spikes_count_last_days(days: int = 7) -> list[sqlite3.Row]:
    """Сколько алертов о всплеске объёма было по каждому токену за N дней."""
    cutoff = time.time() - days * 86400
    conn = get_connection()
    rows = conn.execute("""
        SELECT jetton, COUNT(*) AS spikes
        FROM trades
        WHERE is_spike_alert = 1 AND ts >= ?
        GROUP BY jetton
        ORDER BY spikes DESC
    """, (cutoff,)).fetchall()
    conn.close()
    return rows


if __name__ == "__main__":
    # Небольшая самопроверка модуля на тестовых данных
    init_db()
    save_trade("TESTCOIN", "STON.fi", ton_amount=200, usd_amount=1100)
    save_trade("TESTCOIN", "DeDust", ton_amount=50, usd_amount=275, is_spike_alert=True)
    print("Топ токенов за 24ч:")
    for row in top_tokens_by_volume(hours=24):
        print(dict(row))
    print("Спайки за 7 дней:")
    for row in spikes_count_last_days(days=7):
        print(dict(row))
