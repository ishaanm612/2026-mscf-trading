"""Typed market-state ingestion for the volatility case."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


OPTION_SYMBOL = re.compile(r"RTM(?P<strike>\d+(?:\.\d+)?)(?P<kind>[CP])$")


@dataclass(frozen=True)
class Quote:
    """An executable two-sided quote.

    :param bid: Price available to a seller.
    :param ask: Price available to a buyer.
    """

    bid: float
    ask: float

    @property
    def mid(self) -> float:
        """Return the arithmetic midpoint of bid and ask.

        :returns: Midpoint price.
        """

        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class OptionQuote:
    """A listed RTM option and its executable quote.

    :param symbol: RIT option ticker.
    :param strike: Strike price in RTM dollars.
    :param kind: ``C`` for a call or ``P`` for a put.
    :param quote: Executable option quote.
    :param position: Confirmed contract position.
    """

    symbol: str
    strike: float
    kind: str
    quote: Quote
    position: int


@dataclass(frozen=True)
class MarketState:
    """Coherent volatility-market state built from a RIT snapshot.

    :param current_tick: Current competition clock tick.
    :param status: Case status supplied by RIT.
    :param rtm: Executable RTM quote.
    :param rtm_position: Confirmed RTM-share position.
    :param options: Listed options with quotes and positions.
    :param news_history: Raw RIT news messages observed up to this tick.
    :param raw: Original snapshot for execution-risk validation.
    """

    current_tick: int
    status: str
    rtm: Quote
    rtm_position: int
    options: tuple[OptionQuote, ...]
    news_history: tuple[Mapping[str, Any], ...]
    raw: Mapping[str, Any]

    def time_to_expiry(self, expiry_tick: int, ticks_per_trading_year: int) -> float:
        """Convert remaining case ticks into trading years.

        :param expiry_tick: Tick at which options expire.
        :param ticks_per_trading_year: Case ticks per trading year.
        :returns: Non-negative time to expiry in years.
        """

        return max(0.0, (expiry_tick - self.current_tick) / ticks_per_trading_year)


def parse_option_symbol(symbol: str) -> tuple[float, str] | None:
    """Parse a RIT RTM option ticker.

    :param symbol: Ticker to parse.
    :returns: ``(strike, kind)`` for an RTM option, otherwise ``None``.
    """

    match = OPTION_SYMBOL.fullmatch(symbol)
    return (float(match["strike"]), match["kind"]) if match else None


def _quote_from_security(security: Mapping[str, Any]) -> Quote | None:
    """Validate and construct a quote from one RIT security row.

    :param security: Raw security row.
    :returns: A quote when both sides are valid, otherwise ``None``.
    """

    bid, ask = security.get("bid"), security.get("ask")
    if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)) or bid < 0 or ask < bid:
        return None
    return Quote(float(bid), float(ask))


def from_snapshot(snapshot: Mapping[str, Any]) -> MarketState:
    """Build validated volatility state from a RIT snapshot.

    :param snapshot: Snapshot containing case, securities, and news records.
    :returns: Typed strategy state.
    :raises ValueError: If RTM or a required case field is absent or invalid.
    """

    case = snapshot.get("case")
    rows = snapshot.get("securities")
    if not isinstance(case, Mapping) or not isinstance(rows, Iterable):
        raise ValueError("Volatility snapshot requires case and securities")
    securities = {row.get("ticker"): row for row in rows if isinstance(row, Mapping)}
    rtm_row = securities.get("RTM")
    if not isinstance(rtm_row, Mapping):
        raise ValueError("Volatility snapshot is missing RTM")
    rtm = _quote_from_security(rtm_row)
    if rtm is None:
        raise ValueError("Volatility snapshot has invalid RTM quote")
    options: list[OptionQuote] = []
    for symbol, row in securities.items():
        if not isinstance(symbol, str) or not isinstance(row, Mapping):
            continue
        parsed, quote = parse_option_symbol(symbol), _quote_from_security(row)
        if parsed is None or quote is None:
            continue
        strike, kind = parsed
        options.append(OptionQuote(symbol, strike, kind, quote, int(row.get("position", 0))))
    return MarketState(int(case["tick"]), str(case["status"]), rtm, int(rtm_row.get("position", 0)),
                       tuple(sorted(options, key=lambda item: item.symbol)),
                       tuple(item for item in snapshot.get("news", []) if isinstance(item, Mapping)), snapshot)
