# Contributing

Thanks for helping. OptionDesk aims to stay small and readable, a codebase
people can understand in an afternoon and build on, so focused changes are
easier to accept than large frameworks.

## Setup

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

All four must pass; CI runs them on Python 3.11 to 3.13.

## Guidelines

- **Tests for every change.** Nothing in the test suite may need network access
  or a brokerage account; use `httpx.MockTransport` for HTTP.
- **Safety first.** Changes must not weaken the live-trading guard, the risk
  gates, or secret handling. Never log tokens or credentials.
- **Every `except Exception` must log.** Silent failures in trading code hide
  real losses.
- **No strategies that claim an edge.** Example strategies are welcome as
  teaching material; performance claims are not.

Good first contributions: a holiday calendar in `clock.py`, position recovery
from the broker on startup, another broker or data source behind the existing
interfaces, or historical option data for backtests.
