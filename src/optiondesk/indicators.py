"""Incremental technical indicators. Each ``update`` takes one new value or bar."""

from __future__ import annotations

from datetime import date

from . import clock
from .models import Bar


class EMA:
    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be at least 1")
        self.period = period
        self.alpha = 2 / (period + 1)
        self.value: float | None = None
        self._seed: list[float] = []

    def update(self, x: float) -> float | None:
        """Returns ``None`` until ``period`` values have been seen (seeded with their mean)."""
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) < self.period:
                return None
            self.value = sum(self._seed) / len(self._seed)
            return self.value
        self.value += self.alpha * (x - self.value)
        return self.value


class RSI:
    """Wilder's relative strength index."""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self.value: float | None = None
        self._prev: float | None = None
        self._gains: list[float] = []
        self._losses: list[float] = []
        self._avg_gain = 0.0
        self._avg_loss = 0.0

    def update(self, close: float) -> float | None:
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if self.value is None and len(self._gains) < self.period:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self.period:
                return None
            self._avg_gain = sum(self._gains) / self.period
            self._avg_loss = sum(self._losses) / self.period
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period
        if self._avg_loss == 0:
            self.value = 100.0 if self._avg_gain > 0 else 50.0
        else:
            self.value = 100 - 100 / (1 + self._avg_gain / self._avg_loss)
        return self.value


class ATR:
    """Wilder's average true range."""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self.value: float | None = None
        self._prev_close: float | None = None
        self._ranges: list[float] = []

    def update(self, bar: Bar) -> float | None:
        if self._prev_close is None:
            true_range = bar.high - bar.low
        else:
            true_range = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._prev_close = bar.close
        if self.value is None:
            self._ranges.append(true_range)
            if len(self._ranges) < self.period:
                return None
            self.value = sum(self._ranges) / self.period
        else:
            self.value = (self.value * (self.period - 1) + true_range) / self.period
        return self.value


class VWAP:
    """Volume-weighted average price, reset at the start of each session."""

    def __init__(self) -> None:
        self.value: float | None = None
        self._day: date | None = None
        self._pv = 0.0
        self._volume = 0

    def update(self, bar: Bar) -> float | None:
        day = clock.session_date(bar.start)
        if day != self._day:
            self._day, self._pv, self._volume = day, 0.0, 0
        typical = (bar.high + bar.low + bar.close) / 3
        self._pv += typical * bar.volume
        self._volume += bar.volume
        self.value = self._pv / self._volume if self._volume else typical
        return self.value
