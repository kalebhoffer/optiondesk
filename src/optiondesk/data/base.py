"""The interface every market-data source implements."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Protocol

from ..models import Bar, OptionContract


class MarketData(Protocol):
    def bars(self) -> AsyncIterator[Bar]:
        """Yield completed bars for every configured symbol, in time order."""
        ...

    async def option_chain(self, symbol: str, now: datetime) -> list[OptionContract]:
        """Current option chain for ``symbol``, with quotes and deltas."""
        ...

    async def refresh(self, contracts: Sequence[OptionContract], now: datetime) -> dict[str, OptionContract]:
        """Fresh quotes for contracts already held, keyed by OCC symbol.

        Contracts that could not be quoted are left out of the result.
        """
        ...

    async def close(self) -> None: ...
