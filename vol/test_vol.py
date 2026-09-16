"""Sanity tests for the volatility bot: pricing, clock, news, sizing.

Run from inside vol/:  python3 -m unittest test_vol -v
"""
import math
import unittest

import pricing
from bot import straddle_capacity
from news import forecast_sigma, parse_news


class TestPricing(unittest.TestCase):
    def test_put_call_parity(self):
        spot, strike, years, rate, sigma = 50.0, 48.0, 0.05, 0.02, 0.25
        call = pricing.bs_price("C", spot, strike, years, rate, sigma)
        put = pricing.bs_price("P", spot, strike, years, rate, sigma)
        forward = spot - strike * math.exp(-rate * years)
        self.assertAlmostEqual(call - put, forward, places=10)

    def test_implied_vol_round_trip(self):
        spot, strike, years, rate = 50.0, 50.0, 300 / 3600, 0.0
        price = pricing.bs_price("C", spot, strike, years, rate, 0.23)
        self.assertAlmostEqual(pricing.implied_vol("C", price, spot, strike, years, rate), 0.23, places=5)

    def test_implied_vol_impossible_price(self):
        self.assertIsNone(pricing.implied_vol("C", 0.0, 50.0, 50.0, 0.05, 0.0))
        self.assertIsNone(pricing.implied_vol("C", 1.0, 50.0, 50.0, 0.0, 0.0))

    def test_years_from_tick(self):
        self.assertAlmostEqual(pricing.years_left(0), 300 / 3600)
        self.assertAlmostEqual(pricing.years_left(150), 150 / 3600)
        self.assertEqual(pricing.years_left(300), 0.0)
        self.assertEqual(pricing.years_left(310), 0.0)

    def test_delta_signs(self):
        self.assertGreater(pricing.bs_delta("C", 50, 50, 0.05, 0.0, 0.25), 0)
        self.assertLess(pricing.bs_delta("P", 50, 50, 0.05, 0.0, 0.25), 0)


class TestNews(unittest.TestCase):
    def test_parse_three_templates(self):
        info = parse_news([
            {"tick": 1, "headline": "Welcome", "body": "The risk free rate is 2%. The annualized volatility this week will be 28%."},
            {"tick": 36, "headline": "Forecast", "body": "Volatility next week will be between 10% and 20%."},
            {"tick": 75, "headline": "Update", "body": "The volatility this week will be 15%."},
        ])
        self.assertAlmostEqual(info["rate"], 0.02)
        self.assertEqual(info["exact"], {0: 0.28, 1: 0.15})
        self.assertEqual(info["ranges"], {1: (0.10, 0.20)})
        self.assertEqual(info["unparsed"], [])

    def test_unparsed_vol_news_is_flagged(self):
        info = parse_news([{"tick": 40, "headline": "odd", "body": "volatility outlook uncertain"}])
        self.assertEqual(len(info["unparsed"]), 1)

    def test_forecast_carries_last_exact_forward(self):
        # Week 1 announced 28%; from tick 0 all four weeks carry 28%.
        self.assertAlmostEqual(forecast_sigma(0, {0: 0.28}), 0.28)
        # At tick 75 with week 2 announced at 15%, remaining weeks are all 15%.
        self.assertAlmostEqual(forecast_sigma(75, {0: 0.28, 1: 0.15}), 0.15)

    def test_forecast_time_weights_mixed_weeks(self):
        # At tick 150 with week 3 at 18%: 150 remaining ticks all at 18%.
        self.assertAlmostEqual(forecast_sigma(150, {0: 0.28, 1: 0.15, 2: 0.18}), 0.18)
        # Mid-week 2 (tick 100): 50 ticks at 15%, then 150 at 15% carried -> 15%.
        self.assertAlmostEqual(forecast_sigma(100, {0: 0.28, 1: 0.15}), 0.15)
        # Distinct future week: at tick 225 with week 4 at 22%.
        self.assertAlmostEqual(forecast_sigma(225, {3: 0.22}), 0.22)
        # Mid-week 2 (tick 100) with week 3 already known at 20%:
        # 50 ticks at 15%, then 150 ticks at 20% (75 announced + 75 carried).
        self.assertAlmostEqual(
            forecast_sigma(100, {0: 0.28, 1: 0.15, 2: 0.20}),
            math.sqrt((50 * 0.15 ** 2 + 150 * 0.20 ** 2) / 200))

    def test_forecast_none_without_exact_news(self):
        self.assertIsNone(forecast_sigma(10, {}))
        self.assertIsNone(forecast_sigma(300, {0: 0.28}))


class TestSizing(unittest.TestCase):
    def _options(self, positions):
        return [{"position": p} for p in positions]

    def test_full_size_when_flat(self):
        self.assertEqual(straddle_capacity(1, self._options([0, 0])), 500)
        self.assertEqual(straddle_capacity(-1, self._options([0, 0])), 500)

    def test_net_limit_binds(self):
        # Long 400 calls + 400 puts: net +800, room for 100 more buy-side straddles.
        self.assertEqual(straddle_capacity(1, self._options([400, 400])), 100)
        # Selling against a long book is capped by MAX_STRADDLES, not the limits.
        self.assertEqual(straddle_capacity(-1, self._options([400, 400])), 500)

    def test_gross_limit_binds(self):
        # 1200 long + 1200 short contracts: gross 2400, only 50 straddles left.
        self.assertEqual(straddle_capacity(1, self._options([1200, -1200])), 50)

    def test_never_negative(self):
        self.assertEqual(straddle_capacity(1, self._options([600, 600])), 0)


if __name__ == "__main__":
    unittest.main()
