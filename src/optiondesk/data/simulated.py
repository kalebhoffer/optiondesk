"""Simulated market data for demos, paper trading without a broker, and backtests.

Underlying prices either come from a random walk (:class:`SyntheticMarketData`)
or from a CSV of historical bars (:class:`CsvMarketData`). In both cases
option prices are *modeled* with Black-Scholes at a fixed implied
volatility. That is good enough to exercise the whole pipeline, but it is not
a substitute for historical option data when judging a strategy.
"""

from __future__ import annotations

import asyncio
import csv
import math
import random
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import clock
from ..models import Bar, OptionContract, Right
from ..options import occ, pricing


class OptionModel:
    """Builds a realistic-looking chain around the last underlying price."""

    def __init__(self, implied_vol: float = 0.20, strikes_each_side: int = 15) -> None:
        self.implied_vol = implied_vol
        self.strikes_each_side = strikes_each_side
        self.last_price: dict[str, float] = {}

    def update(self, bar: Bar) -> None:
        self.last_price[bar.symbol] = bar.close

    def chain(self, symbol: str, now: datetime, max_days: int = 21) -> list[OptionContract]:
        spot = self.last_price.get(symbol)
        if spot is None:
            return []
        step = _strike_step(spot)
        center = round(spot / step) * step
        today = clock.session_date(now)
        out: list[OptionContract] = []
        for days in range(max_days + 1):
            expiration = today + timedelta(days=days)
            if expiration.weekday() >= 5:
                continue
            for i in range(-self.strikes_each_side, self.strikes_each_side + 1):
                strike = round(center + i * step, 2)
                if strike <= 0:
                    continue
                for right in (Right.CALL, Right.PUT):
                    out.append(self.quote(symbol, expiration, strike, right, now))
        return out

    def quote(
        self, symbol: str, expiration: date, strike: float, right: Right, now: datetime
    ) -> OptionContract:
        spot = self.last_price[symbol]
        years = _years_to_expiry(expiration, now)
        theo = pricing.price(spot, strike, years, self.implied_vol, right)
        half_spread = max(0.01, theo * 0.015)
        bid = max(0.0, round(theo - half_spread, 2))
        ask = round(max(theo + half_spread, bid + 0.01), 2)
        return OptionContract(
            symbol=occ.build(symbol, expiration, right, strike),
            underlying=symbol,
            expiration=expiration,
            strike=strike,
            right=right,
            bid=bid,
            ask=ask,
            delta=pricing.delta(spot, strike, years, self.implied_vol, right),
            implied_vol=self.implied_vol,
            open_interest=1_000,
            volume=500,
        )

    def refresh(self, contracts: Sequence[OptionContract], now: datetime) -> dict[str, OptionContract]:
        return {
            c.symbol: self.quote(c.underlying, c.expiration, c.strike, c.right, now)
            for c in contracts
            if c.underlying in self.last_price
        }


def _strike_step(spot: float) -> float:
    if spot < 25:
        return 0.5
    if spot < 600:
        return 1.0
    if spot < 1000:
        return 5.0
    return 10.0


def _years_to_expiry(expiration: date, now: datetime) -> float:
    expires = datetime.combine(expiration, clock.CLOSE, tzinfo=clock.ET)
    return max(0.0, (expires - now).total_seconds() / (365 * 24 * 3600))


