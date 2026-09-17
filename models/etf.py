"""Depth-aware ETF signals; proposed legs are not atomic or guaranteed profits."""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping
WEIGHTS = {"BULL": 1, "BEAR": 1, "RITC": 2}
EQUITY_FEE = 0.02
DEFAULT_ENTRY_BUFFER_CAD = 0.10
CONVERTER_BLOCK = 10_000
CONVERTER_COST_USD = 1_500


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
    that leg. A RITC purchase rounds required USD up after its fee; a RITC
    sale rounds net USD proceeds down after its fee.

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
    required_usd = quantity * (etf_price + direction * EQUITY_FEE)
    # Do not turn an exact decimal-cent amount into an extra USD unit merely
    # because binary floats represent it as e.g. 24830.000000000004.
    usd_quantity = (math.ceil(required_usd - 1e-9) if direction == 1
                    else math.floor(required_usd + 1e-9))
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


def tender_opportunity(snapshot: Mapping[str, Any], offer: Mapping[str, Any]) -> dict[str, Any]:
    """Price a fixed RITC tender through liquidation and net USD conversion.

    RITC inventory and its USD cash leg naturally offset while the position is
    worked. The strategy therefore converts only the final net USD profit or
    loss after liquidation, avoiding a needless gross FX round trip.
    """

    report = {"tender_id": offer["tender_id"], "decision": "REVIEW"}
    try:
        if offer["ticker"] != "RITC" or not offer["is_fixed_bid"]:
            raise ValueError("only fixed-price RITC tenders supported")
        action, quantity = offer["action"], offer["quantity"]
        if action not in ("BUY", "SELL"):
            raise ValueError("unknown tender action")
        if not isinstance(quantity, (int, float)) or quantity <= 0 or int(quantity) != quantity:
            raise ValueError("tender quantity must be a positive integer")
        quantity = int(quantity)
        unwind = "SELL" if action == "BUY" else "BUY"
        market = vwap(snapshot["books"]["RITC"], unwind, quantity)
        edge_usd = ((market - offer["price"]) * (1 if action == "BUY" else -1) - EQUITY_FEE) * quantity
        if edge_usd:
            fx_action = "SELL" if edge_usd > 0 else "BUY"
            fx_quantity = max(1, math.ceil(abs(edge_usd) - 1e-9))
            fx_price = vwap(snapshot["books"]["USD"], fx_action, fx_quantity)
            edge_cad = edge_usd * fx_price
        else:
            fx_action, fx_price, edge_cad = "NONE", None, 0.0
        report.update(estimated_unwind_profit_usd=edge_usd,
                      estimated_unwind_profit_cad=edge_cad,
                      net_fx_action=fx_action, net_fx_price=fx_price,
                      note="Full-depth static estimate; only final net USD is converted after liquidation.")
    except (ValueError, KeyError) as error:
        report["skip"] = str(error)
    return report


def manual_converter_opportunities(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Compare supported manual converters with direct liquidation of inventory.

    Only the human-operated converter itself is recommended. Creation requires
    the account to already own one 10,000-share block of both stocks against a
    RITC short. Redemption requires a 10,000-unit RITC long; resulting stocks
    can either offset shorts or be sold by the normal inventory reducer.
    """

    positions = {row["ticker"]: int(row["position"]) for row in snapshot["securities"]}
    books = snapshot["books"]
    block = CONVERTER_BLOCK
    results: list[dict[str, Any]] = []
    try:
        converter_cad = CONVERTER_COST_USD * vwap(books["USD"], "BUY", CONVERTER_COST_USD)
    except (ValueError, KeyError):
        return results

    if positions.get("RITC", 0) >= block:
        try:
            ritc_sale_usd = block * (vwap(books["RITC"], "SELL", block) - EQUITY_FEE)
            direct_value = ritc_sale_usd * vwap(books["USD"], "SELL", math.ceil(ritc_sale_usd - 1e-9))
            stock_value = 0.0
            for ticker in ("BULL", "BEAR"):
                offset = min(block, max(0, -positions.get(ticker, 0)))
                if offset:
                    stock_value += offset * (vwap(books[ticker], "BUY", offset) + EQUITY_FEE)
                remainder = block - offset
                if remainder:
                    stock_value += remainder * (vwap(books[ticker], "SELL", remainder) - EQUITY_FEE)
            advantage = stock_value - converter_cad - direct_value
            results.append({"converter": "ETF-Redemption", "manual_action": "UNWIND",
                            "blocks": positions["RITC"] // block, "block_size": block,
                            "convert_from": {"RITC": block},
                            "convert_to": {"BULL": block, "BEAR": block},
                            "estimated_advantage_cad": advantage,
                            "recommended": advantage > 0,
                            "reason": "manual redemption beats direct RITC liquidation" if advantage > 0
                                      else "direct RITC liquidation is cheaper"})
        except (ValueError, KeyError):
            pass

    creation_blocks = min(max(0, -positions.get("RITC", 0)) // block,
                          max(0, positions.get("BULL", 0)) // block,
                          max(0, positions.get("BEAR", 0)) // block)
    if creation_blocks:
        try:
            ritc_cover_usd = block * (vwap(books["RITC"], "BUY", block) + EQUITY_FEE)
            avoided_cover = ritc_cover_usd * vwap(books["USD"], "BUY", math.ceil(ritc_cover_usd - 1e-9))
            forgone_stock_sales = sum(block * (vwap(books[ticker], "SELL", block) - EQUITY_FEE)
                                      for ticker in ("BULL", "BEAR"))
            advantage = avoided_cover - forgone_stock_sales - converter_cad
            results.append({"converter": "ETF-Creation", "manual_action": "WIND",
                            "blocks": creation_blocks, "block_size": block,
                            "convert_from": {"BULL": block, "BEAR": block},
                            "convert_to": {"RITC": block},
                            "estimated_advantage_cad": advantage,
                            "recommended": advantage > 0,
                            "reason": "manual creation beats direct basket liquidation" if advantage > 0
                                      else "direct basket liquidation is cheaper"})
        except (ValueError, KeyError):
            pass
    return results


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
    tenders = [tender_opportunity(snapshot, offer) for offer in snapshot.get("tenders", [])]
    converters = manual_converter_opportunities(snapshot)
    return {"exposure": exposure(positions), "opportunities": results,
            "tenders": tenders, "manual_converters": converters}
