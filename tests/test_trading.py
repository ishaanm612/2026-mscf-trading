"""Behavior tests using a tiny exchange double, never the practice accounts."""
import copy
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot import Bot
from client import Client, RITReadError, RITTransportError
from execution import Executor
from models import etf
from models.news import forecast
from risk import check, RiskError
from run import demo, format_manual_converter_alert


class Exchange:
    """Fill market orders immediately and update inventory like the RIT account API."""
    def __init__(self, case):
        self.state = demo(case)
        if case == "etf":
            cash_limit = next(limit for limit in self.state["limits"] if limit["name"] == "cash")
            cash_limit.update(gross_limit=10_000_000, net_limit=10_000_000)
        self.orders = {}
        self.requests = []
        self.fail = False
        self.partial = False

    def snapshot(self, *args, **kwargs):
        return copy.deepcopy(self.state)

    def get(self, endpoint):
        if endpoint == "case":
            return self.state["case"].copy()
        if endpoint == "securities":
            return copy.deepcopy(self.state["securities"])
        return self.orders[int(endpoint.split("/")[-1])].copy()

    def request(self, method, endpoint, **params):
        self.requests.append((method, endpoint, params))
        if self.fail:
            raise TimeoutError("Unknown exchange outcome")
        if endpoint.startswith("tenders/"):
            offer = next(o for o in self.state["tenders"] if o["tender_id"] == int(endpoint.split("/")[-1]))
            signed = offer["quantity"] * (1 if offer["action"] == "BUY" else -1)
            self.move(offer["ticker"], signed)
            self.move("USD", -signed * offer["price"])
            self.state["tenders"] = []
            return {"success": True}
        oid = len(self.orders) + 1
        filled = params["quantity"] // 2 if self.partial else params["quantity"]
        self.orders[oid] = {"order_id": oid, "quantity_filled": filled,
                            "status": "CANCELLED" if self.partial else "TRANSACTED"}
        signed = filled * (1 if params["action"] == "BUY" else -1)
        self.move(params["ticker"], signed)
        if params["ticker"] == "RITC":
            side = "asks" if signed > 0 else "bids"
            price = self.state["books"]["RITC"][side][0]["price"]
            self.move("USD", -signed * price - abs(signed) * .02)
        return self.orders[oid].copy()

    def move(self, ticker, quantity):
        security = next(s for s in self.state["securities"] if s["ticker"] == ticker)
        security["position"] += quantity
        for limit in self.state["limits"]:
            terms = []
            for s in self.state["securities"]:
                binding = next((b for b in s["limits"] if b["name"] == limit["name"]), None)
                if binding:
                    terms.append(s["position"] / binding["units"])
            limit["gross"] = sum(abs(p) for p in terms)
            limit["net"] = sum(terms)


