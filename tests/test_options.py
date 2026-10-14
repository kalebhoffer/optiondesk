from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from optiondesk.config import ContractConfig
from optiondesk.models import Direction, Right
from optiondesk.options import occ, pricing
from optiondesk.options.selector import select_contract

from .helpers import MONDAY, contract

# ── OCC symbols ──


def test_build_and_parse_round_trip() -> None:
    symbol = occ.build("SPY", date(2026, 5, 15), Right.CALL, 500)
    assert symbol == "SPY260515C00500000"
    assert occ.parse(symbol) == ("SPY", date(2026, 5, 15), Right.CALL, 500.0)


def test_fractional_strike() -> None:
    symbol = occ.build("F", date(2026, 5, 15), Right.PUT, 12.5)
    assert symbol == "F260515P00012500"
    assert occ.parse(symbol)[3] == 12.5


def test_schwab_padding() -> None:
    assert occ.to_schwab("SPY260515C00500000") == "SPY   260515C00500000"
    assert occ.to_schwab("GOOGL260515P00150000") == "GOOGL 260515P00150000"
    assert occ.parse("SPY   260515C00500000")[0] == "SPY"


@pytest.mark.parametrize("bad", ["SPY", "SPY260515X00500000", "TOOLONGROOT260515C00500000", ""])
def test_parse_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        occ.parse(bad)


# ── pricing ──


def test_put_call_parity() -> None:
    spot, strike, years, vol, rate = 500.0, 505.0, 10 / 365, 0.2, 0.04
    call = pricing.price(spot, strike, years, vol, Right.CALL, rate)
    put = pricing.price(spot, strike, years, vol, Right.PUT, rate)
    assert call - put == pytest.approx(spot - strike * math.exp(-rate * years), abs=1e-9)


def test_deltas() -> None:
    call = pricing.delta(500, 500, 7 / 365, 0.2, Right.CALL)
    put = pricing.delta(500, 500, 7 / 365, 0.2, Right.PUT)
    assert 0.45 < call < 0.6
    assert call - put == pytest.approx(1.0)
    assert pricing.delta(500, 400, 7 / 365, 0.2, Right.CALL) > 0.99


def test_expired_option_is_intrinsic() -> None:
    assert pricing.price(510, 500, 0, 0.2, Right.CALL) == 10
    assert pricing.price(510, 500, 0, 0.2, Right.PUT) == 0


# ── selection ──

RULES = ContractConfig(target_delta=0.40, min_dte=1, max_dte=14, max_spread_pct=0.10, min_open_interest=100)


def test_picks_nearest_expiry_then_closest_delta() -> None:
    near = MONDAY + timedelta(days=2)
    far = MONDAY + timedelta(days=9)
    chain = [
        contract(expiration=far, delta=0.40, strike=500),
        contract(expiration=near, delta=0.55, strike=495),
        contract(expiration=near, delta=0.38, strike=502),
        contract(expiration=near, delta=-0.40, strike=498, right=Right.PUT),
    ]
    chosen = select_contract(chain, Direction.LONG, MONDAY, RULES)
    assert chosen is not None and chosen.strike == 502


def test_short_picks_puts() -> None:
    chain = [contract(delta=0.40), contract(delta=-0.41, right=Right.PUT, strike=497)]
    chosen = select_contract(chain, Direction.SHORT, MONDAY, RULES)
    assert chosen is not None and chosen.right is Right.PUT


@pytest.mark.parametrize(
    "bad",
    [
        {"bid": 1.0, "ask": 1.5},  # spread too wide
        {"open_interest": 10},
        {"expiration": MONDAY},  # 0 DTE, below min_dte
        {"expiration": MONDAY + timedelta(days=30)},
        {"bid": 0.0, "ask": 0.05},
    ],
)
def test_filters(bad: dict[str, object]) -> None:
    assert select_contract([contract(**bad)], Direction.LONG, MONDAY, RULES) is None  # type: ignore[arg-type]
