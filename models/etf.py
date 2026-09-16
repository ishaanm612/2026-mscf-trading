"""Depth-aware ETF signals; proposed legs are not atomic or guaranteed profits."""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping
WEIGHTS = {"BULL": 1, "BEAR": 1, "RITC": 2}
EQUITY_FEE = 0.02
DEFAULT_ENTRY_BUFFER_CAD = 0.10


def exposure(positions: Mapping[str, float]) -> dict[str, float]:
    """Calculate weighted ETF gross and net exposure.

    :param positions: Instrument positions keyed by ticker.
    :returns: Weighted gross and net exposure.
    """
    values = [positions.get(t, 0) * w for t, w in WEIGHTS.items()]
    return {"gross": sum(abs(v) for v in values), "net": sum(values)}


def within_limits(positions: Mapping[str, float], legs: Iterable[tuple[str, float]], gross_limit: float,
                  net_limit: float) -> bool:
    """Check current exposure and every sequential fill, including ETF weight."""
    p = dict(positions)
    for leg in [None, *legs]:
        if leg:
            ticker, quantity = leg
            p[ticker] = p.get(ticker, 0) + quantity
        risk = exposure(p)
        if risk["gross"] > gross_limit or abs(risk["net"]) > net_limit:
            return False
    return True


def vwap(book: Mapping[str, Any], action: str, quantity: int) -> float:
    """Calculate executable depth-weighted price for one order direction.

    :param book: RIT order book.
    :param action: ``BUY`` or ``SELL``.
    :param quantity: Requested positive quantity.
    :returns: Depth-weighted executable price.
    :raises ValueError: If visible liquidity is insufficient.
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    side = "asks" if action == "BUY" else "bids"
    levels = sorted(book[side], key=lambda row: row["price"], reverse=side == "bids")
    remaining, total = quantity, 0.0
    for row in levels:
        available = max(0, row["quantity"] - row.get("quantity_filled", 0))
        take = min(remaining, available)
        total += take * row["price"]
        remaining -= take
        if remaining == 0:
            return total / quantity
    raise ValueError("insufficient visible depth")


def basket_opportunity(snapshot: Mapping[str, Any], direction: int, quantity: int,
                       gross_limit: float | None = None, net_limit: float | None = None,
                       entry_buffer_cad: float = DEFAULT_ENTRY_BUFFER_CAD) -> dict[str, Any]:
    """Price one fully hedged ETF basket using executable depth.

    ``direction=1`` sells the CAD BULL/BEAR basket and buys USD RITC; the
    inverse direction buys the basket and sells RITC.  USD is traded in the
    same direction as RITC to neutralize the currency created or consumed by
    that leg.  Currency quantity is rounded *up* to fund RITC plus its stated
    per-share fee, so a plan never understates the required USD hedge.

    The reported edge includes the three equity commissions and all observed
    bid/ask crossing through the VWAPs.  ``entry_buffer_cad`` is an additional
    reserve for the non-atomic, serial execution sequence; it is not a claim
    that the later unwind is free or guaranteed.

    :param snapshot: Fresh ETF snapshot containing BULL, BEAR, RITC and USD books.
    :param direction: ``1`` for long RITC/short basket; ``-1`` for the inverse.
    :param quantity: Positive RITC and matching basket share quantity.
    :returns: Explainable executable-price and eligibility report.
    :raises ValueError: If inputs are invalid or any required visible depth is absent.
    """

    if direction not in (-1, 1):
        raise ValueError("direction must be -1 or 1")
    if not 0 < quantity <= 10000:
        raise ValueError("ETF child quantity must be 1..10000")
    if entry_buffer_cad < 0:
        raise ValueError("entry buffer cannot be negative")
    books = snapshot["books"]
    positions = {s["ticker"]: s["position"] for s in snapshot["securities"]}
    stock_action = "SELL" if direction == 1 else "BUY"
    etf_action = "BUY" if direction == 1 else "SELL"
    basket_prices = {ticker: vwap(books[ticker], stock_action, quantity) for ticker in ("BULL", "BEAR")}
    etf_price = vwap(books["RITC"], etf_action, quantity)
    # The price and commission are USD/share; USD trades in whole currency units.
    required_usd = quantity * (etf_price + EQUITY_FEE)
    # Do not turn an exact decimal-cent amount into an extra USD unit merely
    # because binary floats represent it as e.g. 24830.000000000004.
    usd_quantity = math.ceil(required_usd - 1e-9)
    fx_price = vwap(books["USD"], etf_action, usd_quantity)
    basket_cad = sum(basket_prices.values())
    etf_cad = etf_price * fx_price
    fees_cad = 2 * EQUITY_FEE + EQUITY_FEE * fx_price
    edge = direction * (basket_cad - etf_cad) - fees_cad
    legs = [("BULL", -direction * quantity), ("BEAR", -direction * quantity), ("RITC", direction * quantity)]
    allowed = None if gross_limit is None or net_limit is None else within_limits(positions, legs, gross_limit, net_limit)
    return {
        "direction": "LONG_RITC_SHORT_BASKET" if direction == 1 else "SHORT_RITC_LONG_BASKET",
        "legs": legs,
        "fx_leg": {"ticker": "USD", "quantity": direction * usd_quantity, "action": etf_action},
        "executable_prices": {"BULL": basket_prices["BULL"], "BEAR": basket_prices["BEAR"],
                              "RITC_usd": etf_price, "USD_cad_per_usd": fx_price},
        "basket_cad_per_unit": basket_cad,
        "ritc_cad_per_unit": etf_cad,
        "fees_cad_per_unit": fees_cad,
        "edge_cad_per_unit": edge,
        "entry_buffer_cad_per_unit": entry_buffer_cad,
        "eligible_after_buffer": edge >= entry_buffer_cad,
        "within_configured_limits": allowed,
    }


def analyze(snapshot: Mapping[str, Any], quantity: int = 1000, gross_limit: float | None = None,
            net_limit: float | None = None) -> dict[str, Any]:
    """Evaluate ETF basket and tender opportunities without trading.

    :param snapshot: Fresh ETF case snapshot.
    :param quantity: Proposed ETF-unit quantity.
    :param gross_limit: Session gross limit when known.
    :param net_limit: Session net limit when known.
    :returns: JSON-compatible opportunity report.
    """
    if not 0 < quantity <= 10000:
        raise ValueError("ETF child quantity must be 1..10000")
    positions = {s["ticker"]: s["position"] for s in snapshot["securities"]}
    results = []
    for direction in (1, -1):
        try:
            results.append(basket_opportunity(snapshot, direction, quantity, gross_limit, net_limit))
        except ValueError as error:
            results.append({"direction": direction, "skip": str(error)})
    tenders = []
    for offer in snapshot.get("tenders", []):
        report = {"tender_id": offer["tender_id"], "decision": "REVIEW"}
        try:
            if offer["ticker"] != "RITC" or not offer["is_fixed_bid"]:
                raise ValueError("only fixed-price RITC tenders supported")
            action, q = offer["action"], offer["quantity"]
            if action not in ("BUY", "SELL"):
                raise ValueError("unknown tender action")
            unwind = "SELL" if action == "BUY" else "BUY"
            market = vwap(snapshot["books"]["RITC"], unwind, q)
            edge = (market - offer["price"]) * (1 if action == "BUY" else -1) - EQUITY_FEE
            report["estimated_unwind_profit_usd"] = edge * q
            report["note"] = "Static depth estimate; unwind needs child orders and fresh quotes."
        except (ValueError, KeyError) as error:
            report["skip"] = str(error)
        tenders.append(report)
    return {"exposure": exposure(positions), "opportunities": results, "tenders": tenders}
