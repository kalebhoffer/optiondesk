"""A small read-only dashboard and JSON API for a running engine.

Binds to 127.0.0.1 by default. If ``OPTIONDESK_API_KEY`` is set, every request
must send it as an ``X-API-Key`` header or a ``key`` query parameter. Put a
TLS reverse proxy in front before exposing it beyond your machine.
"""

from __future__ import annotations

import hmac
from collections import deque
from importlib import resources
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from .bus import Event
from .engine import Engine
from .journal import Journal

_PAGE = (resources.files(__package__) / "dashboard.html").read_text()


def create_app(engine: Engine, *, api_key: str | None = None, journal: Journal | None = None) -> FastAPI:
    app = FastAPI(title="OptionDesk", docs_url=None, redoc_url=None)
    recent: deque[dict[str, Any]] = deque(maxlen=200)

    def remember(event: Event) -> None:
        recent.appendleft({"time": event.time.isoformat(), "topic": event.topic, "data": event.data})

    engine.bus.subscribe("*", remember)

    def require_key(request: Request) -> None:
        if not api_key:
            return
        supplied = request.headers.get("x-api-key") or request.query_params.get("key") or ""
        if not hmac.compare_digest(supplied.encode(), api_key.encode()):
            raise HTTPException(status_code=401, detail="missing or wrong API key")

    def require_same_origin(request: Request) -> None:
        # Browsers won't let another site add a custom header without a CORS
        # preflight this app never approves, so this blocks cross-site POSTs.
        if request.headers.get("x-optiondesk") != "1":
            raise HTTPException(status_code=403, detail="missing X-OptionDesk: 1 header")

    guarded = [Depends(require_key)]
    controls = [Depends(require_key), Depends(require_same_origin)]

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "last_bar": engine.last_bar_time.isoformat() if engine.last_bar_time else None}

    @app.get("/api/status", dependencies=guarded)
    def status() -> dict[str, Any]:
        p = engine.portfolio
        return {
            "mode": engine.config.mode,
            "data": engine.config.data,
            "strategies": [s.name for s in engine.strategies],
            "equity": round(p.equity, 2),
            "cash": round(p.cash, 2),
            "day_pnl": round(p.day_pnl, 2),
            "realized_pnl": round(p.realized_pnl, 2),
            "exposure": round(p.exposure, 2),
            "open_positions": len(p.positions),
            "halted": engine.risk.halted_reason,
            "last_bar": engine.last_bar_time.isoformat() if engine.last_bar_time else None,
        }

    @app.get("/api/positions", dependencies=guarded)
    def positions() -> list[dict[str, Any]]:
        return [
            {
                "id": pos.id,
                "strategy": pos.strategy,
                "contract": pos.contract.symbol,
                "quantity": pos.quantity,
                "entry_price": pos.entry_price,
                "mark": pos.mark,
                "unrealized_pnl": round(pos.unrealized_pnl, 2),
                "return_pct": round(pos.return_pct * 100, 2),
                "opened": pos.entry_time.isoformat(),
            }
            for pos in engine.portfolio.positions.values()
        ]

    @app.get("/api/trades", dependencies=guarded)
    def trades(limit: int = 50) -> list[dict[str, Any]]:
        return [
            {
                "strategy": t.strategy,
                "contract": t.contract_symbol,
                "quantity": t.quantity,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": round(t.pnl, 2),
                "reason": t.exit_reason,
                "closed": t.exit_time.isoformat(),
            }
            for t in reversed(engine.portfolio.closed[-limit:])
        ]

    @app.get("/api/events", dependencies=guarded)
    def events(limit: int = 50) -> list[dict[str, Any]]:
        if journal is not None:
            return journal.recent(limit)
        return list(recent)[:limit]

    @app.post("/api/halt", dependencies=controls)
    def halt() -> dict[str, Any]:
        """Stop opening new positions. Open positions keep being managed."""
        engine.risk.halted_reason = "halted from dashboard"
        return {"halted": True}

    @app.post("/api/resume", dependencies=controls)
    def resume() -> dict[str, Any]:
        engine.risk.halted_reason = None
        return {"halted": False}

    @app.get("/", response_class=HTMLResponse, dependencies=guarded)
    def dashboard() -> str:
        return _PAGE

    return app
