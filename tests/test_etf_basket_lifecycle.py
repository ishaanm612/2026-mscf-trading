"""Regression coverage for ETF basket lifecycle and failure boundaries."""
from __future__ import annotations

import unittest

from bot import Bot
from client import RITReadError
from models.etf_basket import BasketConfig
from models.etf_policy import ETFConfig
from tests.test_etf_accounting import ETFExchange


def stable_basket_edge(exchange: ETFExchange) -> None:
    """Install a liquid, materially positive long-RITC/short-stock entry edge."""
    exchange.state["books"]["BULL"] = {
        "bids": [{"price": 9.99, "quantity": 1_000_000}],
        "asks": [{"price": 10.01, "quantity": 1_000_000}],
    }
    exchange.state["books"]["BEAR"] = {
        "bids": [{"price": 14.99, "quantity": 1_000_000}],
        "asks": [{"price": 15.01, "quantity": 1_000_000}],
    }
    exchange.state["books"]["RITC"] = {
        "bids": [{"price": 24.49, "quantity": 1_000_000}],
        "asks": [{"price": 24.50, "quantity": 1_000_000}],
    }
    exchange.state["books"]["USD"] = {
        "bids": [{"price": 0.9999, "quantity": 5_000_000}],
        "asks": [{"price": 1.0001, "quantity": 5_000_000}],
    }


def basket_bot(exchange: ETFExchange, *, quantity: int = 1_000,
               config: BasketConfig | None = None, etf_config: ETFConfig | None = None,
               executor=None) -> Bot:
    return Bot(exchange, exchange if executor is None else executor, case="etf", basket=True,
               quantity=quantity, gross_limit=300_000, net_limit=200_000,
               basket_config=config or BasketConfig(), etf_config=etf_config)


