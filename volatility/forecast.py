"""News-driven remaining integrated-variance forecasts."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


PERCENT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%")
RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*%?\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*%")


@dataclass(frozen=True)
class VolatilityRegime:
    """A volatility estimate applying from one tick range.

    :param start_tick: First tick covered by the forecast.
    :param end_tick: First tick after the forecast interval.
    :param variance: Annualized variance, not annualized volatility.
    :param news_id: Source RIT news identifier when available.
    """

    start_tick: int
    end_tick: int
    variance: float
    news_id: int | str | None


@dataclass(frozen=True)
class ForecastResult:
    """A remaining-volatility forecast and its audit data.

    :param sigma: Annualized remaining volatility, or ``None`` if unknown.
    :param integrated_variance: Remaining variance times remaining ticks.
    :param regimes: Parsed future and current volatility regimes.
    :param recognized_news_ids: News records parsed successfully.
    :param unparsed_news_ids: Relevant news records rejected conservatively.
    """

    sigma: float | None
    integrated_variance: float | None
    regimes: tuple[VolatilityRegime, ...]
    recognized_news_ids: tuple[int | str | None, ...]
    unparsed_news_ids: tuple[int | str | None, ...]


def _text(item: Mapping[str, Any]) -> str:
    """Join textual RIT news fields into a normalized sentence.

    :param item: Raw RIT news record.
    :returns: Lowercase text used by the deterministic parser.
    """

    return " ".join(str(item.get(field) or "") for field in ("ticker", "headline", "body")).lower()


def _variance_from_text(text: str) -> float | None:
    """Extract one annualized variance estimate from a news message.

    A stated range is reduced as the mean of endpoint variances, preserving the
    requested variance-first representation.

    :param text: Normalized news content.
    :returns: Annualized variance, or ``None`` when no valid percentage exists.
    """

    tail = text.rsplit("volatility", 1)[-1]
    range_match = RANGE.search(tail)
    values = [float(value) / 100.0 for value in (range_match.groups() if range_match else PERCENT.findall(tail))]
    if not values or len(values) > 2 or not all(0.0 < value < 5.0 for value in values):
        return None
    return sum(value * value for value in values) / len(values)


def _interval_for_news(text: str, published_tick: int, period_ticks: int, expiry_tick: int) -> tuple[int, int] | None:
    """Map relative news language to an actual future tick interval.

    :param text: Normalized news content.
    :param published_tick: Tick when the news became available.
    :param period_ticks: Length of a competition volatility regime.
    :param expiry_tick: Option-expiry tick.
    :returns: ``(start, end)`` or ``None`` when the timing is ambiguous.
    """

    explicit = re.search(r"week\s+(\d+)", text)
    current_period = published_tick // period_ticks
    if explicit:
        period = int(explicit.group(1)) - 1
    elif "next week" in text:
        period = current_period + 1
    elif "this week" in text or "current week" in text or (published_tick <= 1 and "current" in text):
        period = current_period
    else:
        return None
    start = max(published_tick if period == current_period else period * period_ticks, period * period_ticks)
    end = min(expiry_tick, (period + 1) * period_ticks)
    return (start, end) if start < end else None


def parse_regimes(news_history: Iterable[Mapping[str, Any]], current_tick: int, expiry_tick: int, period_ticks: int = 75) -> tuple[tuple[VolatilityRegime, ...], tuple[int | str | None, ...], tuple[int | str | None, ...]]:
    """Parse analyst messages into current and future variance regimes.

    :param news_history: Raw news records received from RIT.
    :param current_tick: Current competition tick.
    :param expiry_tick: Option-expiry tick.
    :param period_ticks: Published volatility-regime width in ticks.
    :returns: Regimes, recognized news IDs, and conservatively rejected news IDs.
    """

    regimes: list[VolatilityRegime] = []
    recognized: list[int | str | None] = []
    unparsed: list[int | str | None] = []
    for item in sorted(news_history, key=lambda row: (int(row.get("tick", 0)), str(row.get("news_id", "")))):
        published_tick = int(item.get("tick", 0))
        if published_tick > current_tick:
            continue
        text = _text(item)
        if "volatility" not in text:
            continue
        news_id = item.get("news_id")
        variance = _variance_from_text(text)
        interval = _interval_for_news(text, published_tick, period_ticks, expiry_tick)
        if variance is None or interval is None:
            unparsed.append(news_id)
            continue
        regimes.append(VolatilityRegime(*interval, variance, news_id))
        recognized.append(news_id)
    return tuple(regimes), tuple(recognized), tuple(unparsed)


def estimate_remaining_volatility(current_time: int, expiry_time: int, news_history: Iterable[Mapping[str, Any]], market_state: object | None = None, fallback_sigma: float | None = None) -> ForecastResult:
    """Estimate remaining volatility by averaging integrated variance through expiry.

    The latest known regime is carried forward through an unannounced interval.
    This is explicit in the result rather than a hidden timestamp assumption.

    :param current_time: Current competition tick.
    :param expiry_time: Option-expiry tick.
    :param news_history: Analyst/news records observed so far.
    :param market_state: Reserved for future estimators using state features.
    :param fallback_sigma: Explicit operator forecast used only when no current regime exists.
    :returns: Forecast with integrated variance and parser audit information.
    """

    del market_state
    regimes, recognized, unparsed = parse_regimes(news_history, current_time, expiry_time)
    if unparsed:
        return ForecastResult(None, None, regimes, recognized, unparsed)
    if current_time >= expiry_time:
        return ForecastResult(0.0, 0.0, regimes, recognized, unparsed)
    total, cursor, latest = 0.0, current_time, fallback_sigma * fallback_sigma if fallback_sigma is not None else None
    for regime in sorted(regimes, key=lambda item: (item.start_tick, item.end_tick)):
        if regime.end_tick <= current_time:
            continue
        start, end = max(cursor, regime.start_tick), min(expiry_time, regime.end_tick)
        if cursor < start:
            if latest is None:
                return ForecastResult(None, None, regimes, recognized, unparsed)
            total += (start - cursor) * latest
        if start < end:
            total += (end - start) * regime.variance
            cursor, latest = end, regime.variance
    if cursor < expiry_time:
        if latest is None:
            return ForecastResult(None, None, regimes, recognized, unparsed)
        total += (expiry_time - cursor) * latest
    duration = expiry_time - current_time
    return ForecastResult(math.sqrt(total / duration), total, regimes, recognized, unparsed)
