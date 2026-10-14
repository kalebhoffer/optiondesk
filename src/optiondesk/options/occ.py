"""OCC option symbols.

The canonical form used everywhere in OptionDesk has no padding:
``SPY260515C00500000`` (root, YYMMDD, C/P, strike x 1000 as 8 digits).
Schwab's quote and order endpoints want the root padded to 6 characters with
spaces, ``SPY   260515C00500000``; use :func:`to_schwab` for those.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from ..models import Right

_OCC_RE = re.compile(r"^([A-Z0-9.]{1,6})(\d{6})([CP])(\d{8})$")


def build(underlying: str, expiration: date, right: Right, strike: float) -> str:
    return f"{underlying}{expiration:%y%m%d}{right.value}{round(strike * 1000):08d}"


def parse(symbol: str) -> tuple[str, date, Right, float]:
    """``"SPY260515C00500000"`` -> ``("SPY", date(2026, 5, 15), Right.CALL, 500.0)``."""
    match = _OCC_RE.match(symbol.replace(" ", ""))
    if not match:
        raise ValueError(f"not an OCC option symbol: {symbol!r}")
    root, yymmdd, right, strike = match.groups()
    return (
        root,
        datetime.strptime(yymmdd, "%y%m%d").date(),
        Right(right),
        int(strike) / 1000,
    )


def to_schwab(symbol: str) -> str:
    root, expiration, right, strike = parse(symbol)
    return f"{root:<6}{expiration:%y%m%d}{right.value}{round(strike * 1000):08d}"
