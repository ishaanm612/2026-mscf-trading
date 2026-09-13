"""Pure volatility-strategy orchestration that emits abstract desired trades."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from models.volatility import PortfolioGreeks, portfolio_greeks
from volatility.config import VolatilityConfig
from volatility.forecast import ForecastResult, estimate_remaining_volatility
from volatility.hedging import calculate_hedge_order
from volatility.market_data import MarketState, from_snapshot
from volatility.signals import OptionModel, ParityOpportunity, StraddleOpportunity, find_mispricings, model_options, scan_put_call_parity, select_atm_straddle


@dataclass(frozen=True)
class DesiredTrade:
    """A strategy request independent of transport and fill handling.

    :param symbol: RIT instrument ticker.
    :param quantity: Signed quantity; positive buys and negative sells.
    :param reason: Stable explanation for execution logs.
    """

    symbol: str
    quantity: int
    reason: str


@dataclass(frozen=True)
class StrategyDecision:
    """Everything needed to audit one V1 decision cycle.

    :param state: Typed market state used for the decision.
    :param forecast: Integrated-variance forecast.
    :param models: Fair option prices and Greeks.
    :param portfolio: Aggregated confirmed-position Greeks.
    :param straddle: ATM paired opportunity when available.
    :param parity: Secondary static-arbitrage observations.
    :param desired_trades: Abstract orders for the execution layer.
    :param reason: Human-readable reason for the chosen action.
    :param time_since_latest_news: Ticks since the newest observed news record.
    """

    state: MarketState
    forecast: ForecastResult
    models: tuple[OptionModel, ...]
    portfolio: PortfolioGreeks
    straddle: StraddleOpportunity | None
    parity: tuple[ParityOpportunity, ...]
    desired_trades: tuple[DesiredTrade, ...]
    reason: str
    time_since_latest_news: int | None

    def as_log_fields(self) -> dict[str, Any]:
        """Convert a decision to JSON-compatible structured-log fields.

        :returns: Snapshot, model, signal, portfolio, and desired-order fields.
        """

        return {
            "tick": self.state.current_tick,
            "rtm": {"bid": self.state.rtm.bid, "ask": self.state.rtm.ask, "mid": self.state.rtm.mid,
                    "position": self.state.rtm_position},
            "forecast": {"sigma": self.forecast.sigma, "integrated_variance": self.forecast.integrated_variance,
                         "recognized_news_ids": self.forecast.recognized_news_ids,
                         "unparsed_news_ids": self.forecast.unparsed_news_ids},
            "portfolio": self.portfolio.__dict__,
            "options": [{"symbol": item.quote.symbol, "bid": item.quote.quote.bid, "ask": item.quote.quote.ask,
                         "position": item.quote.position, "fair_price": item.fair.price, "market_iv": item.market_iv,
                         "delta": item.fair.delta, "gamma": item.fair.gamma, "vega": item.fair.vega,
                         "theta": item.fair.theta} for item in self.models],
            "straddle": None if self.straddle is None else {"strike": self.straddle.strike, "side": self.straddle.side,
                         "edge": self.straddle.expected_edge_per_contract},
            "parity": [item.__dict__ for item in self.parity],
            "desired_trades": [item.__dict__ for item in self.desired_trades],
            "reason": self.reason,
            "time_since_latest_news": self.time_since_latest_news,
        }


def _time_since_latest_news(state: MarketState) -> int | None:
    """Return ticks since the latest news record visible in the snapshot.

    :param state: Current typed market state.
    :returns: Age in ticks, or ``None`` when there is no timestamped news.
    """

    ticks = [int(item["tick"]) for item in state.news_history if isinstance(item.get("tick"), (int, float))]
    return state.current_tick - max(ticks) if ticks else None


def _portfolio(state: MarketState, models: tuple[OptionModel, ...], config: VolatilityConfig) -> PortfolioGreeks:
    """Aggregate confirmed inventory using the current option model Greeks.

    :param state: Current market state.
    :param models: Current fair option models.
    :param config: Multiplier configuration.
    :returns: Current portfolio Greeks.
    """

    return portfolio_greeks(state.rtm_position, [(item.quote.position, item.fair) for item in models], config.contract_multiplier)


def _size_for_edge(straddle: StraddleOpportunity, portfolio: PortfolioGreeks, state: MarketState,
                   config: VolatilityConfig) -> int:
    """Size a straddle from edge strength and remaining risk capacity.

    The available size is the smallest of the configured concentration cap,
    remaining server gross/net option capacity, gamma headroom, and vega
    headroom.  Net edge then scales that safe capacity continuously until
    ``edge_for_full_risk`` is reached.  This avoids the old fixed 30-contract
    bucket while still refusing to use all server capacity on a weak signal.

    :param straddle: Cost-adjusted paired option opportunity.
    :param portfolio: Greeks from confirmed current inventory.
    :param state: Fresh raw limits and positions.
    :param config: Risk, capacity, and edge settings.
    :returns: Contracts for each straddle leg, or zero when no capacity remains.
    """

    if straddle.expected_edge_per_contract < config.entry_edge_per_contract:
        return 0
    options = [option for option in state.options]
    gross = sum(abs(option.position) for option in options)
    net = sum(option.position for option in options)
    option_limit = next((limit for limit in state.raw.get("limits", [])
                         if str(limit.get("name", "")).lower() in {"option", "options", "limit-opt"}), {})
    gross_limit = float(option_limit.get("gross_limit", 0)) * config.max_option_position_fraction
    net_limit = float(option_limit.get("net_limit", 0)) * config.max_option_position_fraction
    gross_capacity = max(0, int((gross_limit - gross) // 2))
    if straddle.side == "BUY":
        net_capacity = max(0, int((net_limit - net) // 2))
    else:
        net_capacity = max(0, int((net_limit + net) // 2))
    gamma_per_straddle = abs(straddle.call.gamma + straddle.put.gamma) * config.contract_multiplier
    vega_per_straddle = abs(straddle.call.vega + straddle.put.vega) * config.contract_multiplier
    gamma_capacity = max(0, int((config.max_portfolio_gamma - abs(portfolio.gamma)) / max(gamma_per_straddle, 1e-9)))
    vega_capacity = max(0, int((config.max_portfolio_vega - abs(portfolio.vega)) / max(vega_per_straddle, 1e-9)))
    capacity = min(config.max_straddle_contracts, gross_capacity, net_capacity, gamma_capacity, vega_capacity)
    edge_fraction = min(1.0, straddle.expected_edge_per_contract / config.edge_for_full_risk)
    return max(0, int(capacity * edge_fraction))


class VolatilityStrategy:
    """Stateful V1 controller that reacts to new news and manages existing risk.

    Strategy code never submits an order.  Consumers must validate each
    ``DesiredTrade`` against fresh account state and reconcile confirmed fills.

    :param config: Cost, clock, sizing, and risk settings.
    :param fallback_sigma: Explicit fallback for a heat with incomplete news.
    """

    def __init__(self, config: VolatilityConfig | None = None, fallback_sigma: float | None = None) -> None:
        """Initialize an empty strategy-news cursor.

        :param config: Optional non-default strategy configuration.
        :param fallback_sigma: Explicit operator-supplied fallback volatility.
        """

        self.config = config or VolatilityConfig()
        self.fallback_sigma = fallback_sigma
        self._seen_news_ids: set[int | str | None] = set()

    def decide(self, snapshot: dict[str, Any]) -> StrategyDecision:
        """Model a snapshot and produce a bounded V1 action plan.

        Existing inventory gets priority: breach prevention, delta hedge, and
        convergence exits come before a new news-triggered straddle entry.

        :param snapshot: Fresh coherent RIT volatility snapshot.
        :returns: Full decision record, including abstract desired orders.
        """

        state = from_snapshot(snapshot)
        forecast = estimate_remaining_volatility(state.current_tick, self.config.expiry_tick, state.news_history,
                                                 state, self.fallback_sigma)
        if forecast.sigma is None:
            return StrategyDecision(state, forecast, (), PortfolioGreeks(float(state.rtm_position), 0, 0, 0), None, (), (),
                            "wait: volatility news is incomplete or ambiguous", _time_since_latest_news(state))
        models = model_options(state, forecast.sigma, self.config)
        portfolio = _portfolio(state, models, self.config)
        opportunities = find_mispricings(models, forecast.sigma, self.config)
        straddle = select_atm_straddle(models, opportunities, state)
        parity = scan_put_call_parity(models, state, self.config)
        news_ids = {item.get("news_id") for item in state.news_history}
        new_news = bool(news_ids - self._seen_news_ids)
        self._seen_news_ids.update(news_ids)
        age = _time_since_latest_news(state)
        if state.status != "ACTIVE" or state.current_tick >= self.config.expiry_tick:
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (), "wait: case inactive or expired", age)
        if state.current_tick >= self.config.close_tick:
            closing = tuple(DesiredTrade(item.quote.symbol, -item.quote.position, "expiry inventory reduction")
                            for item in models if item.quote.position)
            if closing:
                return StrategyDecision(state, forecast, models, portfolio, straddle, parity, closing,
                                        "exit: configured expiry window", age)
            if state.rtm_position:
                return StrategyDecision(state, forecast, models, portfolio, straddle, parity,
                                        (DesiredTrade("RTM", -state.rtm_position, "expiry RTM reduction"),),
                                        "exit: configured expiry window", age)
        hedge = calculate_hedge_order(portfolio.delta, self.config.hedge_threshold)
        if abs(portfolio.delta) >= self.config.max_safe_delta:
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity,
                                    (DesiredTrade("RTM", hedge, "delta safety hedge"),), "hedge: internal delta boundary", age)
        if hedge:
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity,
                                    (DesiredTrade("RTM", hedge, "delta hedge"),), "hedge: outside no-trade band", age)
        open_atm = [item for item in models if abs(item.quote.strike - state.rtm.mid) == min(abs(other.quote.strike - state.rtm.mid) for other in models)] if models else []
        if any(item.quote.position for item in open_atm) and (straddle is None or straddle.expected_edge_per_contract < self.config.exit_edge_per_contract):
            exits = tuple(DesiredTrade(item.quote.symbol, -item.quote.position, "straddle convergence exit")
                          for item in open_atm if item.quote.position)
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, exits, "exit: remaining edge below hysteresis threshold", age)
        if any(item.quote.position for item in models):
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (),
                                    "wait: existing option inventory is being held and risk-managed", age)
        if not new_news:
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (), "wait: no new analyst/news event", age)
        if straddle is None:
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (), "wait: ATM straddle lacks executable edge", age)
        quantity = _size_for_edge(straddle, portfolio, state, self.config)
        signed = quantity if straddle.side == "BUY" else -quantity
        if quantity == 0:
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (), "wait: straddle edge below entry threshold", age)
        return StrategyDecision(state, forecast, models, portfolio, straddle, parity,
                                (DesiredTrade(straddle.call.symbol, signed, "ATM volatility straddle"),
                                 DesiredTrade(straddle.put.symbol, signed, "ATM volatility straddle")),
                                "enter: new news and cost-adjusted ATM straddle edge", age)

    def explain(self, decision: StrategyDecision) -> dict[str, Any]:
        """Describe the observable inputs and gates behind a decision.

        The returned structure is deliberately numeric and stable enough for
        JSONL replay.  It records factors rather than claiming causal certainty
        about an eventual fill or P&L outcome.

        :param decision: Decision previously produced by :meth:`decide`.
        :returns: Factor-level rationale, or an empty mapping when disabled.
        """

        if not self.config.explainability_enabled:
            return {}
        straddle = decision.straddle
        entry = None if straddle is None else {
            "strike": straddle.strike,
            "side": straddle.side,
            "combined_edge_per_straddle": straddle.expected_edge_per_contract,
            "call_edge_per_contract": straddle.call.expected_edge_per_contract,
            "put_edge_per_contract": straddle.put.expected_edge_per_contract,
            "entry_threshold": self.config.entry_edge_per_contract,
            "exit_threshold": self.config.exit_edge_per_contract,
            "call_market_iv": straddle.call.market_iv,
            "put_market_iv": straddle.put.market_iv,
        }
        return {
            "decision_reason": decision.reason,
            "forecast_factors": {
                "fair_remaining_sigma": decision.forecast.sigma,
                "integrated_variance": decision.forecast.integrated_variance,
                "recognized_news_ids": decision.forecast.recognized_news_ids,
                "unparsed_news_ids": decision.forecast.unparsed_news_ids,
                "time_since_latest_news_ticks": decision.time_since_latest_news,
            },
            "risk_factors": {
                "portfolio_delta": decision.portfolio.delta,
                "portfolio_gamma": decision.portfolio.gamma,
                "portfolio_vega": decision.portfolio.vega,
                "hedge_threshold": self.config.hedge_threshold,
                "max_safe_delta": self.config.max_safe_delta,
                "expiry_reduction_tick": self.config.close_tick,
            },
            "entry_factors": entry,
            "selected_trades": [trade.__dict__ for trade in decision.desired_trades],
            "cost_assumptions": {
                "option_commission": self.config.option_commission,
                "rtm_commission_per_share": self.config.rtm_commission_per_share,
                "safety_margin_per_contract": self.config.safety_margin_per_contract,
            },
        }