class ReadFailureAfterFirstLegExchange(ETFExchange):
    """Inject a preflight read failure after one confirmed basket leg."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_snapshot = False
        self.after_order = self._after_order

    def _after_order(self, _exchange: ETFExchange, count: int) -> None:
        if count == 1:
            self.fail_next_snapshot = True

    def snapshot(self, *args, **kwargs):
        if self.fail_next_snapshot:
            self.fail_next_snapshot = False
            raise RITReadError("book read lost after confirmed first leg")
        return super().snapshot(*args, **kwargs)


class SessionChangesAfterFirstLegExchange(ETFExchange):
    """Switch to a new/inactive heat after the first confirmed leg."""

    def __init__(self) -> None:
        super().__init__()
        self.after_order = self._after_order

    def _after_order(self, _exchange: ETFExchange, count: int) -> None:
        if count == 1:
            self.state["case"].update(status="INACTIVE", period=2, tick=0)


class AmbiguousSecondLegExecutor:
    """Model a timeout after submission of basket leg two."""

    def __init__(self, exchange: ETFExchange) -> None:
        self.exchange = exchange
        self.calls = 0
        self.unresolved_intent = False

    def order(self, ticker: str, quantity: int) -> dict:
        self.calls += 1
        if self.calls == 2:
            self.unresolved_intent = True
            raise TimeoutError("second basket leg outcome is unknown")
        return self.exchange.order(ticker, quantity)


class ETFBasketLifecycleTests(unittest.TestCase):
    def test_manual_preparation_defers_another_tender(self) -> None:
        exchange = ETFExchange()
        exchange.state["books"]["RITC"]["asks"][0]["price"] = 26
        exchange.state["tenders"] = [{"tender_id": 92, "ticker": "RITC", "action": "SELL",
                                       "is_fixed_bid": True, "quantity": 10_000,
                                       "price": 25.5, "expires": 30}]
        bot = basket_bot(exchange)
        accepted = bot.step(exchange.snapshot())
        self.assertEqual(accepted["tender_assessment"]["selected_route"]["name"], "ETF-Creation")
        exchange.state["tenders"] = [{"tender_id": 93, "ticker": "RITC", "action": "BUY",
                                       "is_fixed_bid": True, "quantity": 1_000,
                                       "price": 1.0, "expires": 30}]
        preparing = bot.step(exchange.snapshot())
        self.assertEqual(preparing["reason"], "prepare manual ETF creation")
        self.assertNotIn("tender_id", preparing)
        self.assertEqual(preparing["tender_assessments"][0]["decision"], "DEFER")

    def test_underwater_held_basket_never_adds_a_second_slice(self) -> None:
        exchange = ETFExchange()
        stable_basket_edge(exchange)
        bot = basket_bot(exchange, quantity=2_000,
                         config=BasketConfig(max_hold_ticks=20),
                         etf_config=ETFConfig(child_size=1_000))
        self.assertEqual(bot.step(exchange.snapshot())["reason"], "basket filled")
        self.assertEqual(exchange.positions()["RITC"], 1_000)

        # RITC becomes cheaper to enter again, so raw entry edge is still
        # positive, but the first slice has a clearly negative close-now P&L.
        exchange.state["books"]["RITC"] = {
            "bids": [{"price": 23.90, "quantity": 1_000_000}],
            "asks": [{"price": 24.00, "quantity": 1_000_000}],
        }
        bot.step(exchange.snapshot())

        self.assertLessEqual(exchange.positions()["RITC"], 1_000)
        self.assertEqual([row["quantity"] for row in exchange.orders
                          if row["ticker"] == "RITC" and row["quantity"] > 0], [1_000])

    def test_max_holding_age_forces_inventory_reduction(self) -> None:
        exchange = ETFExchange()
        stable_basket_edge(exchange)
        bot = basket_bot(exchange, config=BasketConfig(max_hold_ticks=10))
        bot.step(exchange.snapshot())
        exchange.state["case"]["tick"] = 11

        action = bot.step(exchange.snapshot())

        self.assertIn(action["ticker"], {"BULL", "BEAR", "RITC"})
        self.assertLess(abs(exchange.positions()[action["ticker"]]), 1_000)
        self.assertIn("maximum basket holding", bot.etf_reduction_reason)

    def test_dynamic_time_budget_allows_tick_260_but_rejects_tick_285(self) -> None:
        config = BasketConfig(max_hold_ticks=10)
        early = ETFExchange()
        stable_basket_edge(early)
        early.state["case"]["tick"] = 260
        self.assertEqual(basket_bot(early, config=config).step(early.snapshot())["reason"], "basket filled")

        late = ETFExchange()
        stable_basket_edge(late)
        late.state["case"]["tick"] = 285
        result = basket_bot(late, config=config).step(late.snapshot())
        self.assertNotEqual(result.get("reason"), "basket filled")
        self.assertEqual(late.orders, [])

    def test_confirmed_partial_leg_read_failure_latches_reduction_and_bypasses_tender(self) -> None:
        exchange = ReadFailureAfterFirstLegExchange()
        stable_basket_edge(exchange)
        bot = basket_bot(exchange)

        with self.assertRaises(RITReadError):
            bot.step(exchange.snapshot())
        self.assertEqual(exchange.positions()["BULL"], -1_000)
        self.assertIn("partial basket", bot.etf_reduction_reason)

        exchange.state["tenders"] = [{"tender_id": 91, "ticker": "RITC", "action": "BUY",
                                       "is_fixed_bid": True, "quantity": 1_000,
                                       "price": 20.0, "expires": 100}]
        action = bot.step(exchange.snapshot())

        self.assertEqual((action["ticker"], action["quantity"]), ("BULL", 1_000))
        self.assertNotIn("tender_id", action)
        self.assertNotIn("manual_converter", action)

    def test_session_change_after_first_leg_submits_neither_remaining_leg_nor_abort(self) -> None:
        exchange = SessionChangesAfterFirstLegExchange()
        stable_basket_edge(exchange)
        bot = basket_bot(exchange)

        result = bot.step(exchange.snapshot())

        self.assertIn("wait", result)
        self.assertEqual([(row["ticker"], row["quantity"]) for row in exchange.orders], [("BULL", -1_000)])

    def test_ambiguous_second_leg_never_submits_the_third_leg(self) -> None:
        exchange = ETFExchange()
        stable_basket_edge(exchange)
        executor = AmbiguousSecondLegExecutor(exchange)
        bot = basket_bot(exchange, executor=executor)

        with self.assertRaises(TimeoutError):
            bot.step(exchange.snapshot())

        self.assertTrue(executor.unresolved_intent)
        self.assertEqual(executor.calls, 2)
        self.assertEqual([(row["ticker"], row["quantity"]) for row in exchange.orders], [("BULL", -1_000)])


if __name__ == "__main__":
    unittest.main()
