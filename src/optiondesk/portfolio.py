"""Cash, open positions, closed trades and P&L."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime

from . import clock
from .models import ClosedTrade, Fill, OptionContract, Position


@dataclass(frozen=True)
class StrategyStats:
    trades: int
    win_rate: float
    avg_win: float
    avg_loss: float  # positive number


class Portfolio:
    def __init__(self, starting_capital: float) -> None:
        self.starting_capital = starting_capital
        self.cash = starting_capital
        self.positions: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []
        self.last_exit: dict[str, datetime] = {}  # underlying -> time of last exit
        self.day: date | None = None
        self.day_start_equity = starting_capital

    # ── valuation ──

    @property
    def equity(self) -> float:
        return self.cash + sum(p.market_value for p in self.positions.values())

    @property
    def exposure(self) -> float:
        """Market value of all open option premium."""
        return sum(p.market_value for p in self.positions.values())

    @property
    def day_pnl(self) -> float:
        return self.equity - self.day_start_equity

    @property
    def realized_pnl(self) -> float:
        return sum(t.pnl for t in self.closed)

    def roll_day(self, now: datetime) -> None:
        today = clock.session_date(now)
        if today != self.day:
            self.day = today
            self.day_start_equity = self.equity

    # ── changes ──

    def open(self, fill: Fill, strategy: str, now: datetime) -> Position:
        contract = fill.order.contract
        position = Position(
            id=uuid.uuid4().hex[:12],
            strategy=strategy,
            contract=contract,
            quantity=fill.quantity,
            entry_price=fill.price,
            entry_time=now,
            mark=fill.price,
            high_water=fill.price,
            entry_commission=fill.commission,
        )
        self.cash -= fill.price * fill.quantity * 100 + fill.commission
        self.positions[position.id] = position
        return position

    def close(self, position_id: str, fill: Fill, reason: str, now: datetime) -> ClosedTrade:
        """Close all or part of a position. A partial fill leaves the rest open."""
        position = self.positions[position_id]
        quantity = min(fill.quantity, position.quantity)
        entry_commission = position.entry_commission * quantity / position.quantity
        trade = ClosedTrade(
            position_id=position.id,
            strategy=position.strategy,
            contract_symbol=position.contract.symbol,
            quantity=quantity,
            entry_price=position.entry_price,
            exit_price=fill.price,
            entry_time=position.entry_time,
            exit_time=now,
            exit_reason=reason,
            commission=entry_commission + fill.commission,
        )
        self.cash += fill.price * quantity * 100 - fill.commission
        if quantity == position.quantity:
            del self.positions[position_id]
        else:
            position.quantity -= quantity
            position.entry_commission -= entry_commission
        self.closed.append(trade)
        self.last_exit[position.contract.underlying] = now
        return trade

    def mark(self, quotes: Mapping[str, OptionContract]) -> None:
        """Update marks from fresh quotes (keyed by OCC symbol)."""
        for position in self.positions.values():
            quote = quotes.get(position.contract.symbol)
            if quote is None or quote.bid <= 0:
                continue
            position.contract = replace(position.contract, bid=quote.bid, ask=quote.ask, delta=quote.delta)
            # Mark at the bid: that is what the position could be sold for.
            position.mark = quote.bid
            position.high_water = max(position.high_water, position.mark)

    # ── queries ──

    def holds(self, underlying: str) -> bool:
        return any(p.contract.underlying == underlying for p in self.positions.values())

    def stats(self, strategy: str) -> StrategyStats:
        pnls = [t.pnl for t in self.closed if t.strategy == strategy]
        wins = [p for p in pnls if p > 0]
        losses = [-p for p in pnls if p <= 0]
        return StrategyStats(
            trades=len(pnls),
            win_rate=len(wins) / len(pnls) if pnls else 0.0,
            avg_win=sum(wins) / len(wins) if wins else 0.0,
            avg_loss=sum(losses) / len(losses) if losses else 0.0,
        )
