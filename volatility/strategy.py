"""Pure volatility-strategy orchestration that emits abstract desired trades."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from collections import deque
from typing import Any

from models.volatility import PortfolioGreeks, portfolio_greeks
from volatility.config import VolatilityConfig
from volatility.convergence import ConvergenceModel, features_for_straddle
from volatility.forecast import ForecastResult, estimate_remaining_volatility
from volatility.hedging import calculate_hedge_order
from volatility.market_data import MarketState, from_snapshot
from volatility.timing import exit_budget, order_capacity
from volatility.signals import OptionModel, ParityOpportunity, StraddleOpportunity, find_mispricings, held_strike_edge, model_options, scan_put_call_parity, select_atm_straddle


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
                         "unparsed_news_ids": self.forecast.unparsed_news_ids,
                         "unannounced_prior_sigma": self.forecast.unannounced_prior_sigma,
                         "used_unannounced_prior": self.forecast.used_unannounced_prior},
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
    premium_notional = (straddle.call.executable_price + straddle.put.executable_price) * config.contract_multiplier
    notional_capacity = max(0, int(config.target_entry_notional / max(premium_notional, 1e-9)))
    capacity = min(config.max_straddle_contracts, gross_capacity, net_capacity, gamma_capacity, vega_capacity,
                   notional_capacity)
    edge_fraction = min(1.0, straddle.expected_edge_per_contract / config.edge_for_full_risk)
    return max(0, int(capacity * edge_fraction))


def _straddle_entry_children(straddle: StraddleOpportunity, quantity: int, state: MarketState,
                             portfolio: PortfolioGreeks, config: VolatilityConfig) -> tuple[DesiredTrade, ...]:
    """Split a large straddle into alternating legal call/put child orders.

    The exchange caps options at a child-order size.  Alternating legs limits
    temporary directional delta to one child instead of accumulating every
    call before its matching put.  Each proposed child also fits the internal
    delta boundary before the paired leg arrives.

    :param straddle: Selected executable ATM pair.
    :param quantity: Positive contracts per option before side direction.
    :param state: Snapshot containing server order caps.
    :param portfolio: Confirmed inventory Greeks before entry.
    :param config: Strategy limits and contract multiplier.
    :returns: Ordered, signed legal child trades; empty when no safe child fits.
    """

    call_cap = order_capacity(state, straddle.call.symbol)
    put_cap = order_capacity(state, straddle.put.symbol)
    if quantity <= 0 or not call_cap or not put_cap:
        return ()
    signed = 1 if straddle.side == "BUY" else -1
    # A single unpaired child must be legal even before the next fresh cycle
    # can submit its matching leg. Subsequent cycles use confirmed inventory
    # and may insert the normal RTM hedge before continuing this queue.
    delta_per_contract = max(abs(straddle.call.delta), abs(straddle.put.delta)) * config.contract_multiplier
    delta_child_cap = int(config.max_safe_delta / max(delta_per_contract, 1e-9))
    if delta_child_cap <= 0:
        return ()
    remaining = quantity
    children: list[DesiredTrade] = []
    while remaining:
        child = min(remaining, call_cap, put_cap, delta_child_cap)
        quantity_signed = signed * child
        children.extend((DesiredTrade(straddle.call.symbol, quantity_signed, "ATM volatility straddle"),
                         DesiredTrade(straddle.put.symbol, quantity_signed, "ATM volatility straddle")))
        remaining -= child
    return tuple(children)


def _incomplete_option_inventory(models: tuple[OptionModel, ...]) -> bool:
    """Report whether confirmed option holdings are missing a call or put.

    A filled ATM straddle has both kinds. One filled leg is directional until
    the pair completes, so ordinary delta hedges would trade RTM against that
    temporary exposure and then unwind it.

    :param models: Current option models including confirmed positions.
    :returns: True when some option is held but both kinds are not present.
    """

    held = [item for item in models if item.quote.position]
    return bool(held) and {item.quote.kind for item in held} != {"C", "P"}


def _held_straddle_side(models: tuple[OptionModel, ...]) -> str | None:
    """Infer the long-vol or short-vol side of a complete held straddle.

    :param models: Current option models including confirmed positions.
    :returns: ``BUY`` or ``SELL`` when both legs share a sign, otherwise ``None``.
    """

    held = [item for item in models if item.quote.position]
    if {item.quote.kind for item in held} != {"C", "P"}:
        return None
    signs = {1 if item.quote.position > 0 else -1 for item in held}
    if signs == {1}:
        return "BUY"
    if signs == {-1}:
        return "SELL"
    return None


def _closing_option_trades(models: tuple[OptionModel, ...], reason: str) -> tuple[DesiredTrade, ...]:
    """Build signed exits for every confirmed option holding.

    :param models: Current option models including confirmed positions.
    :param reason: Stable execution-log reason for each child order.
    :returns: One desired trade per nonzero option position.
    """

    return tuple(DesiredTrade(item.quote.symbol, -item.quote.position, reason)
                 for item in models if item.quote.position)


class VolatilityStrategy:
    """Stateful V1 controller that reacts to new news and manages existing risk.

    Strategy code never submits an order.  Consumers must validate each
    ``DesiredTrade`` against fresh account state and reconcile confirmed fills.

    :param config: Cost, clock, sizing, and risk settings.
    :param fallback_sigma: Explicit fallback for a heat with incomplete news.
    """

    def __init__(self, config: VolatilityConfig | None = None, fallback_sigma: float | None = None,
                 convergence_model: ConvergenceModel | None = None) -> None:
        """Initialize an empty strategy-news cursor.

        :param config: Optional non-default strategy configuration.
        :param fallback_sigma: Explicit operator-supplied fallback volatility.
        """

        self.config = config or VolatilityConfig()
        self.fallback_sigma = fallback_sigma
        self.convergence_model = convergence_model
        self._convergence_prediction: float | None = None
        self._seen_news_ids: set[int | str | None] = set()
        self._used_entry_news: frozenset[int | str | None] = frozenset()
        self._entry_edge: float | None = None
        self._last_tick: int | None = None
        self._cycle_samples: deque[int] = deque(maxlen=32)
        self._liquidating = False
        self.timing: dict[str, Any] = {}

    @property
    def liquidating(self) -> bool:
        """Report whether the current heat has entered mandatory liquidation.

        :returns: True once the inventory-dependent deadline has been reached.
        """

        return self._liquidating

    def _fresh_recognized_news(self, state: MarketState, forecast: ForecastResult) -> bool:
        """True when unused recognized news is still inside the entry window.

        :param state: Current typed market state.
        :param forecast: Parsed remaining-volatility forecast.
        :returns: Whether that news may still trigger an entry or a flip.
        """

        relevant_news = frozenset(forecast.recognized_news_ids)
        relevant_ticks = [int(item["tick"]) for item in state.news_history
                          if item.get("news_id") in relevant_news and "tick" in item]
        news_age = state.current_tick - max(relevant_ticks) if relevant_ticks else None
        return bool(relevant_news) and relevant_news != self._used_entry_news and news_age is not None and 0 <= news_age <= self.config.news_entry_window_ticks

    def decide(self, snapshot: dict[str, Any]) -> StrategyDecision:
        """Model a snapshot and produce a bounded V1 action plan.

        Existing inventory gets priority: expiry handling, the 6,000-share
        safety hedge, ordinary hedges of complete straddles, then exits, then
        a news-triggered entry. Incomplete one-leg inventory is not
        band-hedged.

        :param snapshot: Fresh coherent RIT volatility snapshot.
        :returns: Full decision record, including abstract desired orders.
        """

        state = from_snapshot(snapshot)
        if self._last_tick is not None:
            if state.current_tick < self._last_tick:
                self._cycle_samples.clear()
                self._seen_news_ids.clear()
                self._used_entry_news = frozenset()
                self._entry_edge = None
                self._liquidating = False
            elif state.current_tick > self._last_tick:
                self._cycle_samples.append(state.current_tick - self._last_tick)
        self._last_tick = state.current_tick
        cycle = max(self._cycle_samples, default=self.config.cycle_ticks_floor)
        positions = {item.symbol: item.position for item in state.options if item.position}
        budget = exit_budget(state, positions, cycle, self.config)
        deadline = budget.liquidation_tick
        if self.config.close_tick is not None:
            deadline = min(deadline, self.config.close_tick)
        self.timing = {**asdict(budget), "liquidation_tick": deadline}
        self._liquidating = self._liquidating or state.current_tick >= deadline
        forecast = estimate_remaining_volatility(state.current_tick, self.config.expiry_tick, state.news_history,
                                                 state, self.fallback_sigma, self.config.unannounced_sigma)
        if forecast.sigma is None:
            return StrategyDecision(state, forecast, (), PortfolioGreeks(float(state.rtm_position), 0, 0, 0), None, (), (),
                            "wait: volatility news is incomplete or ambiguous", _time_since_latest_news(state))
        models = model_options(state, forecast.sigma, self.config)
        portfolio = _portfolio(state, models, self.config)
        opportunities = find_mispricings(models, forecast.sigma, self.config)
        straddle = select_atm_straddle(models, opportunities, state)
        parity = scan_put_call_parity(models, state, self.config)
        news_ids = {item.get("news_id") for item in state.news_history}
        self._seen_news_ids.update(news_ids)
        age = _time_since_latest_news(state)
        if state.status != "ACTIVE" or state.current_tick >= self.config.expiry_tick:
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (), "wait: case inactive or expired", age)
        if self._liquidating:
            closing = tuple(DesiredTrade(item.quote.symbol, -max(-order_capacity(state, item.quote.symbol),
                                             min(item.quote.position, order_capacity(state, item.quote.symbol))), "expiry inventory reduction")
                            for item in models if item.quote.position)
            if closing:
                return StrategyDecision(state, forecast, models, portfolio, straddle, parity, closing,
                                        "exit: configured expiry window", age)
            if state.rtm_position:
                return StrategyDecision(state, forecast, models, portfolio, straddle, parity,
                                        (DesiredTrade("RTM", -max(-order_capacity(state, "RTM"), min(state.rtm_position, order_capacity(state, "RTM"))), "expiry RTM reduction"),),
                                        "exit: configured expiry window", age)
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (),
                                    "wait: configured expiry window blocks new entries", age)
        hedge = calculate_hedge_order(portfolio.delta, self.config.hedge_threshold)
        incomplete = _incomplete_option_inventory(models)
        if not any(item.quote.position for item in models):
            self._entry_edge = None
        if abs(portfolio.delta) >= self.config.max_safe_delta:
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity,
                                    (DesiredTrade("RTM", hedge, "delta safety hedge"),), "hedge: internal delta boundary", age)
        if hedge and not incomplete:
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity,
                                    (DesiredTrade("RTM", hedge, "delta hedge"),), "hedge: outside no-trade band", age)
        held_strikes = sorted({item.quote.strike for item in models if item.quote.position})
        for strike in held_strikes:
            held = tuple(item for item in models if item.quote.strike == strike and item.quote.position)
            remaining = held_strike_edge(held, self.config)
            if remaining < self.config.exit_edge_per_contract:
                exits = tuple(DesiredTrade(item.quote.symbol, -item.quote.position, "straddle convergence exit")
                              for item in held)
                return StrategyDecision(state, forecast, models, portfolio, straddle, parity, exits,
                                        "exit: remaining edge below hysteresis threshold", age)
            complete = {item.quote.kind for item in held} == {"C", "P"}
            if complete and self._entry_edge is not None:
                take_profit = max(self.config.exit_edge_per_contract,
                                  self._entry_edge * self.config.take_profit_remaining_fraction)
                if remaining < take_profit:
                    exits = tuple(DesiredTrade(item.quote.symbol, -item.quote.position, "straddle take-profit exit")
                                  for item in held)
                    return StrategyDecision(state, forecast, models, portfolio, straddle, parity, exits,
                                            "exit: remaining edge below take-profit fraction of entry", age)
        held_side = _held_straddle_side(models)
        if (held_side and straddle is not None and straddle.side != held_side
                and self._fresh_recognized_news(state, forecast)):
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity,
                                    _closing_option_trades(models, "straddle news-reversal exit"),
                                    "exit: new news reversed the held straddle", age)
        if any(item.quote.position for item in models):
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (),
                                    "wait: existing option inventory is being held and risk-managed", age)
        if not self._fresh_recognized_news(state, forecast):
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (), "wait: no new analyst/news event", age)
        if straddle is None:
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (), "wait: ATM straddle lacks executable edge", age)
        if self.convergence_model is not None:
            call, put = straddle.call, straddle.put
            quotes = {item.quote.symbol: item.quote.quote for item in models}
            call_quote, put_quote = quotes[call.symbol], quotes[put.symbol]
            features = features_for_straddle(straddle.expected_edge_per_contract, forecast.sigma,
                                             call.market_iv, put.market_iv, age, state.current_tick,
                                             call_quote.ask - call_quote.bid,
                                             put_quote.ask - put_quote.bid)
            self._convergence_prediction = self.convergence_model.predict(features)
            if self._convergence_prediction < self.config.convergence_min_expected_pnl:
                return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (),
                                        "wait: learned convergence return is insufficient", age)
        quantity = _size_for_edge(straddle, portfolio, state, self.config)
        proposed_budget = None
        entry_deadline = -1
        # A late news event may not leave enough time for the maximum volume.
        # Step down only as far as needed to retain an entry that can be fully
        # liquidated, rather than rejecting a still-viable smaller position.
        while quantity:
            proposed = {straddle.call.symbol: quantity, straddle.put.symbol: quantity}
            proposed_budget = exit_budget(state, proposed, cycle, self.config)
            entry_deadline = proposed_budget.liquidation_tick - 2 * proposed_budget.ticks_per_order - self.config.minimum_holding_ticks
            if self.config.close_tick is not None:
                entry_deadline = min(entry_deadline, self.config.close_tick - 2 * proposed_budget.ticks_per_order - self.config.minimum_holding_ticks)
            if state.current_tick < entry_deadline:
                break
            quantity -= 1
        self.timing.update(entry_deadline_tick=entry_deadline,
                           proposed_exit_budget=None if proposed_budget is None else asdict(proposed_budget))
        if quantity == 0:
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (),
                                    "wait: insufficient time to enter, hold, and liquidate", age)
        children = _straddle_entry_children(straddle, quantity, state, portfolio, self.config)
        if not children:
            return StrategyDecision(state, forecast, models, portfolio, straddle, parity, (),
                                    "wait: straddle child would exceed order or delta limit", age)
        self._used_entry_news = frozenset(forecast.recognized_news_ids)
        self._entry_edge = straddle.expected_edge_per_contract
        return StrategyDecision(state, forecast, models, portfolio, straddle, parity, children,
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
            "take_profit_remaining_fraction": self.config.take_profit_remaining_fraction,
            "entry_edge_at_open": self._entry_edge,
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
                "entry_window_ticks": self.config.news_entry_window_ticks,
                "unannounced_prior_sigma": decision.forecast.unannounced_prior_sigma,
                "used_unannounced_prior": decision.forecast.used_unannounced_prior,
            },
            "risk_factors": {
                "portfolio_delta": decision.portfolio.delta,
                "portfolio_gamma": decision.portfolio.gamma,
                "portfolio_vega": decision.portfolio.vega,
                "hedge_threshold": self.config.hedge_threshold,
                "max_safe_delta": self.config.max_safe_delta,
                "expiry_reduction_tick": self.timing.get("liquidation_tick"),
                "execution_timing": dict(self.timing),
            },
            "entry_factors": entry,
            "convergence_model": None if self.convergence_model is None else {
                "expected_pnl_per_straddle": self._convergence_prediction,
                "minimum_expected_pnl": self.config.convergence_min_expected_pnl,
                "horizon_ticks": self.convergence_model.horizon_ticks,
                "training_samples": self.convergence_model.training_samples,
                "holdout_mae": self.convergence_model.holdout_mae,
                "holdout_directional_accuracy": self.convergence_model.holdout_directional_accuracy,
            },
            "selected_trades": [trade.__dict__ for trade in decision.desired_trades],
            "cost_assumptions": {
                "option_commission": self.config.option_commission,
                "rtm_commission_per_share": self.config.rtm_commission_per_share,
                "safety_margin_per_contract": self.config.safety_margin_per_contract,
            },
        }
