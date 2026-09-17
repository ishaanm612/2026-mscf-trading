"""Staged forecasts must have evidence, a stress exit, and bounded execution."""
import unittest
from unittest.mock import patch

from bot import Bot
from models import etf_policy
from models.etf_liquidity import LiquidityHistory
from run import format_etf_tender_alert
from scripts.supervise_volatility import parse_args, worker_command
from tests.test_etf_accounting import ETFExchange, etf_snapshot
from tests.test_etf_execution_policy import offer


def book(snapshot, tick, *, fresh=True):
    snapshot["case"]["tick"] = tick
    snapshot["books"]["RITC"] = {
        "bids": [{"price": 24.90, "quantity": 10000, "order_id": tick if fresh else 1, "tick": tick if fresh else 1},
                 {"price": 24.50, "quantity": 20000, "order_id": 0, "tick": 1}],
        "asks": [{"price": 24.92, "quantity": 30000, "order_id": 1000 + tick, "tick": tick}]}


def warmed(exchange=None):
    ex = exchange or ETFExchange()
    bot = Bot(ex, ex, case="etf", gross_limit=300000, net_limit=200000,
              etf_config=etf_policy.ETFConfig(manual_wait_ticks=0))
    for tick in (1, 4, 7, 10, 13):
        book(ex.state, tick)
        bot.step(ex.snapshot())
    ex.state["tenders"] = [offer(30000, 24.70)]
    return ex, bot


