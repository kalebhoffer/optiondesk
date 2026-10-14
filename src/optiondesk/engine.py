"""The trading engine: one loop that runs every bar through the pipeline.

For each completed bar:

1. Re-quote open positions on that symbol and apply exit rules.
2. Give the bar to every strategy.
3. For each signal: risk checks -> contract selection -> sizing -> order.

Everything that happens is published on the event bus.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from . import clock
from .broker.base import Broker
from .bus import Event, EventBus
from .config import Config
from .data.base import MarketData
from .exits import exit_reason
from .models import Bar, ClosedTrade, Order, Position, Side, Signal
from .options.selector import select_contract
from .portfolio import Portfolio
from .risk import RiskEngine
from .sizing import size_position
from .strategies import Strategy, create, load_plugins

log = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        config: Config,
        data: MarketData,
        broker: Broker,
        *,
        bus: EventBus | None = None,
        strategies: list[Strategy] | None = None,
    ) -> None:
        self.config = config
        self.data = data
        self.broker = broker
        self.bus = bus or EventBus()
        self.portfolio = Portfolio(config.account.starting_capital)
        self.risk = RiskEngine(config.risk)
        if strategies is None:
            load_plugins(config.plugins)
            strategies = [create(s.name, s.params) for s in config.strategies if s.enabled]
        self.strategies = strategies
        self.bar_length = timedelta(seconds=config.bar_seconds)
        self.last_bar_time: datetime | None = None
        self.equity_curve: list[tuple[datetime, float]] = []

    async def run(self) -> None:
        """Process bars until the data source ends (backtests) or is cancelled (live)."""
        await self._publish(
            "engine.started",
            datetime.now().astimezone(),
            {
                "mode": self.config.mode,
                "data": self.config.data,
                "strategies": [s.name for s in self.strategies],
            },
        )
        try:
            async for bar in self.data.bars():
                await self.on_bar(bar)
        finally:
            await self._publish(
                "engine.stopped",
                self.last_bar_time or datetime.now().astimezone(),
                {
                    "open_positions": len(self.portfolio.positions),
                },
            )

    async def on_bar(self, bar: Bar) -> None:
        now = bar.start + self.bar_length  # the bar is complete at its end
        self.last_bar_time = now
        self.portfolio.roll_day(now)
        await self._manage_positions(bar.symbol, now)
        for strategy in self.strategies:
            try:
                signal = strategy.on_bar(bar)
            except Exception:
                log.exception("strategy %s failed on %s", strategy.name, bar.symbol)
                continue
            if signal is not None:
                await self._handle_signal(signal, now)
        self.equity_curve.append((now, self.portfolio.equity))

    # ── exits ──

    async def _manage_positions(self, symbol: str, now: datetime) -> None:
        held = [p for p in self.portfolio.positions.values() if p.contract.underlying == symbol]
        if not held:
            return
        try:
            quotes = await self.data.refresh([p.contract for p in held], now)
        except Exception as exc:
            log.warning("could not re-quote %s positions: %s", symbol, exc)
            quotes = {}
        self.portfolio.mark(quotes)
        for position in held:
            if position.contract.symbol not in quotes:
                continue  # never exit on a stale quote; try again next bar
            reason = exit_reason(position, now, self.config.exits)
            if reason:
                await self.close_position(position, reason, now)

    async def close_position(self, position: Position, reason: str, now: datetime) -> ClosedTrade | None:
        order = Order(position.contract, Side.SELL_TO_CLOSE, position.quantity, None, reason)
        fill = await self._submit(order, now)
        if fill is None:
            await self._publish("exit.unfilled", now, {"position": position.id, "reason": reason})
            return None
        trade = self.portfolio.close(position.id, fill, reason, now)
        await self._publish(
            "trade.closed",
            now,
            {
                "position": trade.position_id,
                "strategy": trade.strategy,
                "contract": trade.contract_symbol,
                "quantity": trade.quantity,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "pnl": round(trade.pnl, 2),
                "reason": reason,
            },
        )
        return trade

    async def flatten(self, reason: str = "flatten") -> None:
        """Close every open position at the latest quotes."""
        now = self.last_bar_time or datetime.now().astimezone()
        for position in list(self.portfolio.positions.values()):
            await self.close_position(position, reason, now)

    # ── entries ──

    async def _handle_signal(self, signal: Signal, now: datetime) -> None:
        await self._publish(
            "signal",
            now,
            {
                "strategy": signal.strategy,
                "symbol": signal.symbol,
                "direction": signal.direction.value,
                "price": signal.underlying_price,
                "reason": signal.reason,
            },
        )
        decision = self.risk.check(signal, self.portfolio, now)
        if not decision.approved:
            await self._reject(signal, decision.reason, now)
            return

        try:
            chain = await self.data.option_chain(signal.symbol, now)
        except Exception as exc:
            await self._reject(signal, f"option chain unavailable: {exc}", now)
            return
        contract = select_contract(chain, signal.direction, clock.session_date(now), self.config.contracts)
        if contract is None:
            await self._reject(signal, "no contract met the liquidity and delta rules", now)
            return

        offset = self.config.execution.entry_limit_offset * (contract.ask - contract.bid) / 2
        limit = round(contract.mid + offset, 2)
        size = size_position(
            budget=decision.budget,
            equity=self.portfolio.equity,
            premium=limit,
            confidence=signal.confidence,
            stats=self.portfolio.stats(signal.strategy),
            rules=self.config.sizing,
        )
        if size.contracts < 1:
            await self._reject(signal, size.reason, now)
            return

        order = Order(contract, Side.BUY_TO_OPEN, size.contracts, limit, "entry")
        fill = await self._submit(order, now)
        if fill is None:
            await self._reject(signal, "entry order did not fill", now)
            return
        position = self.portfolio.open(fill, signal.strategy, now)
        await self._publish(
            "position.opened",
            now,
            {
                "position": position.id,
                "strategy": signal.strategy,
                "contract": contract.symbol,
                "quantity": fill.quantity,
                "price": fill.price,
                "delta": round(contract.delta, 3),
                "sizing": size.reason,
            },
        )

    async def _submit(self, order: Order, now: datetime) -> Any:
        try:
            fill = await self.broker.submit(order, now)
        except Exception as exc:
            log.exception("broker error on %s %s", order.side.value, order.contract.symbol)
            await self._publish("order.error", now, {"contract": order.contract.symbol, "error": str(exc)})
            return None
        if fill is not None:
            await self._publish(
                "fill",
                now,
                {
                    "contract": order.contract.symbol,
                    "side": order.side.value,
                    "quantity": fill.quantity,
                    "price": fill.price,
                    "tag": order.tag,
                    "broker_order_id": fill.broker_order_id,
                },
            )
        return fill

    async def _reject(self, signal: Signal, reason: str, now: datetime) -> None:
        await self._publish(
            "signal.rejected",
            now,
            {
                "strategy": signal.strategy,
                "symbol": signal.symbol,
                "reason": reason,
            },
        )

    async def _publish(self, topic: str, now: datetime, data: dict[str, Any]) -> None:
        await self.bus.publish(Event(topic, now, data))
