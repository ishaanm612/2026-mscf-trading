"""Unit tests for volatility practice-heat lifecycle handling."""
from __future__ import annotations

import unittest

from scripts.supervise_volatility import parse_args, worker_command
from volatility.supervisor import SessionBoundaryDetector


class SupervisorTests(unittest.TestCase):
    """Confirm the supervisor only treats genuine lifecycle changes as resets."""

    def test_detector_stops_after_inactive_market(self) -> None:
        """End a worker after an active heat becomes inactive."""

        detector = SessionBoundaryDetector()
        self.assertFalse(detector.observe({"period": 1, "tick": 17, "status": "ACTIVE"}))
        self.assertFalse(detector.observe({"period": 1, "tick": 18, "status": "ACTIVE"}))
        self.assertTrue(detector.observe({"period": 1, "tick": 300, "status": "STOPPED"}))

    def test_detector_stops_on_period_or_tick_reset(self) -> None:
        """Treat both server reset formats as a fresh heat boundary."""

        detector = SessionBoundaryDetector()
        self.assertFalse(detector.observe({"period": 1, "tick": 200, "status": "ACTIVE"}))
        self.assertTrue(detector.observe({"period": 1, "tick": 0, "status": "ACTIVE"}))
        detector = SessionBoundaryDetector()
        self.assertFalse(detector.observe({"period": 1, "tick": 200, "status": "ACTIVE"}))
        self.assertTrue(detector.observe({"period": 2, "tick": 1, "status": "ACTIVE"}))

    def test_worker_command_keeps_trading_explicit(self) -> None:
        """Require an explicit flag before a supervised child can trade."""

        plan = worker_command(parse_args([]))
        trade = worker_command(parse_args(["--trade", "--decision-log", "data/decisions.jsonl"]))
        self.assertIn("--plan", plan)
        self.assertNotIn("--trade", plan)
        self.assertIn("--trade", trade)
        self.assertIn("--exit-on-session-change", trade)
        self.assertIn("data/decisions.jsonl", trade)


if __name__ == "__main__":
    unittest.main()
