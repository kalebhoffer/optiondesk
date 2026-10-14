"""Pre-trade risk checks. Every signal passes through these before any order."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import clock
from .config import RiskConfig
from .models import Signal
from .portfolio import Portfolio


@dataclass(frozen=True)
class Decision:
    approved: bool
    reason: str = ""
    budget: float = 0.0  # dollars of premium this trade may spend

    @classmethod
    def reject(cls, reason: str) -> Decision:
        return cls(False, reason)


class RiskEngine:
    """Applies the configured gates in order and returns the first failure.

    Gates: market hours, daily loss limit, max open positions, one position
    per underlying, per-symbol cooldown, and total exposure. An approved
    decision carries the premium budget for the trade: the smaller of the
    per-trade risk limit and the exposure still available.
    """

    def __init__(self, rules: RiskConfig) -> None:
        self.rules = rules
        self.halted_reason: str | None = None  # set to stop all new entries manually

    def check(self, signal: Signal, portfolio: Portfolio, now: datetime) -> Decision:
        r = self.rules
        if self.halted_reason:
            return Decision.reject(f"halted: {self.halted_reason}")
        if not clock.is_market_open(now):
            return Decision.reject("market closed")
        if clock.minutes_since_open(now) < r.no_entries_first_minutes:
            return Decision.reject("too soon after the open")
        if clock.minutes_to_close(now) < r.no_entries_last_minutes:
            return Decision.reject("too close to the close")

        start = portfolio.day_start_equity
        if start > 0 and portfolio.day_pnl / start * 100 <= -r.daily_loss_limit_pct:
            return Decision.reject(f"daily loss limit ({r.daily_loss_limit_pct}%) reached")
        if len(portfolio.positions) >= r.max_open_positions:
            return Decision.reject(f"already at max open positions ({r.max_open_positions})")
        if portfolio.holds(signal.symbol):
            return Decision.reject(f"already holding {signal.symbol}")

        last_exit = portfolio.last_exit.get(signal.symbol)
        if last_exit is not None and (now - last_exit).total_seconds() / 60 < r.cooldown_minutes:
            return Decision.reject(f"{signal.symbol} cooling down after last exit")

        equity = portfolio.equity
        per_trade = equity * r.max_risk_per_trade_pct / 100
        room = equity * r.max_total_exposure_pct / 100 - portfolio.exposure
        budget = min(per_trade, room)
        if budget <= 0:
            return Decision.reject(f"total exposure limit ({r.max_total_exposure_pct}%) reached")
        return Decision(True, budget=budget)
