"""Offline behavioral tests for the typed volatility V1 package."""
from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from analysis.reaction import _observations, read_decisions, trade_observations, write_svg
from run import demo
from volatility.config import VolatilityConfig
from volatility.forecast import estimate_remaining_volatility
from volatility.hedging import calculate_hedge_order
from volatility.logger import StrategyLogger
from volatility.strategy import VolatilityStrategy


class VolatilityV1Tests(unittest.TestCase):
    """Validate baseline strategy decisions without a practice connection."""

    def test_variance_is_aggregated_before_square_root(self) -> None:
        """Combine announced regimes using integrated variance, not mean volatility."""

        news = [
            {"news_id": 1, "tick": 0, "body": "Volatility for week 1 is 20%."},
            {"news_id": 2, "tick": 0, "body": "Volatility for week 2 is 30%."},
            {"news_id": 3, "tick": 0, "body": "Volatility for week 3 is 25%."},
            {"news_id": 4, "tick": 0, "body": "Volatility for week 4 is 25%."},
        ]
        result = estimate_remaining_volatility(0, 300, news)
        self.assertAlmostEqual(result.sigma, math.sqrt((.2**2 + .3**2 + .25**2 + .25**2) / 4))

    def test_news_event_creates_cost_adjusted_atm_straddle(self) -> None:
        """Generate paired ATM orders only after an analyst/news event."""

        snapshot = demo("volatility")
        snapshot["news"] = [{"news_id": 1, "tick": 0, "body": "The current annualized realized volatility is 25%."}]
        decision = VolatilityStrategy().decide(snapshot)
        self.assertEqual(decision.reason, "enter: new news and cost-adjusted ATM straddle edge")
        self.assertEqual({trade.symbol for trade in decision.desired_trades}, {"RTM50C", "RTM50P"})

    def test_hedge_band_avoids_small_rtm_trades(self) -> None:
        """Keep small delta changes inside the configured no-trade band."""

        self.assertEqual(calculate_hedge_order(2999, 3000), 0)
        self.assertEqual(calculate_hedge_order(-3100, 3000), 3100)

    def test_large_delta_is_hedged_before_new_entry(self) -> None:
        """Prioritize safety hedge over a fresh volatility opportunity."""

        snapshot = demo("volatility")
        snapshot["securities"][0]["position"] = 6100
        snapshot["news"] = [{"news_id": 1, "tick": 0, "body": "The current annualized realized volatility is 25%."}]
        decision = VolatilityStrategy(VolatilityConfig()).decide(snapshot)
        self.assertEqual(decision.desired_trades[0].symbol, "RTM")
        self.assertEqual(decision.desired_trades[0].quantity, -6100)

    def test_explainable_log_generates_reaction_chart(self) -> None:
        """Persist factors and render the offline market-maker convergence SVG."""

        snapshot = demo("volatility")
        snapshot["news"] = [{"news_id": 1, "tick": 0, "body": "The current annualized realized volatility is 25%."}]
        strategy = VolatilityStrategy()
        decision = strategy.decide(snapshot)
        fields = decision.as_log_fields()
        fields["explanation"] = strategy.explain(decision)
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "decisions.jsonl"
            chart = Path(directory) / "reaction.svg"
            StrategyLogger(log).write("volatility_decision", fields)
            records = read_decisions(log)
            write_svg(_observations(records), chart, trade_observations(records))
            self.assertIn("Market-maker IV convergence", chart.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
