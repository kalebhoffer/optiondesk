"""Example strategy: opening range breakout."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .. import clock
from ..models import Bar, Direction, Signal
from .base import Strategy, register


@dataclass
class _Day:
    day: date
    high: float = float("-inf")
    low: float = float("inf")
    fired: set[Direction] = field(default_factory=set)


@register("opening_range")
class OpeningRange(Strategy):
    """Track the high and low of the first ``range_minutes`` of the session.

    After the range is set, signal long on the first close above its high and
    short on the first close below its low, at most once per direction per day.
    Ranges narrower than ``min_range_pct`` of price are ignored as noise.
    """

    @classmethod
    def defaults(cls) -> dict[str, Any]:
        return {"range_minutes": 15, "min_range_pct": 0.1}

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        self._days: dict[str, _Day] = {}

    def on_bar(self, bar: Bar) -> Signal | None:
        today = clock.session_date(bar.start)
        state = self._days.get(bar.symbol)
        if state is None or state.day != today:
            state = self._days[bar.symbol] = _Day(today)

        if clock.minutes_since_open(bar.start) < self.params["range_minutes"]:
            state.high = max(state.high, bar.high)
            state.low = min(state.low, bar.low)
            return None
        if state.high == float("-inf"):
            return None  # started mid-session; no range for today
        if (state.high - state.low) / bar.close * 100 < self.params["min_range_pct"]:
            return None

        if bar.close > state.high and Direction.LONG not in state.fired:
            direction, level = Direction.LONG, state.high
        elif bar.close < state.low and Direction.SHORT not in state.fired:
            direction, level = Direction.SHORT, state.low
        else:
            return None
        state.fired.add(direction)
        return Signal(
            strategy=self.name,
            symbol=bar.symbol,
            direction=direction,
            time=bar.start,
            underlying_price=bar.close,
            reason=f"closed {'above' if direction is Direction.LONG else 'below'} "
            f"{self.params['range_minutes']}-minute range ({level:.2f})",
        )
