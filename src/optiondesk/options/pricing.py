"""Black-Scholes pricing, used to simulate option prices in paper and backtest modes."""

from __future__ import annotations

import math

from ..models import Right

_MIN_YEARS = 1 / (365 * 24 * 60)  # one minute; avoids dividing by zero at expiry


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _d1_d2(spot: float, strike: float, years: float, vol: float, rate: float) -> tuple[float, float]:
    years = max(years, _MIN_YEARS)
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * years) / (vol * math.sqrt(years))
    return d1, d1 - vol * math.sqrt(years)


def price(spot: float, strike: float, years: float, vol: float, right: Right, rate: float = 0.04) -> float:
    if years <= 0:
        return max(0.0, spot - strike) if right is Right.CALL else max(0.0, strike - spot)
    d1, d2 = _d1_d2(spot, strike, years, vol, rate)
    discount = math.exp(-rate * max(years, _MIN_YEARS))
    if right is Right.CALL:
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def delta(spot: float, strike: float, years: float, vol: float, right: Right, rate: float = 0.04) -> float:
    d1, _ = _d1_d2(spot, strike, years, vol, rate)
    return _norm_cdf(d1) if right is Right.CALL else _norm_cdf(d1) - 1
