"""Tests for the interpretable convergence-model pipeline."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from analysis.convergence import samples_from_records
from run import demo
from volatility.convergence import ConvergenceModel, ConvergenceSample, features_for_straddle, fit_ridge
from volatility.strategy import VolatilityStrategy


class ConvergenceTests(unittest.TestCase):
    """Verify fitting, persistence, labels, and opt-in entry gating."""

    def test_fit_round_trip_and_holdout_metrics(self) -> None:
        """Fit a model on whole heat splits and preserve its prediction in JSON."""

        samples = [ConvergenceSample((1.0, float(index), .1, 1.0, 200.0, 4.0), float(index), index // 3, index)
                   for index in range(18)]
        model = fit_ridge(samples, 10, holdout_heats=2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            model.save(path)
            restored = ConvergenceModel.load(path)
        self.assertEqual(restored.horizon_ticks, 10)
        self.assertEqual(len(restored.coefficients), 6)
        self.assertGreaterEqual(restored.holdout_directional_accuracy, 0.0)

    def test_sample_labels_use_executable_bid_ask(self) -> None:
        """Label a long straddle with future bids instead of midpoint prices."""

        def record(tick: int, call_bid: float, call_ask: float, put_bid: float, put_ask: float) -> dict:
            return {"tick": tick, "forecast": {"sigma": .2}, "time_since_latest_news": 1,
                    "straddle": {"strike": 50.0, "side": "BUY", "edge": 10.0},
                    "options": [{"symbol": "RTM50C", "bid": call_bid, "ask": call_ask, "market_iv": .2},
                                {"symbol": "RTM50P", "bid": put_bid, "ask": put_ask, "market_iv": .2}]}
        samples = samples_from_records([record(1, 1.0, 1.1, 1.0, 1.1), record(11, 1.2, 1.3, 1.3, 1.4)], 10)
        self.assertEqual(len(samples), 1)
        self.assertAlmostEqual(samples[0].realized_pnl, 30.0)

    def test_model_can_block_an_otherwise_eligible_entry(self) -> None:
        """Keep learned prediction as a bounded entry gate, never a risk override."""

        model = ConvergenceModel(10, (-1_000.0, 0, 0, 0, 0, 0), 20, 1.0, .6)
        snapshot = demo("volatility")
        snapshot["news"] = [{"news_id": 1, "tick": 0, "body": "The current annualized realized volatility is 40%."}]
        decision = VolatilityStrategy(convergence_model=model).decide(snapshot)
        self.assertEqual(decision.reason, "wait: learned convergence return is insufficient")


if __name__ == "__main__":
    unittest.main()
