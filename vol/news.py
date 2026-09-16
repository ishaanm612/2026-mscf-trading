"""Parse the templated analyst news into weekly vol forecasts.

Observed schedule on the practice server: tick 1 carries the risk-free rate
and the starting vol; exact announcements ("this week will be X%") land at
ticks 75/150/225; range forecasts ("next week will be between X% and Y%")
land mid-week at ~36/112/187.

Weeks are 75-tick blocks indexed 0-3: week w covers ticks [75w, 75w+75).
An exact/current message at tick t applies to week t//75; a range message
applies to the following week, t//75 + 1.
"""
import math
import re

from pricing import TOTAL_TICKS

WEEK_TICKS = 75

RANGE = re.compile(r"will be between (\d+(?:\.\d+)?)\s*%\s*and\s*(\d+(?:\.\d+)?)\s*%")
EXACT = re.compile(r"will be (\d+(?:\.\d+)?)\s*%")
CURRENT = re.compile(r"volatility (?:is|of) (\d+(?:\.\d+)?)\s*%")
RATE = re.compile(r"risk[- ]free rate is (\d+(?:\.\d+)?)\s*%")


def parse_news(news_items):
    """Reduce the raw RIT news list to what the strategy needs.

    Returns a dict with:
      rate     -- risk-free rate as a decimal, or None if never announced
      exact    -- {week: sigma} from exact/current announcements (decimals)
      ranges   -- {week: (low, high)} from range forecasts (decimals)
      unparsed -- headlines that mention volatility but matched no template;
                  these must be surfaced loudly, never silently dropped
    """
    rate, exact, ranges, unparsed = None, {}, {}, []
    for item in sorted(news_items, key=lambda row: int(row.get("tick", 0))):
        tick = int(item.get("tick", 0))
        text = f"{item.get('headline') or ''} {item.get('body') or ''}".lower()
        rate_match = RATE.search(text)
        if rate_match:
            rate = float(rate_match.group(1)) / 100.0
        if "volatility" not in text:
            continue
        week = tick // WEEK_TICKS
        range_match = RANGE.search(text)
        exact_match = EXACT.search(text) or CURRENT.search(text)
        if range_match:
            low, high = (float(v) / 100.0 for v in range_match.groups())
            ranges[week + 1] = (low, high)
        elif exact_match:
            exact[week] = float(exact_match.group(1)) / 100.0
        elif not rate_match:
            unparsed.append(text.strip()[:120])
    return {"rate": rate, "exact": exact, "ranges": ranges, "unparsed": unparsed}


def forecast_sigma(tick, exact):
    """Annualized vol for pricing over the remaining life, from EXACT news only.

    Remaining variance is the tick-weighted average across remaining weeks,
    where each week uses its announced vol if known, else the latest earlier
    announcement carried forward. Range forecasts are deliberately excluded:
    per strategy, we do not pre-position on a range (no edge until the MM
    lags a realized announcement); ranges are only a direction hint.

    Returns None before any exact announcement exists or after expiry.
    """
    if tick >= TOTAL_TICKS or not exact:
        return None
    total_variance = 0.0
    for week in range(tick // WEEK_TICKS, TOTAL_TICKS // WEEK_TICKS):
        known = [w for w in exact if w <= week]
        if not known:
            return None
        sigma = exact[max(known)]
        start = max(tick, week * WEEK_TICKS)
        end = (week + 1) * WEEK_TICKS
        total_variance += (end - start) * sigma * sigma
    return math.sqrt(total_variance / (TOTAL_TICKS - tick))
