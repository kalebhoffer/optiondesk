"""Exit rules, checked for every open position on every bar."""

from __future__ import annotations

from datetime import datetime

from . import clock
from .config import ExitConfig
from .models import Position


def exit_reason(position: Position, now: datetime, rules: ExitConfig) -> str | None:
    """Return why ``position`` should be closed now, or ``None`` to keep it.

    Checked in order: stop loss, take profit, trailing stop, maximum hold
    time, end of day (which also closes same-day expirations before the bell).
    """
    ret = position.return_pct
    if ret <= -rules.stop_loss_pct:
        return "stop_loss"
    if ret >= rules.take_profit_pct:
        return "take_profit"
    best = position.high_water / position.entry_price - 1 if position.entry_price > 0 else 0.0
    if best >= rules.trail_activate_pct and position.mark <= position.high_water * (1 - rules.trail_pct):
        return "trailing_stop"
    if (now - position.entry_time).total_seconds() / 60 >= rules.max_hold_minutes:
        return "max_hold_time"
    if clock.minutes_to_close(now) <= rules.flatten_minutes_before_close:
        return "end_of_day"
    if clock.session_date(now) > position.contract.expiration:
        return "expired"  # held past expiration; should not happen with end_of_day on
    return None
