"""Behavior tests using a tiny exchange double, never the practice accounts."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot import Bot
from client import Client, RITReadError, RITTransportError
from execution import Executor
from models.news import forecast
from risk import check, RiskError
from run import demo


class Exchange:
    """Fill market orders immediately and update inventory like the RIT account API."""
    def __init__(self, case):
        self.state = demo(case)
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
            self.move(offer["ticker"], offer["quantity"] * (1 if offer["action"] == "BUY" else -1))
            self.state["tenders"] = []
            return {"success": True}
        oid = len(self.orders) + 1
        filled = params["quantity"] // 2 if self.partial else params["quantity"]
        self.orders[oid] = {"order_id": oid, "quantity_filled": filled,
                            "status": "CANCELLED" if self.partial else "TRANSACTED"}
        self.move(params["ticker"], filled * (1 if params["action"] == "BUY" else -1))
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

    def test_open_orders_block_new_actions(self):
        snapshot = demo("volatility")
        snapshot["orders"] = [{"order_id": 1}]
        with self.assertRaises(RuntimeError):
            Bot(None, case="volatility", sigma=.25).step(snapshot)

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
        self.assertAlmostEqual(forecast(news, 1)["sigma"], .29)
        self.assertAlmostEqual(forecast(news, 75)["sigma"], .18)
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
            self.assertIn("straddle convergence exit", reasons)
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
                self.assertEqual(bot.step(exchange.snapshot())["quantity"], -1000)
                self.assertEqual(bot.step(exchange.snapshot())["quantity"], -1000)
            finally:
                executor.close()

    def test_basket_failure_never_submits_remaining_legs(self):
        exchange = Exchange("etf")
        exchange.state["books"]["USD"] = {"bids": [{"price": 1, "quantity": 1000000}], "asks": [{"price": 1, "quantity": 1000000}]}
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
