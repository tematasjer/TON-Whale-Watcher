"""
volume_tracker.py — модуль для volume-alerts.

Идея: помимо алертов на разовые крупные покупки, полезно отслеживать
АНОМАЛЬНЫЕ ВСПЛЕСКИ ОБЪЁМА торгов по конкретному токену — ситуацию,
когда за короткое окно прошло сильно больше сделок, чем обычно, даже
если каждая отдельная сделка не выглядит крупной. Часто именно это —
первый признак, что в токен зашли деньги (или начался памп/дамп).

Подход:
1. Каждая сделка (своп) записывается в pandas DataFrame: время, токен, сумма в USD.
2. Для каждого токена считаем объём за последнее короткое окно (RECENT_WINDOW)
   и сравниваем с объёмом за предыдущее "базовое" окно (BASELINE_WINDOW) —
   это нужно, чтобы отличить "у токена всегда высокий объём" от "объём вырос".
3. Если recent_volume >= SPIKE_MULTIPLIER * baseline_volume_per_window
   (с поправкой на длину окон) — считаем это всплеском и шлём алерт.
4. Чтобы не спамить одним и тем же алертом каждые 20 секунд, храним время
   последнего алерта по токену и не повторяем его чаще COOLDOWN_MINUTES.

Модуль не зависит от TonAPI напрямую — на вход просто подаются сделки
(jetton, usd_amount, timestamp), это позволяет использовать его для любого
источника данных: TON, Solana-мемпады и т.п.
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass

import pandas as pd

log = logging.getLogger("volume_tracker")

RECENT_WINDOW_MIN = 5        # "недавнее" окно, в котором ищем всплеск
BASELINE_WINDOW_MIN = 60     # "базовое" окно для расчёта нормального объёма
SPIKE_MULTIPLIER = 3.0       # во сколько раз объём должен вырасти, чтобы это был спайк
MIN_BASELINE_USD = 50.0      # если базовый объём почти нулевой, не считаем спайком любую мелочь
COOLDOWN_MINUTES = 15        # не повторять алерт по одному токену чаще, чем раз в N минут
MAX_HISTORY_MIN = BASELINE_WINDOW_MIN + RECENT_WINDOW_MIN + 5  # сколько истории хранить


@dataclass
class SpikeAlert:
    jetton: str
    recent_volume_usd: float
    baseline_avg_usd: float
    multiplier: float


class VolumeTracker:
    """Хранит историю сделок и умеет находить аномальные всплески объёма."""

    def __init__(self) -> None:
        # trades: список dict с полями ts (unix time), jetton, usd_amount
        self._trades: list[dict] = []
        self._last_alert_ts: dict[str, float] = {}

    def add_trade(self, jetton: str, usd_amount: float, ts: float | None = None) -> None:
        ts = ts if ts is not None else time.time()
        self._trades.append({"ts": ts, "jetton": jetton, "usd_amount": usd_amount})
        self._prune_old(ts)

    def _prune_old(self, now: float) -> None:
        cutoff = now - MAX_HISTORY_MIN * 60
        if len(self._trades) > 5000 or (self._trades and self._trades[0]["ts"] < cutoff):
            self._trades = [t for t in self._trades if t["ts"] >= cutoff]

    def _as_dataframe(self) -> pd.DataFrame:
        if not self._trades:
            return pd.DataFrame(columns=["ts", "jetton", "usd_amount"])
        return pd.DataFrame(self._trades)

    def check_spikes(self, now: float | None = None) -> list[SpikeAlert]:
        """
        Проверяет все токены, по которым есть история, и возвращает список
        обнаруженных всплесков объёма (с учётом cooldown, чтобы не дублировать).
        """
        now = now if now is not None else time.time()
        df = self._as_dataframe()
        if df.empty:
            return []

        recent_cutoff = now - RECENT_WINDOW_MIN * 60
        baseline_cutoff = now - (RECENT_WINDOW_MIN + BASELINE_WINDOW_MIN) * 60

        alerts: list[SpikeAlert] = []

        for jetton, group in df.groupby("jetton"):
            recent = group[group["ts"] >= recent_cutoff]["usd_amount"].sum()

            baseline_slice = group[(group["ts"] >= baseline_cutoff) & (group["ts"] < recent_cutoff)]
            baseline_total = baseline_slice["usd_amount"].sum()
            # нормируем базовый объём на длину окна, равную RECENT_WINDOW_MIN,
            # чтобы сравнивать "объём за 5 минут сейчас" с "средним объёмом за 5 минут обычно"
            baseline_avg_per_window = baseline_total * (RECENT_WINDOW_MIN / BASELINE_WINDOW_MIN)
            baseline_avg_per_window = max(baseline_avg_per_window, MIN_BASELINE_USD)

            if recent >= baseline_avg_per_window * SPIKE_MULTIPLIER:
                last_alert = self._last_alert_ts.get(jetton, 0)
                if now - last_alert >= COOLDOWN_MINUTES * 60:
                    alerts.append(SpikeAlert(
                        jetton=jetton,
                        recent_volume_usd=recent,
                        baseline_avg_usd=baseline_avg_per_window,
                        multiplier=recent / baseline_avg_per_window if baseline_avg_per_window else 0.0,
                    ))
                    self._last_alert_ts[jetton] = now

        return alerts


def format_spike_alert(alert: SpikeAlert) -> str:
    return (
        f"📈 <b>Аномальный всплеск объёма</b>\n\n"
        f"Токен: <code>{alert.jetton}</code>\n"
        f"Объём за {RECENT_WINDOW_MIN} мин: ${alert.recent_volume_usd:,.0f}\n"
        f"Обычный объём за такое же окно: ${alert.baseline_avg_usd:,.0f}\n"
        f"Рост: x{alert.multiplier:.1f}"
    )
