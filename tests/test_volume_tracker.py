"""
test_volume_tracker.py — проверка логики детекции всплесков объёма
на синтетических данных, без обращения к TonAPI.

Запуск:
    python -m pytest tests/ -v
или просто:
    python tests/test_volume_tracker.py
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from volume_tracker import (
    VolumeTracker,
    RECENT_WINDOW_MIN,
    BASELINE_WINDOW_MIN,
    SPIKE_MULTIPLIER,
    COOLDOWN_MINUTES,
)


def _minutes_ago(now: float, minutes: float) -> float:
    return now - minutes * 60


def test_no_spike_on_flat_volume():
    """Ровный объём без всплесков не должен давать алертов."""
    now = time.time()
    tracker = VolumeTracker()

    # Стабильные сделки по $20 каждые 2 минуты в течение часа (baseline)
    for i in range(30):
        tracker.add_trade("FLATCOIN", usd_amount=20, ts=_minutes_ago(now, 60 - i * 2))

    alerts = tracker.check_spikes(now=now)
    assert alerts == [], f"Ожидали отсутствие алертов, получили: {alerts}"


def test_spike_detected_on_volume_surge():
    """Резкий рост объёма за последние 5 минут должен триггерить алерт."""
    now = time.time()
    tracker = VolumeTracker()

    # Baseline: скромный объём в течение часа перед недавним окном ($10 x 12 = $120/час)
    for i in range(12):
        tracker.add_trade("SPIKECOIN", usd_amount=10, ts=_minutes_ago(now, 10 + i * 5))

    # Всплеск: крупные сделки в последние 5 минут
    tracker.add_trade("SPIKECOIN", usd_amount=500, ts=_minutes_ago(now, 2))
    tracker.add_trade("SPIKECOIN", usd_amount=500, ts=_minutes_ago(now, 1))

    alerts = tracker.check_spikes(now=now)
    assert len(alerts) == 1, f"Ожидали один алерт, получили: {alerts}"
    assert alerts[0].jetton == "SPIKECOIN"
    assert alerts[0].multiplier >= SPIKE_MULTIPLIER


def test_cooldown_prevents_duplicate_alerts():
    """Повторный вызов check_spikes сразу после алерта не должен слать его снова."""
    now = time.time()
    tracker = VolumeTracker()

    tracker.add_trade("COOLCOIN", usd_amount=10, ts=_minutes_ago(now, 30))
    tracker.add_trade("COOLCOIN", usd_amount=1000, ts=_minutes_ago(now, 1))

    first_pass = tracker.check_spikes(now=now)
    assert len(first_pass) == 1

    # Проверяем сразу же снова — cooldown должен подавить повтор
    second_pass = tracker.check_spikes(now=now + 60)
    assert second_pass == [], "Cooldown должен был подавить повторный алерт"

    # После истечения cooldown алерт может сработать снова (если объём всё ещё высокий
    # относительно НОВОГО baseline — здесь просто проверяем, что подавления уже нет)
    later = now + COOLDOWN_MINUTES * 60 + 61
    tracker.add_trade("COOLCOIN", usd_amount=1000, ts=later - 30)
    third_pass = tracker.check_spikes(now=later)
    assert len(third_pass) == 1, "После окончания cooldown алерт должен снова быть возможен"


def test_low_baseline_does_not_trigger_on_tiny_amounts():
    """MIN_BASELINE_USD должен защищать от лже-алертов на почти нулевой истории."""
    now = time.time()
    tracker = VolumeTracker()

    # Всего одна маленькая сделка — истории почти нет
    tracker.add_trade("TINYCOIN", usd_amount=5, ts=_minutes_ago(now, 1))

    alerts = tracker.check_spikes(now=now)
    assert alerts == [], f"Маленькая сделка без истории не должна давать алерт: {alerts}"


if __name__ == "__main__":
    tests = [
        test_no_spike_on_flat_volume,
        test_spike_detected_on_volume_surge,
        test_cooldown_prevents_duplicate_alerts,
        test_low_baseline_does_not_trigger_on_tiny_amounts,
    ]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print("\nВсе тесты прошли успешно.")
