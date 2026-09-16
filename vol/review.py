"""Post-session analysis of a market-*.jsonl recording.

Answers the calibration questions from CLAUDE.md using only the raw
recording written by bot.py:
  1. Every news message verbatim, with how (or whether) our parser read it.
  2. Was the MM stale at tick 1 (free trade in week 1)?
  3. How fast does the MM's IV converge after each announcement (exit timing)?
  4. Realized vol per week vs announced (gamma P&L sanity).
  5. P&L trajectory and where it ended up.

Run:  python3 review.py logs/market-YYYYMMDD-HHMMSS.jsonl
"""
import json
import math
import sys
from pathlib import Path

import pricing
from bot import OPTION
from news import WEEK_TICKS, forecast_sigma, parse_news

CONVERGED = 0.01  # "converged" = MM IV within 1 vol pt of forecast


def load_rows(path):
    """One row per tick (last snapshot wins), sorted by tick."""
    by_tick = {}
    with open(path) as handle:
        for line in handle:
            row = json.loads(line)
            by_tick[int(row["case"]["tick"])] = row
    return [by_tick[t] for t in sorted(by_tick)]


def atm_iv(securities, tick, rate):
    """(spot, atm strike, average call/put mid IV) for one snapshot."""
    rtm = next(s for s in securities if s["ticker"] == "RTM")
    spot = (rtm["bid"] + rtm["ask"]) / 2.0
    years = pricing.years_left(tick)
    options = [(float(m.group(1)), m.group(2), s) for s in securities
               if (m := OPTION.fullmatch(str(s["ticker"])))]
    if not options or years <= 0:
        return spot, None, None
    strike = min({k for k, _, _ in options}, key=lambda k: abs(k - spot))
    ivs = [iv for k, kind, s in options if k == strike
           if (iv := pricing.implied_vol(kind, (s["bid"] + s["ask"]) / 2, spot, strike, years, rate))]
    return spot, strike, sum(ivs) / len(ivs) if ivs else None


def main(path):
    rows = load_rows(path)
    if not rows:
        print("empty recording")
        return
    all_news = max((r["news"] for r in rows), key=len)
    info = parse_news(all_news)
    rate = info["rate"] or 0.0

    print(f"=== {Path(path).name}: ticks {rows[0]['case']['tick']}-{rows[-1]['case']['tick']} ===\n")

    print("--- 1. news verbatim (fix news.py templates against this) ---")
    announcements = []  # (tick, week, sigma) exact announcements, in order
    for item in sorted(all_news, key=lambda n: int(n.get("tick", 0))):
        tick = int(item.get("tick", 0))
        text = f"{item.get('headline') or ''} | {item.get('body') or ''}"
        one = parse_news([item])
        if one["exact"]:
            week, sigma = next(iter(one["exact"].items()))
            tag, announcements = f"parsed EXACT wk{week + 1} {sigma:.0%}", announcements + [(tick, week, sigma)]
        elif one["ranges"]:
            week, (lo, hi) = next(iter(one["ranges"].items()))
            tag = f"parsed RANGE wk{week + 1} {lo:.0%}-{hi:.0%}"
        elif one["rate"] is not None:
            tag = f"parsed RATE {one['rate']:.2%}"
        else:
            tag = "!! UNPARSED"
        print(f"  [tick {tick:3d}] {tag}: {text.strip()[:150]}")
    if info["rate"] is None:
        print("  !! no risk-free rate parsed; review assumed r=0")

    def exact_known_at(tick):
        """Exact vols announced at or before a tick -- no hindsight leakage."""
        return {week: sigma for ann_tick, week, sigma in announcements if ann_tick <= tick}

    print("\n--- 2. was the MM stale at the start? ---")
    for row in rows[:8]:
        tick = int(row["case"]["tick"])
        sigma = forecast_sigma(tick, exact_known_at(tick))
        spot, strike, iv = atm_iv(row["securities"], tick, rate)
        print(f"  [tick {tick:3d}] ATM K={strike} MM IV {iv:.1%} vs forecast-so-far {sigma:.1%}"
              if iv and sigma else f"  [tick {tick:3d}] insufficient data")

    print("\n--- 3. MM IV convergence after each exact announcement (exit timing) ---")
    by_tick = {int(r["case"]["tick"]): r for r in rows}
    for ann_tick, week, _ in announcements:
        if ann_tick < 2:
            continue  # start-of-heat vol handled above
        gaps, converged_at = [], None
        for k in range(0, 41):
            row = by_tick.get(ann_tick + k)
            if row is None:
                continue
            sigma = forecast_sigma(ann_tick + k, exact_known_at(ann_tick + k))
            _, _, iv = atm_iv(row["securities"], ann_tick + k, rate)
            if sigma and iv:
                gaps.append(f"+{k}:{(iv - sigma) * 100:+.1f}")
                if converged_at is None and abs(iv - sigma) < CONVERGED:
                    converged_at = k
        print(f"  announcement @ tick {ann_tick} (wk{week + 1}): gap in vol pts "
              + " ".join(gaps[:12]) + (" ..." if len(gaps) > 12 else ""))
        print(f"    -> converged (<1pt) after {converged_at} ticks" if converged_at is not None
              else "    -> never within 1pt in the 40-tick window")

    print("\n--- 4. realized vol per week vs announced (gamma check) ---")
    spots = {int(r["case"]["tick"]): atm_iv(r["securities"], int(r["case"]["tick"]), rate)[0] for r in rows}
    for week in range(4):
        ticks = [t for t in sorted(spots) if week * WEEK_TICKS <= t < (week + 1) * WEEK_TICKS]
        returns = [math.log(spots[b] / spots[a]) for a, b in zip(ticks, ticks[1:])
                   if b - a == 1 and spots[a] > 0]
        if len(returns) < 10:
            continue
        realized = math.sqrt(sum(r * r for r in returns) / len(returns) * pricing.TICKS_PER_YEAR)
        announced = info["exact"].get(week)
        print(f"  week {week + 1}: realized {realized:.1%}"
              + (f" vs announced {announced:.1%} ({(realized - announced) * 100:+.1f} pts)" if announced else " (no exact announced)"))

    print("\n--- 5. P&L (account nlv from the recording) ---")
    nlvs = [(int(r["case"]["tick"]), rtm.get("nlv")) for r in rows
            if (rtm := next(s for s in r["securities"] if s["ticker"] == "RTM")).get("nlv") is not None]
    if nlvs:
        values = [v for _, v in nlvs]
        lo_tick = min(nlvs, key=lambda p: p[1])
        print(f"  start {values[0]:,.0f} -> end {values[-1]:,.0f}"
              f" | worst {lo_tick[1]:,.0f} @ tick {lo_tick[0]} | best {max(values):,.0f}")
    final = rows[-1]["securities"]
    for s in final:
        if s.get("position") or s.get("realized"):
            print(f"  {s['ticker']:>8}: position {s.get('position', 0):+d}"
                  f" realized {s.get('realized', 0):+,.0f} unrealized {s.get('unrealized', 0):+,.0f}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 review.py logs/market-<timestamp>.jsonl")
    main(sys.argv[1])
