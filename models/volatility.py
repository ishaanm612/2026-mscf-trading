"""Black-Scholes pricing, implied volatility, and portfolio Greeks."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, Mapping

OptionKind = Literal["C", "P"]


@dataclass(frozen=True)
class OptionGreeks:
    """The price and Greeks of one option, quoted per option share.

    :param price: Black-Scholes fair price per share.
    :param delta: Price change per one-dollar RTM change.
    :param gamma: Delta change per one-dollar RTM change.
    :param vega: Price change for a one-unit volatility change.
    :param theta: Price change per trading year of time passage.
    """

    price: float
    delta: float
    gamma: float
    vega: float
    theta: float


@dataclass(frozen=True)
class PortfolioGreeks:
    """Portfolio Greeks measured in RTM-share and dollar units.

    :param delta: Portfolio delta in RTM shares.
    :param gamma: Portfolio gamma per RTM dollar.
    :param vega: Portfolio vega per one-unit volatility change.
    :param theta: Portfolio theta per trading year.
    """

    delta: float
    gamma: float
    vega: float
    theta: float


def _normal_cdf(value: float) -> float:
    """Return standard normal cumulative probability.

    :param value: Standard-normal coordinate.
    :returns: Cumulative probability.
    """

    return (1.0 + math.erf(value / math.sqrt(2.0))) / 2.0


def _normal_pdf(value: float) -> float:
    """Return standard normal density.

    :param value: Standard-normal coordinate.
    :returns: Probability density.
    """

    return math.exp(-value * value / 2.0) / math.sqrt(2.0 * math.pi)


def _validate_inputs(
    spot: float, strike: float, years: float, rate: float, sigma: float, kind: str
) -> None:
    """Reject malformed Black-Scholes inputs before calculation.

    :param spot: RTM spot price.
    :param strike: Option strike price.
    :param years: Remaining trading years.
    :param rate: Annual risk-free rate.
    :param sigma: Annualized volatility.
    :param kind: ``C`` for call or ``P`` for put.
    :raises ValueError: If an input is outside the Black-Scholes domain.
    """

    if kind not in ("C", "P") or min(spot, strike, sigma) <= 0 or years < 0:
        raise ValueError("invalid option inputs")
    if not all(math.isfinite(value) for value in (spot, strike, years, rate, sigma)):
        raise ValueError("option inputs must be finite")


def option_greeks(
    spot: float,
    strike: float,
    years: float,
    rate: float,
    sigma: float,
    kind: OptionKind,
) -> OptionGreeks:
    """Price one European option and calculate Black-Scholes Greeks.

    :param spot: RTM midpoint.
    :param strike: Option strike.
    :param years: Remaining trading time under the competition convention.
    :param rate: Configured continuously compounded annual rate.
    :param sigma: Forecast annualized remaining volatility.
    :param kind: ``C`` for a call or ``P`` for a put.
    :returns: Fair value and Greeks per option share.
    """

    _validate_inputs(spot, strike, years, rate, sigma, kind)
    if years == 0:
        call_delta = 1.0 if spot > strike else 0.0 if spot < strike else 0.5
        price = max(spot - strike, 0.0) if kind == "C" else max(strike - spot, 0.0)
        return OptionGreeks(
            price, call_delta if kind == "C" else call_delta - 1.0, 0.0, 0.0, 0.0
        )
    root_time = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + sigma * sigma / 2.0) * years) / (
        sigma * root_time
    )
    d2 = d1 - sigma * root_time
    discount = math.exp(-rate * years)
    call_price = spot * _normal_cdf(d1) - strike * discount * _normal_cdf(d2)
    call_delta = _normal_cdf(d1)
    gamma = _normal_pdf(d1) / (spot * sigma * root_time)
    vega = spot * _normal_pdf(d1) * root_time
    call_theta = -(spot * _normal_pdf(d1) * sigma) / (
        2.0 * root_time
    ) - rate * strike * discount * _normal_cdf(d2)
    if kind == "C":
        return OptionGreeks(call_price, call_delta, gamma, vega, call_theta)
    return OptionGreeks(
        call_price - spot + strike * discount,
        call_delta - 1.0,
        gamma,
        vega,
        call_theta + rate * strike * discount,
    )


def bs_call_price(
    spot: float, strike: float, years: float, rate: float, sigma: float
) -> float:
    """Return the Black-Scholes call price.

    :param spot: RTM midpoint.
    :param strike: Call strike.
    :param years: Remaining trading years.
    :param rate: Annual risk-free rate.
    :param sigma: Annualized volatility.
    :returns: Fair call price per share.
    """

    return option_greeks(spot, strike, years, rate, sigma, "C").price


def bs_put_price(
    spot: float, strike: float, years: float, rate: float, sigma: float
) -> float:
    """Return the Black-Scholes put price.

    :param spot: RTM midpoint.
    :param strike: Put strike.
    :param years: Remaining trading years.
    :param rate: Annual risk-free rate.
    :param sigma: Annualized volatility.
    :returns: Fair put price per share.
    """

    return option_greeks(spot, strike, years, rate, sigma, "P").price


def bs_call_delta(
    spot: float, strike: float, years: float, rate: float, sigma: float
) -> float:
    """Return the Black-Scholes call delta.

    :param spot: RTM midpoint.
    :param strike: Call strike.
    :param years: Remaining trading years.
    :param rate: Annual risk-free rate.
    :param sigma: Annualized volatility.
    :returns: Call delta per option share.
    """

    return option_greeks(spot, strike, years, rate, sigma, "C").delta


def bs_put_delta(
    spot: float, strike: float, years: float, rate: float, sigma: float
) -> float:
    """Return the Black-Scholes put delta.

    :param spot: RTM midpoint.
    :param strike: Put strike.
    :param years: Remaining trading years.
    :param rate: Annual risk-free rate.
    :param sigma: Annualized volatility.
    :returns: Put delta per option share.
    """

    return option_greeks(spot, strike, years, rate, sigma, "P").delta


def bs_gamma(
    spot: float, strike: float, years: float, rate: float, sigma: float
) -> float:
    """Return Black-Scholes gamma, common to calls and puts.

    :param spot: RTM midpoint.
    :param strike: Option strike.
    :param years: Remaining trading years.
    :param rate: Annual risk-free rate.
    :param sigma: Annualized volatility.
    :returns: Gamma per option share.
    """

    return option_greeks(spot, strike, years, rate, sigma, "C").gamma


def bs_vega(
    spot: float, strike: float, years: float, rate: float, sigma: float
) -> float:
    """Return Black-Scholes vega, common to calls and puts.

    :param spot: RTM midpoint.
    :param strike: Option strike.
    :param years: Remaining trading years.
    :param rate: Annual risk-free rate.
    :param sigma: Annualized volatility.
    :returns: Vega per one-unit volatility change.
    """

    return option_greeks(spot, strike, years, rate, sigma, "C").vega


def bs_theta(
    spot: float,
    strike: float,
    years: float,
    rate: float,
    sigma: float,
    kind: OptionKind,
) -> float:
    """Return Black-Scholes theta for a call or put.

    :param spot: RTM midpoint.
    :param strike: Option strike.
    :param years: Remaining trading years.
    :param rate: Annual risk-free rate.
    :param sigma: Annualized volatility.
    :param kind: ``C`` for a call or ``P`` for a put.
    :returns: Theta per trading year.
    """

    return option_greeks(spot, strike, years, rate, sigma, kind).theta


def implied_volatility(
    price: float,
    spot: float,
    strike: float,
    years: float,
    rate: float,
    kind: OptionKind,
) -> float | None:
    """Solve implied volatility by bounded bisection.

    :param price: Observed option price.
    :param spot: RTM midpoint.
    :param strike: Option strike.
    :param years: Remaining trading years.
    :param rate: Annual risk-free rate.
    :param kind: ``C`` for a call or ``P`` for a put.
    :returns: Implied annualized volatility, or ``None`` for an impossible price.
    """

    if years <= 0 or not math.isfinite(price):
        return None
    low, high = 0.000001, 5.0
    low_price = option_greeks(spot, strike, years, rate, low, kind).price
    high_price = option_greeks(spot, strike, years, rate, high, kind).price
    if not low_price < price < high_price:
        return None
    for _ in range(70):
        middle = (low + high) / 2.0
        if option_greeks(spot, strike, years, rate, middle, kind).price < price:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def portfolio_greeks(
    rtm_position: int, option_positions: list[tuple[int, OptionGreeks]], multiplier: int
) -> PortfolioGreeks:
    """Aggregate confirmed stock and option inventory into portfolio Greeks.

    :param rtm_position: Confirmed RTM shares.
    :param option_positions: ``(contracts, per-share Greeks)`` pairs.
    :param multiplier: RTM shares represented by one option contract.
    :returns: Aggregated portfolio Greeks.
    """

    delta, gamma, vega, theta = float(rtm_position), 0.0, 0.0, 0.0
    for contracts, greeks in option_positions:
        scale = contracts * multiplier
        delta += scale * greeks.delta
        gamma += scale * greeks.gamma
        vega += scale * greeks.vega
        theta += scale * greeks.theta
    return PortfolioGreeks(delta, gamma, vega, theta)


def bs(
    spot: float,
    strike: float,
    years: float,
    rate: float,
    sigma: float,
    kind: OptionKind,
) -> tuple[float, float, float]:
    """Return legacy ``(price, delta, vega)`` values for existing callers.

    :param spot: RTM midpoint.
    :param strike: Option strike.
    :param years: Remaining trading years.
    :param rate: Annual risk-free rate.
    :param sigma: Annualized volatility.
    :param kind: ``C`` for a call or ``P`` for a put.
    :returns: Fair price, delta, and vega per option share.
    """

    greeks = option_greeks(spot, strike, years, rate, sigma, kind)
    return greeks.price, greeks.delta, greeks.vega


def implied_vol(
    price: float,
    spot: float,
    strike: float,
    years: float,
    rate: float,
    kind: OptionKind,
) -> float | None:
    """Compatibility alias for :func:`implied_volatility`.

    :param price: Observed option price.
    :param spot: RTM midpoint.
    :param strike: Option strike.
    :param years: Remaining trading years.
    :param rate: Annual risk-free rate.
    :param kind: ``C`` for a call or ``P`` for a put.
    :returns: Implied annualized volatility or ``None``.
    """

    return implied_volatility(price, spot, strike, years, rate, kind)


def analyze(snapshot: Mapping[str, Any], sigma: float, rate: float = 0.0) -> dict[str, Any]:
    """Provide the original JSON analysis shape for legacy CLI callers.

    :param snapshot: RIT volatility snapshot.
    :param sigma: Annualized remaining-volatility assumption.
    :param rate: Annualized continuously compounded risk-free rate.
    :returns: JSON-compatible option and portfolio analysis.
    :raises ValueError: If input quotes or forecast parameters are invalid.
    """

    if not math.isfinite(sigma) or sigma <= 0 or not math.isfinite(rate):
        raise ValueError("sigma must be finite and positive; rate must be finite")
    securities = {str(item["ticker"]): item for item in snapshot["securities"]}
    underlying = securities["RTM"]
    spot = (float(underlying["bid"]) + float(underlying["ask"])) / 2.0
    years = max(0.0, 300 - int(snapshot["case"]["tick"])) / 3600.0
    option_rows: list[dict[str, Any]] = []
    positions: list[tuple[int, OptionGreeks]] = []
    gross = net = 0
    import re
    for ticker, security in securities.items():
        if ticker == "RTM":
            continue
        match = re.fullmatch(r"RTM(\d+(?:\.\d+)?)([CP])", ticker)
        if match is None:
            if security.get("position", 0):
                raise ValueError(f"Unrecognized held instrument: {ticker}")
            continue
        strike, kind = float(match.group(1)), match.group(2)
        greeks = option_greeks(spot, strike, years, rate, sigma, kind)  # type: ignore[arg-type]
        position = int(security.get("position", 0))
        positions.append((position, greeks))
        gross += abs(position)
        net += position
        bid, ask = security.get("bid"), security.get("ask")
        if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)) or bid < 0 or ask < bid:
            option_rows.append({"ticker": ticker, "skip": "missing or invalid quote"})
            continue
        reserve = 0.04 + abs(greeks.delta) * 0.02
        buy_edge, sell_edge = greeks.price - ask - reserve, bid - greeks.price - reserve
        signal = "HOLD" if years == 0 or max(buy_edge, sell_edge) <= 0 else ("BUY" if buy_edge > sell_edge else "SELL")
        option_rows.append({"ticker": ticker, "fair": greeks.price, "delta": greeks.delta, "gamma": greeks.gamma,
                            "vega_per_vol_point": greeks.vega / 100.0, "theta": greeks.theta,
                            "implied_vol": implied_volatility((bid + ask) / 2.0, spot, strike, years, rate, kind),
                            "signal": signal, "edge_per_share": max(buy_edge, sell_edge)})
    portfolio = portfolio_greeks(int(underlying.get("position", 0)), positions, 100)
    hedge = -round(portfolio.delta)
    return {"sigma_assumption": sigma, "years_remaining": years, "options": option_rows,
            "portfolio_delta_shares": portfolio.delta, "portfolio_gamma": portfolio.gamma,
            "portfolio_vega": portfolio.vega, "portfolio_theta": portfolio.theta,
            "delta_limit_breached": abs(portfolio.delta) > 7000, "option_gross": gross, "option_net": net,
            "position_limits_breached": gross > 2500 or abs(net) > 1000 or abs(int(underlying.get("position", 0))) > 50000,
            "suggested_rtm_hedge_shares": hedge,
            "hedge_within_position_limit": abs(int(underlying.get("position", 0)) + hedge) <= 50000,
            "news": snapshot.get("news", [])}
