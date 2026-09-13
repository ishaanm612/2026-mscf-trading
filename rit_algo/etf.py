"""Depth-aware ETF signals; proposed legs are not atomic or guaranteed profits."""
WEIGHTS = {"BULL": 1, "BEAR": 1, "RITC": 2}


def exposure(positions):
    values = [positions.get(t, 0) * w for t, w in WEIGHTS.items()]
    return {"gross": sum(abs(v) for v in values), "net": sum(values)}


def within_limits(positions, legs, gross_limit, net_limit):
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


def vwap(book, action, quantity):
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


def analyze(snapshot, quantity=1000, gross_limit=None, net_limit=None):
    if not 0 < quantity <= 10000:
        raise ValueError("ETF child quantity must be 1..10000")
    books = snapshot["books"]
    positions = {s["ticker"]: s["position"] for s in snapshot["securities"]}
    results = []
    for direction in (1, -1):
        legs = [("BULL", -direction * quantity), ("BEAR", -direction * quantity),
                ("RITC", direction * quantity)]
        try:
            stock_side = "SELL" if direction == 1 else "BUY"
            etf_side = "BUY" if direction == 1 else "SELL"
            basket = sum(vwap(books[t], stock_side, quantity) for t in ("BULL", "BEAR"))
            etf = vwap(books["RITC"], etf_side, quantity)
            fx = vwap(books["USD"], etf_side, quantity * (etf + 0.02))
            edge = direction * (basket - etf * fx) - 0.04 - 0.02 * fx
            allowed = None if gross_limit is None or net_limit is None else within_limits(
                positions, legs, gross_limit, net_limit)
            results.append({"legs": legs, "edge_cad_per_unit": edge,
                            "within_configured_limits": allowed})
        except ValueError as error:
            results.append({"legs": legs, "skip": str(error)})
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
            market = vwap(books["RITC"], unwind, q)
            edge = (market - offer["price"]) * (1 if action == "BUY" else -1) - 0.02
            report["estimated_unwind_profit_usd"] = edge * q
            report["note"] = "Static depth estimate; unwind needs child orders and fresh quotes."
        except (ValueError, KeyError) as error:
            report["skip"] = str(error)
        tenders.append(report)
    return {"exposure": exposure(positions), "opportunities": results, "tenders": tenders}
