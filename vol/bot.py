"""RITC volatility-case bot: poll, explain, trade, hedge.

The whole strategy in one paragraph: parse the analyst news into weekly
exact vols, price every option off that forecast, and trade ATM straddles
against the market maker while its flat quoted IV lags the latest
announcement. Exit when the MM's IV converges to the forecast; from week 4
hold through expiry (gamma P&L) instead. Delta-hedge with RTM shares
whenever portfolio delta drifts toward the penalty band.

Every processed tick prints a block explaining what the bot sees, what it
decided, and why. The same fields go to vol/logs/decisions.jsonl for replay.

Run:  python3 bot.py [--dry-run]   (from inside vol/)
"""
import argparse
import json
import re
import time
from pathlib import Path

import pricing
from news import forecast_sigma, parse_news
from rit import Client

# Case constants (from the case PDF, verified on the practice server).
MULT = 100            # shares per option contract
GROSS_LIMIT = 2500    # option contracts, gross
NET_LIMIT = 1000      # option contracts, net
RTM_LIMIT = 50000     # RTM shares
MAX_OPT_ORDER = 100   # contracts per order
MAX_RTM_ORDER = 10000 # shares per order
DELTA_BAND = 7000     # penalty band: $0.10 per share over, per second

# Strategy knobs (ours to tune on the practice server).
MAX_STRADDLES = 500    # per side; 500 straddles = net 1000 contracts = the net limit
ENTRY_EDGE = 10.0      # $ per straddle, net of costs, required to enter
COST_RESERVE = 10.0    # entry + exit commissions (4 x $2) + $2 hedge/safety cushion
CONVERGED_IV = 0.01    # exit once MM IV is within 1 vol point of our forecast
HEDGE_TRIGGER = 5000   # hedge back toward 0 when |delta| exceeds this (band is 7000)
WEEK4_TICK = 225       # from here hold through expiry; no convergence exit
POLL_SECONDS = 0.25

OPTION = re.compile(r"RTM(\d+(?:\.\d+)?)([CP])$")


def option_rows(securities):
    """Extract option quotes/positions from the raw securities list."""
    rows = []
    for security in securities:
        match = OPTION.fullmatch(str(security.get("ticker", "")))
        if match:
            rows.append({"symbol": security["ticker"], "strike": float(match.group(1)),
                         "kind": match.group(2), "bid": float(security["bid"]),
                         "ask": float(security["ask"]), "position": int(security["position"])})
    return rows


def portfolio_delta(spot, years, rate, sigma, options, rtm_position):
    """Portfolio delta in RTM shares: stock counts 1:1, options via BS delta."""
    delta = float(rtm_position)
    for option in options:
        if option["position"]:
            delta += option["position"] * MULT * pricing.bs_delta(
                option["kind"], spot, option["strike"], years, rate, sigma)
    return delta


def straddle_capacity(side_sign, options):
    """Straddles we may still add on this side under the gross/net limits."""
    gross = sum(abs(option["position"]) for option in options)
    net = sum(option["position"] for option in options)
    gross_room = (GROSS_LIMIT - gross) // 2
    net_room = (NET_LIMIT - net) // 2 if side_sign > 0 else (NET_LIMIT + net) // 2
    return int(max(0, min(MAX_STRADDLES, gross_room, net_room)))


def submit(client, ticker, quantity, dry_run, lines):
    """Submit one signed market order, chunked to the exchange max order size.

    Fills are not polled here; positions are re-read from the API next tick,
    so partial fills self-correct (we always trade toward a target).
    """
    max_order = MAX_RTM_ORDER if ticker == "RTM" else MAX_OPT_ORDER
    remaining = abs(quantity)
    while remaining > 0:
        chunk = min(remaining, max_order)
        action = "BUY" if quantity > 0 else "SELL"
        if dry_run:
            lines.append(f"  DRY-RUN would send: {action} {chunk} {ticker} @ market")
        else:
            client.request("POST", "orders", ticker=ticker, type="MARKET",
                           action=action, quantity=chunk)
            lines.append(f"  sent: {action} {chunk} {ticker} @ market")
        remaining -= chunk
        time.sleep(0.05)


