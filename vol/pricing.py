"""Black-Scholes pricing for the RTM volatility case.

Time convention (case + practice server): 300 ticks = 20 trading days at
15 ticks/day, and a year is 240 trading days, so one year = 3600 ticks.
T must always come from the current tick, never a hardcoded 1/12.
"""
import math

TOTAL_TICKS = 300
TICKS_PER_YEAR = 3600


def years_left(tick):
    """Time to expiry in years at a given case tick."""
    return max(0.0, (TOTAL_TICKS - tick) / TICKS_PER_YEAR)


def _cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _pdf(x):
    return math.exp(-x * x / 2.0) / math.sqrt(2.0 * math.pi)


def bs_price(kind, spot, strike, years, rate, sigma):
    """European option price per share. kind is 'C' or 'P'."""
    if years <= 0.0:
        return max(spot - strike, 0.0) if kind == "C" else max(strike - spot, 0.0)
    d1 = (math.log(spot / strike) + (rate + sigma * sigma / 2.0) * years) / (sigma * math.sqrt(years))
    d2 = d1 - sigma * math.sqrt(years)
    call = spot * _cdf(d1) - strike * math.exp(-rate * years) * _cdf(d2)
    if kind == "C":
        return call
    return call - spot + strike * math.exp(-rate * years)  # put-call parity


def bs_delta(kind, spot, strike, years, rate, sigma):
    """Delta per share: price change per $1 move in RTM."""
    if years <= 0.0:
        intrinsic = 1.0 if spot > strike else 0.0
        return intrinsic if kind == "C" else intrinsic - 1.0
    d1 = (math.log(spot / strike) + (rate + sigma * sigma / 2.0) * years) / (sigma * math.sqrt(years))
    return _cdf(d1) if kind == "C" else _cdf(d1) - 1.0


def bs_vega(spot, strike, years, rate, sigma):
    """Vega per share per 1.00 of vol (divide by 100 for per vol point)."""
    if years <= 0.0:
        return 0.0
    d1 = (math.log(spot / strike) + (rate + sigma * sigma / 2.0) * years) / (sigma * math.sqrt(years))
    return spot * _pdf(d1) * math.sqrt(years)


def implied_vol(kind, price, spot, strike, years, rate):
    """Implied vol at an observed price, by bisection. None if impossible."""
    if years <= 0.0 or not math.isfinite(price):
        return None
    low, high = 1e-6, 5.0
    if not bs_price(kind, spot, strike, years, rate, low) < price < bs_price(kind, spot, strike, years, rate, high):
        return None
    for _ in range(60):
        mid = (low + high) / 2.0
        if bs_price(kind, spot, strike, years, rate, mid) < price:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0
