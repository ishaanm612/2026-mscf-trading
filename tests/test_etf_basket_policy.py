"""Basket convergence policy regressions."""
from __future__ import annotations

import copy
import unittest

from models import etf, etf_basket, etf_policy
from tests.test_etf_accounting import etf_snapshot


class BasketPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = etf_snapshot()
        self.config = etf_basket.BasketConfig()
        self.execution = etf_policy.ETFConfig()

    def test_entry_requires_exit_cost_and_execution_reserve(self) -> None:
        strong = copy.deepcopy(self.snapshot)
        strong["books"]["RITC"]["asks"][0]["price"] = 24.40
        strong["books"]["RITC"]["bids"][0]["price"] = 24.38
        report = etf_basket.entry_report(strong, 1, 1000, self.config, self.execution)
        self.assertTrue(report["eligible"])
        self.assertGreater(report["exit_cost_cad"], 0)
        self.assertGreater(report["reserve_cad"], 0)
        # A 10-cent opening edge no longer passes when its expected closure
        # cannot pay the exit costs plus the serial uncertainty reserve.
        shallow = copy.deepcopy(self.snapshot)
        shallow["books"]["RITC"]["asks"][0]["price"] = 24.82
        rejected = etf_basket.entry_report(shallow, 1, 1000, self.config, self.execution)
        self.assertFalse(rejected["eligible"])

    def test_serial_completion_can_beat_abort_with_negative_conditional_value(self) -> None:
        fills = [etf.executable_trade_cashflow(self.snapshot, "BULL", -1000)]
        self.snapshot["books"]["RITC"]["asks"][0]["price"] += .03
        report = etf_basket.serial_report(self.snapshot, fills,
                                          [("BEAR", -1000), ("RITC", 1000)],
                                          self.config, self.execution)
        self.assertTrue(report["finish"])
        self.assertLess(report["conditional_profit_cad"], 0)
        self.assertGreater(report["conditional_profit_cad"], report["abort_pnl_cad"])

    def test_serial_rejects_sharply_bad_conditional_value(self) -> None:
        fills = [etf.executable_trade_cashflow(self.snapshot, "BULL", 1000)]
        report = etf_basket.serial_report(self.snapshot, fills,
                                          [("BEAR", 1000), ("RITC", -1000)],
                                          self.config, self.execution)
        self.assertFalse(report["finish"])
        self.assertLess(report["conditional_profit_cad"], report["abort_pnl_cad"])

    def test_partial_inventory_reserve_increases_with_time_and_volatility(self) -> None:
        filled = [etf.executable_trade_cashflow(self.snapshot, "BULL", -1000)]
        short = etf_basket._partial_inventory_reserve(
            self.snapshot, filled, 3, self.execution, {"BULL": .01})
        long = etf_basket._partial_inventory_reserve(
            self.snapshot, filled, 9, self.execution, {"BULL": .01})
        volatile = etf_basket._partial_inventory_reserve(
            self.snapshot, filled, 9, self.execution, {"BULL": .02})
        self.assertGreater(long["reserve_cad"], short["reserve_cad"])
        self.assertGreater(volatile["reserve_cad"], long["reserve_cad"])
        self.assertEqual(long["children"][0]["ticker"], "BULL")

    def test_holding_exits_on_stop_before_minimum_age(self) -> None:
        entry = [etf.executable_trade_cashflow(self.snapshot, ticker, quantity)
                 for ticker, quantity in (("BULL", -1000), ("BEAR", -1000), ("RITC", 1000))]
        held = {"entry_fills": entry, "entry_tick": self.snapshot["case"]["tick"]}
        for ticker in ("BULL", "BEAR"):
            self.snapshot["books"][ticker]["asks"][0]["price"] += 1.0
            self.snapshot["books"][ticker]["bids"][0]["price"] += 1.0
        report = etf_basket.holding_report(self.snapshot, held, self.config, self.execution)
        self.assertEqual(report["action"], "EXIT")
        self.assertEqual(report["reason"], "stop loss reached")

    def test_holding_exits_at_maximum_age(self) -> None:
        entry = [etf.executable_trade_cashflow(self.snapshot, ticker, quantity)
                 for ticker, quantity in (("BULL", -1000), ("BEAR", -1000), ("RITC", 1000))]
        held = {"entry_fills": entry, "entry_tick": 1}
        self.snapshot["case"]["tick"] = 61
        report = etf_basket.holding_report(self.snapshot, held, self.config, self.execution)
        self.assertEqual(report["action"], "EXIT")
        self.assertEqual(report["reason"], "maximum basket holding time reached")

    def test_gap_reversal_exits_even_without_a_profitable_close(self) -> None:
        entry = [etf.executable_trade_cashflow(self.snapshot, ticker, quantity)
                 for ticker, quantity in (("BULL", -1000), ("BEAR", -1000), ("RITC", 1000))]
        held = {"entry_fills": entry, "entry_tick": 1}
        # FX moves the parity relation past convergence while the naturally
        # offsetting RITC/USD position still costs money to close.
        self.snapshot["books"]["USD"] = {"bids": [{"price": 1.02, "quantity": 1000000}],
                                           "asks": [{"price": 1.021, "quantity": 1000000}]}
        report = etf_basket.holding_report(self.snapshot, held, self.config, self.execution)
        self.assertLess(report["close_pnl_cad"], 0)
        self.assertGreater(report["close_pnl_cad"], -report["stop_loss_cad"])
        self.assertEqual(report["exit_reason"], "basket parity gap converged or reversed")

    def test_held_exit_reserve_uses_legal_largest_weighted_children(self) -> None:
        entry = [etf.executable_trade_cashflow(self.snapshot, ticker, quantity)
                 for ticker, quantity in (("BULL", -20_000), ("BEAR", -20_000), ("RITC", 20_000))]
        held = {"entry_fills": entry, "entry_tick": 1}
        report = etf_basket.holding_report(self.snapshot, held, self.config, self.execution)
        children = report["close_reserve"]["children"]
        self.assertEqual([row["ticker"] for row in children],
                         ["RITC", "BULL", "BEAR", "RITC", "BULL", "BEAR"])
        self.assertTrue(all(abs(row["quantity"]) == 10_000 for row in children))

    def test_variance_does_not_diversify_same_ticker_children(self) -> None:
        import math
        r = etf_basket._execution_reserve(self.snapshot, [("BULL", 1000), ("BULL", 1000)],
                                          self.execution, {"BULL": .01})
        expected = .25 * math.sqrt(3 * (2000 * .01) ** 2 + 3 * (1000 * .01) ** 2)
        self.assertAlmostEqual(r["reserve_cad"], expected)

    def test_exit_clock_is_independent_and_margin_is_two_cents(self) -> None:
        r = etf_basket.entry_report(self.snapshot, 1, 1000, self.config, self.execution)
        self.assertEqual(r["required_convergence_pnl_cad"], 20)
        self.assertEqual(r["entry_reserve"]["horizon_ticks"], 9)
        self.assertEqual(r["exit_reserve"]["horizon_ticks"], 9)
        self.assertAlmostEqual(r["reserve_cad"], r["execution_risk_cad"] + r["fx_risk_cad"])


if __name__ == "__main__":
    unittest.main()
