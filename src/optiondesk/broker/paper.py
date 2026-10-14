"""Simulated broker. Fills are deliberately conservative."""

from __future__ import annotations

from datetime import datetime

from ..models import Fill, Order, Side


class PaperBroker:
    """Fills orders against the quote attached to the order's contract.

    * Market buys fill at the ask; market sells at the bid.
    * Limit buys fill at ``min(limit, ask)`` if the limit is at least the mid,
      otherwise not at all. Limit sells mirror this.

    Real fills between the mid and the far side are common for liquid
    contracts, so this is pessimistic for market orders and roughly fair for
    limits near the mid. It never models partial fills or queue position.
    """

    def __init__(self, commission_per_contract: float = 0.65) -> None:
        self.commission_per_contract = commission_per_contract
        self._next_id = 1

    async def submit(self, order: Order, now: datetime) -> Fill | None:
        c = order.contract
        if order.quantity <= 0 or c.bid <= 0 or c.ask < c.bid:
            return None
        buying = order.side is Side.BUY_TO_OPEN
        if order.limit_price is None:
            price = c.ask if buying else c.bid
        elif buying:
            if order.limit_price < c.mid:
                return None
            price = min(order.limit_price, c.ask)
        else:
            if order.limit_price > c.mid:
                return None
            price = max(order.limit_price, c.bid)
        order_id = f"paper-{self._next_id}"
        self._next_id += 1
        return Fill(
            order=order,
            price=price,
            quantity=order.quantity,
            time=now,
            commission=self.commission_per_contract * order.quantity,
            broker_order_id=order_id,
        )

    async def close(self) -> None:
        return None
