"""Core data types shared by every part of the platform."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class Right(StrEnum):
    CALL = "C"
    PUT = "P"


class Direction(StrEnum):
    """Which way a strategy expects the underlying to move."""

    LONG = "long"  # bullish: buy calls
    SHORT = "short"  # bearish: buy puts

    @property
    def right(self) -> Right:
        return Right.CALL if self is Direction.LONG else Right.PUT


class Side(StrEnum):
    BUY_TO_OPEN = "BUY_TO_OPEN"
    SELL_TO_CLOSE = "SELL_TO_CLOSE"


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar for an underlying."""

    symbol: str
    start: datetime  # timezone-aware, start of the bar
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


@dataclass(frozen=True)
class OptionContract:
    """A single listed option with its latest quote and Greeks."""

    symbol: str  # OCC symbol, e.g. "SPY260515C00500000"
    underlying: str
    expiration: date
    strike: float
    right: Right
    bid: float
    ask: float
    delta: float = 0.0
    implied_vol: float = 0.0
    open_interest: int = 0
    volume: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        """Bid/ask spread as a fraction of the mid price."""
        return (self.ask - self.bid) / self.mid if self.mid > 0 else float("inf")


@dataclass(frozen=True)
class Signal:
    """A strategy's request to open a position."""

    strategy: str
    symbol: str
    direction: Direction
    time: datetime
    underlying_price: float
    reason: str = ""
    confidence: float = 1.0  # 0..1; strategies may scale it, sizing may use it


@dataclass(frozen=True)
class Order:
    contract: OptionContract
    side: Side
    quantity: int
    limit_price: float | None = None  # None means market order
    tag: str = ""  # why the order exists, e.g. "entry" or "stop_loss"


@dataclass(frozen=True)
class Fill:
    order: Order
    price: float  # per share; one contract is 100 shares
    quantity: int
    time: datetime
    commission: float = 0.0
    broker_order_id: str = ""


@dataclass
class Position:
    """An open option position held by the platform."""

    id: str
    strategy: str
    contract: OptionContract
    quantity: int
    entry_price: float
    entry_time: datetime
    mark: float
    high_water: float  # best mark seen since entry, for trailing stops
    entry_commission: float = 0.0
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.quantity * 100

    @property
    def market_value(self) -> float:
        return self.mark * self.quantity * 100

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis

    @property
    def return_pct(self) -> float:
        return self.mark / self.entry_price - 1 if self.entry_price > 0 else 0.0


@dataclass(frozen=True)
class ClosedTrade:
    position_id: str
    strategy: str
    contract_symbol: str
    quantity: int
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    exit_reason: str
    commission: float

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.quantity * 100 - self.commission
