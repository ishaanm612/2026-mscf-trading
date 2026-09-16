"""Offline behavioral tests for the typed volatility V1 package."""
from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from analysis.reaction import _observations, read_decisions, trade_observations, write_svg
from run import demo
from volatility.config import VolatilityConfig
from volatility.forecast import estimate_remaining_volatility, parse_regimes
from volatility.hedging import calculate_hedge_order
from volatility.logger import StrategyLogger
from volatility.strategy import VolatilityStrategy


def _remaining_sigma(tick: int, news: list[dict], expiry: int = 300, prior: float = 0.20) -> float:
    """Reproduce remaining volatility with the unannounced-week prior."""

    regimes, _, _ = parse_regimes(news, tick, expiry)
    total = 0.0
    for current in range(tick, expiry):
        applicable = next((regime for regime in reversed(regimes)
                           if regime.start_tick <= current < regime.end_tick), None)
        total += applicable.variance if applicable is not None else prior * prior
    return math.sqrt(total / (expiry - tick))


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
        snapshot["news"] = [{"news_id": 1, "tick": 0, "body": "The current annualized realized volatility is 40%."}]
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
        snapshot["news"] = [{"news_id": 1, "tick": 0, "body": "The current annualized realized volatility is 40%."}]
        decision = VolatilityStrategy(VolatilityConfig()).decide(snapshot)
        self.assertEqual(decision.desired_trades[0].symbol, "RTM")
        self.assertEqual(decision.desired_trades[0].quantity, -6100)

    def test_expiry_window_never_opens_a_flat_straddle(self) -> None:
        """Block fresh signals once the configured inventory-reduction window starts."""

        snapshot = demo("volatility")
        snapshot["case"]["tick"] = 261
        snapshot["news"] = [{"news_id": 1, "tick": 261, "body": "New analyst event."}]
        decision = VolatilityStrategy(VolatilityConfig(close_tick=240), fallback_sigma=.32).decide(snapshot)
        self.assertEqual(decision.desired_trades, ())
        self.assertEqual(decision.reason, "wait: configured expiry window blocks new entries")

    def test_latest_announcement_replaces_range_and_midweek_revision(self) -> None:
        """Prefer new information only for its applicable interval, without lookahead."""

        news = [
            {"news_id": 1, "tick": 1, "body": "Current volatility is 40%."},
            {"news_id": 2, "tick": 36, "body": "Volatility next week is between 31% and 36%."},
            {"news_id": 3, "tick": 75, "body": "Volatility this week is 35%."},
            {"news_id": 4, "tick": 90, "body": "Volatility this week is 30%."},
        ]
        self.assertAlmostEqual(estimate_remaining_volatility(75, 300, reversed(news)).sigma, _remaining_sigma(75, news))
        self.assertAlmostEqual(estimate_remaining_volatility(89, 300, news).sigma, _remaining_sigma(89, news))
        self.assertAlmostEqual(estimate_remaining_volatility(90, 300, news).sigma, _remaining_sigma(90, news))
        later = estimate_remaining_volatility(151, 300, news)
        self.assertAlmostEqual(later.sigma, .20)
        self.assertTrue(later.used_unannounced_prior)

    def test_exits_actual_strike_when_signal_reverses(self) -> None:
        """Close long and short inventory even when another strike becomes ATM."""

        for position, sigma in ((10, .10), (-10, .40)):
            for spot in (50.0, 51.0):
                with self.subTest(position=position, spot=spot):
                    snapshot = demo("volatility")
                    snapshot["securities"][0].update(bid=spot-.01, ask=spot+.01)
                    for row in snapshot["securities"]:
                        if row["ticker"] in {"RTM50C", "RTM50P"}:
                            row["position"] = position
                    decision = VolatilityStrategy(fallback_sigma=sigma).decide(snapshot)
                    self.assertEqual({t.symbol: t.quantity for t in decision.desired_trades},
                                     {"RTM50C": -position, "RTM50P": -position})

    def test_exit_budget_scales_with_inventory_and_delays(self) -> None:
        """Move liquidation earlier for more children or slower observed cycles."""

        from volatility.market_data import from_snapshot
        from volatility.timing import exit_budget
        state = from_snapshot(demo("volatility"))
        config = VolatilityConfig()
        small = exit_budget(state, {"RTM50C": 69, "RTM50P": 69}, 2, config)
        large = exit_budget(state, {"RTM50C": 250, "RTM50P": 250}, 2, config)
        slow = exit_budget(state, {"RTM50C": 69, "RTM50P": 69}, 6, config)
        self.assertGreater(small.liquidation_tick, 240)
        self.assertLess(large.liquidation_tick, small.liquidation_tick)
        self.assertLess(slow.liquidation_tick, small.liquidation_tick)

    def test_late_entry_reserves_full_lifecycle(self) -> None:
        """Allow post-240 entries with enough time, then reject insufficient runway."""

        snapshot = demo("volatility")
        snapshot["news"] = [{"news_id": 1, "tick": 240, "body": "Volatility this week is 100%."}]
        snapshot["case"]["tick"] = 241
        self.assertTrue(VolatilityStrategy().decide(snapshot).desired_trades)
        snapshot["case"]["tick"] = 290
        snapshot["news"][0]["tick"] = 290
        decision = VolatilityStrategy().decide(snapshot)
        self.assertFalse(decision.desired_trades)
        self.assertIn("insufficient time", decision.reason)

    def test_liquidation_latches_and_chunks_large_positions(self) -> None:
        """Keep reducing after inventory shrinks and respect server child sizes."""

        snapshot = demo("volatility")
        snapshot["case"]["tick"] = 268
        snapshot["securities"][1]["position"] = 250
        strategy = VolatilityStrategy(fallback_sigma=.3)
        decision = strategy.decide(snapshot)
        self.assertEqual(decision.desired_trades[0].quantity, -100)
        snapshot["case"]["tick"] = 269
        snapshot["securities"][1]["position"] = 50
        decision = strategy.decide(snapshot)
        self.assertEqual(decision.desired_trades[0].quantity, -50)
        self.assertEqual(decision.reason, "exit: configured expiry window")

    def test_news_window_rechecks_quotes_without_reentering(self) -> None:
        """Retain an unused news event for a short window and consume it on entry."""

        snapshot = demo("volatility")
        snapshot["news"] = [{"news_id": 1, "tick": 1, "body": "Current volatility is 40%."}]
        snapshot["case"]["tick"] = 1
        strategy = VolatilityStrategy()
        original = [dict(row) for row in snapshot["securities"]]
        for row in snapshot["securities"][1:]:
            row.update(bid=0, ask=10)
        self.assertFalse(strategy.decide(snapshot).desired_trades)
        snapshot["securities"] = original
        snapshot["case"]["tick"] = 2
        self.assertTrue(strategy.decide(snapshot).desired_trades)
        self.assertFalse(strategy.decide(snapshot).desired_trades)
        snapshot["case"]["tick"] = 20
        self.assertFalse(VolatilityStrategy().decide(snapshot).desired_trades)

    def test_incomplete_straddle_skips_ordinary_hedge(self) -> None:
        """Finish the missing option leg before hedging temporary one-leg delta."""

        snapshot = demo("volatility")
        next(row for row in snapshot["securities"] if row["ticker"] == "RTM50C")["position"] = 69
        decision = VolatilityStrategy(fallback_sigma=.25).decide(snapshot)
        self.assertEqual(decision.reason, "wait: existing option inventory is being held and risk-managed")
        self.assertEqual(decision.desired_trades, ())

    def test_safety_hedge_still_fires_on_incomplete_inventory(self) -> None:
        """Keep the 6,000-share boundary even while a straddle is incomplete."""

        snapshot = demo("volatility")
        snapshot["securities"][0]["position"] = 5000
        next(row for row in snapshot["securities"] if row["ticker"] == "RTM50C")["position"] = 69
        decision = VolatilityStrategy(fallback_sigma=.25).decide(snapshot)
        self.assertEqual(decision.reason, "hedge: internal delta boundary")
        self.assertEqual(decision.desired_trades[0].symbol, "RTM")

    def test_take_profit_exits_after_entry_edge_decays(self) -> None:
        """Free capital once remaining edge is a small fraction of the entry edge."""

        snapshot = demo("volatility")
        snapshot["news"] = [{"news_id": 1, "tick": 0, "body": "The current annualized realized volatility is 40%."}]
        strategy = VolatilityStrategy()
        self.assertTrue(strategy.decide(snapshot).desired_trades)
        for row in snapshot["securities"]:
            if row["ticker"] in {"RTM50C", "RTM50P"}:
                row["position"] = 10
        strategy._entry_edge = 1_000.0
        snapshot["case"]["tick"] = 2
        decision = strategy.decide(snapshot)
        self.assertEqual(decision.reason, "exit: remaining edge below take-profit fraction of entry")
        self.assertEqual({trade.symbol for trade in decision.desired_trades}, {"RTM50C", "RTM50P"})

    def test_new_news_exits_when_straddle_side_reverses(self) -> None:
        """Close a complete straddle when a fresh announcement flips the ATM side."""

        snapshot = demo("volatility")
        snapshot["news"] = [{"news_id": 1, "tick": 0, "body": "Current volatility is 40%."}]
        config = VolatilityConfig(exit_edge_per_contract=-1_000.0, take_profit_remaining_fraction=0.0)
        strategy = VolatilityStrategy(config)
        self.assertTrue(strategy.decide(snapshot).desired_trades)
        for row in snapshot["securities"]:
            if row["ticker"] in {"RTM50C", "RTM50P"}:
                row["position"] = 10
        strategy._entry_edge = None
        snapshot["case"]["tick"] = 1
        snapshot["news"].append({"news_id": 2, "tick": 1, "body": "Volatility this week is 5%."})
        decision = strategy.decide(snapshot)
        self.assertEqual(decision.reason, "exit: new news reversed the held straddle")
        self.assertEqual({trade.symbol: trade.quantity for trade in decision.desired_trades},
                         {"RTM50C": -10, "RTM50P": -10})

    def test_explainable_log_generates_reaction_chart(self) -> None:
        """Persist factors and render the offline market-maker convergence SVG."""

        snapshot = demo("volatility")
        snapshot["news"] = [{"news_id": 1, "tick": 0, "body": "The current annualized realized volatility is 40%."}]
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
