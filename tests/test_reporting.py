"""Tests for concise volatility runner output."""
from __future__ import annotations

import unittest

from run import demo
from volatility.reporting import format_volatility_report, summarize_volatility_result


class ReportingTests(unittest.TestCase):
    """Confirm console reports expose decisions without option-data dumps."""

    def test_summary_keeps_entry_facts_and_omits_all_options(self) -> None:
        """Present selected straddle and risk information in one compact record."""

        snapshot = demo("volatility")
        result = {"ticker": "RTM50C", "quantity": 10, "reason": "volatility mispricing",
                  "decision": {"reason": "enter", "forecast": {"sigma": .25}, "time_since_latest_news": 2,
                               "portfolio": {"delta": 0, "gamma": 0, "vega": 0},
                               "straddle": {"strike": 50, "side": "BUY", "edge": 12.345},
                               "options": [{"symbol": "RTM48C"}], "explanation": {}}}
        report = summarize_volatility_result(snapshot, result)
        self.assertEqual(report["order"]["symbol"], "RTM50C")
        self.assertEqual(report["straddle"]["edge_per_straddle"], 12.35)
        self.assertNotIn("options", report)

    def test_summary_reports_wait_reason(self) -> None:
        """Expose a wait without inventing a decision payload."""

        report = summarize_volatility_result(demo("volatility"), {"wait": "open account orders"})
        self.assertEqual(report["action"], "WAIT")
        self.assertEqual(report["reason"], "open account orders")

    def test_formatting_is_a_compact_operator_line(self) -> None:
        """Render important trade facts without JSON or all-option output."""

        line = format_volatility_report({"tick": 17, "action": "WAIT", "fair_volatility": .25,
                                         "portfolio_delta": -123, "reason": "waiting for edge"})
        self.assertIn("tick 17", line)
        self.assertIn("fair IV 25.00%", line)
        self.assertNotIn("{", line)
