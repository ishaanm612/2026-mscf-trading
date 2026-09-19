import math
import unittest
from models import etf, volatility
from run import demo


class Models(unittest.TestCase):
    def test_put_call_parity_and_iv(self):
        for strike in (48, 50, 52):
            c, dc, _ = volatility.bs(50, strike, 1/12, .03, .25, "C")
            p, dp, _ = volatility.bs(50, strike, 1/12, .03, .25, "P")
            self.assertAlmostEqual(c-p, 50-strike*math.exp(-.03/12))
            self.assertAlmostEqual(dc-dp, 1)
            self.assertAlmostEqual(volatility.implied_vol(c, 50, strike, 1/12, .03, "C"), .25)

    def test_expiry_and_bad_iv(self):
        self.assertEqual(volatility.bs(51, 50, 0, 0, .2, "C"), (1, 1, 0))
        self.assertIsNone(volatility.implied_vol(100, 50, 50, 1/12, 0, "C"))

    def test_hedge_includes_existing_stock(self):
        snapshot = demo("volatility")
        snapshot["securities"][0]["position"] = 1200
        result = volatility.analyze(snapshot, .25)
        self.assertEqual(result["suggested_rtm_hedge_shares"], -1200)
        snapshot["securities"][1]["position"] = 10
        d = result["options"][0]["delta"]
        self.assertAlmostEqual(volatility.analyze(snapshot, .25)["portfolio_delta_shares"], 1200 + 1000*d)

    def test_weighted_intermediate_risk(self):
        self.assertEqual(etf.exposure({"RITC": 100}), {"gross": 200, "net": 200})
        legs = [("BULL", -100), ("BEAR", -100), ("RITC", 100)]
        self.assertFalse(etf.within_limits({}, legs, 1000, 150))
        self.assertTrue(etf.within_limits({}, legs, 1000, 200))

    def test_depth_uses_remaining_quantity(self):
        book = {"asks": [{"price": 10, "quantity": 100, "quantity_filled": 50},
                         {"price": 11, "quantity": 100}]}
        self.assertEqual(etf.vwap(book, "BUY", 100), 10.5)
        with self.assertRaises(ValueError):
            etf.vwap(book, "BUY", 200)

    def test_tender_is_evaluated_without_acceptance(self):
        snapshot = demo("etf")
        snapshot["tenders"] = [{"tender_id": 1, "ticker": "RITC", "action": "BUY",
                                "quantity": 1000, "price": 30, "is_fixed_bid": True}]
        report = etf.analyze(snapshot)["tenders"][0]
        self.assertLess(report["estimated_unwind_profit_usd"], 0)
        self.assertEqual(report["decision"], "REVIEW")

    def test_basket_report_does_not_charge_unexecuted_gross_fx(self):
        snapshot = demo("etf")
        snapshot["books"]["USD"] = {"bids": [{"price": 1.00, "quantity": 1000000}],
                                    "asks": [{"price": 1.01, "quantity": 1000000}]}
        report = etf.basket_opportunity(snapshot, 1, 1000, 300000, 200000)
        self.assertIsNone(report["fx_leg"])
        self.assertAlmostEqual(report["basket_cad_per_unit"], 24.98)
        self.assertAlmostEqual(report["ritc_cad_per_unit"], 24.81 * 1.005)
        self.assertAlmostEqual(report["fees_cad_per_unit"], .04 + .02 * 1.005)
        self.assertFalse(report["eligible_after_buffer"])
        self.assertTrue(report["within_configured_limits"])

    def test_short_ritc_basket_also_defers_fx_until_final_net_usd(self):
        report = etf.basket_opportunity(demo("etf"), -1, 1000, 300000, 200000)
        self.assertIsNone(report["fx_leg"])
        self.assertIn("No gross FX trade", report["fx_treatment"])

    def test_tender_profit_is_converted_from_net_usd_to_cad(self):
        snapshot = demo("etf")
        offer = {"tender_id": 2, "ticker": "RITC", "action": "BUY",
                 "quantity": 1000, "price": 24.70, "is_fixed_bid": True}
        report = etf.tender_opportunity(snapshot, offer)
        self.assertAlmostEqual(report["estimated_unwind_profit_usd"], 70.0)
        self.assertAlmostEqual(report["estimated_unwind_profit_cad"], 69.3)

    def test_converter_recommendations_follow_inventory_direction(self):
        redemption = demo("etf")
        next(row for row in redemption["securities"] if row["ticker"] == "RITC")["position"] = 10000
        rec = next(item for item in etf.manual_converter_opportunities(redemption)
                   if item["converter"] == "ETF-Redemption")
        self.assertEqual(rec["manual_action"], "UNWIND")
        self.assertTrue(rec["recommended"])

        creation = demo("etf")
        for row in creation["securities"]:
            if row["ticker"] in {"BULL", "BEAR"}:
                row["position"] = 10000
            elif row["ticker"] == "RITC":
                row["position"] = -10000
        creation["books"]["RITC"]["asks"] = [{"price": 30, "quantity": 1000000}]
        rec = next(item for item in etf.manual_converter_opportunities(creation)
                   if item["converter"] == "ETF-Creation")
        self.assertEqual(rec["manual_action"], "WIND")
        self.assertTrue(rec["recommended"])


if __name__ == "__main__":
    unittest.main()
