"""Live order execution through the Schwab Trader API.

This places real orders with real money. The engine only constructs it when
``mode = "live"`` *and* ``OPTIONDESK_LIVE_TRADING=yes-i-understand`` is set.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from ..models import Fill, Order, Side
from ..options import occ
from ..schwab import SchwabClient, SchwabError
from .base import snap_to_tick

log = logging.getLogger(__name__)

_TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED"}


class SchwabBroker:
    def __init__(
        self,
        client: SchwabClient,
        *,
        account_number: str | None = None,
        commission_per_contract: float = 0.65,
        fill_timeout: float = 30.0,
        poll_interval: float = 1.0,
    ) -> None:
        self.client = client
        self.account_number = account_number
        self.commission_per_contract = commission_per_contract
        self.fill_timeout = fill_timeout
        self.poll_interval = poll_interval
        self.account_hash: str | None = None

    async def connect(self) -> None:
        """Look up the account's hash, which Schwab uses in place of the account number."""
        accounts = await self.client.get_json("/trader/v1/accounts/accountNumbers")
        if not isinstance(accounts, list) or not accounts:
            raise SchwabError("no Schwab accounts are linked to this login")
        if self.account_number:
            match = [a for a in accounts if str(a.get("accountNumber")) == self.account_number]
            if not match:
                raise SchwabError("SCHWAB_ACCOUNT_NUMBER is not linked to this login")
            chosen = match[0]
        else:
            if len(accounts) > 1:
                raise SchwabError("several accounts are linked; set SCHWAB_ACCOUNT_NUMBER to pick one")
            chosen = accounts[0]
        self.account_hash = str(chosen["hashValue"])

    async def option_positions(self) -> list[dict[str, Any]]:
        """Option positions currently in the account (for startup warnings)."""
        data = await self.client.get_json(f"/trader/v1/accounts/{self._hash()}", {"fields": "positions"})
        positions = (data or {}).get("securitiesAccount", {}).get("positions", []) or []
        return [p for p in positions if (p.get("instrument") or {}).get("assetType") == "OPTION"]

    def _hash(self) -> str:
        if not self.account_hash:
            raise SchwabError("call connect() before trading")
        return self.account_hash

    @staticmethod
    def order_body(order: Order) -> dict[str, Any]:
        body: dict[str, Any] = {
            "orderType": "MARKET" if order.limit_price is None else "LIMIT",
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": order.side.value,
                    "quantity": order.quantity,
                    "instrument": {"symbol": occ.to_schwab(order.contract.symbol), "assetType": "OPTION"},
                }
            ],
        }
        if order.limit_price is not None:
            if order.limit_price <= 0:
                raise ValueError("limit price must be positive")
            price = snap_to_tick(order.limit_price, round_up=order.side is Side.BUY_TO_OPEN)
            body["price"] = f"{price:.2f}"
        return body

    async def submit(self, order: Order, now: datetime) -> Fill | None:
        if order.quantity <= 0:
            return None
        path = f"/trader/v1/accounts/{self._hash()}/orders"
        resp = await self.client.request("POST", path, json_body=self.order_body(order))
        if resp.status_code not in (200, 201):
            log.error("order rejected by Schwab (HTTP %s): %s", resp.status_code, resp.text[:300])
            return None
        order_id = resp.headers.get("Location", "").rstrip("/").rsplit("/", 1)[-1]
        if not order_id:
            log.error("Schwab accepted the order but returned no order id; check the account manually")
            return None

        status = await self._wait(path, order_id)
        if status.get("status") not in _TERMINAL:
            # Timed out: cancel the remainder, then read what actually filled.
            cancel = await self.client.request("DELETE", f"{path}/{order_id}")
            if cancel.status_code not in (200, 201, 204):
                log.error("could not cancel Schwab order %s (HTTP %s)", order_id, cancel.status_code)
            status = await self._wait(path, order_id, timeout=10.0)
        return self._to_fill(order, status, order_id, now)

    async def _wait(self, path: str, order_id: str, timeout: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (self.fill_timeout if timeout is None else timeout)
        status: dict[str, Any] = {}
        while True:
            try:
                status = await self.client.get_json(f"{path}/{order_id}")
            except SchwabError as exc:
                log.warning("order status check failed: %s", exc)
            if status.get("status") in _TERMINAL or time.monotonic() >= deadline:
                return status
            await asyncio.sleep(self.poll_interval)

    def _to_fill(self, order: Order, status: dict[str, Any], order_id: str, now: datetime) -> Fill | None:
        legs = [
            leg
            for activity in status.get("orderActivityCollection") or []
            for leg in activity.get("executionLegs") or []
        ]
        quantity = sum(int(float(leg.get("quantity", 0))) for leg in legs)
        if quantity <= 0:
            if status.get("status") == "FILLED":
                log.error("Schwab order %s FILLED without execution legs; check the account", order_id)
            return None
        price = sum(float(leg["price"]) * int(float(leg["quantity"])) for leg in legs) / quantity
        return Fill(
            order=order,
            price=price,
            quantity=quantity,
            time=now,
            commission=self.commission_per_contract * quantity,
            broker_order_id=order_id,
        )

    async def close(self) -> None:
        await self.client.close()
