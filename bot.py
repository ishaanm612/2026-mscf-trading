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


def clip(position: float, size: int) -> int:
    """Keep the sign, cap absolute size, and truncate fractional currency units."""
    return int(math.copysign(min(abs(position), size), position)) if position else 0


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
        """Validate then optionally submit one signed market order.

        :param snapshot: Fresh account state.
        :param ticker: Instrument ticker.
        :param quantity: Signed order quantity.
        :param reason: Audit explanation.
        :param deltas: Current instrument delta weights.
        :returns: Submitted or planned action.
        """
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
            # Re-read case immediately before mutation; do not trade an older snapshot.
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
            self.executor.order(ticker, quantity)
        return action

    def step(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Produce at most one safe action from a fresh snapshot.

        :param snapshot: Fresh RIT case snapshot.
        :returns: Action or wait explanation.
        """
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
        priority_hedge = decision.reason.startswith("hedge:") and not self.flatten_only
        if priority_hedge:
            # Interrupt the pair, preserving the remaining leg for fresh validation.
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

    def etf_step(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Priority: FX hedge, basket hold/exit, inventory unwind, then new trades."""
        positions = {s["ticker"]: s["position"] for s in snapshot["securities"]}
        closing = self.flatten_only or snapshot["case"]["tick"] >= 250
        # Currency position is reconciled after equity/tender fills, never estimated from order intent.
        usd = positions.get("USD", 0)
        if abs(usd) >= 1:
            return self.submit(snapshot, "USD", -clip(usd, 2500000), "hedge USD cash")
        analysis = etf.analyze(snapshot, self.quantity, self.gross_limit, self.net_limit)
        if self.held_basket and not closing:
            expected, direction = self.held_basket
            if all(positions[t] == p for t, p in expected.items()):
                opportunity = analysis["opportunities"][0 if direction == 1 else 1]
                if opportunity.get("edge_cad_per_unit", -1) > 0:
                    return {"wait": "hold basket for convergence"}
            self.held_basket = None
        # A tender, a restarted bot, or an exit is unwound one child at a time.
        if any(positions[t] for t in etf.WEIGHTS):
            return self.unwind_etf(snapshot, positions)
        if closing:
            return {"wait": "flat"}
        tender_action = self.choose_tender(snapshot, positions, analysis)
        if tender_action:
            return tender_action
        if self.basket:
            return self.enter_basket(snapshot, positions, analysis) or {"wait": "no eligible basket"}
        return {"wait": "no eligible ETF trade"}

    def unwind_etf(self, snapshot: Mapping[str, Any], positions: Mapping[str, float]) -> dict[str, Any]:
        """Reduce the largest weighted holding that can legally be closed now."""
        for ticker in sorted(etf.WEIGHTS, key=lambda t: abs(positions[t])*etf.WEIGHTS[t], reverse=True):
            if not positions[ticker]:
                continue
            q = -clip(positions[ticker], self.quantity)
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
        """Accept at most one fixed ETF offer, only while existing equity inventory is flat.

        Tenders are capped at 10,000 units for this MVP even when the session
        permits larger offers. Full visible unwind depth and a 5-cent cushion
        are required; this is a conservative estimate, not a guaranteed profit.
        """
        for offer in snapshot.get("tenders", []):
            report = next(r for r in analysis["tenders"] if r["tender_id"] == offer["tender_id"])
            # Minimum edge reserves $0.05/unit for adverse movement in a chunked unwind.
            if (offer.get("ticker") != "RITC" or not offer.get("is_fixed_bid")
                    or offer.get("expires", 0) <= snapshot["case"]["tick"] + 3
                    or report.get("estimated_unwind_profit_usd", -1) < .05 * offer["quantity"]
                    or offer["quantity"] > 10000):
                continue
            q = int(offer["quantity"]) * (1 if offer["action"] == "BUY" else -1)
            if abs(q) != offer["quantity"]:
                continue
            try:
                risk.check(snapshot, "RITC", q, "etf", gross_limit=self.gross_limit,
                           net_limit=self.net_limit, tender=True)
            except risk.RiskError:
                continue
            if self.executor:
                current = self.client.get("case")
                if (current["status"] != "ACTIVE" or current.get("period") != snapshot["case"].get("period")
                        or not 0 <= current["tick"] - snapshot["case"]["tick"] <= 2
                        or current["tick"] >= 250 or offer["expires"] <= current["tick"] + 3):
                    return {"wait": "tender snapshot expired"}
                self.executor.tender(offer, positions["RITC"])
            return {"tender_id": offer["tender_id"], "reason": "profitable depth-backed tender"}
        return None

    def enter_basket(self, snapshot: Mapping[str, Any], positions: Mapping[str, float],
                     analysis: Mapping[str, Any]) -> dict[str, Any] | None:
        """Execute optional basket legs serially, checking inventory between legs.

        Exchange legs are NOT atomic. A failure propagates out of the runner;
        do not catch it and submit a replacement basket. Restart into inventory
        reduction after reconciling the interrupted journal.
        """
        for opportunity in analysis["opportunities"]:
            if opportunity.get("edge_cad_per_unit", -1) < .10 or not opportunity.get("within_configured_limits"):
                continue
            legs = opportunity["legs"]
            if not self.executor:
                return {"basket": legs, "reason": "basket mispricing"}
            for index, (ticker, q) in enumerate(legs):
                fresh = snapshot if index == 0 else self.client.snapshot("etf", trading=True)
                # Confirm actual inventory after the previous fully filled child order.
                actual = {s["ticker"]: s["position"] for s in fresh["securities"]}
                expected = dict(positions)
                for prior_ticker, prior_q in legs[:index]:
                    expected[prior_ticker] += prior_q
                if any(actual[t] != expected[t] for t in etf.WEIGHTS):
                    raise RuntimeError("Basket inventory mismatch; stop and reconcile")
                self.submit(fresh, ticker, q, "basket leg")
            expected = {t: positions[t] for t in etf.WEIGHTS}
            for ticker, q in legs:
                expected[ticker] += q
            self.held_basket = (expected, 1 if legs[-1][1] > 0 else -1)
            return {"basket": legs, "reason": "basket filled"}
        return None
