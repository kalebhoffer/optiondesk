from __future__ import annotations

from datetime import timedelta

import pytest

from optiondesk.config import ExitConfig, RiskConfig, SizingConfig
from optiondesk.exits import exit_reason
from optiondesk.models import Direction, Fill, Order, Side, Signal
from optiondesk.portfolio import Portfolio, StrategyStats
from optiondesk.risk import RiskEngine
from optiondesk.sizing import kelly_fraction, size_position

from .helpers import MONDAY, contract, et, position

NOON = et(MONDAY, 12)


def signal(symbol: str = "SPY") -> Signal:
    return Signal("test", symbol, Direction.LONG, NOON, 500.0)


def open_position(p: Portfolio, *, symbol: str = "SPY", price: float = 2.0, qty: int = 1) -> str:
    c = contract(underlying=symbol)
    fill = Fill(Order(c, Side.BUY_TO_OPEN, qty), price, qty, NOON)
    return p.open(fill, "test", NOON).id


# ── risk ──


def test_approves_with_budget() -> None:
    p = Portfolio(10_000)
    p.roll_day(NOON)
    d = RiskEngine(RiskConfig(max_risk_per_trade_pct=2)).check(signal(), p, NOON)
    assert d.approved and d.budget == pytest.approx(200)


@pytest.mark.parametrize(
    ("moment", "reason"),
    [
        (et(MONDAY, 8), "market closed"),
        (et(MONDAY + timedelta(days=5), 12), "market closed"),  # Saturday
        (et(MONDAY, 9, 32), "too soon"),
        (et(MONDAY, 15, 45), "too close"),
    ],
)
def test_time_gates(moment, reason) -> None:  # type: ignore[no-untyped-def]
    p = Portfolio(10_000)
    assert reason in RiskEngine(RiskConfig()).check(signal(), p, moment).reason


def test_daily_loss_limit() -> None:
    p = Portfolio(10_000)
    p.roll_day(NOON)
    p.cash -= 600  # down 6% on the day
    assert "daily loss" in RiskEngine(RiskConfig(daily_loss_limit_pct=5)).check(signal(), p, NOON).reason


def test_position_limits_and_duplicates() -> None:
    p = Portfolio(100_000)
    p.roll_day(NOON)
    open_position(p, symbol="SPY")
    risk = RiskEngine(RiskConfig(max_open_positions=2))
    assert "already holding SPY" in risk.check(signal("SPY"), p, NOON).reason
    open_position(p, symbol="QQQ")
    assert "max open positions" in risk.check(signal("IWM"), p, NOON).reason


def test_cooldown() -> None:
    p = Portfolio(10_000)
    p.last_exit["SPY"] = NOON - timedelta(minutes=5)
    risk = RiskEngine(RiskConfig(cooldown_minutes=15))
    assert "cooling down" in risk.check(signal(), p, NOON).reason
    p.last_exit["SPY"] = NOON - timedelta(minutes=20)
    assert risk.check(signal(), p, NOON).approved


def test_exposure_limit_shrinks_budget_then_blocks() -> None:
    p = Portfolio(10_000)
    open_position(p, symbol="QQQ", price=8.0, qty=2)  # $1,600 premium
    risk = RiskEngine(RiskConfig(max_risk_per_trade_pct=5, max_total_exposure_pct=20))
    d = risk.check(signal(), p, NOON)
    assert d.approved and d.budget == pytest.approx(10_000 * 0.2 - 1_600, abs=1)
    open_position(p, symbol="IWM", price=4.0, qty=1)
    assert "exposure" in risk.check(signal(), p, NOON).reason


def test_manual_halt() -> None:
    risk = RiskEngine(RiskConfig())
    risk.halted_reason = "testing"
    assert "halted" in risk.check(signal(), Portfolio(10_000), NOON).reason


# ── sizing ──

NO_HISTORY = StrategyStats(0, 0, 0, 0)


def test_fixed_fraction() -> None:
    s = size_position(
        budget=500, equity=25_000, premium=2.0, confidence=1, stats=NO_HISTORY, rules=SizingConfig()
    )
    assert s.contracts == 2
    half = size_position(
        budget=500, equity=25_000, premium=2.0, confidence=0.5, stats=NO_HISTORY, rules=SizingConfig()
    )
    assert half.contracts == 1


def test_too_expensive_and_max_contracts() -> None:
    assert (
        size_position(
            budget=100, equity=1, premium=2.0, confidence=1, stats=NO_HISTORY, rules=SizingConfig()
        ).contracts
        == 0
    )
    capped = size_position(
        budget=10_000,
        equity=1,
        premium=0.5,
        confidence=1,
        stats=NO_HISTORY,
        rules=SizingConfig(max_contracts=3),
    )
    assert capped.contracts == 3


def test_kelly_math() -> None:
    assert kelly_fraction(StrategyStats(50, 0.5, 200, 100)) == pytest.approx(0.25)
    assert kelly_fraction(StrategyStats(50, 0.3, 100, 100)) == 0.0


