"""The interface every broker implements."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Protocol

from ..models import Fill, Order


class Broker(Protocol):
    async def submit(self, order: Order, now: datetime) -> Fill | None:
        """Place ``order`` and wait for the outcome.

        Returns the fill (possibly for fewer contracts than ordered), or
        ``None`` if nothing filled. Any unfilled remainder must be cancelled
        before returning, so the platform never has orders it doesn't know about.
        """
        ...

    async def close(self) -> None: ...


def snap_to_tick(price: float, *, round_up: bool) -> float:
    """Round an option price to a valid increment ($0.05 under $3, $0.10 above).

    Penny-pilot symbols allow $0.01, but the coarser ticks are valid for every
    symbol. Buys round up and sells round down so the limit stays marketable.
    """
    tick = 0.05 if price < 3.0 else 0.10
    steps = round(price / tick, 6)  # absorb float error: 1.15 / 0.05 = 22.999...
    whole = math.ceil(steps) if round_up else math.floor(steps)
    return round(max(tick, whole * tick), 2)