class Trading(unittest.TestCase):
    def test_get_timeout_retries_but_post_timeout_does_not(self):
        """Retry safe market-data reads while preserving one-shot mutations."""

        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        environment = {"RIT_API_MODE": "dma", "RIT_USERNAME": "test", "RIT_PASSWORD": "test"}
        with patch.dict(os.environ, environment, clear=True), patch("client.time.sleep"), \
             patch("client.urlopen", side_effect=[TimeoutError("temporary"), response]) as open_request:
            self.assertEqual(Client("http://example.test/v1").get("case"), {"ok": True})
            self.assertEqual(open_request.call_count, 2)
        with patch.dict(os.environ, environment, clear=True), patch("client.urlopen", side_effect=TimeoutError("temporary")) as open_request:
            with self.assertRaises(RITTransportError):
                Client("http://example.test/v1").request("POST", "orders")
            self.assertEqual(open_request.call_count, 1)

    def test_exhausted_get_timeout_is_a_recoverable_read_error(self):
        """Classify exhausted read retries so watched loops can continue safely."""

        environment = {"RIT_API_MODE": "dma", "RIT_USERNAME": "test", "RIT_PASSWORD": "test"}
        with patch.dict(os.environ, environment, clear=True), patch("client.time.sleep"), \
             patch("client.urlopen", side_effect=TimeoutError("temporary")) as open_request:
            with self.assertRaises(RITReadError):
                Client("http://example.test/v1").get("case")
            self.assertEqual(open_request.call_count, 3)

    def test_incoherent_snapshot_is_a_recoverable_read_error(self):
        """Allow a watch loop to retry a case transition without placing an order."""

        environment = {"RIT_API_MODE": "dma", "RIT_USERNAME": "test", "RIT_PASSWORD": "test"}
        responses = [
            {"tick": 10, "period": 1, "status": "ACTIVE"},
            [],
            [],
            {"tick": 13, "period": 1, "status": "ACTIVE"},
        ]
        with patch.dict(os.environ, environment, clear=True), patch.object(Client, "get", side_effect=responses):
            with self.assertRaises(RITReadError):
                Client("http://example.test/v1").snapshot("volatility")

    def test_open_orders_pause_then_resume_with_confirmed_inventory(self) -> None:
        """Click orders pause the worker and their fills are hedged on resumption."""

        snapshot = demo("volatility")
        snapshot["orders"] = [{"order_id": 1}]
        bot = Bot(None, case="volatility", sigma=.25)
        self.assertIn("wait", bot.step(snapshot))
        snapshot["orders"] = []
        snapshot["securities"][0]["position"] = 4000
        self.assertEqual(bot.step(snapshot)["quantity"], -4000)

    def test_external_fill_invalidates_queued_straddle_leg(self) -> None:
        """Do not complete an old pair after a user flattens the account."""

        from volatility.strategy import DesiredTrade
        snapshot = demo("volatility")
        bot = Bot(None, case="volatility", sigma=.25)
        bot.expected_volatility_positions = bot.position_map(snapshot)
        bot.expected_volatility_positions["RTM50C"] = 10
        bot.pending_volatility_trades = [DesiredTrade("RTM50P", 10, "ATM volatility straddle")]
        result = bot.step(snapshot)
        self.assertIn("wait", result)
        self.assertEqual(bot.pending_volatility_trades, [])

    def test_click_order_during_preflight_prevents_submission(self) -> None:
        """A newly opened UI order invalidates a previously planned bot order."""

        snapshot = demo("volatility")
        snapshot["securities"][0]["position"] = 4000
        fresh = copy.deepcopy(snapshot)
        fresh["orders"] = [{"order_id": 99}]
        client = MagicMock()
        client.snapshot.return_value = fresh
        executor = MagicMock()
        bot = Bot(client, executor, case="volatility", sigma=.25)
        self.assertIn("wait", bot.step(snapshot))
        executor.order.assert_not_called()

    def test_risk_rejection_discards_legs_and_next_cycle_hedges(self) -> None:
        """Reject an unsafe candidate without execution, then manage fresh exposure."""

        from volatility.strategy import DesiredTrade
        snapshot = demo("volatility")
        client, executor = MagicMock(), MagicMock()
        bot = Bot(client, executor, case="volatility", sigma=.25)
        bot.pending_volatility_trades = [DesiredTrade("RTM50C", 100, "ATM volatility straddle"),
                                         DesiredTrade("RTM50P", 100, "ATM volatility straddle")]
        with patch("bot.risk.check", side_effect=RiskError("Projected delta exceeds internal 6000-share band")):
            result = bot.step(snapshot)
        self.assertFalse(result["risk_rejection"]["submitted"])
        self.assertEqual(bot.pending_volatility_trades, [])
        executor.order.assert_not_called()
        snapshot["securities"][0]["position"] = 4000
        client.snapshot.return_value = snapshot
        client.get.return_value = snapshot["case"]
        self.assertEqual(bot.step(snapshot)["quantity"], -4000)
        executor.order.assert_called_once_with("RTM", -4000)

    def test_real_delta_gate_returns_wait(self) -> None:
        """Exercise the actual 6000-share gate with an oversized deep-ITM leg."""

        from volatility.strategy import DesiredTrade
        snapshot = demo("volatility")
        executor = MagicMock()
        bot = Bot(MagicMock(), executor, case="volatility", sigma=.5)
        bot.pending_volatility_trades = [DesiredTrade("RTM48C", 100, "ATM volatility straddle")]
        result = bot.step(snapshot)
        self.assertIn("6000-share", result["risk_rejection"]["reason"])
        executor.order.assert_not_called()

    def test_expired_submission_snapshot_returns_wait(self) -> None:
        """A last-moment market reset is handled before order submission."""

        snapshot = demo("volatility")
        snapshot["securities"][0]["position"] = 4000
        client, executor = MagicMock(), MagicMock()
        client.snapshot.return_value = snapshot
        client.get.return_value = {"status": "STOPPED", "tick": 0, "period": 1}
        result = Bot(client, executor, case="volatility", sigma=.25).step(snapshot)
        self.assertIn("snapshot expired", result["wait"])
        executor.order.assert_not_called()

    def test_hedge_preserves_and_completes_second_leg(self) -> None:
        """Complete the queued put before hedging a one-leg call fill."""

        from volatility.strategy import DesiredTrade
        exchange = Exchange("volatility")
        exchange.move("RTM50C", 69)
        with tempfile.TemporaryDirectory() as directory:
            executor = Executor(exchange, Path(directory) / "journal.jsonl")
            bot = Bot(exchange, executor, case="volatility", sigma=.4)
            bot.pending_volatility_trades = [DesiredTrade("RTM50P", 69, "ATM volatility straddle")]
            try:
                self.assertEqual(bot.step(exchange.snapshot())["ticker"], "RTM50P")
                self.assertEqual(next(r["position"] for r in exchange.state["securities"]
                                      if r["ticker"] == "RTM50P"), 69)
                self.assertEqual(bot.pending_volatility_trades, [])
            finally:
                executor.close()

    def test_safety_hedge_preserves_and_completes_second_leg(self) -> None:
        """A 6,000-delta safety hedge may interrupt the pair without dropping the put."""

        from volatility.strategy import DesiredTrade
        exchange = Exchange("volatility")
        exchange.move("RTM50C", 69)
        exchange.move("RTM", 4000)
        with tempfile.TemporaryDirectory() as directory:
            executor = Executor(exchange, Path(directory) / "journal.jsonl")
            bot = Bot(exchange, executor, case="volatility", sigma=.4)
            bot.pending_volatility_trades = [DesiredTrade("RTM50P", 69, "ATM volatility straddle")]
            try:
                self.assertEqual(bot.step(exchange.snapshot())["ticker"], "RTM")
                self.assertEqual(bot.pending_volatility_trades[0].symbol, "RTM50P")
                self.assertEqual(bot.step(exchange.snapshot())["ticker"], "RTM50P")
                self.assertEqual(next(r["position"] for r in exchange.state["securities"]
                                      if r["ticker"] == "RTM50P"), 69)
            finally:
                executor.close()

    def test_invalid_pending_leg_unwinds_first_leg(self) -> None:
        """An obsolete pair request must not leave a lone option held indefinitely."""

        from volatility.strategy import DesiredTrade
        snapshot = demo("volatility")
        next(r for r in snapshot["securities"] if r["ticker"] == "RTM50C")["position"] = 10
        bot = Bot(None, case="volatility", sigma=.1)
        bot.pending_volatility_trades = [DesiredTrade("RTM50P", 10, "ATM volatility straddle")]
        result = bot.step(snapshot)
        self.assertEqual((result["ticker"], result["quantity"]), ("RTM50C", -10))

    def test_second_risk_gate_can_reject_without_crashing(self) -> None:
        """Fresh server limits may invalidate a candidate before any mutation."""

        snapshot = demo("volatility")
        snapshot["securities"][0]["position"] = 4000
        client, executor = MagicMock(), MagicMock()
        client.snapshot.return_value = snapshot
        bot = Bot(client, executor, case="volatility", sigma=.25)
        with patch("bot.risk.check", side_effect=[None, RiskError("Projected server position limit breach")]):
            self.assertIn("risk_rejection", bot.step(snapshot))
        executor.order.assert_not_called()

    def test_uncertain_execution_still_propagates(self) -> None:
        """Never turn an ambiguous submitted order into a retryable no-trade result."""

        snapshot = demo("volatility")
        snapshot["securities"][0]["position"] = 4000
        client, executor = MagicMock(), MagicMock()
        client.snapshot.return_value = snapshot
        client.get.return_value = snapshot["case"]
        executor.order.side_effect = TimeoutError("Unknown exchange outcome")
        with self.assertRaises(TimeoutError):
            Bot(client, executor, case="volatility", sigma=.25).step(snapshot)
        executor.order.assert_called_once()

    def test_projected_server_limit_uses_inverse_units(self):
        snapshot = demo("etf")
        snapshot["limits"][0]["gross_limit"] = 150
        with self.assertRaises(RiskError):
            check(snapshot, "RITC", 100, "etf", gross_limit=1000, net_limit=1000)

    def test_missing_limits_fail_closed(self):
        snapshot = demo("etf")
        del snapshot["limits"]
        with self.assertRaises(RiskError):
            check(snapshot, "BULL", 1, "etf", gross_limit=1000, net_limit=1000)

    def test_unknown_news_never_opens_trade(self):
        snapshot = demo("volatility")
        snapshot["news"] = [{"tick": 0, "body": "Volatility could rise substantially", "news_id": 1}]
        self.assertIn("wait", Bot(None, case="volatility", sigma=.25).step(snapshot))

    def test_actual_news_formats_and_week_boundary(self):
        news = [
            {"news_id": 1, "tick": 1, "ticker": "Week 1", "body": "The current risk free rate is 0%. The current annualized realized volatility is 29%."},
            {"news_id": 2, "tick": 36, "body": "The realized volatility of RTM next week will be between 16% and 21%"},
            {"news_id": 3, "tick": 75, "ticker": "Week 2", "body": "The realized volatility of RTM this week will be 18%"},
        ]
        self.assertAlmostEqual(forecast(news, 1)["sigma"], math.sqrt((74 * .29 ** 2 + 225 * .20 ** 2) / 299))
        self.assertAlmostEqual(forecast(news, 75)["sigma"], math.sqrt((75 * .18 ** 2 + 150 * .20 ** 2) / 225))
        self.assertLess(forecast(news, 36)["sigma"], .29)
        self.assertIsNone(forecast([], 1)["sigma"])

    def test_initial_null_news_fields(self):
        self.assertEqual(forecast([{"body": None, "headline": None}], 1, .2)["sigma"], .2)

    def test_order_timeout_is_not_retried_and_blocks_restart(self):
        exchange = Exchange("etf")
        exchange.fail = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.jsonl"
            executor = Executor(exchange, path)
            try:
                with self.assertRaises(TimeoutError):
                    executor.order("BULL", 1)
            finally:
                executor.close()
            self.assertEqual(len(exchange.requests), 1)
            with self.assertRaises(RuntimeError):
                Executor(exchange, path)

    def test_partial_fill_requires_explicit_reconciliation(self):
        exchange = Exchange("etf")
        exchange.partial = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.jsonl"
            executor = Executor(exchange, path)
            try:
                with self.assertRaises(RuntimeError):
                    executor.order("BULL", 10)
            finally:
                executor.close()
            self.assertEqual(exchange.state["securities"][0]["position"], 5)
            Executor.reconcile(path, exchange.state)
            Executor(exchange, path).close()

    def test_full_round_enters_hedges_exits_flat(self):
        exchange = Exchange("volatility")
        exchange.state["news"] = [{"news_id": 1, "tick": 0,
                                    "body": "The current annualized realized volatility is 25%."}]
        with tempfile.TemporaryDirectory() as directory:
            executor = Executor(exchange, Path(directory) / "journal.jsonl")
            bot = Bot(exchange, executor, case="volatility", sigma=.25)
            reasons = []
            try:
                for tick in range(300):
                    exchange.state["case"]["tick"] = tick
                    result = bot.step(exchange.snapshot())
                    reasons.append(result.get("reason"))
            finally:
                executor.close()
            self.assertIn("volatility mispricing", reasons)
            self.assertTrue(any(reason in {"straddle convergence exit", "straddle take-profit exit",
                                           "expiry inventory reduction"} for reason in reasons))
            self.assertTrue(all(s["position"] == 0 for s in exchange.state["securities"]))

    def test_profitable_tender_is_unwound_in_children(self):
        exchange = Exchange("etf")
        exchange.state["tenders"] = [{"tender_id": 1, "ticker": "RITC", "action": "BUY",
                                      "is_fixed_bid": True, "price": 20, "quantity": 2000, "expires": 30}]
        with tempfile.TemporaryDirectory() as directory:
            executor = Executor(exchange, Path(directory) / "journal.jsonl")
            bot = Bot(exchange, executor, case="etf", gross_limit=300000, net_limit=200000)
            try:
                self.assertEqual(bot.step(exchange.snapshot())["tender_id"], 1)
                self.assertEqual(bot.step(exchange.snapshot())["quantity"], -2000)
                fx = bot.step(exchange.snapshot())
                self.assertEqual(fx["ticker"], "USD")
                self.assertLess(fx["quantity"], 0)
                self.assertTrue(all(row["position"] == 0 for row in exchange.state["securities"]))
            finally:
                executor.close()

    def test_large_profitable_tender_uses_server_risk_and_fast_unwind_children(self):
        """Permit a full legal tender and reduce it in 10,000-share children."""

        exchange = Exchange("etf")
        exchange.state["tenders"] = [{"tender_id": 2, "ticker": "RITC", "action": "BUY",
                                      "is_fixed_bid": True, "price": 20, "quantity": 78000, "expires": 30}]
        with tempfile.TemporaryDirectory() as directory:
            executor = Executor(exchange, Path(directory) / "journal.jsonl")
            bot = Bot(exchange, executor, case="etf", gross_limit=300000, net_limit=200000)
            try:
                snapshot = exchange.snapshot()
                assessment = bot.tender_assessments(snapshot, bot.position_map(snapshot),
                                                    etf.analyze(snapshot, 1000, 300000, 200000))[0]
                self.assertEqual(assessment["decision"], "ACCEPT")
                self.assertIn("clears buffer", assessment["reason"])
                self.assertEqual(bot.step(exchange.snapshot())["tender_id"], 2)
                manual = bot.step(exchange.snapshot())
                self.assertEqual(manual["manual_converter"]["converter"], "ETF-Redemption")
                alert = format_manual_converter_alert(manual["manual_converter"])
                self.assertIn("MANUAL UNWIND REQUIRED", alert)
                self.assertIn("PAUSE AUTOMATED UNWIND", alert)
                # If converter economics deteriorate, direct liquidation still
                # uses the full legal child rather than the basket entry size.
                for ticker in ("BULL", "BEAR"):
                    exchange.state["books"][ticker]["bids"] = [{"price": 1, "quantity": 1000000}]
                self.assertEqual(bot.step(exchange.snapshot())["quantity"], -10000)
            finally:
                executor.close()

    def test_tender_assessment_names_a_negative_unwind_rejection(self):
        """Operators receive the binding tender rejection rather than a silent wait."""

        snapshot = demo("etf")
        snapshot["tenders"] = [{"tender_id": 3, "ticker": "RITC", "action": "BUY",
                                 "is_fixed_bid": True, "price": 30, "quantity": 1000, "expires": 20}]
        bot = Bot(None, case="etf", gross_limit=300000, net_limit=200000)
        assessment = bot.tender_assessments(snapshot, bot.position_map(snapshot),
                                            etf.analyze(snapshot, 1000, 300000, 200000))[0]
        self.assertEqual(assessment["decision"], "REJECT")
        self.assertIn("unwind edge", assessment["reason"])

    def test_tender_near_offer_expiry_remains_eligible(self):
        """Offer expiry is an acceptance deadline, not a liquidation deadline."""

        snapshot = demo("etf")
        snapshot["case"]["tick"] = 28
        snapshot["tenders"] = [{"tender_id": 6, "ticker": "RITC", "action": "BUY",
                                 "is_fixed_bid": True, "price": 20, "quantity": 1000, "expires": 30}]
        bot = Bot(None, case="etf", gross_limit=300000, net_limit=200000)
        assessment = bot.tender_assessments(snapshot, bot.position_map(snapshot),
                                            etf.analyze(snapshot, 1000, 300000, 200000))[0]
        self.assertEqual(assessment["decision"], "ACCEPT")

    def test_tender_assessment_reserves_time_for_full_liquidation(self):
        """Reject a large tender when its legal child sequence cannot finish."""

        snapshot = demo("etf")
        next(row for row in snapshot["limits"] if row["name"] == "cash").update(
            gross_limit=10_000_000, net_limit=10_000_000)
        snapshot["case"]["tick"] = 261
        snapshot["tenders"] = [{"tender_id": 4, "ticker": "RITC", "action": "BUY",
                                 "is_fixed_bid": True, "price": 20, "quantity": 100000, "expires": 290}]
        bot = Bot(None, case="etf", gross_limit=300000, net_limit=200000)
        assessment = bot.tender_assessments(snapshot, bot.position_map(snapshot),
                                            etf.analyze(snapshot, 1000, 300000, 200000))[0]
        self.assertEqual(assessment["decision"], "REJECT")
        self.assertIn("insufficient time", assessment["reason"])

    def test_tender_is_repriced_from_a_fresh_snapshot_before_acceptance(self):
        """A stale profitable offer cannot survive a changed fresh preflight."""

        exchange = Exchange("etf")
        exchange.state["tenders"] = [{"tender_id": 5, "ticker": "RITC", "action": "BUY",
                                      "is_fixed_bid": True, "price": 20, "quantity": 2000, "expires": 30}]
        stale = exchange.snapshot()
        exchange.state["tenders"][0]["price"] = 30
        with tempfile.TemporaryDirectory() as directory:
            executor = Executor(exchange, Path(directory) / "journal.jsonl")
            bot = Bot(exchange, executor, case="etf", gross_limit=300000, net_limit=200000)
            try:
                result = bot.step(stale)
                self.assertIn("fresh tender rejected", result["wait"])
                self.assertEqual(result["tender_assessment"]["decision"], "REJECT")
                self.assertFalse(any(event[1].startswith("tenders/") for event in exchange.requests))
            finally:
                executor.close()

    def test_basket_failure_never_submits_remaining_legs(self):
        exchange = Exchange("etf")
        exchange.state["books"]["USD"] = {"bids": [{"price": 1, "quantity": 1000000}], "asks": [{"price": 1, "quantity": 1000000}]}
        exchange.state["books"]["RITC"] = {"bids": [{"price": 24.39, "quantity": 1000000}],
                                            "asks": [{"price": 24.41, "quantity": 1000000}]}
        exchange.partial = True
        with tempfile.TemporaryDirectory() as directory:
            executor = Executor(exchange, Path(directory) / "journal.jsonl")
            bot = Bot(exchange, executor, case="etf", basket=True, gross_limit=300000, net_limit=200000)
            try:
                with self.assertRaises(RuntimeError):
                    bot.step(exchange.snapshot())
            finally:
                executor.close()
            self.assertEqual(len(exchange.requests), 1)


if __name__ == "__main__":
    unittest.main()
