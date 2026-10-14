from __future__ import annotations

from datetime import date, datetime, timedelta

from optiondesk import clock
from optiondesk.config import Config, StrategyConfig
from optiondesk.models import Bar, OptionContract, Position, Right

MONDAY = date(2026, 5, 4)


def et(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=clock.ET)


def bar(
    symbol: str,
    start: datetime,
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
    volume: int = 1000,
) -> Bar:
    return Bar(
        symbol,
        start,
        close,
        high if high is not None else close,
        low if low is not None else close,
        close,
        volume,
    )


def contract(
    *,
    underlying: str = "SPY",
    expiration: date = MONDAY + timedelta(days=4),
    strike: float = 500.0,
    right: Right = Right.CALL,
    bid: float = 2.00,
    ask: float = 2.10,
    delta: float = 0.40,
    open_interest: int = 1000,
) -> OptionContract:
    from optiondesk.options import occ

    return OptionContract(
        symbol=occ.build(underlying, expiration, right, strike),
        underlying=underlying,
        expiration=expiration,
        strike=strike,
        right=right,
        bid=bid,
        ask=ask,
        delta=delta,
        open_interest=open_interest,
    )


def position(
    *,
    entry: float = 2.0,
    mark: float = 2.0,
    high: float | None = None,
    opened: datetime | None = None,
    c: OptionContract | None = None,
) -> Position:
    return Position(
        id="p1",
        strategy="test",
        contract=c or contract(),
        quantity=2,
        entry_price=entry,
        entry_time=opened or et(MONDAY, 10),
        mark=mark,
        high_water=high if high is not None else max(entry, mark),
    )


def config(**overrides: object) -> Config:
    cfg = Config(strategies=[StrategyConfig("ema_cross")])
    for key, value in overrides.items():
        setattr(cfg, key, value)
    cfg.validate()
    return cfg
