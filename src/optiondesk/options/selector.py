"""Choose which option contract to trade for a signal."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from ..config import ContractConfig
from ..models import Direction, OptionContract


def select_contract(
    chain: Iterable[OptionContract],
    direction: Direction,
    today: date,
    rules: ContractConfig,
) -> OptionContract | None:
    """Pick the liquid contract closest to the target delta.

    Filters to the right side (calls for long, puts for short), the allowed
    days-to-expiration window, a maximum bid/ask spread and a minimum open
    interest, then prefers the nearest expiration and the delta closest to
    ``rules.target_delta``. Returns ``None`` when nothing qualifies.
    """
    right = direction.right
    candidates = [
        c
        for c in chain
        if c.right is right
        and rules.min_dte <= (c.expiration - today).days <= rules.max_dte
        and c.bid > 0
        and c.ask >= c.bid
        and c.spread_pct <= rules.max_spread_pct
        and c.open_interest >= rules.min_open_interest
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda c: (c.expiration, abs(abs(c.delta) - rules.target_delta)),
    )
