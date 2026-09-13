"""Strict, deterministic weekly volatility extraction; unknown wording blocks entry."""

import math
import re
from typing import Any, Iterable, Mapping


def forecast(news: Iterable[Mapping[str, Any]], tick: int, fallback: float | None = None) -> dict[str, Any]:
    """Parse weekly news and return a variance-weighted remaining forecast.

    :param news: Raw RIT news rows visible at the current tick.
    :param tick: Current competition tick.
    :param fallback: Explicit volatility used before the first parsed regime.
    :returns: Forecast sigma and parser audit details.
    """
    weeks = {}
    recognized = []
    unknown = []
    for item in sorted(news, key=lambda x: (x.get("tick", 0), x.get("news_id", 0))):
        published = item.get("tick", 0)
        if published > tick:
            continue
        text = ((item.get("headline") or "") + " " + (item.get("body") or "")).lower()
        if "volatility" not in text:
            continue
        # Ignore the interest-rate percentage preceding volatility in the opening news.
        volatility_text = text.rsplit("volatility", 1)[1]
        numbers = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%", volatility_text)
        range_match = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:%\s*)?(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*%",
            volatility_text,
        )
        values = [
            float(n) / 100 for n in (range_match.groups() if range_match else numbers)
        ]
        explicit = re.search(r"week\s+(\d)", item.get("ticker") or "", re.I)
        explicit = explicit or re.search(r"(?:for|during)\s+week\s+(\d)", text)
        week = min(3, max(0, int(published) // 75))
        if explicit:
            week = int(explicit[1]) - 1
        elif "next week" in text:
            week += 1
        elif (
            "this week" not in text
            and "current week" not in text
            and not (published <= 1 and "current" in text)
        ):
            unknown.append(item.get("news_id"))
            continue
        if (
            not values
            or len(values) > 2
            or not all(0 < v < 5 for v in values)
            or not 0 <= week <= 3
        ):
            unknown.append(item.get("news_id"))
            continue
        # Midpoint variance for a range, not midpoint volatility.
        weeks[week] = sum(v * v for v in values) / len(values)
        recognized.append(item.get("news_id"))
    current = min(3, max(0, int(tick) // 75))
    variance = weeks.get(current, fallback * fallback if fallback is not None else None)
    if variance is None or unknown:
        return {"sigma": None, "recognized": recognized, "unparsed": unknown}
    weighted = duration = 0
    for week in range(current, 4):
        variance = weeks.get(week, variance)
        seconds = max(0, (week + 1) * 75 - max(tick, week * 75))
        weighted += variance * seconds
        duration += seconds
    return {
        "sigma": math.sqrt(weighted / duration) if duration else math.sqrt(variance),
        "recognized": recognized,
        "unparsed": unknown,
        "assumption": "Latest known variance carried forward into unannounced weeks",
    }