class StagedTenderTests(unittest.TestCase):
    def test_arrivals_not_repeated_depth_and_reset_or_stale_history(self):
        history = LiquidityHistory()
        s = etf_snapshot()
        for tick in (1, 4, 7, 10, 13):
            book(s, tick, fresh=False)
            history.observe(s)
            history.observe(s)
        self.assertIsNone(history.estimate(s, "bids"))
        self.assertEqual(len(history.intervals["bids"]), 4)
        for tick in (16, 19, 22, 25, 28, 31, 34, 37, 40, 43):
            book(s, tick)
            history.observe(s)
        self.assertIsNotNone(history.estimate(s, "bids"))
        s["case"]["tick"] += 3
        self.assertIsNone(history.estimate(s, "bids"))
        book(s, 1)
        history.observe(s)
        self.assertIsNone(history.estimate(s, "bids"))

    def test_default_gate_uses_two_nonzero_intervals_not_zero_filled_quantile(self):
        history = LiquidityHistory()
        s = etf_snapshot()
        # Two separated arrivals amid empty intervals are evidence of some
        # replenishment; the old lower quartile collapsed this to zero.
        for tick, fresh in ((1, False), (4, True), (7, False), (10, True), (13, False), (16, False)):
            book(s, tick, fresh=fresh)
            history.observe(s)
        estimate = history.estimate(s, "bids")
        self.assertIsNotNone(estimate)
        self.assertEqual(estimate["active_intervals"], 2)
        self.assertEqual(estimate["participation"], .5)

    def test_cold_history_does_not_manufacture_liquidity(self):
        ex, bot = warmed()
        bot.etf_liquidity = LiquidityHistory()
        a = bot.tender_assessments(ex.snapshot(), ex.positions(), {})[0]
        self.assertEqual(a["decision"], "REJECT")
        self.assertIn("evidence", a["staged_unavailable"])

    def test_profitable_staging_accepts_with_negative_but_bounded_fallback(self):
        ex, bot = warmed()
        a = bot.tender_assessments(ex.snapshot(), ex.positions(), {})[0]
        self.assertEqual(a["decision"], "ACCEPT")
        route = a["selected_route"]
        self.assertEqual(route["name"], "DIRECT_STAGED")
        self.assertGreater(route["profit_cad"], 0)
        self.assertLess(route["staging"]["fallback_profit_cad"], 0)
        self.assertEqual(route["staging"]["spacing_ticks"], 6)
        self.assertEqual([f["fill_tick_offset"] for f in route["fills"]], [3, 9, 15])
        self.assertEqual([f["fill_tick_offset"] for f in route["reserve"]["children"]], [3, 9, 15])
        self.assertEqual([f["price"] for f in route["fallback_route"]["fills"]], [24.9, 24.5, 24.5])
        self.assertIn("replenishment is NOT guaranteed", format_etf_tender_alert(a))

    def test_sell_tender_uses_ask_arrivals_and_buy_children(self):
        ex = ETFExchange()
        bot = Bot(ex, ex, case="etf", gross_limit=300000, net_limit=200000,
                  etf_config=etf_policy.ETFConfig(manual_wait_ticks=0))
        for tick in (1, 4, 7, 10, 13):
            book(ex.state, tick)
            ex.state["books"]["RITC"]["bids"] = [{"price": 24.68, "quantity": 30000}]
            ex.state["books"]["RITC"]["asks"] = [
                {"price": 24.70, "quantity": 10000, "order_id": tick + 1000, "tick": tick},
                {"price": 25.10, "quantity": 20000, "order_id": 0, "tick": 1}]
            bot.step(ex.snapshot())
        ex.state["tenders"] = [offer(30000, 24.90, "SELL")]
        self.assertEqual(bot.step(ex.snapshot())["tender_assessment"]["selected_route"]["name"], "DIRECT_STAGED")
        ex.state["tenders"] = []
        for tick in (13, 19, 25):
            ex.state["case"]["tick"] = tick
            self.assertEqual(bot.step(ex.snapshot())["quantity"], 10000)
        self.assertEqual(bot.step(ex.snapshot())["ticker"], "USD")
        self.assertTrue(ex.is_flat())
        self.assertGreater(ex.cad_cash, 0)

    def test_disable_staging_and_no_replenishment_depth_do_not_override_policy(self):
        ex, bot = warmed()
        bot.etf_config = etf_policy.ETFConfig(staged_tenders=False, manual_wait_ticks=0)
        self.assertEqual(bot.tender_assessments(ex.snapshot(), ex.positions(), {})[0]["decision"], "REJECT")
        ex, bot = warmed()
        bot.step(ex.snapshot())
        ex.state["tenders"] = []
        ex.state["books"]["RITC"]["bids"] = []
        result = bot.step(ex.snapshot())
        self.assertEqual(result["exit_reason"], "staged tender fallback depth disappeared")
        self.assertEqual(ex.orders, [])
        self.assertTrue(result["inventory_reduction_required"])

    def test_lower_child_cap_and_cash_limits_apply_to_staged_route(self):
        ex, bot = warmed()
        ex._security("RITC")["max_trade_size"] = 5000
        report = bot.tender_assessments(ex.snapshot(), ex.positions(), {})[0]
        self.assertEqual(report["selected_route"]["name"], "DIRECT_STAGED")
        self.assertTrue(all(abs(f["quantity"]) <= 5000 for f in report["selected_route"]["fills"]))
        ex.state["limits"][1]["gross_limit"] = 100000
        report = bot.tender_assessments(ex.snapshot(), ex.positions(), {})[0]
        self.assertEqual(report["decision"], "REJECT")
        self.assertIn("cash", report["reason"])

    def test_deep_loss_and_insufficient_depth_never_get_staged_override(self):
        for price, depth in ((23.5, 20000), (24.5, 10000)):
            ex, bot = warmed()
            ex.state["books"]["RITC"]["bids"][1].update(price=price, quantity=depth)
            a = bot.tender_assessments(ex.snapshot(), ex.positions(), {})[0]
            self.assertEqual(a["decision"], "REJECT")
            self.assertFalse(any(r["name"] == "DIRECT_STAGED" for r in a["routes"]))

    def test_schedule_too_slow_or_too_late_rejects(self):
        for kind in ("slow", "late"):
            ex, bot = warmed()
            if kind == "slow":
                bot.etf_config = etf_policy.ETFConfig(manual_wait_ticks=0, tender_max_unwind_ticks=5)
            else:
                ex.state["case"]["tick"] = 285
                bot.etf_liquidity.previous = (285, 1)
                ex.state["tenders"][0]["expires"] = 290
            self.assertEqual(bot.tender_assessments(ex.snapshot(), ex.positions(), {})[0]["decision"], "REJECT")

    def test_confirmed_children_are_paced_and_final_fx_is_closed(self):
        ex, bot = warmed()
        self.assertEqual(bot.step(ex.snapshot())["tender_id"], 7)
        ex.state["tenders"] = []
        for tick in (13, 19, 25):
            book(ex.state, tick)
            action = bot.step(ex.snapshot())
            self.assertEqual(action["quantity"], -10000)
            self.assertEqual(action["reason"], "staged tender unwind")
            if tick < 25:
                ex.state["case"]["tick"] += 1
                count = len(ex.orders)
                self.assertIn("replenish", bot.step(ex.snapshot())["wait"])
                self.assertEqual(len(ex.orders), count)
        self.assertEqual(bot.step(ex.snapshot())["ticker"], "USD")
        self.assertTrue(ex.is_flat())
        self.assertGreater(ex.cad_cash, 0)

    def test_missing_replenishment_exits_instead_of_waiting_forever(self):
        ex, bot = warmed()
        bot.step(ex.snapshot())
        ex.state["tenders"] = []
        bot.step(ex.snapshot())
        # Remaining stock is still liquid, but the projected 24.675 quote
        # fails. Wait one bounded slot, then liquidate at the actual book.
        ex.state["books"]["RITC"]["bids"] = [{"price": 24.6, "quantity": 20000}]
        ex.state["case"]["tick"] = 19
        self.assertIn("worse than forecast", bot.step(ex.snapshot())["wait"])
        ex.state["case"]["tick"] = 25
        action = bot.step(ex.snapshot())
        self.assertIn("did not replenish", action["exit_reason"])
        self.assertEqual(action["quantity"], -10000)
        self.assertIsNone(bot.active_etf_route)
        bot.step(ex.snapshot())
        bot.step(ex.snapshot())
        self.assertTrue(ex.is_flat())

    def test_loss_or_external_inventory_change_latches_fallback(self):
        for cause in ("loss", "inventory"):
            ex, bot = warmed()
            bot.step(ex.snapshot())
            ex.state["tenders"] = []
            if cause == "loss":
                ex.state["books"]["RITC"]["bids"] = [{"price": 23, "quantity": 30000}]
            else:
                ex.move("RITC", -1)
            action = bot.step(ex.snapshot())
            self.assertTrue(action["inventory_reduction_required"])
            self.assertIsNone(bot.active_etf_route)

    def test_fresh_preflight_price_bound_and_ambiguous_order_are_not_bypassed(self):
        ex, bot = warmed()
        bot.step(ex.snapshot())
        ex.state["tenders"] = []
        s = ex.snapshot()
        ex.state["books"]["RITC"]["bids"][0]["price"] = 24.8
        action = bot.step(s)
        self.assertIn("price bound", action["wait"])
        self.assertEqual(ex.orders, [])
        book(ex.state, 13)
        with patch.object(ex, "order", side_effect=RuntimeError("ambiguous fill")) as mutation:
            with self.assertRaisesRegex(RuntimeError, "ambiguous fill"):
                bot.step(ex.snapshot())
            self.assertEqual(mutation.call_count, 1)
        self.assertEqual(bot.active_etf_route["staging"]["completed_children"], 0)

    def test_supervisor_forwards_staging_controls(self):
        args = parse_args(["--case", "etf", "--gross-limit", "300000", "--net-limit", "200000",
                           "--no-staged-tenders", "--tender-max-fallback-loss", ".08",
                           "--tender-max-unwind-ticks", "40"])
        cmd = worker_command(args)
        self.assertIn("--no-staged-tenders", cmd)
        self.assertEqual(cmd[cmd.index("--tender-max-fallback-loss") + 1], "0.08")
        self.assertEqual(cmd[cmd.index("--tender-max-unwind-ticks") + 1], "40")

    def test_supervisor_forwards_arrival_gate_controls(self):
        args = parse_args(["--case", "etf", "--gross-limit", "300000", "--net-limit", "200000",
                           "--staged-min-active-intervals", "3", "--staged-participation", ".4"])
        cmd = worker_command(args)
        self.assertEqual(cmd[cmd.index("--staged-min-active-intervals") + 1], "3")
        self.assertEqual(cmd[cmd.index("--staged-participation") + 1], "0.4")


if __name__ == "__main__":
    unittest.main()
