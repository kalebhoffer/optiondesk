"""Example strategy: fast/slow EMA crossover with an RSI filter."""

from __future__ import annotations

from typing import Any

from ..indicators import EMA, RSI
from ..models import Bar, Direction, Signal
from .base import Strategy, register


@register("ema_cross")
class EmaCross(Strategy):
    """Go long when the fast EMA crosses above the slow EMA, short on the opposite.

    Skips longs when RSI is already overbought and shorts when oversold.
    """

    @classmethod
    def defaults(cls) -> dict[str, Any]:
        return {"fast": 9, "slow": 21, "rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30}

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        if self.params["fast"] >= self.params["slow"]:
            raise ValueError("ema_cross: fast must be shorter than slow")
        self._state: dict[str, tuple[EMA, EMA, RSI]] = {}
        self._last_diff: dict[str, float] = {}

    def on_bar(self, bar: Bar) -> Signal | None:
        if bar.symbol not in self._state:
            self._state[bar.symbol] = (
                EMA(self.params["fast"]),
                EMA(self.params["slow"]),
                RSI(self.params["rsi_period"]),
            )
        fast_ema, slow_ema, rsi_ind = self._state[bar.symbol]
        fast = fast_ema.update(bar.close)
        slow = slow_ema.update(bar.close)
        rsi = rsi_ind.update(bar.close)
        if fast is None or slow is None or rsi is None:
            return None

        diff = fast - slow
        previous = self._last_diff.get(bar.symbol)
        self._last_diff[bar.symbol] = diff
        if previous is None:
            return None

        if previous <= 0 < diff and rsi < self.params["rsi_overbought"]:
            direction = Direction.LONG
        elif previous >= 0 > diff and rsi > self.params["rsi_oversold"]:
            direction = Direction.SHORT
        else:
            return None
        return Signal(
            strategy=self.name,
            symbol=bar.symbol,
            direction=direction,
            time=bar.start,
            underlying_price=bar.close,
            reason=f"EMA{self.params['fast']} crossed EMA{self.params['slow']} (RSI {rsi:.0f})",
        )
