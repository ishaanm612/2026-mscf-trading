"""European Black-Scholes values and portfolio delta in underlying shares."""
import math
import re


def bs(spot, strike, years, rate, sigma, kind):
    if kind not in ("C", "P") or min(spot, strike, sigma) <= 0 or years < 0:
        raise ValueError("invalid option inputs")
    sign = 1 if kind == "C" else -1
    if years == 0:
        delta = (1.0 if spot > strike else 0.0) if spot != strike else 0.5
        return max(sign * (spot - strike), 0), delta - (kind == "P"), 0.0
    root = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + sigma * sigma / 2) * years) / (sigma * root)
    d2 = d1 - sigma * root
    cdf = lambda x: (1 + math.erf(x / math.sqrt(2))) / 2
    price = sign * (spot * cdf(sign * d1) - strike * math.exp(-rate * years) * cdf(sign * d2))
    return price, cdf(d1) - (kind == "P"), spot * math.exp(-d1*d1/2) / math.sqrt(2*math.pi) * root


def implied_vol(price, spot, strike, years, rate, kind):
    if years <= 0 or not math.isfinite(price):
        return None
    low, high = 0.000001, 5.0
    if not bs(spot, strike, years, rate, low, kind)[0] < price < bs(spot, strike, years, rate, high, kind)[0]:
        return None
    for _ in range(70):
        mid = (low + high) / 2
        if bs(spot, strike, years, rate, mid, kind)[0] < price:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def analyze(snapshot, sigma, rate=0.0):
    if not math.isfinite(sigma) or sigma <= 0 or not math.isfinite(rate):
        raise ValueError("sigma must be finite and positive; rate must be finite")
    securities = {s["ticker"]: s for s in snapshot["securities"]}
    underlying = securities["RTM"]
    spot = (underlying["bid"] + underlying["ask"]) / 2
    years = max(0, 300 - snapshot["case"]["tick"]) / 3600
    delta = underlying["position"]
    gross = net = 0
    rows = []
    for ticker, security in securities.items():
        match = re.fullmatch(r"RTM(\d+(?:\.\d+)?)([CP])", ticker)
        if not match:
            if ticker != "RTM" and security.get("position", 0):
                raise ValueError(f"Unrecognized held instrument: {ticker}")
            continue
        strike, kind = float(match[1]), match[2]
        fair, d, vega = bs(spot, strike, years, rate, sigma, kind)
        position = security["position"]
        delta += position * 100 * d
        gross += abs(position)
        net += position
        bid, ask = security["bid"], security["ask"]
        if bid is None or ask is None or not 0 <= bid <= ask:
            rows.append({"ticker": ticker, "skip": "missing or invalid quote"})
            continue
        # Reserve entry + exit option commission and an initial stock hedge fee.
        cost = 0.04 + abs(d) * 0.02
        buy_edge, sell_edge = fair - ask - cost, bid - fair - cost
        action = "HOLD" if years == 0 or max(buy_edge, sell_edge) <= 0 else ("BUY" if buy_edge > sell_edge else "SELL")
        rows.append({"ticker": ticker, "fair": fair, "delta": d, "vega_per_vol_point": vega / 100,
                     "implied_vol": implied_vol((bid + ask)/2, spot, strike, years, rate, kind),
                     "signal": action, "edge_per_share": max(buy_edge, sell_edge)})
    hedge = -round(delta)
    return {"sigma_assumption": sigma, "years_remaining": years, "options": rows,
            "portfolio_delta_shares": delta, "delta_limit_breached": abs(delta) > 7000,
            "option_gross": gross, "option_net": net,
            "position_limits_breached": gross > 2500 or abs(net) > 1000 or abs(underlying["position"]) > 50000,
            "suggested_rtm_hedge_shares": hedge,
            "hedge_within_position_limit": abs(underlying["position"] + hedge) <= 50000,
            "news": snapshot.get("news", [])}
