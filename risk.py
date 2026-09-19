"""Pre-trade gates. Refuse unknown state and any existing open orders."""
import math
from typing import Any, Mapping
from models import etf


class RiskError(ValueError):
    """A proposed action fails a known pre-trade rule."""


def check_etf_projection(snapshot: Mapping[str, Any], projected: Mapping[str, float],
                         gross_limit: float, net_limit: float) -> None:
    """Check complete hypothetical positions, including tender/converter cash.

    Server counters are authoritative baselines. Apply all instrument deltas
    to them using inverse units; never silently reset them to a local estimate.
    """
    if snapshot.get("orders") != []:
        raise RiskError("Open orders or missing order state")
    if not all(math.isfinite(p) for p in projected.values()):
        raise RiskError("Nonfinite projected position")
    value = etf.exposure(projected)
    if value["gross"] > gross_limit or abs(value["net"]) > net_limit:
        raise RiskError("ETF weighted position limit")
    limits = snapshot.get("limits")
    if not limits:
        raise RiskError("Missing session limits")
    names = {limit["name"] for limit in limits}
    gross_delta = {name: 0.0 for name in names}
    net_delta = dict(gross_delta)
    for security in snapshot["securities"]:
        ticker = security["ticker"]
        before, after = security["position"], projected.get(ticker, security["position"])
        if before == after:
            continue
        bindings = security.get("limits", [])
        if not bindings:
            raise RiskError(f"Missing limit bindings for {ticker}")
        for binding in bindings:
            name, units = binding["name"], binding["units"]
            if name not in names or not math.isfinite(units) or units <= 0:
                raise RiskError("Invalid security limit binding")
            gross_delta[name] += (abs(after) - abs(before)) / units
            net_delta[name] += (after - before) / units
    for limit in limits:
        name = limit["name"]
        if (limit["gross"] + gross_delta[name] > limit["gross_limit"]
                or abs(limit["net"] + net_delta[name]) > limit["net_limit"]):
            raise RiskError(f"Projected server {name} limit breach")


def check(snapshot: Mapping[str, Any], ticker: str, quantity: int, case: str,
          deltas: Mapping[str, float] | None = None, gross_limit: int | None = None,
          net_limit: int | None = None, tender: bool = False) -> None:
    """Reject a proposed order that breaches local or server-reported limits.

    :param snapshot: Fresh account snapshot including limits and open orders.
    :param ticker: Instrument to trade.
    :param quantity: Signed integer quantity.
    :param case: Case identifier.
    :param deltas: Per-instrument delta weights for the volatility case.
    :param gross_limit: Configured ETF gross limit.
    :param net_limit: Configured ETF net limit.
    :param tender: Whether this check evaluates tender inventory instead of order size.
    :raises RiskError: If a known safety rule is violated.
    """
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity == 0:
        raise RiskError("Order must have a nonzero integer signed quantity")
    if snapshot["case"]["status"] != "ACTIVE" or snapshot["case"]["tick"] >= 299:
        raise RiskError("Case is not accepting new strategy actions")
    if snapshot.get("orders") != []:
        raise RiskError("Open orders or missing order state; reconcile before trading")
    securities = {s["ticker"]: s for s in snapshot["securities"]}
    security = securities[ticker]
    if security.get("is_tradeable") is not True:
        raise RiskError("Instrument is not confirmed tradeable")
    cap = 2500000 if ticker == "USD" else (100 if ticker.startswith("RTM") and ticker != "RTM" else 10000)
    cap = min(cap, security["max_trade_size"])
    if abs(quantity) > cap and not tender:
        raise RiskError("Order exceeds size limit")
    positions = {t: s["position"] for t, s in securities.items()}
    before_position = positions[ticker]
    if not all(math.isfinite(p) for p in positions.values()):
        raise RiskError("Invalid position")
    if case == "etf":
        if gross_limit is None or net_limit is None or min(gross_limit, net_limit) <= 0:
            raise RiskError("ETF session gross/net limits must be configured")
        if ticker == "USD":
            if abs(positions[ticker] + quantity) > abs(positions[ticker]):
                raise RiskError("FX orders must reduce existing currency exposure")
        elif ticker not in etf.WEIGHTS or not etf.within_limits(positions, [(ticker, quantity)], gross_limit, net_limit):
            raise RiskError("ETF weighted position limit")
    else:
        positions[ticker] += quantity
        options = [p for t, p in positions.items() if t != "RTM"]
        if abs(positions["RTM"]) > 50000 or sum(abs(p) for p in options) > 2500 or abs(sum(options)) > 1000:
            raise RiskError("Volatility position limit")
        if deltas is None or set(positions) - set(deltas):
            raise RiskError("Missing instrument delta")
        before = sum(s["position"] * deltas[t] for t, s in securities.items())
        after = sum(p*deltas[t] for t, p in positions.items())
        if not math.isfinite(after) or (abs(after) > 6000 and abs(after) >= abs(before)):
            raise RiskError("Projected delta exceeds internal 6000-share band")
    # Server-provided limit headroom is an additional gate, never a substitute for local checks.
    limits = snapshot.get("limits")
    if not isinstance(limits, list) or not limits:
        raise RiskError("Missing session limits")
    bindings = {b["name"]: b["units"] for b in security["limits"]}
    if not bindings and ticker != "USD":
        raise RiskError("Missing security limit bindings")
    if bindings.keys() - {limit["name"] for limit in limits}:
        raise RiskError("Unknown security limit binding")
    for limit in limits:
        # API 'units' is instrument units per risk unit: RITC's 0.5 means 2x risk.
        units = bindings.get(limit["name"])
        if units is not None and (not math.isfinite(units) or units <= 0):
            raise RiskError("Invalid security limit units")
        weight = 1 / units if units is not None else 0
        projected_gross = limit["gross"] + weight * (abs(before_position + quantity) - abs(before_position))
        projected_net = limit["net"] + weight * quantity
        if projected_gross > limit["gross_limit"] or abs(projected_net) > limit["net_limit"]:
            raise RiskError("Projected server position limit breach")
