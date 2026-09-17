"""Conservative serial case strategies. Each cycle starts from fresh account state."""
import copy
import math
from dataclasses import replace
from typing import Any, Mapping

from models import etf, volatility, news
from models import etf_policy
from models import etf_basket
from models.etf_liquidity import LiquidityHistory
from client import RITReadError
import risk
from volatility.config import VolatilityConfig
from volatility.convergence import ConvergenceModel
from volatility.strategy import DesiredTrade, VolatilityStrategy
from volatility.signals import find_mispricings


ETF_UNWIND_MAX_CHILD = 10_000
ETF_TENDER_TICKS_PER_ACTION = 3
ETF_TENDER_LIQUIDATION_BUFFER_TICKS = 5


def clip(position: float, size: int) -> int:
    """Keep the sign, cap absolute size, and truncate fractional currency units."""
    return int(math.copysign(min(abs(position), size), position)) if position else 0


def _fill_price(order: Mapping[str, Any] | None, fallback: float) -> float:
    """Prefer an exchange-reported fill VWAP while retaining a quoted fallback."""
    if order:
        for key in ("vwap", "avg_price", "price"):
            value = order.get(key)
            if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
                return float(value)
    return fallback


class Bot:
    def __init__(self, client: Any, executor: Any = None, *, case: str, sigma: float | None = None,
                 rate: float = 0.0, quantity: int = 1000, gross_limit: int | None = None,
                 net_limit: int | None = None, flatten_only: bool = False, basket: bool = False,
                 explainability: bool = True, convergence_model_path: str | None = None,
                 etf_config: etf_policy.ETFConfig | None = None,
                 basket_config: etf_basket.BasketConfig | None = None) -> None:
        """Create a case bot and its pure volatility decision controller.

        :param client: RIT client used only when execution is enabled.
        :param executor: Durable execution adapter, or ``None`` for plans.
        :param case: Case identifier.
        :param sigma: Optional explicit volatility fallback.
        :param rate: Annual risk-free rate.
        :param quantity: ETF child-order cap.
        :param gross_limit: ETF gross exposure limit.
        :param net_limit: ETF net exposure limit.
        :param flatten_only: Prevent new exposure and reduce inventory only.
        :param basket: Enable optional serial ETF basket execution.
        :param explainability: Include factor-level rationale with volatility decisions.
        """
        self.client, self.executor, self.case = client, executor, case
        self.sigma, self.rate, self.quantity = sigma, rate, quantity
        self.gross_limit, self.net_limit = gross_limit, net_limit
        self.flatten_only, self.basket = flatten_only, basket
        self.held_basket = None
        self.etf_config = etf_config or etf_policy.ETFConfig()
        self.basket_config = basket_config or etf_basket.BasketConfig()
        self.etf_reduction_reason: str | None = None
        self.basket_cooldown_until = 0
        self.basket_exit_context: dict[str, Any] = {}
        self.etf_market_risk = etf_policy.MarketRisk()
        self.etf_liquidity = LiquidityHistory()
        self.etf_sigmas: dict[str, float] = {}
        self.etf_assessments: list[dict[str, Any]] = []
        self.manual_since: int | None = None
        self.manual_disabled = False
        self.active_etf_route: dict[str, Any] | None = None
        self.last_tick = None
        self.last_period = None
        convergence_model = ConvergenceModel.load(convergence_model_path) if convergence_model_path else None
        self.volatility_strategy = VolatilityStrategy(replace(VolatilityConfig(), risk_free_rate=rate,
                                                              explainability_enabled=explainability), sigma,
                                                   convergence_model)
        self.pending_volatility_trades: list[DesiredTrade] = []
        self.expected_volatility_positions: dict[str, float] | None = None

    def submit(self, snapshot: Mapping[str, Any], ticker: str, quantity: int, reason: str,
               deltas: Mapping[str, float] | None = None, *, price_bound: float | None = None) -> dict[str, Any]:
        """Validate then optionally submit one signed market order.

        :param snapshot: Fresh account state.
        :param ticker: Instrument ticker.
        :param quantity: Signed order quantity.
        :param reason: Audit explanation.
        :param deltas: Current instrument delta weights.
        :returns: Submitted or planned action.
        """
        risk.check(snapshot, ticker, quantity, self.case, deltas, self.gross_limit, self.net_limit)
        if self.executor and self.case == "etf":
            try:
                fresh = self.client.snapshot("etf", trading=True)
            except RITReadError:
                return {"wait": "ETF account preflight unavailable; replan before submission"}
            if (not self._same_etf_session(snapshot, fresh)
                    or fresh["case"]["tick"] - snapshot["case"]["tick"] > 2):
                return {"wait": "ETF submission snapshot expired; replan from fresh state"}
            if (fresh.get("orders") != [] or self.position_map(fresh) != self.position_map(snapshot)):
                return {"wait": "ETF account changed before submission; replan from confirmed positions"}
            risk.check(fresh, ticker, quantity, self.case, deltas, self.gross_limit, self.net_limit)
            snapshot = fresh
        if self.case == "etf" and ticker in etf.WEIGHTS:
            projected = self.position_map(snapshot)
            quote = etf.executable_trade_cashflow(snapshot, ticker, quantity)
            if price_bound is not None and (quote["price"] > price_bound if quantity > 0 else quote["price"] < price_bound):
                return {"wait": "fresh ETF child quote crossed staged price bound"}
            etf_policy.apply_cash(projected, quote)
            risk.check_etf_projection(snapshot, projected, self.gross_limit, self.net_limit)
        action = {"ticker": ticker, "quantity": quantity, "reason": reason}
        if self.case == "etf" and ticker in etf.WEIGHTS:
            action["quoted_price"] = quote["price"]
        if self.executor and self.case == "volatility":
            try:
                fresh = self.client.snapshot("volatility", trading=True)
            except RITReadError:
                self.pending_volatility_trades.clear()
                self.expected_volatility_positions = None
                return {"wait": "account preflight unavailable; retry with fresh state"}
            old_positions = self.position_map(snapshot)
            if fresh.get("orders") != [] or self.position_map(fresh) != old_positions:
                self.pending_volatility_trades.clear()
                self.expected_volatility_positions = None
                return {"wait": "account changed before submission; replan from confirmed positions"}
            risk.check(fresh, ticker, quantity, self.case, deltas, self.gross_limit, self.net_limit)
        if self.executor:
            # Re-read case immediately before mutation; do not trade an older snapshot.
            try:
                current = self.client.get("case")
            except RITReadError:
                self.pending_volatility_trades.clear()
                return {"wait": "case preflight unavailable; replan before submission"}
            old = snapshot["case"]
            if (current["status"] != "ACTIVE" or current.get("period") != old.get("period")
                    or not 0 <= current["tick"] - old["tick"] <= 2 or current["tick"] >= 299):
                self.pending_volatility_trades.clear()
                return {"wait": "snapshot expired before submission; replan from fresh state"}
            fill = self.executor.order(ticker, quantity)
            if self.case == "etf":
                action["fill"] = dict(fill)
        return action

    def step(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Produce at most one safe action from a fresh snapshot.

        :param snapshot: Fresh RIT case snapshot.
        :returns: Action or wait explanation.
        """
        state = snapshot["case"]
        if self.last_tick is not None and (state["tick"] < self.last_tick or state.get("period") != self.last_period):
            self.held_basket = None
            self.manual_since = None
            self.manual_disabled = False
            self.active_etf_route = None
            self.etf_reduction_reason = None
            self.basket_cooldown_until = 0
            self.basket_exit_context = {}
        self.last_tick, self.last_period = state["tick"], state.get("period")
        if state["status"] != "ACTIVE" or state["tick"] >= 299:
            return {"wait": "inactive or final tick"}
        if not isinstance(snapshot.get("orders"), list):
            raise RuntimeError("Missing or invalid open-order state")
        if snapshot["orders"]:
            if self.case != "volatility":
                # A visible account order is not evidence that one of our
                # mutations had an ambiguous outcome. Wait without submitting
                # or cancelling, then manage confirmed inventory when it clears.
                # The runner separately latches unresolved executor failures.
                if self.held_basket:
                    self._reduce_basket("open account orders invalidated basket intent", state["tick"])
                return {"wait": "open account orders; waiting for fills or cancellation",
                        "open_order_ids": [order.get("order_id") for order in snapshot["orders"]]}
            self.pending_volatility_trades.clear()
            self.expected_volatility_positions = None
            return {"wait": "open account orders; waiting for fills or cancellation",
                    "open_order_ids": [order.get("order_id") for order in snapshot["orders"]]}
        if self.case == "volatility":
            return self.volatility_step(snapshot)
        self.etf_sigmas = self.etf_market_risk.observe(snapshot)
        self.etf_liquidity.observe(snapshot)
        self.etf_assessments = []
        result = self.etf_step(snapshot)
        result["tender_assessments"] = self.etf_assessments
        result["market_risk_sigmas"] = self.etf_sigmas
        return result

    @staticmethod
    def position_map(snapshot: Mapping[str, Any]) -> dict[str, float]:
        """Extract account inventory for detecting fills outside this bot.

        :param snapshot: Account snapshot with confirmed security positions.
        :returns: Instrument quantities without assuming who placed the orders.
        """

        return {str(row["ticker"]): float(row["position"]) for row in snapshot["securities"]}

    def volatility_step(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Execute at most one validated V1 volatility action from a fresh decision.

        :param snapshot: Current coherent RIT volatility snapshot.
        :returns: Submitted action or an explainable no-trade decision.
        """
        actual_positions = self.position_map(snapshot)
        if (self.expected_volatility_positions is not None
                and actual_positions != self.expected_volatility_positions):
            self.pending_volatility_trades.clear()
        self.expected_volatility_positions = actual_positions.copy()
        decision = self.volatility_strategy.decide(dict(snapshot))
        decision_fields = decision.as_log_fields()
        decision_fields["explanation"] = self.volatility_strategy.explain(decision)
        securities = {str(item["ticker"]): item for item in snapshot["securities"]}
        deltas = {"RTM": 1.0, **{item.quote.symbol: 100.0 * item.fair.delta for item in decision.models}}
        if self.volatility_strategy.liquidating:
            # Discard pending entry legs once the inventory exit deadline arrives.
            self.pending_volatility_trades.clear()
        if self.flatten_only:
            self.pending_volatility_trades = [DesiredTrade(symbol, -int(row["position"]), "flatten option")
                                              for symbol, row in securities.items()
                                              if symbol != "RTM" and row.get("position", 0)]
            if not self.pending_volatility_trades and securities["RTM"].get("position", 0):
                self.pending_volatility_trades = [DesiredTrade("RTM", -int(securities["RTM"]["position"]), "flatten RTM")]
        exit_reasons = {"straddle convergence exit", "straddle take-profit exit",
                        "straddle news-reversal exit", "expiry inventory reduction"}
        pending_exit = bool(self.pending_volatility_trades
                            and self.pending_volatility_trades[0].reason in exit_reasons)
        priority_hedge = (decision.reason == "hedge: internal delta boundary"
                          and not self.flatten_only and not pending_exit)
        forced_option_exit = decision.reason.startswith("exit:") and any(item.quote.position for item in decision.models)
        if forced_option_exit and not pending_exit:
            # Exit plans can cover hundreds of contracts. Submit only one
            # legal child at a time. Queue alternating legs so normal delta
            # hedges cannot split a pair into directional inventory while the
            # serial exit is in progress.
            self.pending_volatility_trades.clear()
            remaining = [(item.quote.symbol, -int(item.quote.position))
                         for item in decision.models if item.quote.position]
            exit_reason = ("expiry inventory reduction" if decision.reason == "exit: configured expiry window"
                           else "straddle convergence exit")
            while any(quantity for _, quantity in remaining):
                next_remaining = []
                for symbol, quantity in remaining:
                    cap = int(securities[symbol].get("max_trade_size", 0))
                    if cap <= 0:
                        next_remaining.append((symbol, quantity))
                        continue
                    child = clip(quantity, cap)
                    self.pending_volatility_trades.append(DesiredTrade(symbol, child, exit_reason))
                    next_remaining.append((symbol, quantity - child))
                if next_remaining == remaining:
                    break
                remaining = next_remaining
            if not self.pending_volatility_trades:
                return {"wait": "option exit lacks a legal child order", "decision": decision_fields}
            trade = self.pending_volatility_trades.pop(0)
        elif priority_hedge:
            # Only the 6,000-delta safety hedge may interrupt the pair.
            # Ordinary band hedges wait until both straddle legs are confirmed.
            trade = decision.desired_trades[0]
        else:
            if self.pending_volatility_trades and self.pending_volatility_trades[0].reason == "ATM volatility straddle":
                pending = self.pending_volatility_trades[0]
                side = "BUY" if pending.quantity > 0 else "SELL"
                opportunities = find_mispricings(decision.models, decision.forecast.sigma or 0.0,
                                                  self.volatility_strategy.config)
                valid = any(item.symbol == pending.symbol and item.side == side for item in opportunities)
                if not valid or decision.reason.startswith("exit:"):
                    self.pending_volatility_trades.clear()
                    # The pair's thesis no longer supports completing the second
                    # leg. Explicitly unwind the confirmed first leg instead.
                    self.pending_volatility_trades.extend(
                        DesiredTrade(item.quote.symbol, -item.quote.position, "aborted straddle unwind")
                        for item in decision.models if item.quote.position)
            if not self.pending_volatility_trades:
                self.pending_volatility_trades.extend(decision.desired_trades)
            if not self.pending_volatility_trades:
                return {"wait": decision.reason, "decision": decision_fields}
            trade = self.pending_volatility_trades.pop(0)
        if trade.quantity == 0:
            return {"wait": decision.reason, "decision": decision_fields}
        reason = "volatility mispricing" if trade.reason == "ATM volatility straddle" else trade.reason
        try:
            action = self.submit(snapshot, trade.symbol, trade.quantity, reason, deltas)
        except risk.RiskError as error:
            # All RiskError gates run before Executor.order: no mutation occurred.
            # Discard dependent legs and let the next fresh decision hedge or exit.
            self.pending_volatility_trades.clear()
            rejection = {"symbol": trade.symbol, "quantity": trade.quantity,
                         "reason": str(error), "submitted": False}
            decision_fields["risk_rejection"] = rejection
            decision_fields["reason"] = "wait: pre-trade risk rejection"
            return {"wait": "pre-trade risk rejection; replan from fresh state",
                    "risk_rejection": rejection, "decision": decision_fields}
        if self.executor and "ticker" in action:
            self.expected_volatility_positions[trade.symbol] += trade.quantity
        action["decision"] = decision_fields
        return action

    def option_entry(self, snapshot: Mapping[str, Any], analysis: Mapping[str, Any], securities: Mapping[str, Any],
                     rows: Mapping[str, Any], deltas: Mapping[str, float]) -> dict[str, Any] | None:
        """Choose one ten-contract trade with enough stock capacity to hedge it."""
        for row in sorted(rows.values(), key=lambda r: r["edge_per_share"], reverse=True):
            if row["signal"] == "HOLD" or row["edge_per_share"] < .03:
                continue
            ticker = row["ticker"]
            q = 10 if row["signal"] == "BUY" else -10
            # Small MVP inventory caps, stricter than case limits.
            if abs(securities[ticker]["position"] + q) > 50 or analysis["option_gross"] + abs(q) > 200:
                continue
            projected_delta = analysis["portfolio_delta_shares"] + q*deltas[ticker]
            if abs(securities["RTM"]["position"] - round(projected_delta)) > 45000:
                continue
            try:
                return self.submit(snapshot, ticker, q, "volatility mispricing", deltas)
            except risk.RiskError:
                continue
        return None

    def _basket_liquidation_budget(self, snapshot: Mapping[str, Any], positions: Mapping[str, float]) -> dict[str, int]:
        """Reserve enough late-round time to flatten basket equities and net USD."""
        securities = {row["ticker"]: row for row in snapshot["securities"]}
        actions = 0
        for ticker in etf.WEIGHTS:
            position = abs(int(positions.get(ticker, 0)))
            if not position:
                continue
            cap = min(self.etf_config.child_size, int(securities[ticker].get("max_trade_size", 0)))
            if cap <= 0:
                return {"actions": 300, "reserve_ticks": 300, "latest_start_tick": 0}
            actions += math.ceil(position / cap)
        # Price final USD when possible; never assume one FX order is enough.
        try:
            net_usd = etf.liquidation_value(snapshot, positions)["net_usd_before_fx"]
        except (ValueError, KeyError):
            net_usd = abs(positions.get("USD", 0))
            ritc = abs(positions.get("RITC", 0))
            if ritc:
                try:
                    net_usd += ritc * etf.vwap(snapshot["books"]["RITC"], "BUY", 1)
                except (ValueError, KeyError):
                    return {"actions": 300, "reserve_ticks": 300, "latest_start_tick": 0}
        fx_cap = min(2_500_000, int(securities["USD"].get("max_trade_size", 0)))
        if fx_cap <= 0:
            return {"actions": 300, "reserve_ticks": 300, "latest_start_tick": 0}
        actions += math.ceil(abs(net_usd) / fx_cap)
        reserve = math.ceil(actions * self.etf_config.ticks_per_action + self.etf_config.end_buffer_ticks)
        return {"actions": actions, "reserve_ticks": reserve,
                "latest_start_tick": max(0, 298 - reserve)}

    def etf_step(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Value held baskets before adding risk; finish a latched exit before entries.

        Basket P&L uses historical fills and executable exits in CAD. A stop,
        timeout, failed serial entry or inventory mismatch latches reduction
        through the final USD child, even if prices subsequently recover.
        """
        positions = {s["ticker"]: s["position"] for s in snapshot["securities"]}
        tick = snapshot["case"]["tick"]
        target = min(self.quantity, self.basket_config.max_quantity)
        size = min(target, self.etf_config.child_size,
                   *(int(s["max_trade_size"]) for s in snapshot["securities"] if s["ticker"] in etf.WEIGHTS))
        budget = self._basket_liquidation_budget(snapshot, positions)
        forced = self.flatten_only or tick >= budget["latest_start_tick"]
        holding = None
        if self.held_basket and not self.etf_reduction_reason:
            expected = self.held_basket["expected_positions"]
            expected_usd = self.held_basket.get("expected_usd", positions.get("USD", 0))
            if (any(positions[t] != p for t, p in expected.items())
                    or not math.isclose(positions.get("USD", 0), expected_usd, abs_tol=1e-4, rel_tol=0)):
                self._reduce_basket("basket inventory changed outside recorded fills", tick,
                                    {"expected_positions": expected, "actual_positions": positions,
                                     "expected_usd": expected_usd})
            else:
                try:
                    holding = etf_basket.holding_report(snapshot, self.held_basket,
                        self.basket_config, self.etf_config, self.etf_sigmas)
                    if holding["exit_reason"]:
                        self._reduce_basket(holding["exit_reason"], tick, {"basket_holding": holding})
                except (ValueError, KeyError) as error:
                    self._reduce_basket(f"basket exit pricing unavailable: {error}", tick)
        if forced and any(positions[t] for t in etf.WEIGHTS):
            self._reduce_basket("flatten-only" if self.flatten_only else "liquidation deadline", tick,
                                {"liquidation_budget": budget, "basket_holding": holding})

        self.etf_assessments = self.tender_assessments(snapshot, positions, {})
        busy = self.etf_reduction_reason or ("tender liquidation route active" if
                   self.active_etf_route or self.manual_since is not None else None)
        if not busy and self.manual_disabled and any(positions[t] for t in etf.WEIGHTS):
            busy = "manual route expired; direct reduction required"
        if busy:
            for assessment in self.etf_assessments:
                assessment["economic_decision"] = assessment["decision"]
                assessment.update(decision="DEFER", reason=busy)
        elif not forced:
            tender = self.choose_tender(snapshot, positions, {})
            if tender:
                return tender
        equity_flat = not any(positions[t] for t in etf.WEIGHTS)
        if equity_flat:
            self.manual_since = None
            self.manual_disabled = False
            self.active_etf_route = None
        if self.etf_reduction_reason and not equity_flat:
            return {**self.unwind_etf(snapshot, positions), **self.basket_exit_context,
                    "exit_reason": self.etf_reduction_reason, "inventory_reduction_required": True}

        if not equity_flat:
            if self.active_etf_route and self.active_etf_route.get("staging"):
                return self.manage_staged_tender(snapshot, positions)
            conversion = self.manage_converter(snapshot, positions, forced)
            if conversion:
                if self.held_basket:
                    conversion["retired_basket_basis"] = self.held_basket
                    conversion["basket_transition"] = "convergence basket handed to manual conversion route"
                self.held_basket = None
                return conversion
            if self.held_basket and holding:
                filled = sum(abs(row["quantity"]) for row in self.held_basket["entry_fills"]
                             if row["ticker"] == "RITC")
                if (size > 0 and holding["add_allowed"] and filled < target
                        and tick >= self.basket_cooldown_until
                        and holding["age_ticks"] + 3 * self.etf_config.ticks_per_action
                            + self.basket_config.min_hold_ticks < self.basket_config.max_hold_ticks):
                    addition = self.basket_analysis(snapshot, positions, min(target - filled, size))
                    direction = "LONG_RITC_SHORT_BASKET" if self.held_basket["direction"] == 1 else "SHORT_RITC_LONG_BASKET"
                    addition["opportunities"] = [r for r in addition["opportunities"] if r.get("direction") == direction]
                    action = self.enter_basket(snapshot, positions, addition)
                    if action:
                        return {**action, "basket_holding_before_add": holding}
                return {"wait": "hold convergence basket", "basket_holding": holding,
                        "basket_close": holding["close"], "liquidation_budget": budget}
            return self.unwind_etf(snapshot, positions)
        usd = positions.get("USD", 0)
        if abs(usd) >= 1:
            # USD orders are whole units. Round the final cash balance to the
            # nearest legal unit instead of truncating and systematically
            # leaving almost C$1 of residual currency exposure.
            cap = etf_policy.child_cap(snapshot, "USD", self.etf_config)
            side = "bids" if usd > 0 else "asks"
            visible = int(sum(max(0, r["quantity"] - r.get("quantity_filled", 0))
                              for r in snapshot["books"]["USD"][side]))
            fx_quantity = -int(math.copysign(min(round(abs(usd)), cap, visible), usd))
            if not fx_quantity:
                return {"wait": "no visible depth for net USD child"}
            action = self.submit(snapshot, "USD", fx_quantity, "hedge net USD cash")
            if self.etf_reduction_reason:
                action.update(exit_reason=self.etf_reduction_reason, inventory_reduction_required=True,
                              **self.basket_exit_context)
            return action
        if self.etf_reduction_reason:
            reason = self.etf_reduction_reason
            self.etf_reduction_reason = None
            self.basket_exit_context = {}
            self.basket_cooldown_until = max(self.basket_cooldown_until,
                                             tick + self.basket_config.cooldown_ticks)
            return {"wait": "basket reduction complete; cooldown", "exit_reason": reason,
                    "cooldown_until": self.basket_cooldown_until}
        if self.flatten_only:
            return {"wait": "flat"}
        if tick < self.basket_cooldown_until:
            return {"wait": "basket cooldown", "cooldown_until": self.basket_cooldown_until}
        if self.basket:
            if size <= 0:
                return {"wait": "ETF security has no legal entry child size"}
            analysis = self.basket_analysis(snapshot, positions, size)
            return self.enter_basket(snapshot, positions, analysis) or {"wait": "no eligible basket",
                                                                      "basket_candidates": analysis["opportunities"]}
        return {"wait": "no eligible ETF trade"}

    def _reduce_basket(self, reason: str, tick: int, context: dict | None = None) -> None:
        """Latch a direct exit until equities and final USD are flat; never add or retry."""
        if not self.etf_reduction_reason:
            self.etf_reduction_reason = reason
            self.basket_exit_context = context or {}
            if self.held_basket:
                self.basket_exit_context["basket_basis"] = self.held_basket
        self.held_basket = None
        self.manual_disabled = True
        self.active_etf_route = None
        self.basket_cooldown_until = tick + self.basket_config.cooldown_ticks

    def basket_analysis(self, snapshot: Mapping[str, Any], positions: Mapping[str, float],
                        maximum: int) -> dict[str, Any]:
        """Size each balanced slice to current depth and intermediate risk headroom."""
        opportunities = []
        for direction in (1, -1):
            low, high, best, last_error = 1, maximum, None, "no legal basket capacity"
            last_report = None
            while low <= high:
                size = (low + high) // 2
                try:
                    candidate = etf.basket_opportunity(snapshot, direction, size, self.gross_limit, self.net_limit)
                    if not candidate["within_configured_limits"]:
                        raise risk.RiskError("insufficient intermediate weighted headroom")
                    projected = dict(positions)
                    for ticker, quantity in candidate["legs"]:
                        fill = etf.executable_trade_cashflow(snapshot, ticker, quantity)
                        etf_policy.apply_cash(projected, fill)
                        risk.check_etf_projection(snapshot, projected, self.gross_limit, self.net_limit)
                    report = etf_basket.entry_report(snapshot, direction, size, self.basket_config,
                                                     self.etf_config, self.etf_sigmas)
                    last_report = report
                    candidate["basket_value"] = report
                    candidate["eligible_after_buffer"] = report["eligible"]
                    if not report["eligible"]:
                        raise ValueError(report["reason"])
                    budget = self._basket_liquidation_budget(snapshot, projected)
                    entry_ticks = len(candidate["legs"]) * self.etf_config.ticks_per_action
                    if (snapshot["case"]["tick"] + entry_ticks + self.basket_config.min_hold_ticks
                            >= budget["latest_start_tick"]):
                        raise ValueError("insufficient time for basket entry, convergence window and liquidation")
                    candidate["liquidation_budget"] = budget
                except (ValueError, KeyError) as error:
                    last_error = str(error)
                    high = size - 1
                else:
                    best = candidate
                    low = size + 1
            opportunities.append(best or {"direction": direction, "skip": last_error,
                                           "basket_value": last_report})
        return {"opportunities": opportunities}

    def check_route(self, snapshot: Mapping[str, Any], route: Mapping[str, Any]) -> None:
        """Validate cash, equity exposure and tradeability at every plan stage."""
        if route.get("fallback_route"):
            self.check_route(snapshot, route["fallback_route"])
        securities = {s["ticker"]: s for s in snapshot["securities"]}
        if route["fx_children"] and securities["USD"].get("is_tradeable") is not True:
            raise risk.RiskError("Route requires non-tradeable USD")
        for fill in [*route["preparation"], *route["fills"]]:
            if securities[fill["ticker"]].get("is_tradeable") is not True:
                raise risk.RiskError("Route requires a non-tradeable instrument")
        for projected in route["stages"]:
            risk.check_etf_projection(snapshot, projected, self.gross_limit, self.net_limit)
        final = dict(route["stages"][-1])
        final["CAD"] = final.get("CAD", 0) + route["fx"]["cad_value"]
        final["USD"] = 0
        risk.check_etf_projection(snapshot, final, self.gross_limit, self.net_limit)

    def manage_staged_tender(self, snapshot: Mapping[str, Any], positions: Mapping[str, float]) -> dict:
        """Pace fresh, confirmed children; failed replenishment latches direct exit.

        Forecast prices are not executable promises. If the next quote is
        worse, wait at most six ticks beyond its slot, never beyond the route
        deadline. Stops initiate liquidation, not guaranteed loss protection.
        Ambiguous order outcomes propagate unchanged to journal reconciliation.
        """
        route = self.active_etf_route
        stage = route["staging"]
        tick = snapshot["case"]["tick"]
        reason = None
        expected = stage["expected_positions"]
        if any(not math.isclose(positions.get(t, 0), expected.get(t, 0), abs_tol=1e-4, rel_tol=0)
               for t in (*etf.WEIGHTS, "USD", "CAD")):
            reason = "staged tender inventory changed outside confirmed fills"
        try:
            liquidation = etf.liquidation_value(snapshot, positions)
            pnl = positions.get("CAD", 0) + liquidation["total_cad"] - stage["baseline_total_cad"]
            stage["current_fallback_pnl_cad"] = pnl
            if pnl <= -stage["fallback_loss_cap_cad"]:
                reason = "staged tender fallback loss threshold reached"
        except (ValueError, KeyError):
            reason = "staged tender fallback depth disappeared"
        if tick >= stage["deadline_tick"]:
            reason = "staged tender unwind deadline"
        index = stage["completed_children"]
        if index >= len(route["fills"]):
            reason = "staged tender schedule exhausted with inventory remaining"
        if reason:
            context = {"staged_tender": dict(stage)}
            self._reduce_basket(reason, tick, context)
            return {**self.unwind_etf(snapshot, positions), **context, "exit_reason": reason,
                    "inventory_reduction_required": True}
        if tick < stage["next_child_tick"]:
            return {"wait": "staged tender: allow observed liquidity to replenish", "staged_tender": dict(stage)}
        child = route["fills"][index]
        q = child["quantity"]
        try:
            quote = etf.executable_trade_cashflow(snapshot, "RITC", q)
            favorable = quote["price"] <= child["price"] if q > 0 else quote["price"] >= child["price"]
        except (ValueError, KeyError):
            favorable = False
        if not favorable:
            if tick >= stage["next_child_tick"] + stage["max_wait_ticks"]:
                reason = "staged tender liquidity did not replenish; direct fallback"
                context = {"staged_tender": dict(stage)}
                self._reduce_basket(reason, tick, context)
                return {**self.unwind_etf(snapshot, positions), **context, "exit_reason": reason,
                        "inventory_reduction_required": True}
            return {"wait": "staged tender: next child quote worse than forecast", "staged_tender": dict(stage)}
        action = self.submit(snapshot, "RITC", q, "staged tender unwind", price_bound=child["price"])
        if self.executor and "wait" not in action:
            price = _fill_price(action.get("fill"), action["quoted_price"])
            etf_policy.apply_cash(expected, {"ticker": "RITC", "quantity": q,
                                           "cashflow": etf.trade_cashflow("RITC", q, price)})
            stage["completed_children"] += 1
            stage["next_child_tick"] = tick + stage["spacing_ticks"]
        return {**action, "staged_tender": copy.deepcopy(stage)}

    def manage_converter(self, snapshot: Mapping[str, Any], positions: Mapping[str, float],
                         forced: bool) -> dict[str, Any] | None:
        """Bound manual waits; after timeout, latch direct reduction until flat."""
        tick = snapshot["case"]["tick"]
        if forced or (self.manual_since is not None
                      and tick - self.manual_since >= self.etf_config.manual_wait_ticks):
            self.manual_disabled = True
            self.active_etf_route = None
        if self.manual_disabled or not any(positions[t] for t in etf.WEIGHTS):
            return None
        candidates = []
        for route in etf_policy.routes(snapshot, positions, self.etf_config):
            try:
                self.check_route(snapshot, route)
            except risk.RiskError:
                continue
            candidates.append(route)
        direct = next((r for r in candidates if not r["converter"]), None)
        manual = [r for r in candidates if r["converter"]
                  and tick <= r["budget"]["latest_accept_tick"]]
        if not manual:
            return None
        best = max(manual, key=lambda r: r["total_cad"])
        if direct and best["total_cad"] <= direct["total_cad"]:
            return None
        # Preserve time for a direct fallback as well as converter execution.
        deadline = min(tick + self.etf_config.manual_wait_ticks,
                       298 - (direct or best)["budget"]["reserve_ticks"])
        if deadline <= tick:
            self.manual_disabled = True
            return None
        self.active_etf_route = best
        if best["preparation"]:
            fill = best["preparation"][0]
            return self.submit(snapshot, fill["ticker"], fill["quantity"], "prepare manual ETF creation")
        if self.manual_since is None:
            self.manual_since = tick
        redemption = best["converter"] == "ETF-Redemption"
        advantage = best["total_cad"] - direct["total_cad"] if direct else None
        rec = {"converter": best["converter"], "manual_action": "UNWIND" if redemption else "WIND",
               "blocks": best["blocks"], "block_size": etf.CONVERTER_BLOCK,
               "convert_from": {"RITC": 10000} if redemption else {"BULL": 10000, "BEAR": 10000},
               "convert_to": {"BULL": 10000, "BEAR": 10000} if redemption else {"RITC": 10000},
               "estimated_advantage_cad": advantage / best["blocks"] if advantage is not None else None,
               "deadline_tick": min(deadline, self.manual_since + self.etf_config.manual_wait_ticks),
               "fallback": "automatic inventory reduction after deadline"}
        return {"wait": f"manual {best['converter']} recommended", "manual_converter": rec}

    def unwind_etf(self, snapshot: Mapping[str, Any], positions: Mapping[str, float]) -> dict[str, Any]:
        """Reduce the largest weighted holding in a depth-supported child.

        ETF risk is greatest while an unpaired holding remains. Use up to the
        venue's 10,000-share stock-order limit rather than the smaller basket
        entry size, while requiring all shares of the selected child to be
        executable against displayed depth.
        """
        securities = {str(item["ticker"]): item for item in snapshot["securities"]}
        for ticker in sorted(etf.WEIGHTS, key=lambda t: abs(positions[t])*etf.WEIGHTS[t], reverse=True):
            if not positions[ticker]:
                continue
            order_cap = min(self.etf_config.child_size, int(securities[ticker].get("max_trade_size", 0)))
            side = "BUY" if positions[ticker] < 0 else "SELL"
            levels = snapshot["books"][ticker]["asks" if side == "BUY" else "bids"]
            visible = int(sum(max(0, level["quantity"] - level.get("quantity_filled", 0)) for level in levels))
            q = -clip(positions[ticker], min(abs(int(positions[ticker])), order_cap, visible))
            if not q:
                continue
            try:
                etf.vwap(snapshot["books"][ticker], "BUY" if q > 0 else "SELL", abs(q))
            except ValueError:
                continue
            try:
                return self.submit(snapshot, ticker, q, "unwind inventory")
            except risk.RiskError:
                continue
        return {"wait": "no unwind satisfies depth and risk limits"}

    def choose_tender(self, snapshot: Mapping[str, Any], positions: Mapping[str, float],
                      analysis: Mapping[str, Any]) -> dict[str, Any] | None:
        """Accept one portfolio-valued fixed tender when no exit/manual route is latched.

        Full visible unwind depth is required for the complete tender. The
        modeled profit must clear a size-aware liquidation/slippage reserve;
        the server and local weighted-risk checks bound tender size.
        """
        assessments = sorted(self.etf_assessments,
                             key=lambda r: (r.get("risk_reducing", False), r.get("surplus_cad", -1e30)),
                             reverse=True)
        for assessment in assessments:
            if assessment["decision"] != "ACCEPT":
                continue
            offer = assessment["offer"]
            accepted_assessment = assessment
            if self.executor:
                fresh = self.client.snapshot("etf", trading=True)
                current = fresh["case"]
                if (current["status"] != "ACTIVE" or current.get("period") != snapshot["case"].get("period")
                        or not 0 <= current["tick"] - snapshot["case"]["tick"] <= 2):
                    rejected = dict(assessment)
                    rejected.update(decision="REJECT",
                                    reason="fresh preflight crossed a session boundary or advanced over two ticks")
                    return {"wait": "tender snapshot expired", "tender_assessment": rejected}
                fresh_positions = {row["ticker"]: row["position"] for row in fresh["securities"]}
                fresh_analysis = {}
                fresh_assessments = self.tender_assessments(fresh, fresh_positions, fresh_analysis)
                refreshed = next((item for item in fresh_assessments
                                  if item["tender_id"] == offer["tender_id"]), None)
                if refreshed is None:
                    rejected = dict(assessment)
                    rejected.update(decision="REJECT", reason="tender disappeared before acceptance")
                    return {"wait": "tender disappeared before acceptance", "tender_assessment": rejected}
                if refreshed["decision"] != "ACCEPT":
                    return {"wait": f"fresh tender rejected: {refreshed['reason']}",
                            "tender_assessment": refreshed}
                offer = refreshed["offer"]
                accepted_assessment = refreshed
                self.executor.tender(offer, fresh_positions["RITC"])
                self.held_basket = None
                self.manual_since = None
                self.manual_disabled = False
                self.active_etf_route = accepted_assessment["selected_route"]
                if self.active_etf_route.get("staging"):
                    stage = self.active_etf_route["staging"]
                    expected = dict(fresh_positions)
                    q = int(offer["quantity"]) * (1 if offer["action"] == "BUY" else -1)
                    expected["RITC"] += q
                    expected["USD"] = expected.get("USD", 0) - q * offer["price"]
                    tick = current["tick"]
                    stage.update(expected_positions=expected, completed_children=0, next_child_tick=tick,
                                 deadline_tick=tick + self.active_etf_route["budget"]["reserve_ticks"]
                                                     - self.etf_config.end_buffer_ticks)
            return {"tender_id": offer["tender_id"], "reason": "tender clears selected route policy",
                    "tender_assessment": accepted_assessment}
        return None

    def tender_assessments(self, snapshot: Mapping[str, Any], positions: Mapping[str, float],
                           analysis: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Rank feasible post-tender portfolios, explaining every candidate route."""
        assessments: list[dict[str, Any]] = []
        for offer in snapshot.get("tenders", []):
            report = etf_policy.tender_report(snapshot, offer, self.etf_config, self.etf_sigmas, self.etf_liquidity)
            assessment = {"offer": offer, "tender_id": offer.get("tender_id"),
                          "action": offer.get("action"), "quantity": offer.get("quantity"),
                          "price": offer.get("price"), "expires": offer.get("expires"),
                          "estimated_unwind_profit_usd": report.get("estimated_unwind_profit_usd"),
                          "estimated_unwind_profit_cad": report.get("estimated_unwind_profit_cad"),
                          "minimum_profit_cad": report.get("liquidation_reserve_cad"),
                          "liquidation_reserve": report.get("liquidation_reserve"),
                          "risk_reducing": report.get("risk_reducing", False), "routes": []}
            if report.get("staged_unavailable"):
                assessment["staged_unavailable"] = report["staged_unavailable"]
            if "skip" in report:
                assessment.update(decision="REJECT", reason=f"unwind estimate unavailable: {report['skip']}")
            elif self.flatten_only:
                assessment.update(decision="REJECT", reason="new tender exposure is disabled in flatten-only mode")
            elif isinstance(offer.get("expires"), (int, float)) and offer["expires"] < snapshot["case"]["tick"]:
                assessment.update(decision="REJECT", reason="tender offer is already expired")
            else:
                q = int(offer["quantity"]) * (1 if offer["action"] == "BUY" else -1)
                try:
                    risk.check(snapshot, "RITC", q, "etf", gross_limit=self.gross_limit,
                               net_limit=self.net_limit, tender=True)
                    risk.check_etf_projection(snapshot, report["projected_positions"],
                                              self.gross_limit, self.net_limit)
                except risk.RiskError as error:
                    assessment.update(decision="REJECT", reason=f"risk gate: {error}")
                else:
                    eligible = []
                    for route in report["routes"]:
                        rejection = None
                        try:
                            self.check_route(snapshot, route)
                        except risk.RiskError as error:
                            rejection = f"risk gate: {error}"
                        if route["converter"] and self.etf_config.manual_wait_ticks == 0:
                            rejection = "manual conversion is disabled"
                        if snapshot["case"]["tick"] > route["budget"]["latest_accept_tick"]:
                            rejection = "insufficient time for a full child-order liquidation"
                        if rejection is None and route["surplus_cad"] < 0:
                            rejection = ("unwind edge is negative before risk reserves"
                                         if route["profit_cad"] < 0 else
                                         "unwind edge is below execution + FX risk reserve")
                        assessment["routes"].append({"name": route["name"], "blocks": route["blocks"],
                            "profit_cad": route["profit_cad"], "reserve": route["reserve"],
                            "surplus_cad": route["surplus_cad"], "budget": route["budget"],
                            "staging": route.get("staging"),
                            "rejection": rejection})
                        if rejection is None:
                            eligible.append(route)
                    if eligible:
                        best = max(eligible, key=lambda r: r["surplus_cad"])
                        assessment.update(decision="ACCEPT", selected_route=best,
                                          estimated_unwind_profit_cad=best["profit_cad"],
                                          minimum_profit_cad=best["minimum_profit_cad"],
                                          liquidation_reserve=best["reserve"],
                                          liquidation_budget=best["budget"], surplus_cad=best["surplus_cad"],
                                          reason=f"{best['name']} clears buffer, execution + FX reserve and risk gates")
                    else:
                        best = max(assessment["routes"], key=lambda r: r["surplus_cad"])
                        assessment.update(decision="REJECT", reason=best["rejection"],
                                          liquidation_budget=best["budget"])
            assessments.append(assessment)
        return assessments

    @staticmethod
    def _same_etf_session(original: Mapping[str, Any], fresh: Mapping[str, Any]) -> bool:
        """Never carry a serial entry or abort across an observed market reset."""
        a, b = original["case"], fresh["case"]
        return (b["status"] == "ACTIVE" and a.get("period") == b.get("period")
                and a["tick"] <= b["tick"] < 299)

    def _abort_basket(self, snapshot: Mapping[str, Any], base_positions: Mapping[str, float],
                      entry_fills: list[Mapping[str, Any]]) -> None:
        """Reverse confirmed basket legs in reverse fill order after repricing says abort."""
        expected = {ticker: float(base_positions[ticker]) for ticker in etf.WEIGHTS}
        for fill in entry_fills:
            expected[fill["ticker"]] += int(fill["quantity"])
        for fill in reversed(entry_fills):
            fresh = self.client.snapshot("etf", trading=True)
            if not self._same_etf_session(snapshot, fresh):
                raise RITReadError("session changed during basket abort; recheck inventory")
            actual = {s["ticker"]: s["position"] for s in fresh["securities"]}
            if any(actual[t] != expected[t] for t in etf.WEIGHTS):
                raise RuntimeError("Basket abort inventory mismatch; stop and reconcile")
            ticker, quantity = fill["ticker"], -int(fill["quantity"])
            action = self.submit(fresh, ticker, quantity, "abort basket after serial repricing")
            if "wait" in action:
                raise RITReadError(action["wait"])
            expected[ticker] += quantity

    def enter_basket(self, snapshot: Mapping[str, Any], positions: Mapping[str, float],
                     analysis: Mapping[str, Any]) -> dict[str, Any] | None:
        """Execute a capped convergence slice, repricing the next leg on its own snapshot.

        Confirmed partial fills latch direct reduction on read/risk failures.
        Unknown mutation outcomes still propagate to the execution halt; no
        order is retried and no automatic abort follows an ambiguous fill.
        """
        for opportunity in analysis["opportunities"]:
            if (not opportunity.get("eligible_after_buffer")
                    or not opportunity.get("within_configured_limits")):
                continue
            legs = opportunity["legs"]
            planned = dict(positions)
            for ticker, quantity in legs:
                etf_policy.apply_cash(planned, etf.executable_trade_cashflow(snapshot, ticker, quantity))
            budget = self._basket_liquidation_budget(snapshot, planned)
            if (snapshot["case"]["tick"] + len(legs) * self.etf_config.ticks_per_action
                    + self.basket_config.min_hold_ticks >= budget["latest_start_tick"]):
                continue
            if not self.executor:
                return {"basket": legs, "reason": "capped convergence basket",
                        "basket_value": opportunity.get("basket_value"), "liquidation_budget": budget}
            entry_fills: list[dict[str, Any]] = []
            serial_reports = []
            fresh = snapshot
            try:
                for index, (ticker, q) in enumerate(legs):
                    if not self._same_etf_session(snapshot, fresh):
                        self._reduce_basket("partial basket crossed session boundary", snapshot["case"]["tick"])
                        return {"wait": "session changed during serial basket", "inventory_reduction_required": True}
                    actual = self.position_map(fresh)
                    expected = dict(positions)
                    for prior in entry_fills:
                        etf_policy.apply_cash(expected, prior)
                    if (any(actual[t] != expected[t] for t in etf.WEIGHTS)
                            or not math.isclose(actual.get("USD", 0), expected.get("USD", 0), abs_tol=1e-4, rel_tol=0)):
                        raise RuntimeError("Basket inventory mismatch; stop and reconcile")
                    if index:
                        choice = etf_basket.serial_report(fresh, entry_fills, legs[index:],
                            self.basket_config, self.etf_config, self.etf_sigmas)
                        serial_reports.append(choice)
                        remaining_budget = self._basket_liquidation_budget(fresh, planned)
                        too_late = (fresh["case"]["tick"] + (len(legs) - index) * self.etf_config.ticks_per_action
                                    + self.basket_config.min_hold_ticks >= remaining_budget["latest_start_tick"])
                        if not choice["finish"] or too_late:
                            self._reduce_basket("partial basket aborted after repricing", fresh["case"]["tick"],
                                                {"serial_reprice": choice, "entry_fills": entry_fills})
                            # Missing depth is a recoverable inventory reduction,
                            # never an instruction to send an unpriceable reversal.
                            if "abort_exit_fills" not in choice:
                                return {"wait": "basket depth changed; reduce confirmed inventory",
                                        "serial_reprice": choice, "inventory_reduction_required": True}
                            self._abort_basket(snapshot, positions, entry_fills)
                            return {"basket": legs[:index], "reason": "basket aborted after serial repricing",
                                    "aborted": True, "serial_reprice": choice,
                                    "inventory_reduction_required": True}
                    side = "BUY" if q > 0 else "SELL"
                    quoted_price = etf.vwap(fresh["books"][ticker], side, abs(q))
                    action = self.submit(fresh, ticker, q, "basket leg")
                    if "wait" in action:
                        if entry_fills:
                            self._reduce_basket("partial basket preflight unavailable", fresh["case"]["tick"])
                            action["inventory_reduction_required"] = True
                        return action
                    fill_price = _fill_price(action.get("fill"), quoted_price)
                    entry_fills.append({"ticker": ticker, "quantity": q, "price": fill_price,
                                        "cashflow": etf.trade_cashflow(ticker, q, fill_price)})
                    if index < len(legs) - 1:
                        fresh = self.client.snapshot("etf", trading=True)
            except Exception:
                if entry_fills:
                    self._reduce_basket("partial basket interrupted; reduce confirmed inventory",
                                        snapshot["case"]["tick"], {"entry_fills": entry_fills})
                raise
            expected = {t: positions[t] for t in etf.WEIGHTS}
            for ticker, q in legs:
                expected[ticker] += q
            prior_fills = self.held_basket["entry_fills"] if self.held_basket else []
            entry_tick = self.held_basket["entry_tick"] if self.held_basket else snapshot["case"]["tick"]
            self.held_basket = {"expected_positions": expected,
                                "expected_usd": positions.get("USD", 0) + etf.cashflow_totals(entry_fills)["USD"],
                                "direction": 1 if legs[-1][1] > 0 else -1,
                                "entry_fills": [*prior_fills, *entry_fills],
                                "entry_cashflows": etf.cashflow_totals([*prior_fills, *entry_fills]),
                                "entry_tick": entry_tick}
            return {"basket": legs, "reason": "basket filled", "entry_fills": entry_fills,
                    "basket_value": opportunity.get("basket_value"), "serial_reports": serial_reports,
                    "basket_basis": self.held_basket,
                    "target_quantity": min(self.quantity, self.basket_config.max_quantity),
                    "filled_quantity": sum(abs(r["quantity"]) for r in self.held_basket["entry_fills"]
                                           if r["ticker"] == "RITC")}
        return None
