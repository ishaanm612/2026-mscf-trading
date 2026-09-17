"""Adversarial ETF eligibility, scheduling, cash-risk and recovery regressions."""
import copy
import unittest
from unittest.mock import MagicMock, patch

from bot import Bot
from client import RITReadError
from models import etf, etf_basket, etf_policy
from run import main
from scripts.supervise_volatility import parse_args, worker_command
from tests.test_etf_accounting import ETFExchange, etf_snapshot


def offer(quantity=10000, price=24.50, action="BUY"):
    return {"tender_id": 7, "ticker": "RITC", "is_fixed_bid": True,
            "action": action, "quantity": quantity, "price": price, "expires": 30}


def trader(exchange=None, **kwargs):
    return Bot(exchange, exchange, case="etf", gross_limit=300000, net_limit=200000, **kwargs)


class ETFExecutionPolicy(unittest.TestCase):
    def test_known_open_order_waits_then_resumes_without_mutations(self):
        ex = ETFExchange()
        bot = trader(ex)
        ex.state["orders"] = [{"order_id": 42, "status": "OPEN"}]
        waiting = bot.step(ex.snapshot())
        self.assertEqual(waiting["open_order_ids"], [42])
        self.assertEqual(ex.orders, [])
        ex.state["orders"] = []
        self.assertEqual(bot.step(ex.snapshot())["wait"], "no eligible ETF trade")

    def test_open_order_during_held_basket_latches_reduction_after_it_clears(self):
        from tests.test_etf_basket_lifecycle import stable_basket_edge
        ex = ETFExchange()
        stable_basket_edge(ex)
        bot = trader(ex, basket=True)
        bot.step(ex.snapshot())
        count = len(ex.orders)
        ex.state["orders"] = [{"order_id": 42, "status": "OPEN"}]
        self.assertIn("open account orders", bot.step(ex.snapshot())["wait"])
        self.assertEqual(len(ex.orders), count)
        ex.state["orders"] = []
        result = bot.step(ex.snapshot())
        self.assertTrue(result["inventory_reduction_required"])
        self.assertEqual(result["reason"], "unwind inventory")

    def test_tender_weights_change_eligibility_without_changing_basket_weights(self):
        s = etf_snapshot()
        s["tenders"] = [offer(100000, 24.74)]
        old = trader(etf_config=etf_policy.ETFConfig(tender_execution_k=.25, tender_fx_k=.5,
                                                   manual_wait_ticks=0))
        new = trader(etf_config=etf_policy.ETFConfig(manual_wait_ticks=0))
        for bot in (old, new):
            bot.etf_sigmas = {"RITC": .04, "BULL": .04, "BEAR": .04, "USD": .002}
        self.assertEqual(old.tender_assessments(s, old.position_map(s), {})[0]["decision"], "REJECT")
        result = new.tender_assessments(s, new.position_map(s), {})[0]
        self.assertEqual(result["decision"], "ACCEPT")
        self.assertEqual(result["selected_route"]["name"], "DIRECT")
        self.assertEqual(result["liquidation_reserve"]["execution_k"], .15)
        self.assertEqual(new.etf_config.execution_k, .25)
        self.assertEqual(new.etf_config.fx_k, .5)
        s["tenders"] = [offer(100000, 30)]
        self.assertEqual(new.tender_assessments(s, new.position_map(s), {})[0]["decision"], "REJECT")

    def test_startup_reserve_uses_spread_then_observed_volatility(self):
        s = etf_snapshot()
        config = etf_policy.ETFConfig()
        route = etf_policy.route_plan(s, {"RITC": 10000, "USD": -247000}, config)
        startup = etf_policy.reserve(s, route, config)
        observed = etf_policy.reserve(s, route, config, {"RITC": .04, "USD": .002})
        self.assertAlmostEqual(startup["children"][0]["sigma_per_sqrt_tick"], .005)
        self.assertIn("startup", startup["children"][0]["sigma_source"])
        self.assertIn("observed", observed["children"][0]["sigma_source"])
        self.assertGreater(observed["reserve_cad"], startup["reserve_cad"])

    def test_reserve_decomposes_and_does_not_charge_depth_twice(self):
        s = etf_snapshot()
        config = etf_policy.ETFConfig()
        p = {"RITC": 20000, "USD": -490000}
        route = etf_policy.route_plan(s, p, config)
        r = etf_policy.reserve(s, route, config, {"RITC": .01, "USD": .0001})
        self.assertAlmostEqual(r["reserve_cad"], r["execution_risk_cad"] + r["fx_risk_cad"])
        s["books"]["RITC"]["bids"] = [{"price": 24.79, "quantity": 10000},
                                       {"price": 24.69, "quantity": 10000}]
        deep = etf_policy.route_plan(s, p, config)
        stressed = etf_policy.reserve(s, deep, config, {"RITC": .01, "USD": .0001})
        self.assertAlmostEqual(route["total_cad"] - deep["total_cad"], 990)
        self.assertAlmostEqual(stressed["execution_risk_cad"], r["execution_risk_cad"])
        self.assertEqual([row["price"] for row in deep["fills"]], [24.79, 24.69])

    def test_realistic_positive_offer_clears_reserve_and_loss_does_not(self):
        s = etf_snapshot()
        s["tenders"] = [offer(80000, 24.65)]
        bot = trader()
        a = bot.tender_assessments(s, bot.position_map(s), {})[0]
        self.assertEqual(a["decision"], "ACCEPT")
        self.assertGreater(a["surplus_cad"], 0)
        s["tenders"] = [offer(80000, 30)]
        self.assertEqual(bot.tender_assessments(s, bot.position_map(s), {})[0]["decision"], "REJECT")

    def test_manual_route_can_qualify_before_acceptance_without_full_ritc_depth(self):
        s = etf_snapshot()
        s["books"]["USD"] = {"bids": [{"price": 1, "quantity": 5000000}],
                                "asks": [{"price": 1, "quantity": 5000000}]}
        s["books"]["RITC"]["bids"] = [{"price": 24.4, "quantity": 1000}]
        s["tenders"] = [offer(10000, 24.5)]
        bot = trader()
        a = bot.tender_assessments(s, bot.position_map(s), {})[0]
        self.assertEqual(a["decision"], "ACCEPT")
        self.assertEqual(a["selected_route"]["name"], "ETF-Redemption")
        self.assertGreater(a["estimated_unwind_profit_cad"], 0)

    def test_offsetting_tender_preempts_existing_inventory(self):
        ex = ETFExchange()
        ex.move("RITC", -10000)
        ex.move("USD", 250000)
        ex.state["tenders"] = [offer(10000, 24.5)]
        result = trader(ex).step(ex.snapshot())
        self.assertEqual(result["tender_id"], 7)
        self.assertTrue(result["tender_assessment"]["risk_reducing"])
        self.assertEqual(ex.positions()["RITC"], 0)
        self.assertEqual(ex.orders, [])

    def test_manual_timeout_latches_until_flat_and_forced_exit_never_waits(self):
        for flatten in (False, True):
            ex = ETFExchange()
            ex.move("RITC", 20000)
            ex.move("USD", -490000)
            ex.state["books"]["RITC"]["bids"] = [{"price": 24.4, "quantity": 1000000}]
            bot = trader(ex, flatten_only=flatten)
            first = bot.step(ex.snapshot())
            if flatten:
                self.assertEqual(first["ticker"], "RITC")
            else:
                self.assertIn("manual_converter", first)
                ex.state["case"]["tick"] = 10
                self.assertEqual(bot.step(ex.snapshot())["ticker"], "RITC")
            ex.state["case"]["tick"] = 11
            self.assertEqual(bot.step(ex.snapshot())["ticker"], "RITC")
            self.assertEqual(ex.positions()["RITC"], 0)

    def test_late_converter_cannot_override_exit(self):
        ex = ETFExchange()
        ex.move("RITC", 10000)
        ex.move("USD", -245000)
        ex.state["books"]["RITC"]["bids"] = [{"price": 24.4, "quantity": 1000000}]
        ex.state["case"]["tick"] = 298
        result = trader(ex).step(ex.snapshot())
        self.assertEqual(result["ticker"], "RITC")

    def test_fx_children_respect_lower_server_cap(self):
        ex = ETFExchange()
        ex.move("USD", 25000)
        ex._security("USD")["max_trade_size"] = 10000
        bot = trader(ex)
        sizes = [bot.step(ex.snapshot())["quantity"] for _ in range(3)]
        self.assertEqual(sizes, [-10000, -10000, -5000])
        self.assertTrue(ex.is_flat())

    def test_cash_capacity_checked_at_tender_acceptance(self):
        s = etf_snapshot()
        s["limits"][1].update(gross_limit=100000, net_limit=100000)
        s["tenders"] = [offer(10000, 24.5)]
        bot = trader()
        a = bot.tender_assessments(s, bot.position_map(s), {})[0]
        self.assertEqual(a["decision"], "REJECT")
        self.assertIn("cash", a["reason"])

    def test_creation_routes_check_intermediate_gross_capacity(self):
        s = etf_snapshot()
        for side in ("bids", "asks"):
            s["books"]["USD"][side][0]["quantity"] = 5000000
        s["books"]["RITC"]["asks"] = [{"price": 27, "quantity": 1000000}]
        s["tenders"] = [offer(100000, 26, "SELL")]
        bot = trader()
        a = bot.tender_assessments(s, bot.position_map(s), {})[0]
        full = next(r for r in a["routes"] if r["name"] == "ETF-Creation" and r["blocks"] == 10)
        self.assertIn("risk gate", full["rejection"])

    def test_creation_tender_prepares_stocks_then_requests_manual_wind(self):
        ex = ETFExchange()
        ex.state["books"]["USD"] = {"bids": [{"price": 1, "quantity": 5000000}],
                                    "asks": [{"price": 1, "quantity": 5000000}]}
        ex.state["books"]["RITC"]["asks"] = [{"price": 26, "quantity": 1000000}]
        ex.state["tenders"] = [offer(10000, 25.5, "SELL")]
        bot = trader(ex)
        accepted = bot.step(ex.snapshot())
        self.assertEqual(accepted["tender_assessment"]["selected_route"]["name"], "ETF-Creation")
        for _ in range(2):
            action = bot.step(ex.snapshot())
            self.assertEqual(action["reason"], "prepare manual ETF creation")
        rec = bot.step(ex.snapshot())["manual_converter"]
        self.assertEqual(rec["manual_action"], "WIND")
        ex.converter(rec["converter"], rec["blocks"])
        bot.step(ex.snapshot())
        self.assertTrue(ex.is_flat())
        self.assertGreater(ex.realized_pnl_cad, 0)

    def test_disabling_manual_routes_prevents_converter_only_tender_acceptance(self):
        s = etf_snapshot()
        s["books"]["RITC"]["bids"] = [{"price": 24, "quantity": 1000000}]
        s["tenders"] = [offer(10000, 24.5)]
        bot = trader(etf_config=etf_policy.ETFConfig(manual_wait_ticks=0))
        self.assertEqual(bot.tender_assessments(s, bot.position_map(s), {})[0]["decision"], "REJECT")

    def test_parent_target_slices_down_to_remaining_risk_capacity(self):
        ex = ETFExchange()
        ex.state["books"]["RITC"] = {"bids": [{"price": 24.39, "quantity": 1_000_000}],
                                        "asks": [{"price": 24.40, "quantity": 1_000_000}]}
        # A 10k held slice consumes 40k weighted gross.  The next balanced
        # child is therefore capped at 2k by the 48k session limit.
        ex.move("BULL", -10_000)
        ex.move("BEAR", -10_000)
        ex.move("RITC", 10_000)
        bot = Bot(ex, ex, case="etf", basket=True, quantity=20000, gross_limit=48000,
                  net_limit=30000, etf_config=etf_policy.ETFConfig(manual_wait_ticks=0))
        analysis = bot.basket_analysis(ex.snapshot(), bot.position_map(ex.snapshot()), 10_000)
        selected = next(row for row in analysis["opportunities"] if row.get("eligible_after_buffer"))
        self.assertEqual(selected["legs"], [("BULL", -2_000), ("BEAR", -2_000), ("RITC", 2_000)])
        projected = ex.positions()
        for ticker, quantity in selected["legs"]:
            projected[ticker] += quantity
        self.assertEqual(etf.exposure(projected)["gross"], 48000)

    def test_basket_shrinks_child_when_large_size_loses_its_edge(self):
        ex = ETFExchange()
        ex.state["books"]["RITC"]["asks"] = [{"price": 24.40, "quantity": 1000},
                                            {"price": 25.5, "quantity": 1000000}]
        bot = trader(ex, basket=True, quantity=20000,
                     etf_config=etf_policy.ETFConfig(manual_wait_ticks=0))
        result = bot.step(ex.snapshot())
        self.assertEqual(result["reason"], "basket filled")
        self.assertGreaterEqual(result["filled_quantity"], 1000)
        self.assertLess(result["filled_quantity"], 10000)

    def test_missing_depth_after_confirmed_leg_reduces_inventory(self):
        ex = ETFExchange()
        ex.state["books"]["RITC"] = {"bids": [{"price": 24.39, "quantity": 1_000_000}],
                                        "asks": [{"price": 24.40, "quantity": 1_000_000}]}
        def remove_depth(exchange, count):
            if count == 1:
                exchange.state["books"]["BEAR"]["bids"] = []
        ex.after_order = remove_depth
        bot = trader(ex, basket=True)
        first = bot.step(ex.snapshot())
        self.assertIn("depth changed", first["wait"])
        second = bot.step(ex.snapshot())
        self.assertEqual((second["ticker"], second["quantity"]), ("BULL", 1000))
        self.assertTrue(ex.is_flat())

    def test_large_basket_builds_balanced_children_and_consumes_depth(self):
        ex = ETFExchange()
        ex.state["books"]["RITC"]["asks"] = [{"price": 24, "quantity": 10000},
            {"price": 24.05, "quantity": 10000}, {"price": 24.1, "quantity": 10000}]
        ex.state["books"]["RITC"]["bids"] = [{"price": 23.99, "quantity": 1000000}]
        bot = trader(ex, basket=True, quantity=25000,
                     etf_config=etf_policy.ETFConfig(manual_wait_ticks=0),
                     basket_config=etf_basket.BasketConfig(max_quantity=25_000))
        planning = ex.snapshot()
        positions = bot.position_map(planning)
        observed_prices = []
        for quantity in (10_000, 10_000, 5_000):
            selected = next(row for row in bot.basket_analysis(planning, positions, quantity)["opportunities"]
                            if row.get("eligible_after_buffer"))
            self.assertEqual(selected["legs"], [("BULL", -quantity), ("BEAR", -quantity),
                                                ("RITC", quantity)])
            observed_prices.append(selected["executable_prices"]["RITC_usd"])
            for ticker, child in selected["legs"]:
                fill = etf_policy.consume(planning, ticker, child)
                etf_policy.apply_cash(positions, fill)
        self.assertEqual(observed_prices, [24, 24.05, 24.1])

    def test_supervisor_passes_target_child_and_reserve_parameters(self):
        command = worker_command(parse_args(["--case", "etf", "--gross-limit", "300000",
            "--net-limit", "200000", "--quantity", "25000", "--child-size", "5000",
            "--execution-risk-k", "0.75", "--tender-execution-risk-k", "0.12",
            "--tender-fx-risk-k", "0.20", "--decision-log", "data/etf-decisions.jsonl"]))
        for flag, value in (("--quantity", "25000"), ("--child-size", "5000"),
                            ("--execution-risk-k", "0.75"), ("--tender-execution-risk-k", "0.12"),
                            ("--tender-fx-risk-k", "0.2"), ("--decision-log", "data/etf-decisions.jsonl")):
            self.assertEqual(command[command.index(flag) + 1], value)

    def test_market_risk_uses_past_ticks_and_resets_between_heats(self):
        tracker = etf_policy.MarketRisk()
        s = etf_snapshot()
        tracker.observe(s)
        s["case"]["tick"] += 4
        for side in ("bids", "asks"):
            s["books"]["RITC"][side][0]["price"] += .10
        self.assertAlmostEqual(tracker.observe(s)["RITC"], .05)
        s["case"]["tick"] = 0
        self.assertEqual(tracker.observe(s), {})

    def test_preflight_read_recovers_but_post_submission_read_halts(self):
        for unresolved in (False, True):
            client, bot, executor = MagicMock(), MagicMock(), MagicMock()
            executor.unresolved_intent = unresolved
            s = etf_snapshot()
            client.snapshot.side_effect = [s, copy.deepcopy(s), KeyboardInterrupt()]
            bot.step.side_effect = [RITReadError("read unavailable"), {"wait": "recovered"}]
            with patch("sys.argv", ["run.py", "etf", "--source", "api", "--trade", "--watch",
                       "--gross-limit", "300000", "--net-limit", "200000"]), \
                 patch("run.Client", return_value=client), patch("run.Bot", return_value=bot), \
                 patch("run.Executor", return_value=executor), patch("run.StrategyLogger"), \
                 patch("run.time.sleep"), patch("run.load_env_file"), \
                 patch("run.configure_case_environment"), patch("builtins.print"):
                main()
            self.assertEqual(bot.step.call_count, 1 if unresolved else 2)
            executor.order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