def test_kelly_needs_history_then_applies() -> None:
    rules = SizingConfig(method="kelly", kelly_fraction=0.5, kelly_min_trades=20)
    young = size_position(
        budget=1_000,
        equity=10_000,
        premium=1.0,
        confidence=1,
        stats=StrategyStats(5, 0.1, 10, 100),
        rules=rules,
    )
    assert young.reason == "fixed fraction"
    losing = size_position(
        budget=1_000,
        equity=10_000,
        premium=1.0,
        confidence=1,
        stats=StrategyStats(30, 0.2, 100, 100),
        rules=rules,
    )
    assert losing.contracts == 0 and "no edge" in losing.reason
    edge = size_position(
        budget=1_000,
        equity=10_000,
        premium=1.0,
        confidence=1,
        stats=StrategyStats(30, 0.5, 200, 100),
        rules=rules,
    )
    # half-Kelly of 25% = 12.5% of equity = $1,250, capped at the $1,000 budget
    assert edge.contracts == 10


# ── exits ──

RULES = ExitConfig()


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"entry": 2.0, "mark": 1.39}, "stop_loss"),
        ({"entry": 2.0, "mark": 3.25}, "take_profit"),
        ({"entry": 2.0, "mark": 2.10, "high": 2.60}, "trailing_stop"),
        ({"entry": 2.0, "mark": 2.45, "high": 2.60}, None),  # trailing armed, not hit
        ({"entry": 2.0, "mark": 2.10}, None),
    ],
)
def test_price_exits(kwargs, expected) -> None:  # type: ignore[no-untyped-def]
    assert exit_reason(position(**kwargs, opened=NOON), NOON + timedelta(minutes=5), RULES) == expected


def test_time_exits() -> None:
    p = position(opened=et(MONDAY, 10))
    assert exit_reason(p, et(MONDAY, 13, 0), RULES) == "max_hold_time"
    late = position(opened=et(MONDAY, 15))
    assert exit_reason(late, et(MONDAY, 15, 46), RULES) == "end_of_day"


def test_same_day_expiration_is_not_closed_at_entry() -> None:
    p = position(opened=NOON, c=contract(expiration=MONDAY))
    assert exit_reason(p, NOON + timedelta(minutes=1), RULES) is None
    late = position(opened=et(MONDAY, 15), c=contract(expiration=MONDAY))
    assert exit_reason(late, et(MONDAY, 15, 50), RULES) == "end_of_day"


# ── portfolio ──


def test_cash_and_pnl_accounting() -> None:
    p = Portfolio(10_000)
    c = contract()
    pos = p.open(Fill(Order(c, Side.BUY_TO_OPEN, 2), 2.00, 2, NOON, commission=1.30), "test", NOON)
    assert p.cash == pytest.approx(10_000 - 400 - 1.30)
    assert p.equity == pytest.approx(10_000 - 1.30)

    p.mark({c.symbol: contract(bid=2.50, ask=2.60)})
    assert pos.mark == 2.50 and pos.high_water == 2.50
    assert pos.unrealized_pnl == pytest.approx(100)

    trade = p.close(pos.id, Fill(Order(c, Side.SELL_TO_CLOSE, 2), 2.50, 2, NOON, commission=1.30), "tp", NOON)
    assert trade.pnl == pytest.approx(100 - 2.60)
    assert p.cash == pytest.approx(10_000 + 100 - 2.60)
    assert not p.positions and p.last_exit["SPY"] == NOON


def test_partial_close() -> None:
    p = Portfolio(10_000)
    c = contract()
    pos = p.open(Fill(Order(c, Side.BUY_TO_OPEN, 4), 1.00, 4, NOON, commission=4.0), "test", NOON)
    trade = p.close(pos.id, Fill(Order(c, Side.SELL_TO_CLOSE, 4), 1.50, 1, NOON, commission=1.0), "x", NOON)
    assert trade.quantity == 1 and trade.commission == pytest.approx(2.0)
    assert p.positions[pos.id].quantity == 3
    assert p.positions[pos.id].entry_commission == pytest.approx(3.0)


def test_day_roll_resets_day_pnl() -> None:
    p = Portfolio(10_000)
    p.roll_day(NOON)
    p.cash += 500
    assert p.day_pnl == 500
    p.roll_day(NOON + timedelta(days=1))
    assert p.day_pnl == 0


def test_stats() -> None:
    p = Portfolio(10_000)
    for price in (3.0, 1.0, 3.0):
        c = contract()
        pos = p.open(Fill(Order(c, Side.BUY_TO_OPEN, 1), 2.0, 1, NOON), "s", NOON)
        p.close(pos.id, Fill(Order(c, Side.SELL_TO_CLOSE, 1), price, 1, NOON), "x", NOON)
    stats = p.stats("s")
    assert stats.trades == 3 and stats.win_rate == pytest.approx(2 / 3)
    assert stats.avg_win == pytest.approx(100) and stats.avg_loss == pytest.approx(100)
