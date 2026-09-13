"""Executable volatility and static-arbitrage signal generation."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from models.volatility import OptionGreeks, implied_volatility, option_greeks
from volatility.config import VolatilityConfig
from volatility.market_data import MarketState, OptionQuote


Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class OptionModel:
    """Fair and market measurements for one option.

    :param quote: Executable option quote and confirmed position.
    :param fair: Black-Scholes fair value and Greeks.
    :param market_iv: Implied volatility at the quote midpoint.
    """

    quote: OptionQuote
    fair: OptionGreeks
    market_iv: float | None


@dataclass(frozen=True)
class Opportunity:
    """One executable option-side opportunity after cost reservations.

    :param symbol: Option ticker.
    :param side: Direction needed to capture the edge.
    :param fair_price: Forecast-model price per option share.
    :param executable_price: Ask for a buy or bid for a sell.
    :param expected_edge_per_contract: Net expected edge after reserved costs.
    :param fair_iv: Forecast remaining volatility.
    :param market_iv: Midpoint implied volatility.
    :param delta: Fair-model delta per option share.
    :param gamma: Fair-model gamma per option share.
    :param vega: Fair-model vega per option share.
    """

    symbol: str
    side: Side
    fair_price: float
    executable_price: float
    expected_edge_per_contract: float
    fair_iv: float
    market_iv: float | None
    delta: float
    gamma: float
    vega: float


@dataclass(frozen=True)
class StraddleOpportunity:
    """A paired ATM call-and-put volatility signal.

    :param strike: Common strike of the call and put.
    :param side: Long-vol buy or short-vol sell direction.
    :param call: Call-side opportunity.
    :param put: Put-side opportunity.
    :param expected_edge_per_contract: Combined net edge for one straddle.
    """

    strike: float
    side: Side
    call: Opportunity
    put: Opportunity
    expected_edge_per_contract: float


@dataclass(frozen=True)
class ParityOpportunity:
    """An executable put-call-parity discrepancy.

    :param strike: Common option strike.
    :param edge_per_pair: Net edge after all four executable legs and reserves.
    :param description: Human-readable trade construction.
    """

    strike: float
    edge_per_pair: float
    description: str


def model_options(state: MarketState, fair_sigma: float, config: VolatilityConfig) -> tuple[OptionModel, ...]:
    """Price every listed option using the current remaining-volatility estimate.

    :param state: Validated market state.
    :param fair_sigma: Forecast remaining annualized volatility.
    :param config: Strategy and clock configuration.
    :returns: Fair models in ticker order.
    """

    years = state.time_to_expiry(config.expiry_tick, config.ticks_per_trading_year)
    return tuple(
        OptionModel(
            quote=quote,
            fair=option_greeks(state.rtm.mid, quote.strike, years, config.risk_free_rate, fair_sigma, quote.kind),
            market_iv=implied_volatility(quote.quote.mid, state.rtm.mid, quote.strike, years, config.risk_free_rate, quote.kind),
        )
        for quote in state.options
    )


def _reserved_cost_per_contract(model: OptionModel, config: VolatilityConfig) -> float:
    """Estimate round-trip costs and a model-risk cushion for one option contract.

    The executable bid or ask already accounts for crossing the entry spread, so
    it is not charged again here.  The reserve still assumes a future option
    exit commission and one initial-plus-one-closing RTM hedge.  That gives::

        2 * option_commission
        + 2 * abs(delta) * contract_multiplier * rtm_commission_per_share
        + safety_margin_per_contract

    A straddle sums this reserve for each leg.  This deliberately overstates
    RTM hedge cost because call and put deltas partially offset; V1 accepts the
    false negatives in exchange for avoiding fragile, small theoretical edges.
    Calibration can replace this with a portfolio-level hedge estimate.

    :param model: Fair option model.
    :param config: Cost and multiplier settings.
    :returns: Conservative dollar reserve per contract.
    """

    hedge_shares = abs(model.fair.delta) * config.contract_multiplier
    return (2.0 * config.option_commission
            + 2.0 * hedge_shares * config.rtm_commission_per_share
            + config.safety_margin_per_contract)


def find_mispricings(models: tuple[OptionModel, ...], fair_sigma: float, config: VolatilityConfig) -> tuple[Opportunity, ...]:
    """Compare fair values with executable bids and asks after transaction reserves.

    :param models: Fair models for listed options.
    :param fair_sigma: Forecast remaining annualized volatility.
    :param config: Cost and multiplier settings.
    :returns: Both buy and sell opportunities with positive net edge only.
    """

    opportunities: list[Opportunity] = []
    for model in models:
        reserve = _reserved_cost_per_contract(model, config)
        buy_edge = (model.fair.price - model.quote.quote.ask) * config.contract_multiplier - reserve
        sell_edge = (model.quote.quote.bid - model.fair.price) * config.contract_multiplier - reserve
        if buy_edge > 0.0:
            opportunities.append(Opportunity(model.quote.symbol, "BUY", model.fair.price, model.quote.quote.ask,
                                             buy_edge, fair_sigma, model.market_iv, model.fair.delta,
                                             model.fair.gamma, model.fair.vega))
        if sell_edge > 0.0:
            opportunities.append(Opportunity(model.quote.symbol, "SELL", model.fair.price, model.quote.quote.bid,
                                             sell_edge, fair_sigma, model.market_iv, model.fair.delta,
                                             model.fair.gamma, model.fair.vega))
    return tuple(sorted(opportunities, key=lambda item: item.expected_edge_per_contract, reverse=True))


def select_atm_straddle(models: tuple[OptionModel, ...], opportunities: tuple[Opportunity, ...], state: MarketState) -> StraddleOpportunity | None:
    """Choose the nearest-ATM paired call and put with a common executable side.

    :param models: Fair models used to identify the nearest listed strike.
    :param opportunities: Cost-adjusted one-leg opportunities.
    :param state: Current RTM midpoint used for ATM selection.
    :returns: Best eligible straddle, or ``None`` when either leg lacks edge.
    """

    if not models:
        return None
    strike = min({model.quote.strike for model in models}, key=lambda value: abs(value - state.rtm.mid))
    indexed = {(item.symbol, item.side): item for item in opportunities}
    call = next((model.quote.symbol for model in models if model.quote.strike == strike and model.quote.kind == "C"), None)
    put = next((model.quote.symbol for model in models if model.quote.strike == strike and model.quote.kind == "P"), None)
    if call is None or put is None:
        return None
    candidates = [
        StraddleOpportunity(strike, side, indexed[(call, side)], indexed[(put, side)],
                            indexed[(call, side)].expected_edge_per_contract + indexed[(put, side)].expected_edge_per_contract)
        for side in ("BUY", "SELL") if (call, side) in indexed and (put, side) in indexed
    ]
    return max(candidates, key=lambda item: item.expected_edge_per_contract) if candidates else None


def scan_put_call_parity(models: tuple[OptionModel, ...], state: MarketState, config: VolatilityConfig) -> tuple[ParityOpportunity, ...]:
    """Find executable put-call-parity discrepancies after cost reservations.

    :param models: Fair models for all listed options.
    :param state: Current market state.
    :param config: Rate, multiplier, and cost configuration.
    :returns: Positive-net-edge parity opportunities for logging and later use.
    """

    years = state.time_to_expiry(config.expiry_tick, config.ticks_per_trading_year)
    grouped: dict[float, dict[str, OptionModel]] = {}
    for model in models:
        grouped.setdefault(model.quote.strike, {})[model.quote.kind] = model
    results: list[ParityOpportunity] = []
    for strike, pair in grouped.items():
        if set(pair) != {"C", "P"}:
            continue
        call, put = pair["C"].quote.quote, pair["P"].quote.quote
        forward_difference = state.rtm.mid - strike * math.exp(-config.risk_free_rate * years)
        reserve = 2.0 * config.option_commission + config.rtm_commission_per_share * config.contract_multiplier
        cheap_conversion = (call.bid - put.ask - forward_difference) * config.contract_multiplier - reserve
        cheap_reversal = (forward_difference - (call.ask - put.bid)) * config.contract_multiplier - reserve
        if cheap_conversion > 0:
            results.append(ParityOpportunity(strike, cheap_conversion, "sell call, buy put, buy RTM-equivalent"))
        if cheap_reversal > 0:
            results.append(ParityOpportunity(strike, cheap_reversal, "buy call, sell put, sell RTM-equivalent"))
    return tuple(sorted(results, key=lambda item: item.edge_per_pair, reverse=True))
