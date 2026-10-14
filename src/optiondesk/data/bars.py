"""Build fixed-interval bars from polled price snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .. import clock
from ..models import Bar


@dataclass
class _Building:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class BarAggregator:
    """Turns a stream of (price, cumulative day volume) snapshots into bars.

    Call :meth:`update` with every snapshot; it returns the bars that the
    snapshot completed (usually none, sometimes one per symbol).
    """

    def __init__(self, bar_seconds: int = 60) -> None:
        self.bar_seconds = bar_seconds
        self._building: dict[str, _Building] = {}
        self._last_volume: dict[str, tuple[date, int]] = {}

    def _bucket(self, moment: datetime) -> datetime:
        stamp = moment.timestamp()
        return datetime.fromtimestamp(stamp - stamp % self.bar_seconds, tz=moment.tzinfo)

    def _volume_delta(self, symbol: str, cumulative: int, moment: datetime) -> int:
        day = clock.session_date(moment)
        previous = self._last_volume.get(symbol)
        self._last_volume[symbol] = (day, cumulative)
        if previous is None or previous[0] != day:
            return 0
        return max(0, cumulative - previous[1])

    def update(self, symbol: str, price: float, cumulative_volume: int, moment: datetime) -> list[Bar]:
        if price <= 0:
            return []
        bucket = self._bucket(moment)
        volume = self._volume_delta(symbol, cumulative_volume, moment)
        done: list[Bar] = []
        current = self._building.get(symbol)
        if current is not None and current.start != bucket:
            done.append(self._finish(symbol, current))
            current = None
        if current is None:
            self._building[symbol] = _Building(bucket, price, price, price, price, volume)
        else:
            current.high = max(current.high, price)
            current.low = min(current.low, price)
            current.close = price
            current.volume += volume
        return done

    def flush(self) -> list[Bar]:
        """Complete every bar still being built (for example, at the close)."""
        done = [self._finish(symbol, b) for symbol, b in self._building.items()]
        self._building.clear()
        return sorted(done, key=lambda bar: (bar.start, bar.symbol))

    def _finish(self, symbol: str, b: _Building) -> Bar:
        return Bar(symbol, b.start, b.open, b.high, b.low, b.close, b.volume)
