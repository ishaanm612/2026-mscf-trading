"""Adversarial ETF basket lifecycle checks across successive snapshots."""
from __future__ import annotations

import math
import unittest

from models.etf_basket import BasketConfig
from models.etf_policy import ETFConfig
from tests.test_etf_basket_lifecycle import basket_bot, stable_basket_edge
from tests.test_etf_accounting import ETFExchange


class ETFBasketEdgeTests(unittest.TestCase):
    def test_stale_account_snapshot_blocks_etf_submit_before_order(self) -> None:
        exchange = ETFExchange()
        bot = basket_bot(exchange)
        outer = exchange.snapshot()
        exchange.move("BULL", 1)  # Simulate a click trade after strategy valuation.

        action = bot.submit(outer, "BULL", -1_000, "test stale ETF account")

        self.assertIn("account changed before submission", action["wait"])
        self.assertEqual(exchange.orders, [])

    def test_new_open_order_blocks_etf_submit_before_order(self) -> None:
        exchange = ETFExchange()
        bot = basket_bot(exchange)
        outer = exchange.snapshot()
        exchange.state["orders"] = [{"order_id": 123, "ticker": "BULL", "status": "OPEN"}]

        action = bot.submit(outer, "BULL", -1_000, "test open ETF order")

        self.assertIn("account changed before submission", action["wait"])
        self.assertEqual(exchange.orders, [])

    def test_profit_exit_latches_even_if_later_quotes_reverse(self) -> None:
        exchange = ETFExchange()
        stable_basket_edge(exchange)
        bot = basket_bot(exchange, config=BasketConfig(min_hold_ticks=10))
        self.assertEqual(bot.step(exchange.snapshot())["reason"], "basket filled")

        # Make every close leg favorable enough to clear the reserve.
        exchange.state["books"]["RITC"] = {"bids": [{"price": 24.79, "quantity": 1_000_000}],
                                           "asks": [{"price": 24.81, "quantity": 1_000_000}]}
        first = bot.step(exchange.snapshot())
        self.assertEqual(first["exit_reason"], "executable profit clears take-profit and exit reserve")
        self.assertEqual((first["ticker"], first["quantity"]), ("RITC", -1_000))

        # A later price reversal must not revive the convergence position.
        exchange.state["books"]["BULL"]["asks"][0]["price"] = 15.00
        second = bot.step(exchange.snapshot())
        self.assertEqual((second["ticker"], second["quantity"]), ("BULL", 1_000))
        self.assertEqual(second["exit_reason"], first["exit_reason"])

    def test_external_usd_change_invalidates_basket_basis_and_latches_exit(self) -> None:
        exchange = ETFExchange()
        stable_basket_edge(exchange)
        bot = basket_bot(exchange)
        self.assertEqual(bot.step(exchange.snapshot())["reason"], "basket filled")

        exchange.move("USD", 1)
        action = bot.step(exchange.snapshot())

        self.assertEqual(action["exit_reason"], "basket inventory changed outside recorded fills")
        self.assertTrue(action["inventory_reduction_required"])
        self.assertEqual((action["ticker"], action["quantity"]), ("RITC", -1_000))

    def test_basket_target_is_capped_before_any_order_is_submitted(self) -> None:
        exchange = ETFExchange()
        stable_basket_edge(exchange)
        bot = basket_bot(exchange, quantity=20_000, config=BasketConfig(max_quantity=1_000))

        action = bot.step(exchange.snapshot())

        self.assertEqual(action["target_quantity"], 1_000)
        self.assertEqual(action["filled_quantity"], 1_000)
        self.assertEqual(max(abs(order["quantity"]) for order in exchange.orders), 1_000)

    def test_second_slice_preserves_original_basis_and_usd_expectation(self) -> None:
        exchange = ETFExchange()
        stable_basket_edge(exchange)
        bot = basket_bot(exchange, quantity=2_000, config=BasketConfig(),
                         etf_config=ETFConfig(child_size=1_000, execution_k=0, fx_k=0))
        self.assertEqual(bot.step(exchange.snapshot())["filled_quantity"], 1_000)
        original_tick = bot.held_basket["entry_tick"]

        # A small positive close is insufficient to take profit but permits a
        # second, independently qualified slice. Zero reserve coefficients
        # isolate state accounting from the deliberately changing quotes.
        for ticker, bid, ask in (("BULL", 9.91, 9.92), ("BEAR", 14.91, 14.92),
                                 ("RITC", 24.50, 24.51)):
            exchange.state["books"][ticker] = {"bids": [{"price": bid, "quantity": 1_000_000}],
                                                 "asks": [{"price": ask, "quantity": 1_000_000}]}
        exchange.state["case"]["tick"] = 2
        action = bot.step(exchange.snapshot())

        self.assertEqual(action["filled_quantity"], 2_000)
        self.assertEqual(bot.held_basket["entry_tick"], original_tick)
        self.assertEqual(len(bot.held_basket["entry_fills"]), 6)
        self.assertTrue(math.isclose(bot.held_basket["expected_usd"],
                                     exchange.positions()["USD"], abs_tol=1e-9))

    def test_second_leg_account_drift_latches_partial_reduction(self) -> None:
        class DriftAfterFirstLeg(ETFExchange):
            def __init__(self) -> None:
                super().__init__()
                self.after_order = self._drift

            def _drift(self, exchange: ETFExchange, count: int) -> None:
                if count == 1:
                    exchange.move("USD", 1)

        exchange = DriftAfterFirstLeg()
        stable_basket_edge(exchange)
        bot = basket_bot(exchange)

        with self.assertRaises(RuntimeError):
            bot.step(exchange.snapshot())

        self.assertEqual([(row["ticker"], row["quantity"]) for row in exchange.orders], [("BULL", -1_000)])
        self.assertIn("partial basket", bot.etf_reduction_reason)


if __name__ == "__main__":
    unittest.main()