def process_tick(client, dry_run, log_path):
    """Read the market, print one explained decision block, act on it.

    Decision priority, first match wins:
      1. case inactive/expired ........ wait
      2. no exact vol parsed yet ...... wait (never trade on a guess)
      3. |delta| >= 5,000 ............. hedge RTM back toward zero
      4. holding + MM IV converged .... close all options (pre-week-4 only)
      5. holding otherwise ............ hold (week 4 holds through expiry)
      6. flat + straddle edge > $10 ... enter ATM straddles up to net limit
      7. otherwise .................... wait, printing the insufficient edge
    """
    case = client.get("case")
    tick, status = int(case["tick"]), str(case["status"])
    securities = client.get("securities")
    info = parse_news(client.get("news"))

    rtm = next(s for s in securities if s["ticker"] == "RTM")
    spot = (float(rtm["bid"]) + float(rtm["ask"])) / 2.0
    rtm_position = int(rtm["position"])
    options = option_rows(securities)
    years = pricing.years_left(tick)
    rate = info["rate"] if info["rate"] is not None else 0.0
    sigma = forecast_sigma(tick, info["exact"])

    lines = [f"[tick {tick:3d} | {status}] spot {spot:.2f} | T {years:.4f}y | r {rate:.2%}"]
    exact_text = ", ".join(f"wk{w + 1} {v:.0%}" for w, v in sorted(info["exact"].items()))
    range_text = ", ".join(f"wk{w + 1} {lo:.0%}-{hi:.0%}" for w, (lo, hi) in sorted(info["ranges"].items()))
    lines.append(f"  news: exact [{exact_text or 'none'}] | ranges (hint only) [{range_text or 'none'}]")
    for headline in info["unparsed"]:
        lines.append(f"  !! UNPARSED vol news (check template): {headline}")

    decision, trades = "wait", []
    gross = sum(abs(o["position"]) for o in options)
    net = sum(o["position"] for o in options)

    if status != "ACTIVE" or tick >= pricing.TOTAL_TICKS:
        decision = "wait: case not active or expired"
    elif sigma is None:
        decision = "wait: no exact vol announcement parsed yet"
    else:
        # ATM diagnostics: our forecast vs what the MM's quotes imply.
        strike = min({o["strike"] for o in options}, key=lambda k: abs(k - spot))
        call = next(o for o in options if o["strike"] == strike and o["kind"] == "C")
        put = next(o for o in options if o["strike"] == strike and o["kind"] == "P")
        call_iv = pricing.implied_vol("C", (call["bid"] + call["ask"]) / 2, spot, strike, years, rate)
        put_iv = pricing.implied_vol("P", (put["bid"] + put["ask"]) / 2, spot, strike, years, rate)
        market_iv = (call_iv + put_iv) / 2 if call_iv and put_iv else call_iv or put_iv
        fair = (pricing.bs_price("C", spot, strike, years, rate, sigma)
                + pricing.bs_price("P", spot, strike, years, rate, sigma))
        buy_edge = (fair - (call["ask"] + put["ask"])) * MULT - COST_RESERVE
        sell_edge = ((call["bid"] + put["bid"]) - fair) * MULT - COST_RESERVE
        delta = portfolio_delta(spot, years, rate, sigma, options, rtm_position)
        lines.append(f"  forecast σ {sigma:.1%} | ATM K={strike:g} MM IV {market_iv:.1%}"
                     if market_iv else f"  forecast σ {sigma:.1%} | ATM K={strike:g} MM IV n/a")
        lines.append(f"  straddle fair ${fair:.2f} vs mkt {call['bid'] + put['bid']:.2f}/{call['ask'] + put['ask']:.2f}"
                     f" | edge/straddle: BUY {buy_edge:+.0f}$, SELL {sell_edge:+.0f}$ (after ${COST_RESERVE:.0f} costs)")
        lines.append(f"  position: opts gross {gross}/{GROSS_LIMIT} net {net:+d}/±{NET_LIMIT}"
                     f" | RTM {rtm_position:+d} | delta {delta:+.0f} (hedge at ±{HEDGE_TRIGGER}, band ±{DELTA_BAND})")

        if abs(delta) >= HEDGE_TRIGGER:
            hedge = max(-MAX_RTM_ORDER, min(MAX_RTM_ORDER, -round(delta)))
            hedge = max(-RTM_LIMIT - rtm_position, min(RTM_LIMIT - rtm_position, hedge))
            decision = f"hedge: |delta| {abs(delta):.0f} >= {HEDGE_TRIGGER}, trade RTM back toward 0"
            trades = [("RTM", hedge)]
        elif gross and tick < WEEK4_TICK and market_iv is not None and abs(market_iv - sigma) < CONVERGED_IV:
            decision = (f"exit: MM IV {market_iv:.1%} converged to forecast {sigma:.1%}"
                        f" (within {CONVERGED_IV:.0%}), close all options")
            trades = [(o["symbol"], -o["position"]) for o in options if o["position"]]
        elif gross:
            hold_why = "week 4: holding through expiry for gamma" if tick >= WEEK4_TICK \
                else f"MM IV {market_iv:.1%} still {abs(market_iv - sigma):.1%} from forecast" if market_iv \
                else "MM IV unavailable"
            decision = f"hold: {hold_why}"
        elif max(buy_edge, sell_edge) > ENTRY_EDGE:
            sign = 1 if buy_edge >= sell_edge else -1
            size = straddle_capacity(sign, options)
            if size:
                side = "BUY" if sign > 0 else "SELL"
                decision = (f"enter: {side} {size} straddles @ K={strike:g},"
                            f" edge ${max(buy_edge, sell_edge):.0f}/straddle > ${ENTRY_EDGE:.0f} threshold")
                # Interleave call/put chunks so an error mid-entry never
                # leaves a large naked single leg.
                remaining = size
                while remaining:
                    chunk = min(remaining, MAX_OPT_ORDER)
                    trades += [(call["symbol"], sign * chunk), (put["symbol"], sign * chunk)]
                    remaining -= chunk
            else:
                decision = "wait: edge present but no capacity under gross/net limits"
        else:
            decision = f"wait: best edge ${max(buy_edge, sell_edge):.0f}/straddle below ${ENTRY_EDGE:.0f} threshold"

    lines.append(f"  >> {decision}")
    for ticker, quantity in trades:
        submit(client, ticker, quantity, dry_run, lines)
    print("\n".join(lines), flush=True)
    with log_path.open("a") as log:
        log.write(json.dumps({"tick": tick, "status": status, "spot": spot, "sigma": sigma,
                              "news": {k: v for k, v in info.items() if k != "unparsed"},
                              "gross": gross, "net": net, "rtm": rtm_position,
                              "decision": decision, "trades": trades}) + "\n")
    return tick, len(info["exact"]) + len(info["ranges"])


def main():
    parser = argparse.ArgumentParser(description="RITC volatility bot")
    parser.add_argument("--dry-run", action="store_true", help="explain decisions but send no orders")
    args = parser.parse_args()
    client = Client()
    log_path = Path(__file__).parent / "logs" / "decisions.jsonl"
    log_path.parent.mkdir(exist_ok=True)
    print(f"volatility bot starting ({'DRY RUN' if args.dry_run else 'LIVE'}), logging to {log_path}")
    last = None
    try:
        while True:
            try:
                case = client.get("case")
                news_count = len(client.get("news"))
                current = (case["tick"], case["period"], news_count)
                if current != last:
                    process_tick(client, args.dry_run, log_path)
                    last = current
            except (RuntimeError, OSError) as error:
                print(f"  transient error, retrying: {error}", flush=True)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("stopped by user; check open positions in the RIT client")


if __name__ == "__main__":
    main()
