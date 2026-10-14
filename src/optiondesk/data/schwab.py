"""Live market data from Schwab: polled quotes aggregated into bars, plus option chains."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import clock
from ..config import ContractConfig
from ..models import Bar, OptionContract, Right
from ..options import occ
from ..schwab import SchwabClient, SchwabError
from .bars import BarAggregator

log = logging.getLogger(__name__)

_QUOTE_BATCH = 20  # Schwab's limit for symbols per /quotes request


class SchwabMarketData:
    def __init__(
        self,
        client: SchwabClient,
        symbols: Sequence[str],
        contracts: ContractConfig,
        *,
        bar_seconds: int = 60,
        poll_seconds: float = 5.0,
    ) -> None:
        self.client = client
        self.symbols = list(symbols)
        self.rules = contracts
        self.poll_seconds = poll_seconds
        self.aggregator = BarAggregator(bar_seconds)

    async def bars(self) -> AsyncIterator[Bar]:
        while True:
            now = datetime.now(UTC)
            if not clock.is_market_open(now):
                for bar in self.aggregator.flush():
                    yield bar
                await asyncio.sleep(30)
                continue
            try:
                quotes = await self._quotes(self.symbols)
            except SchwabError as exc:
                log.warning("quote poll failed: %s", exc)
                await asyncio.sleep(self.poll_seconds)
                continue
            completed: list[Bar] = []
            for symbol in self.symbols:
                quote = quotes.get(symbol, {}).get("quote", {})
                price = float(quote.get("lastPrice") or 0)
                volume = int(quote.get("totalVolume") or 0)
                completed.extend(self.aggregator.update(symbol, price, volume, now))
            for bar in sorted(completed, key=lambda b: (b.start, b.symbol)):
                yield bar
            await asyncio.sleep(self.poll_seconds)

    async def _quotes(self, symbols: Sequence[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for i in range(0, len(symbols), _QUOTE_BATCH):
            batch = symbols[i : i + _QUOTE_BATCH]
            data = await self.client.get_json("/marketdata/v1/quotes", {"symbols": ",".join(batch)})
            if isinstance(data, dict):
                out.update(data)
        return out

    async def option_chain(self, symbol: str, now: datetime) -> list[OptionContract]:
        today = clock.session_date(now)
        data = await self.client.get_json(
            "/marketdata/v1/chains",
            {
                "symbol": symbol,
                "contractType": "ALL",
                "strategy": "SINGLE",
                "strikeCount": 30,
                "fromDate": (today + timedelta(days=self.rules.min_dte)).isoformat(),
                "toDate": (today + timedelta(days=self.rules.max_dte)).isoformat(),
            },
        )
        return parse_chain(symbol, data)

    async def refresh(self, contracts: Sequence[OptionContract], now: datetime) -> dict[str, OptionContract]:
        by_schwab = {occ.to_schwab(c.symbol): c for c in contracts}
        quotes = await self._quotes(list(by_schwab))
        out: dict[str, OptionContract] = {}
        for schwab_symbol, contract in by_schwab.items():
            entry = quotes.get(schwab_symbol) or quotes.get(contract.symbol) or {}
            quote = entry.get("quote", {})
            bid = float(quote.get("bidPrice") or 0)
            ask = float(quote.get("askPrice") or 0)
            if bid > 0 and ask >= bid:
                out[contract.symbol] = replace(contract, bid=bid, ask=ask)
        return out

    async def close(self) -> None:
        await self.client.close()


def parse_chain(symbol: str, data: Any) -> list[OptionContract]:
    """Parse a ``/marketdata/v1/chains`` response into contracts.

    Rows without a two-sided quote or with an impossible delta are dropped.
    """
    if not isinstance(data, dict):
        return []
    out: list[OptionContract] = []
    for right, key in ((Right.CALL, "callExpDateMap"), (Right.PUT, "putExpDateMap")):
        for exp_key, strikes in (data.get(key) or {}).items():
            expiration = datetime.strptime(exp_key.split(":")[0], "%Y-%m-%d").date()
            for strike_key, rows in strikes.items():
                for row in rows:
                    try:
                        bid = float(row.get("bid") or 0)
                        ask = float(row.get("ask") or 0)
                        delta = float(row.get("delta") or 0)
                    except (TypeError, ValueError):
                        continue
                    if bid <= 0 or ask < bid or not -1.0 <= delta <= 1.0:
                        continue
                    strike = float(strike_key)
                    # Use Schwab's own symbol: index options (SPXW) have a
                    # different root than the underlying.
                    raw = str(row.get("symbol") or "").replace(" ", "")
                    out.append(
                        OptionContract(
                            symbol=raw or occ.build(symbol, expiration, right, strike),
                            underlying=symbol,
                            expiration=expiration,
                            strike=strike,
                            right=right,
                            bid=bid,
                            ask=ask,
                            delta=delta,
                            implied_vol=float(row.get("volatility") or 0) / 100,
                            open_interest=int(row.get("openInterest") or 0),
                            volume=int(row.get("totalVolume") or 0),
                        )
                    )
    return out
