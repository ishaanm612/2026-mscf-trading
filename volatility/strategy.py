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


def _size_for_edge(edge: float, config: VolatilityConfig) -> int:
    """Choose conservative straddle size from net dollar edge buckets.

    :param edge: Net expected edge per straddle contract.
    :param config: Entry threshold and maximum V1 size.
    :returns: Contract count for each leg, or zero when ineligible.
    """

    if edge < config.entry_edge_per_contract:
        return 0
    if edge < 2.0 * config.entry_edge_per_contract:
        return min(5, config.max_straddle_contracts)
    if edge < 4.0 * config.entry_edge_per_contract:
        return min(15, config.max_straddle_contracts)
    return min(30, config.max_straddle_contracts)


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
        if not new_news:
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (), "wait: no new analyst/news event", age)
        if straddle is None:
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (), "wait: ATM straddle lacks executable edge", age)
        quantity = _size_for_edge(straddle.expected_edge_per_contract, self.config)
        signed = quantity if straddle.side == "BUY" else -quantity
        if quantity == 0:
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (), "wait: straddle edge below entry threshold", age)
        return StrategyDecision(state, forecast, models, portfolio, straddle, parity,
                                (DesiredTrade(straddle.call.symbol, signed, "ATM volatility straddle"),
                                 DesiredTrade(straddle.put.symbol, signed, "ATM volatility straddle")),
                                "enter: new news and cost-adjusted ATM straddle edge", age)
