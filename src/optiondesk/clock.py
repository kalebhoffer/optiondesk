"""U.S. equity market hours.

Weekends are closed. Exchange holidays and early closes are *not* modeled;
add a holiday calendar here if you need them.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)


def to_et(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware")
    return moment.astimezone(ET)


def session_date(moment: datetime) -> date:
    return to_et(moment).date()


def is_market_open(moment: datetime) -> bool:
    local = to_et(moment)
    return local.weekday() < 5 and OPEN <= local.time() < CLOSE


def minutes_since_open(moment: datetime) -> float:
    local = to_et(moment)
    opened = datetime.combine(local.date(), OPEN, tzinfo=ET)
    return (local - opened).total_seconds() / 60


def minutes_to_close(moment: datetime) -> float:
    local = to_et(moment)
    closes = datetime.combine(local.date(), CLOSE, tzinfo=ET)
    return (closes - local).total_seconds() / 60
