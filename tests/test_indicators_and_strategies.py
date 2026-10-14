from __future__ import annotations

from datetime import timedelta

import pytest

from optiondesk.indicators import ATR, EMA, RSI, VWAP
from optiondesk.models import Direction
from optiondesk.strategies import create, registered
from optiondesk.strategies.base import Strategy, register

from .helpers import MONDAY, bar, et

# ── indicators ──


def test_ema_seeds_with_mean_then_smooths() -> None:
    ema = EMA(3)
    assert ema.update(1) is None
    assert ema.update(2) is None
    assert ema.update(3) == 2.0
    assert ema.update(4) == pytest.approx(3.0)  # 2 + 0.5 * (4 - 2)


def test_rsi_extremes() -> None:
    up = RSI(5)
    for x in range(10):
        value = up.update(100 + x)
    assert value == 100.0
    flat = RSI(5)
    for _ in range(10):
        value = flat.update(100)
    assert value == 50.0


def test_atr() -> None:
    atr = ATR(2)
    t = et(MONDAY, 10)
    assert atr.update(bar("SPY", t, 100, high=101, low=99)) is None
    assert atr.update(bar("SPY", t, 100, high=104, low=100)) == pytest.approx(3.0)


def test_vwap_resets_each_session() -> None:
    vwap = VWAP()
    vwap.update(bar("SPY", et(MONDAY, 10), 100, volume=100))
    assert vwap.update(bar("SPY", et(MONDAY, 10, 1), 110, volume=100)) == pytest.approx(105)
    assert vwap.update(bar("SPY", et(MONDAY + timedelta(days=1), 10), 200, volume=10)) == 200


# ── registry ──


def test_builtins_registered() -> None:
    assert {"ema_cross", "opening_range"} <= set(registered())


def test_unknown_strategy_and_params() -> None:
    with pytest.raises(ValueError, match="unknown strategy"):
        create("nope", {})
    with pytest.raises(ValueError, match="unknown parameter"):
        create("ema_cross", {"speed": 3})
    with pytest.raises(ValueError, match="fast must be shorter"):
        create("ema_cross", {"fast": 30, "slow": 10})


def test_custom_strategy_registration() -> None:
    @register("test_always_long")
    class AlwaysLong(Strategy):
        def on_bar(self, b):  # type: ignore[no-untyped-def]
            return None

    assert isinstance(create("test_always_long", {}), AlwaysLong)
    with pytest.raises(ValueError, match="already registered"):
        register("test_always_long")(AlwaysLong)


# ── ema_cross ──


def test_ema_cross_signals_on_crossovers_only() -> None:
    strat = create(
        "ema_cross", {"fast": 2, "slow": 4, "rsi_period": 2, "rsi_overbought": 101, "rsi_oversold": -1}
    )
    prices = [100, 99, 98, 97, 96, 95, 97, 100, 103, 106, 104, 100, 96, 92]
    signals = []
    for i, p in enumerate(prices):
        s = strat.on_bar(bar("SPY", et(MONDAY, 10) + timedelta(minutes=i), p))
        if s:
            signals.append(s.direction)
    assert signals == [Direction.LONG, Direction.SHORT]


def test_ema_cross_rsi_filter_blocks_overbought_longs() -> None:
    strat = create("ema_cross", {"fast": 2, "slow": 4, "rsi_period": 2, "rsi_overbought": 50})
    prices = [100, 99, 98, 97, 96, 95, 97, 100, 103, 106]
    assert all(
        strat.on_bar(bar("SPY", et(MONDAY, 10) + timedelta(minutes=i), p)) is None
        for i, p in enumerate(prices)
    )


# ── opening_range ──


def _feed(strat: Strategy, day_offset: int, closes: list[float]) -> list[Direction]:
    out = []
    day = MONDAY + timedelta(days=day_offset)
    for i, c in enumerate(closes):
        s = strat.on_bar(bar("SPY", et(day, 9, 30) + timedelta(minutes=i), c, high=c + 0.1, low=c - 0.1))
        if s:
            out.append(s.direction)
    return out


def test_opening_range_breakouts_once_per_direction_per_day() -> None:
    strat = create("opening_range", {"range_minutes": 3, "min_range_pct": 0.0})
    # range 99.9-101.1, then up through, back down, up again, then down through
    fired = _feed(strat, 0, [100, 101, 100.5, 101.5, 100.5, 101.6, 99.0, 98.0])
    assert fired == [Direction.LONG, Direction.SHORT]
    assert _feed(strat, 1, [100, 101, 100.5, 101.5]) == [Direction.LONG]  # new day resets


def test_opening_range_ignores_tiny_ranges() -> None:
    strat = create("opening_range", {"range_minutes": 3, "min_range_pct": 5.0})
    assert _feed(strat, 0, [100, 100.1, 100, 103]) == []
