"""A minimal in-process publish/subscribe bus.

The engine publishes everything it does (signals, risk decisions, fills,
closed trades) as events. The journal and dashboard subscribe to them, and
you can add your own subscribers for alerts, metrics or logging without
touching the engine.
"""

from __future__ import annotations

import inspect
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

Handler = Callable[["Event"], Awaitable[None] | None]


@dataclass(frozen=True)
class Event:
    topic: str  # e.g. "signal", "risk.rejected", "fill", "trade.closed"
    time: datetime
    data: dict[str, Any] = field(default_factory=dict)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> None:
        """Register ``handler`` for ``topic``; use ``"*"`` for every event."""
        self._handlers[topic].append(handler)

    async def publish(self, event: Event) -> None:
        """Deliver ``event`` to its subscribers.

        A failing subscriber is logged and skipped; it never stops trading.
        """
        for handler in (*self._handlers.get(event.topic, ()), *self._handlers.get("*", ())):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("event handler failed for topic %s", event.topic)
