"""Decide how many contracts to buy."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import SizingConfig
from .portfolio import StrategyStats


@dataclass(frozen=True)
class Size:
    contracts: int
    reason: str


def kelly_fraction(stats: StrategyStats) -> float:
    """Full-Kelly fraction ``W - (1 - W) / R`` from a strategy's closed trades.

    ``R`` is average win over average loss. Returns 0 when there is no edge
    or not enough information.
    """
    if stats.avg_loss <= 0 or stats.avg_win <= 0:
        return 0.0
    payoff = stats.avg_win / stats.avg_loss
    return max(0.0, stats.win_rate - (1 - stats.win_rate) / payoff)


def size_position(
    *,
    budget: float,
    equity: float,
    premium: float,
    confidence: float,
    stats: StrategyStats,
    rules: SizingConfig,
) -> Size:
    """Contracts to buy at ``premium`` per share, spending at most ``budget`` dollars.

    For long options the premium paid is the maximum loss, so the budget is
    the risk. ``fixed_fraction`` spends the budget scaled by the signal's
    confidence. ``kelly`` spends a fraction of the Kelly-optimal amount once a
    strategy has ``kelly_min_trades`` closed trades, never more than the
    budget, and nothing if its record shows no edge.
    """
    if premium <= 0 or budget <= 0:
        return Size(0, "no budget or premium")
    spend = budget * max(0.0, min(confidence, 1.0))
    reason = "fixed fraction"
    if rules.method == "kelly" and stats.trades >= rules.kelly_min_trades:
        kelly = kelly_fraction(stats) * rules.kelly_fraction
        if kelly <= 0:
            return Size(0, f"kelly: no edge after {stats.trades} trades")
        spend = min(spend, equity * kelly)
        reason = f"kelly {kelly:.1%}"
    contracts = min(math.floor(spend / (premium * 100)), rules.max_contracts)
    if contracts < 1:
        return Size(0, f"budget ${spend:,.0f} is less than one contract (${premium * 100:,.0f})")
    return Size(contracts, reason)
