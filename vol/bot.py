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
# Position structure: ATM straddles carry the vol bet; a smaller strangle at
# the farthest strikes the OTHER way nets it back under the net limit while
# filling gross. 850x2 + 350x2 = 2400 gross (limit 2500); 1700 - 700 = 1000
# net (limit 1000). Far wings have much less vega than ATM, so net vega is
# ~1.4-1.7x what 500 plain straddles gave under the same net cap.
ATM_STRADDLES = 850    # straddles at the ATM strike, in the edge direction
WING_CONTRACTS = 350   # contracts per far wing (lowest put + highest call), opposite way
LATE_ENTRY_TICK = 260  # from here, new entries at half size: late deep-ITM books
LATE_ENTRY_SCALE = 0.5 # were the worst trips and carry unhedgeable-delta fine risk
ENTRY_EDGE = 15.0      # $ per straddle, net of costs, required to enter
                       # $10 was noise-chasing (heat 1), but $40 exceeds what
                       # late-heat vega (~$6-8 per vol pt at tick 150+) can even
                       # produce, and would skip real week-3/4 shocks (heat 3)
COST_RESERVE = 10.0    # entry + exit commissions (4 x $2) + $2 hedge/safety cushion
CONVERGED_IV = 0.01    # exit when the position's REMAINING directional edge < 1 vol pt
HEDGE_TRIGGER = 2000   # hedge back toward 0 when |delta| exceeds this (band is 7000)
                       # was 5000: near expiry an ATM book's delta flips tens of
                       # thousands as spot crosses the strike, so hedge early and fully
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


