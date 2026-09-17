"""Conservative serial case strategies. Each cycle starts from fresh account state."""
import math
from dataclasses import replace
from typing import Any, Mapping

from models import etf, volatility, news
from client import RITReadError
import risk
from volatility.config import VolatilityConfig
from volatility.convergence import ConvergenceModel
from volatility.strategy import DesiredTrade, VolatilityStrategy
from volatility.signals import find_mispricings


ETF_UNWIND_MAX_CHILD = 10_000
ETF_TENDER_TICKS_PER_ACTION = 3
ETF_TENDER_LIQUIDATION_BUFFER_TICKS = 5
ETF_BASKET_LAST_ENTRY_TICK = 250


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
                 explainability: bool = True, convergence_model_path: str | None = None) -> None:
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
        self.last_tick = None
        self.last_period = None
        convergence_model = ConvergenceModel.load(convergence_model_path) if convergence_model_path else None
        self.volatility_strategy = VolatilityStrategy(replace(VolatilityConfig(), risk_free_rate=rate,
                                                              explainability_enabled=explainability), sigma,
                                                   convergence_model)
        self.pending_volatility_trades: list[DesiredTrade] = []
        self.expected_volatility_positions: dict[str, float] | None = None

    def submit(self, snapshot: Mapping[str, Any], ticker: str, quantity: int, reason: str,
               deltas: Mapping[str, float] | None = None) -> dict[str, Any]:
        """Validate then optionally submit one signed market order."""
        risk.check(snapshot, ticker, quantity, self.case, deltas, self.gross_limit, self.net_limit)
        action = {"ticker": ticker, "quantity": quantity, "reason": reason}
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
            try:
                current = self.client.get("case")
            except RITReadError:
                if self.case != "volatility":
                    raise
                self.pending_volatility_trades.clear()
                return {"wait": "case preflight unavailable; replan before submission"}
            old = snapshot["case"]
            if (current["status"] != "ACTIVE" or current.get("period") != old.get("period")
                    or not 0 <= current["tick"] - old["tick"] <= 2 or current["tick"] >= 299):
                if self.case != "volatility":
                    raise RuntimeError("Snapshot expired before submission")
                self.pending_volatility_trades.clear()
                return {"wait": "snapshot expired before submission; replan from fresh state"}
            fill = self.executor.order(ticker, quantity)
            action["fill"] = dict(fill)
        return action

    def step(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Produce at most one safe action from a fresh snapshot."""
        state = snapshot["case"]
        if self.last_tick is not None and (state["tick"] < self.last_tick or state.get("period") != self.last_period):
            self.held_basket = None
        self.last_tick, self.last_period = state["tick"], state.get("period")
        if state["status"] != "ACTIVE" or state["tick"] >= 299:
            return {"wait": "inactive or final tick"}
        if not isinstance(snapshot.get("orders"), list):
            raise RuntimeError("Missing or invalid open-order state")
        if snapshot["orders"]:
            if self.case != "volatility":
                raise RuntimeError("Existing open orders must be reconciled before running the bot")
            self.pending_volatility_trades.clear()
            self.expected_volatility_positions = None
            return {"wait": "open account orders; waiting for fills or cancellation",
                    "open_order_ids": [order.get("order_id") for order in snapshot["orders"]]}
        return self.volatility_step(snapshot) if self.case == "volatility" else self.etf_step(snapshot)

    @staticmethod
    def position_map(snapshot: Mapping[str, Any]) -> dict[str, float]:
        """Extract account inventory for detecting fills outside this bot."""
        return {str(row["ticker"]): float(row["position"]) for row in snapshot["securities"]}

    def volatility_step(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Execute at most one validated V1 volatility action from a fresh decision."""
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

    @staticmethod
    def _basket_liquidation_budget(snapshot: Mapping[str, Any], positions: Mapping[str, float]) -> dict[str, int]:
        """Reserve enough late-round time to flatten basket equities and net USD."""
        securities = {row["ticker"]: row for row in snapshot["securities"]}
        actions = 0
        for ticker in etf.WEIGHTS:
            position = abs(int(positions.get(ticker, 0)))
            if not position:
                continue
            cap = min(ETF_UNWIND_MAX_CHILD, int(securities[ticker].get("max_trade_size", 0)))
            if cap <= 0:
                return {"actions": 300, "reserve_ticks": 300, "latest_start_tick": 0}
            actions += math.ceil(position / cap)
        if actions:
            actions += 1  # final net-USD conversion after all equities are flat
        reserve = actions * ETF_TENDER_TICKS_PER_ACTION + ETF_TENDER_LIQUIDATION_BUFFER_TICKS
        return {"actions": actions, "reserve_ticks": reserve,
                "latest_start_tick": max(0, 298 - reserve)}

    def etf_step(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Manage tracked baskets, inventory, final USD, tenders, then new baskets."""
        positions = {s["ticker"]: s["position"] for s in snapshot["securities"]}
        tick = snapshot["case"]["tick"]
        analysis = etf.analyze(snapshot, self.quantity, self.gross_limit, self.net_limit)

        if self.held_basket:
            expected = self.held_basket["expected_positions"]
            if all(positions[t] == p for t, p in expected.items()):
                budget = self._basket_liquidation_budget(snapshot, positions)
                forced = self.flatten_only or tick >= budget["latest_start_tick"]
                try:
                    close = etf.basket_close_now(snapshot, self.held_basket)
                except (ValueError, KeyError) as error:
                    if not forced:
                        return {"wait": f"basket close cannot be priced: {error}",
                                "liquidation_budget": budget}
                    close = {"pnl_cad": None, "pricing_error": str(error)}
                if not forced and close["pnl_cad"] <= 0:
                    return {"wait": "hold basket; executable close-now P&L is not positive",
                            "basket_close": close, "liquidation_budget": budget}
                self.held_basket = None
            else:
                self.held_basket = None

        recommended_converter = next((item for item in analysis.get("manual_converters", [])
                                      if item.get("recommended")), None)
        if recommended_converter:
            return {"wait": f"manual {recommended_converter['converter']} recommended",
                    "manual_converter": recommended_converter}
        if any(positions[t] for t in etf.WEIGHTS):
            return self.unwind_etf(snapshot, positions)
        usd = positions.get("USD", 0)
        if abs(usd) >= 1:
            # USD orders are whole units. Round the final cash balance to the
            # nearest legal unit instead of truncating and systematically
            # leaving almost C$1 of residual currency exposure.
            fx_quantity = -int(math.copysign(min(round(abs(usd)), 2_500_000), usd))
            return self.submit(snapshot, "USD", fx_quantity, "hedge net USD cash")
        tender_action = self.choose_tender(snapshot, positions, analysis)
        if tender_action:
            return tender_action
        if self.flatten_only:
            return {"wait": "flat"}
        if tick >= ETF_BASKET_LAST_ENTRY_TICK:
            return {"wait": "no new basket late in round"}
        if self.basket:
            return self.enter_basket(snapshot, positions, analysis) or {"wait": "no eligible basket"}
        return {"wait": "no eligible ETF trade"}

    def unwind_etf(self, snapshot: Mapping[str, Any], positions: Mapping[str, float]) -> dict[str, Any]:
        """Reduce the largest weighted holding in a depth-supported child."""
        securities = {str(item["ticker"]): item for item in snapshot["securities"]}
        for ticker in sorted(etf.WEIGHTS, key=lambda t: abs(positions[t])*etf.WEIGHTS[t], reverse=True):
            if not positions[ticker]:
                continue
            order_cap = min(ETF_UNWIND_MAX_CHILD, int(securities[ticker].get("max_trade_size", 0)))
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
        """Accept one fixed RITC tender only when its stressed liquidation remains attractive."""
        assessments = self.tender_assessments(snapshot, positions, analysis)
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
                fresh_analysis = etf.analyze(fresh, self.quantity, self.gross_limit, self.net_limit)
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
            return {"tender_id": offer["tender_id"], "reason": "profitable depth-backed tender",
                    "tender_assessment": accepted_assessment}
        return None

    @staticmethod
    def _tender_liquidation_budget(snapshot: Mapping[str, Any], quantity: int) -> dict[str, int]:
        """Reserve time for legal RITC children and one final net FX hedge."""
        securities = {row["ticker"]: row for row in snapshot["securities"]}
        ritc_cap = min(ETF_UNWIND_MAX_CHILD, int(securities["RITC"].get("max_trade_size", 0)))
        fx_cap = int(securities["USD"].get("max_trade_size", 0))
        if ritc_cap <= 0 or fx_cap <= 0:
            return {"actions": 300, "reserve_ticks": 300, "latest_accept_tick": 0}
        actions = math.ceil(quantity / ritc_cap) + 1
        reserve = actions * ETF_TENDER_TICKS_PER_ACTION + ETF_TENDER_LIQUIDATION_BUFFER_TICKS
        return {"actions": actions, "reserve_ticks": reserve,
                "latest_accept_tick": max(0, 298 - reserve)}

    def tender_assessments(self, snapshot: Mapping[str, Any], positions: Mapping[str, float],
                           analysis: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Explain accept/reject decisions for every visible ETF tender."""
        reports = {item["tender_id"]: item for item in analysis.get("tenders", [])}
        existing_inventory = any(positions.get(ticker, 0) for ticker in etf.WEIGHTS)
        assessments: list[dict[str, Any]] = []
        for offer in snapshot.get("tenders", []):
            quantity = offer.get("quantity")
            report = reports.get(offer.get("tender_id"), {})
            reserve = report.get("liquidation_reserve_cad")
            budget = (self._tender_liquidation_budget(snapshot, int(quantity))
                      if isinstance(quantity, (int, float)) and quantity > 0 and int(quantity) == quantity else None)
            assessment = {"offer": offer, "tender_id": offer.get("tender_id"),
                          "action": offer.get("action"), "quantity": quantity,
                          "price": offer.get("price"), "expires": offer.get("expires"),
                          "estimated_unwind_profit_usd": report.get("estimated_unwind_profit_usd"),
                          "estimated_unwind_profit_cad": report.get("estimated_unwind_profit_cad"),
                          "minimum_profit_cad": reserve,
                          "liquidation_reserve": report.get("liquidation_reserve"),
                          "liquidation_budget": budget}
            if offer.get("ticker") != "RITC" or not offer.get("is_fixed_bid"):
                assessment.update(decision="REJECT", reason="only fixed-price RITC tenders are supported")
            elif "skip" in report:
                assessment.update(decision="REJECT", reason=f"unwind estimate unavailable: {report['skip']}")
            elif self.flatten_only:
                assessment.update(decision="REJECT", reason="new tender exposure is disabled in flatten-only mode")
            elif existing_inventory:
                assessment.update(decision="DEFER", reason="existing ETF inventory must be reduced first")
            elif isinstance(offer.get("expires"), (int, float)) and offer["expires"] < snapshot["case"]["tick"]:
                assessment.update(decision="REJECT", reason="tender offer is already expired")
            elif offer.get("action") not in ("BUY", "SELL") or not isinstance(quantity, (int, float)) or int(quantity) != quantity:
                assessment.update(decision="REJECT", reason="tender action or quantity is invalid")
            elif snapshot["case"]["tick"] > budget["latest_accept_tick"]:
                assessment.update(decision="REJECT", reason="insufficient time for a full child-order liquidation")
            elif reserve is None:
                assessment.update(decision="REJECT", reason="size-aware liquidation reserve is unavailable")
            elif report.get("estimated_unwind_profit_cad", float("-inf")) < reserve:
                assessment.update(decision="REJECT",
                                  reason="FX-adjusted unwind edge is below the size-aware liquidation/slippage reserve")
            else:
                q = int(quantity) * (1 if offer["action"] == "BUY" else -1)
                try:
                    risk.check(snapshot, "RITC", q, "etf", gross_limit=self.gross_limit,
                               net_limit=self.net_limit, tender=True)
                except risk.RiskError as error:
                    assessment.update(decision="REJECT", reason=f"risk gate: {error}")
                else:
                    assessment.update(decision="ACCEPT",
                                      reason="full-depth unwind edge clears size-aware reserve and risk gates")
            assessments.append(assessment)
        return assessments

    def _abort_basket(self, base_positions: Mapping[str, float], entry_fills: list[Mapping[str, Any]]) -> None:
        """Reverse confirmed basket legs in reverse fill order after repricing says abort."""
        expected = {ticker: float(base_positions[ticker]) for ticker in etf.WEIGHTS}
        for fill in entry_fills:
            expected[fill["ticker"]] += int(fill["quantity"])
        for fill in reversed(entry_fills):
            fresh = self.client.snapshot("etf", trading=True)
            actual = {s["ticker"]: s["position"] for s in fresh["securities"]}
            if any(actual[t] != expected[t] for t in etf.WEIGHTS):
                raise RuntimeError("Basket abort inventory mismatch; stop and reconcile")
            ticker, quantity = fill["ticker"], -int(fill["quantity"])
            self.submit(fresh, ticker, quantity, "abort basket after serial repricing")
            expected[ticker] += quantity

    def enter_basket(self, snapshot: Mapping[str, Any], positions: Mapping[str, float],
                     analysis: Mapping[str, Any]) -> dict[str, Any] | None:
        """Execute serial basket legs with a fresh finish-versus-abort check after every fill."""
        for opportunity in analysis["opportunities"]:
            if (not opportunity.get("eligible_after_buffer")
                    or not opportunity.get("within_configured_limits")):
                continue
            legs = opportunity["legs"]
            if not self.executor:
                return {"basket": legs, "reason": "basket mispricing"}
            entry_fills: list[dict[str, Any]] = []
            for index, (ticker, q) in enumerate(legs):
                fresh = snapshot if index == 0 else self.client.snapshot("etf", trading=True)
                actual = {s["ticker"]: s["position"] for s in fresh["securities"]}
                expected = dict(positions)
                for prior in entry_fills:
                    expected[prior["ticker"]] += int(prior["quantity"])
                if any(actual[t] != expected[t] for t in etf.WEIGHTS):
                    raise RuntimeError("Basket inventory mismatch; stop and reconcile")
                side = "BUY" if q > 0 else "SELL"
                quoted_price = etf.vwap(fresh["books"][ticker], side, abs(q))
                action = self.submit(fresh, ticker, q, "basket leg")
                fill_price = _fill_price(action.get("fill"), quoted_price)
                entry_fills.append({"ticker": ticker, "quantity": q, "price": fill_price,
                                    "cashflow": etf.trade_cashflow(ticker, q, fill_price)})
                if index < len(legs) - 1:
                    repriced = self.client.snapshot("etf", trading=True)
                    after = {s["ticker"]: s["position"] for s in repriced["securities"]}
                    expected[ticker] += q
                    if any(after[t] != expected[t] for t in etf.WEIGHTS):
                        raise RuntimeError("Basket inventory mismatch after fill; stop and reconcile")
                    choice = etf.serial_basket_choice(repriced, entry_fills, legs[index + 1:])
                    if not choice["finish"]:
                        self._abort_basket(positions, entry_fills)
                        return {"basket": legs[:index + 1], "reason": "basket aborted after serial repricing",
                                "aborted": True, "serial_reprice": choice}
            expected = {t: positions[t] for t in etf.WEIGHTS}
            for ticker, q in legs:
                expected[ticker] += q
            self.held_basket = {"expected_positions": expected,
                                "direction": 1 if legs[-1][1] > 0 else -1,
                                "entry_fills": entry_fills,
                                "entry_cashflows": etf.cashflow_totals(entry_fills),
                                "entry_tick": snapshot["case"]["tick"]}
            return {"basket": legs, "reason": "basket filled", "entry_fills": entry_fills}
        return None