class SyntheticMarketData:
    """Random-walk minute bars during market hours, with modeled options.

    Deterministic for a given seed, so runs are reproducible.
    """

    def __init__(
        self,
        symbols: Sequence[str],
        *,
        start: date,
        days: int = 5,
        bar_seconds: int = 60,
        start_price: float = 500.0,
        annual_vol: float = 0.18,
        implied_vol: float = 0.20,
        seed: int = 7,
        speed: float = 0.0,
    ) -> None:
        self.symbols = list(symbols)
        self.start = start
        self.days = days
        self.bar_seconds = bar_seconds
        self.start_price = start_price
        self.annual_vol = annual_vol
        self.speed = speed
        self.rng = random.Random(seed)
        self.options = OptionModel(implied_vol)

    def generate(self) -> Iterator[Bar]:
        """All bars, synchronously. ``bars()`` streams the same sequence."""
        prices = {s: self.start_price * (1 + 0.1 * i) for i, s in enumerate(self.symbols)}
        step_years = self.bar_seconds / (252 * 6.5 * 3600)
        sigma = self.annual_vol * math.sqrt(step_years)
        for day in _weekdays(self.start, self.days):
            # A gentle per-day drift so trends exist for strategies to find.
            drift = {s: self.rng.gauss(0, sigma * 0.15) for s in self.symbols}
            moment = datetime.combine(day, clock.OPEN, tzinfo=clock.ET)
            close = datetime.combine(day, clock.CLOSE, tzinfo=clock.ET)
            while moment < close:
                for symbol in self.symbols:
                    yield self._next_bar(symbol, prices, moment, sigma, drift[symbol])
                moment += timedelta(seconds=self.bar_seconds)

    async def bars(self) -> AsyncIterator[Bar]:
        last: datetime | None = None
        for bar in self.generate():
            if self.speed and last is not None and bar.start != last:
                await asyncio.sleep(self.speed)
            last = bar.start
            self.options.update(bar)
            yield bar

    def _next_bar(
        self, symbol: str, prices: dict[str, float], start: datetime, sigma: float, drift: float
    ) -> Bar:
        open_ = prices[symbol]
        path = [open_]
        for _ in range(4):
            path.append(path[-1] * math.exp(drift / 4 + self.rng.gauss(0, sigma / 2)))
        close = path[-1]
        prices[symbol] = close
        return Bar(
            symbol=symbol,
            start=start,
            open=round(open_, 2),
            high=round(max(path), 2),
            low=round(min(path), 2),
            close=round(close, 2),
            volume=self.rng.randint(50_000, 250_000),
        )

    async def option_chain(self, symbol: str, now: datetime) -> list[OptionContract]:
        return self.options.chain(symbol, now)

    async def refresh(self, contracts: Sequence[OptionContract], now: datetime) -> dict[str, OptionContract]:
        return self.options.refresh(contracts, now)

    async def close(self) -> None:
        return None


class CsvMarketData:
    """Replay historical bars from a CSV file, with modeled options.

    Expected columns: ``timestamp,symbol,open,high,low,close,volume``.
    Timestamps are ISO 8601; ones without a timezone are read as U.S. Eastern.
    """

    def __init__(self, path: str | Path, *, implied_vol: float = 0.20) -> None:
        self.path = Path(path)
        self.options = OptionModel(implied_vol)

    def _load(self) -> list[Bar]:
        rows: list[Bar] = []
        with self.path.open(newline="") as fh:
            for line, row in enumerate(csv.DictReader(fh), start=2):
                try:
                    start = datetime.fromisoformat(row["timestamp"])
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=clock.ET)
                    rows.append(
                        Bar(
                            symbol=row["symbol"].strip().upper(),
                            start=start,
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=int(float(row.get("volume") or 0)),
                        )
                    )
                except (KeyError, ValueError) as exc:
                    raise ValueError(f"{self.path}:{line}: bad row ({exc})") from None
        rows.sort(key=lambda b: (b.start, b.symbol))
        return rows

    async def bars(self) -> AsyncIterator[Bar]:
        for bar in self._load():
            self.options.update(bar)
            yield bar

    async def option_chain(self, symbol: str, now: datetime) -> list[OptionContract]:
        return self.options.chain(symbol, now)

    async def refresh(self, contracts: Sequence[OptionContract], now: datetime) -> dict[str, OptionContract]:
        return self.options.refresh(contracts, now)

    async def close(self) -> None:
        return None


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def write_sample_csv(path: str | Path, symbols: Sequence[str], start: date, days: int, seed: int = 7) -> int:
    """Write synthetic bars to ``path`` in the CSV format above. Returns the row count."""
    bars = list(SyntheticMarketData(symbols, start=start, days=days, seed=seed).generate())
    with Path(path).open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "symbol", "open", "high", "low", "close", "volume"])
        for b in bars:
            writer.writerow([b.start.isoformat(), b.symbol, b.open, b.high, b.low, b.close, b.volume])
    return len(bars)