def weave(legs, rtm_quantity=0, book_net=0):
    """Chunk a multi-leg trade round-robin so delta stays near zero mid-way.

    legs are (symbol, signed quantity to trade). Options go out 100 contracts
    per order, RTM in proportional slices up to 10k per round. Heat 3 showed
    why: closing all calls, then all puts, then the RTM serially left the book
    ~35k shares unbalanced for 2-3 ticks and collected ~$7k of band fines.
    """
    legs = [[symbol, quantity] for symbol, quantity in legs if quantity]
    rounds = max(((abs(q) + MAX_OPT_ORDER - 1) // MAX_OPT_ORDER for _, q in legs), default=0)
    trades, rtm_left, net = [], rtm_quantity, book_net
    for i in range(rounds):
        # Within each round, send the chunks that move net toward zero FIRST.
        # At net -1000 the server 400-rejects any sell before a buy has made
        # room -- alphabetical ordering deadlocked a whole exit on that.
        for leg in sorted(legs, key=lambda l: 0 if l[1] * net < 0 else 1):
            chunk = max(-MAX_OPT_ORDER, min(MAX_OPT_ORDER, leg[1]))
            if chunk:
                trades.append((leg[0], chunk))
                leg[1] -= chunk
                net += chunk
        share = max(-MAX_RTM_ORDER, min(MAX_RTM_ORDER, round(rtm_left / (rounds - i))))
        if share:
            trades.append(("RTM", share))
            rtm_left -= share
    if rtm_left:
        trades.append(("RTM", rtm_left))
    return trades


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
            try:
                client.request("POST", "orders", ticker=ticker, type="MARKET",
                               action=action, quantity=chunk)
                lines.append(f"  sent: {action} {chunk} {ticker} @ market")
            except RuntimeError as error:
                # A rejected order executed nothing; skip it and keep going.
                # One refused chunk must never freeze the rest of a sequence
                # (an exit deadlocked on this, leaving the book unhedged).
                lines.append(f"  REJECTED {action} {chunk} {ticker}: {error} -- skipping")
        remaining -= chunk
        time.sleep(0.05)


def process_tick(client, dry_run, decisions_path, market_path):
    """Read the market, print one explained decision block, act on it.

    Decision priority, first match wins:
      1. case inactive/expired ........ wait
      2. no exact vol parsed yet ...... wait (never trade on a guess)
      3. holding + remaining directional edge < 1 vol pt
         ............................. close options AND RTM hedge, interleaved
         (exit MUST outrank the hedge: a big book breaches the hedge trigger
         every tick and would starve the exit forever)
      4. option delta beyond RTM hedge capacity
         ............................. deleverage: close highest-delta legs
      5. |delta| >= 2,000 ............. hedge full delta back toward zero
      6. holding otherwise ............ hold, showing the remaining edge
      7. flat + straddle edge > $15 ... enter ATM straddles + opposite wings
         (wings exclude the ATM strike; half size from tick 260)
      8. otherwise .................... wait, printing the insufficient edge
    """
    case = client.get("case")
    if "volatility" not in str(case.get("name", "")).lower():
        # Server ports get reshuffled between practice days; never trade a
        # case this bot was not built for.
        raise RuntimeError(f"connected to wrong case: {case.get('name')!r} -- check the port")
    tick, status = int(case["tick"]), str(case["status"])
    securities = client.get("securities")
    raw_news = client.get("news")
    info = parse_news(raw_news)

    # Record the raw market FIRST, before any decision logic can fail: with
    # every security row (quotes, positions, realized/unrealized P&L, nlv)
    # and the verbatim news text, a heat can be fully replayed offline to
    # calibrate the parser, the MM's convergence speed, and realized vol.
    with market_path.open("a") as market_log:
        market_log.write(json.dumps({"time": time.time(), "case": case,
                                     "securities": securities, "news": raw_news}) + "\n")

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
    metrics = {}  # everything worth studying after the session
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
        metrics = {"atm_strike": strike, "call_iv": call_iv, "put_iv": put_iv,
                   "market_iv": market_iv, "iv_gap": market_iv - sigma if market_iv else None,
                   "fair_straddle": fair, "buy_edge": buy_edge, "sell_edge": sell_edge,
                   "delta": delta}
        lines.append(f"  forecast σ {sigma:.1%} | ATM K={strike:g} MM IV {market_iv:.1%}"
                     if market_iv else f"  forecast σ {sigma:.1%} | ATM K={strike:g} MM IV n/a")
        lines.append(f"  straddle fair ${fair:.2f} vs mkt {call['bid'] + put['bid']:.2f}/{call['ask'] + put['ask']:.2f}"
                     f" | edge/straddle: BUY {buy_edge:+.0f}$, SELL {sell_edge:+.0f}$ (after ${COST_RESERVE:.0f} costs)")
        lines.append(f"  position: opts gross {gross}/{GROSS_LIMIT} net {net:+d}/±{NET_LIMIT}"
                     f" | RTM {rtm_position:+d} | delta {delta:+.0f} (hedge at ±{HEDGE_TRIGGER}, band ±{DELTA_BAND})")

        # NOTE: the exit check must come BEFORE the hedge check. A big book at
        # high realized vol breaches the hedge trigger every tick, and in that
        # state a hedge-first ordering starves the exit forever (this held a
        # dead position from tick ~150 to 240 and gave back ~$40k). Exiting is
        # safe without a pre-hedge: weave() unwinds options and RTM together.
        if gross and market_iv is not None and \
                ((sigma - market_iv) if net >= 0 else (market_iv - sigma)) < CONVERGED_IV:
            # Remaining edge is DIRECTIONAL: long straddles profit while MM IV
            # is still below forecast, short while above. This also exits when
            # the MM overshoots past us (heat 1: long at 16.6%, MM overshot to
            # 24.7% while realized was 18.6% -- the old week-4 "hold for gamma"
            # rule bled ~$20k there). Holding for gamma is just the same test:
            # keep longs only while implied < forecast.
            decision = (f"exit: remaining edge {(sigma - market_iv if net >= 0 else market_iv - sigma) * 100:+.1f} "
                        f"vol pts < {CONVERGED_IV:.0%} (MM IV {market_iv:.1%} vs forecast {sigma:.1%})")
            # Close every option leg AND the RTM hedge, woven together so the
            # book is never one-legged mid-unwind (heat 1 and 3 fine source).
            trades = weave([(o["symbol"], -o["position"]) for o in options if o["position"]],
                           -rtm_position, book_net=net)
        elif gross and abs(rtm_position - round(delta)) - RTM_LIMIT > 2000:
            # Hedgeability guard: near expiry a deep-ITM book can carry more
            # delta than RTM can hedge (~85k shares vs the 50k limit in heat 6
            # of the endurance run: ~$2.6k/sec of band fines, unfixable by
            # hedging). Close the highest-delta legs until the rest is
            # hedgeable, with ~2k shares of headroom on each side.
            excess = abs(rtm_position - round(delta)) - RTM_LIMIT
            held = sorted((o for o in options if o["position"]),
                          key=lambda o: -abs(pricing.bs_delta(o["kind"], spot, o["strike"],
                                                              years, rate, sigma) * o["position"]))
            closes, reduction = [], 0.0
            for option in held:
                per_contract = abs(pricing.bs_delta(option["kind"], spot, option["strike"],
                                                    years, rate, sigma)) * MULT
                if per_contract < 1.0:
                    continue
                quantity = min(abs(option["position"]), int((excess + 2000 - reduction) / per_contract) + 1)
                closes.append((option["symbol"], -quantity if option["position"] > 0 else quantity))
                reduction += quantity * per_contract
                if reduction >= excess + 2000:
                    break
            decision = (f"deleverage: option delta {abs(rtm_position - delta):.0f} exceeds RTM hedge"
                        f" capacity by {excess:.0f} shares, closing {sum(abs(q) for _, q in closes)}"
                        f" contracts to stay hedgeable")
            trades = weave(closes, book_net=net)
        elif abs(delta) >= HEDGE_TRIGGER:
            # Hedge the FULL delta (submit() splits it into 10k-share orders);
            # capping at one order per tick let big strike-crossing delta flips
            # sit outside the ±7000 band collecting $0.10/share/sec fines.
            hedge = max(-RTM_LIMIT - rtm_position, min(RTM_LIMIT - rtm_position, -round(delta)))
            decision = f"hedge: |delta| {abs(delta):.0f} >= {HEDGE_TRIGGER}, trade RTM back toward 0"
            trades = [("RTM", hedge)]
        elif gross:
            decision = ("hold: remaining edge "
                        f"{(sigma - market_iv if net >= 0 else market_iv - sigma) * 100:+.1f} vol pts"
                        f" (MM IV {market_iv:.1%} vs forecast {sigma:.1%})"
                        if market_iv else "hold: MM IV unavailable")
        elif max(buy_edge, sell_edge) > ENTRY_EDGE:
            sign = 1 if buy_edge >= sell_edge else -1
            side = "BUY" if sign > 0 else "SELL"
            # ATM straddles in the edge direction plus opposite far wings.
            # Wings must EXCLUDE the ATM strike: on a 48-52 chain an entry at
            # an edge strike used to trade its wing INTO its own ATM leg,
            # self-cancelling ~30% of the intended vega (1/3 of endurance-run
            # entries). If the chain has no strike on one side, the surviving
            # wing doubles up; with no wing at all, cap ATM at the net limit.
            scale = LATE_ENTRY_SCALE if tick >= LATE_ENTRY_TICK else 1.0
            atm_size, wing_size = int(ATM_STRADDLES * scale), int(WING_CONTRACTS * scale)
            low_put = min((o for o in options if o["kind"] == "P" and o["strike"] < strike),
                          key=lambda o: o["strike"], default=None)
            high_call = max((o for o in options if o["kind"] == "C" and o["strike"] > strike),
                            key=lambda o: o["strike"], default=None)
            wings = ([(low_put, wing_size), (high_call, wing_size)] if low_put and high_call
                     else [(low_put, 2 * wing_size)] if low_put
                     else [(high_call, 2 * wing_size)] if high_call else [])
            if not wings:
                atm_size = min(atm_size, NET_LIMIT // 2)
            wing_text = "+".join(f"{q}x{o['symbol']}" for o, q in wings) or "none"
            decision = (f"enter: {side} {atm_size} straddles @ K={strike:g}"
                        f" + {'SELL' if sign > 0 else 'BUY'} wings {wing_text},"
                        f" edge ${max(buy_edge, sell_edge):.0f}/straddle > ${ENTRY_EDGE:.0f} threshold")
            trades = weave([(call["symbol"], sign * atm_size),
                            (put["symbol"], sign * atm_size)]
                           + [(o["symbol"], -sign * q) for o, q in wings])
        else:
            decision = f"wait: best edge ${max(buy_edge, sell_edge):.0f}/straddle below ${ENTRY_EDGE:.0f} threshold"

    lines.append(f"  >> {decision}")
    for ticker, quantity in trades:
        submit(client, ticker, quantity, dry_run, lines)
    print("\n".join(lines), flush=True)
    with decisions_path.open("a") as log:
        log.write(json.dumps({"time": time.time(), "tick": tick, "status": status,
                              "spot": spot, "sigma": sigma, "news": info,
                              "gross": gross, "net": net, "rtm": rtm_position,
                              "decision": decision, "trades": trades,
                              "dry_run": dry_run, **metrics}) + "\n")
    return tick, len(info["exact"]) + len(info["ranges"])


def main():
    parser = argparse.ArgumentParser(description="RITC volatility bot")
    parser.add_argument("--dry-run", action="store_true", help="explain decisions but send no orders")
    args = parser.parse_args()
    client = Client()
    # One timestamped pair of files per run, so separate heats never mix:
    # decisions-*.jsonl is what the bot thought; market-*.jsonl is the full
    # raw recording (replayable with review.py and shareable as calibration
    # data -- it contains positions/P&L but no names).
    logs = Path(__file__).parent / "logs"
    logs.mkdir(exist_ok=True)
    tag = time.strftime("%Y%m%d-%H%M%S")
    decisions_path, market_path = logs / f"decisions-{tag}.jsonl", logs / f"market-{tag}.jsonl"
    print(f"volatility bot starting ({'DRY RUN' if args.dry_run else 'LIVE'})\n"
          f"  decisions -> {decisions_path}\n  recording -> {market_path}")
    last = None
    try:
        while True:
            try:
                case = client.get("case")
                news_count = len(client.get("news"))
                current = (case["tick"], case["period"], news_count)
                if current != last:
                    process_tick(client, args.dry_run, decisions_path, market_path)
                    last = current
            except (RuntimeError, OSError) as error:
                print(f"  transient error, retrying: {error}", flush=True)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("stopped by user; check open positions in the RIT client")


if __name__ == "__main__":
    main()
