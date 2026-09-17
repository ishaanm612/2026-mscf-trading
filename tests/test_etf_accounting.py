"""ETF accounting tests with an executable CAD/USD cashflow simulator."""
import copy
import unittest

from bot import Bot
from models import etf


def etf_snapshot() -> dict:
    prices = {"BULL": 10.0, "BEAR": 15.0, "RITC": 24.8, "USD": 1.0}
    state = {
        "case": {"tick": 1, "period": 1, "status": "ACTIVE"},
        "orders": [],
        "securities": [],
        "books": {ticker: {
            "bids": [{"price": price - .01, "quantity": 1_000_000}],
            "asks": [{"price": price + .01, "quantity": 1_000_000}],
        } for ticker, price in prices.items()},
        "tenders": [],
        "limits": [
            {"name": "stock", "gross": 0, "net": 0, "gross_limit": 300_000, "net_limit": 200_000},
            {"name": "cash", "gross": 0, "net": 0, "gross_limit": 10_000_000, "net_limit": 10_000_000},
        ],
    }
    for ticker in prices:
        name = "cash" if ticker == "USD" else "stock"
        state["securities"].append({
            "ticker": ticker,
            "position": 0,
            "is_tradeable": True,
            "max_trade_size": 2_500_000 if ticker == "USD" else 10_000,
            "limits": [{"name": name, "units": .5 if ticker == "RITC" else 1}],
        })
    return state


class ETFExchange:
    """Immediate-fill ETF simulator with actual CAD/USD cash accounting.

    Security positions track BULL/BEAR/RITC inventory and the account's USD cash
    balance. ``cad_cash`` is the realized CAD cash account. Equity commissions,
    FX crossings, tenders, and manual converter fees all mutate those balances,
    allowing tests to assert final realized P&L rather than only positions.
    """

    def __init__(self) -> None:
        self.state = etf_snapshot()
        self.cad_cash = 0.0
        self.commission_cad = 0.0
        self.commission_usd = 0.0
        self.orders: list[dict] = []
        self.events: list[dict] = []
        self.after_order = None

    def snapshot(self, *args, **kwargs):
        return copy.deepcopy(self.state)

    def get(self, endpoint):
        if endpoint == "case":
            return self.state["case"].copy()
        if endpoint == "securities":
            return copy.deepcopy(self.state["securities"])
        raise KeyError(endpoint)

    def _security(self, ticker: str) -> dict:
        return next(row for row in self.state["securities"] if row["ticker"] == ticker)

    def _recompute_limits(self) -> None:
        for limit in self.state["limits"]:
            terms = []
            for security in self.state["securities"]:
                binding = next((item for item in security["limits"] if item["name"] == limit["name"]), None)
                if binding:
                    terms.append(security["position"] / binding["units"])
            limit["gross"] = sum(abs(value) for value in terms)
            limit["net"] = sum(terms)

    def move(self, ticker: str, quantity: float) -> None:
        self._security(ticker)["position"] += quantity
        self._recompute_limits()

    def order(self, ticker: str, quantity: int) -> dict:
        action = "BUY" if quantity > 0 else "SELL"
        price = etf.vwap(self.state["books"][ticker], action, abs(quantity))
        if ticker in {"BULL", "BEAR"}:
            commission = abs(quantity) * etf.EQUITY_FEE
            self.cad_cash += -quantity * price - commission
            self.commission_cad += commission
            self.move(ticker, quantity)
        elif ticker == "RITC":
            commission = abs(quantity) * etf.EQUITY_FEE
            self.move("USD", -quantity * price - commission)
            self.commission_usd += commission
            self.move(ticker, quantity)
        elif ticker == "USD":
            self.cad_cash += -quantity * price
            self.move("USD", quantity)
        else:
            raise KeyError(ticker)
        fill = {"order_id": len(self.orders) + 1, "quantity_filled": abs(quantity),
                "status": "TRANSACTED", "vwap": price}
        self.orders.append({"ticker": ticker, "quantity": quantity, **fill})
        self.events.append({"event": "order", "ticker": ticker, "quantity": quantity,
                            "price": price})
        if self.after_order:
            self.after_order(self, len(self.orders))
        return fill

    def tender(self, offer: dict, position_before: int) -> None:
        quantity = int(offer["quantity"]) * (1 if offer["action"] == "BUY" else -1)
        self.move("RITC", quantity)
        self.move("USD", -quantity * float(offer["price"]))
        self.state["tenders"] = [row for row in self.state["tenders"]
                                  if row["tender_id"] != offer["tender_id"]]
        self.events.append({"event": "tender", "tender_id": offer["tender_id"],
                            "quantity": quantity, "price": offer["price"]})

    def converter(self, name: str, blocks: int) -> None:
        units = blocks * etf.CONVERTER_BLOCK
        if name == "ETF-Redemption":
            self.move("RITC", -units)
            self.move("BULL", units)
            self.move("BEAR", units)
        elif name == "ETF-Creation":
            self.move("BULL", -units)
            self.move("BEAR", -units)
            self.move("RITC", units)
        else:
            raise ValueError(name)
        self.move("USD", -blocks * etf.CONVERTER_COST_USD)
        self.events.append({"event": "converter", "name": name, "blocks": blocks})

    def positions(self) -> dict[str, float]:
        return {row["ticker"]: row["position"] for row in self.state["securities"]}

    def is_flat(self) -> bool:
        return all(abs(value) < 1e-9 for value in self.positions().values())

    @property
    def realized_pnl_cad(self) -> float:
        if not self.is_flat():
            raise AssertionError("realized P&L requested before all CAD/USD exposures are flat")
        return self.cad_cash


class ETFAccounting(unittest.TestCase):
    def test_tender_reserve_is_material_and_scales_with_size(self):
        small = etf_snapshot()
        large = etf_snapshot()
        small_offer = {"tender_id": 1, "ticker": "RITC", "action": "BUY",
                       "quantity": 1_000, "price": 20.0, "is_fixed_bid": True}
        large_offer = {**small_offer, "tender_id": 2, "quantity": 50_000}
        small_report = etf.tender_opportunity(small, small_offer)
        large_report = etf.tender_opportunity(large, large_offer)
        # At the reduced tender weights a tiny calm-market route binds on
        # the profit floor; larger multi-child routes still need more reserve.
        self.assertEqual(small_report["liquidation_reserve_cad"], 2.50)
        self.assertGreater(large_report["liquidation_reserve_cad"],
                           50 * small_report["liquidation_reserve_cad"])
        direct = next(row for row in large_report["routes"] if row["name"] == "DIRECT")
        self.assertEqual(direct["reserve"]["child_orders"], 5)

    def test_converter_uses_resulting_net_usd_and_optimizes_block_count(self):
        snapshot = etf_snapshot()
        for row in snapshot["securities"]:
            if row["ticker"] == "RITC":
                row["position"] = 78_000
            elif row["ticker"] == "USD":
                # Tender purchase cash is part of the route economics.
                row["position"] = -1_560_000
        snapshot["books"]["USD"]["bids"][0]["quantity"] = 5_000_000
        snapshot["books"]["USD"]["asks"][0]["quantity"] = 5_000_000
        rec = next(row for row in etf.manual_converter_opportunities(snapshot)
                   if row["converter"] == "ETF-Redemption")
        self.assertEqual(rec["available_blocks"], 7)
        self.assertEqual(rec["blocks"], 2)
        self.assertEqual(rec["evaluated_block_counts"], list(range(1, 8)))
        self.assertGreater(rec["estimated_total_advantage_cad"], 0)
        self.assertLess(rec["resulting_net_usd_before_fx"], 0)

    def test_basket_entry_does_not_execute_a_gross_fx_round_trip(self):
        snapshot = etf_snapshot()
        report = etf.basket_opportunity(snapshot, 1, 1_000, 300_000, 200_000)
        self.assertIsNone(report["fx_leg"])
        self.assertIn("No gross FX trade", report["fx_treatment"])
        self.assertAlmostEqual(report["edge_cad_per_unit"], .11)

    def test_tender_round_tracks_cash_commission_fx_and_realized_pnl(self):
        exchange = ETFExchange()
        exchange.state["tenders"] = [{"tender_id": 7, "ticker": "RITC", "action": "BUY",
                                      "is_fixed_bid": True, "price": 20.0,
                                      "quantity": 2_000, "expires": 30}]
        bot = Bot(exchange, exchange, case="etf", gross_limit=300_000, net_limit=200_000)
        first = bot.step(exchange.snapshot())
        self.assertEqual(first["tender_id"], 7)
        self.assertEqual(exchange.positions()["USD"], -40_000)
        second = bot.step(exchange.snapshot())
        self.assertEqual((second["ticker"], second["quantity"]), ("RITC", -2_000))
        self.assertAlmostEqual(exchange.commission_usd, 40.0)
        third = bot.step(exchange.snapshot())
        self.assertEqual(third["ticker"], "USD")
        self.assertTrue(exchange.is_flat())
        self.assertAlmostEqual(exchange.realized_pnl_cad, 9_444.60, places=2)

    def test_manual_converter_simulator_tracks_fee_and_final_realized_pnl(self):
        exchange = ETFExchange()
        exchange.state["tenders"] = [{"tender_id": 8, "ticker": "RITC", "action": "BUY",
                                      "is_fixed_bid": True, "price": 20.0,
                                      "quantity": 10_000, "expires": 30}]
        bot = Bot(exchange, exchange, case="etf", gross_limit=300_000, net_limit=200_000)
        self.assertEqual(bot.step(exchange.snapshot())["tender_id"], 8)
        # Direct RITC liquidation deteriorates after the tender, making the
        # manual redemption route the better executable unwind.
        exchange.state["books"]["RITC"]["bids"] = [{"price": 24.50, "quantity": 1_000_000}]
        manual = bot.step(exchange.snapshot())["manual_converter"]
        self.assertEqual((manual["converter"], manual["blocks"]), ("ETF-Redemption", 1))
        exchange.converter(manual["converter"], manual["blocks"])
        self.assertEqual(exchange.positions()["USD"], -201_500)
        self.assertEqual(exchange.events[-1], {"event": "converter", "name": "ETF-Redemption", "blocks": 1})
        for _ in range(4):
            if exchange.is_flat():
                break
            bot.step(exchange.snapshot())
        self.assertTrue(exchange.is_flat())
        self.assertAlmostEqual(exchange.commission_cad, 400.0)
        self.assertAlmostEqual(exchange.realized_pnl_cad, 45_885.0, places=2)

    def test_serial_basket_reprices_and_reverses_after_bad_first_fill(self):
        exchange = ETFExchange()
        # C$0.50+ parity gap is large enough to clear entry, exit and serial
        # risk reserves before the first leg is filled.
        exchange.state["books"]["RITC"] = {
            "bids": [{"price": 24.39, "quantity": 1_000_000}],
            "asks": [{"price": 24.40, "quantity": 1_000_000}],
        }
        def shock(ex: ETFExchange, count: int) -> None:
            if count == 1:
                ex.state["books"]["BEAR"]["bids"] = [{"price": 10.0, "quantity": 1_000_000}]
        exchange.after_order = shock
        bot = Bot(exchange, exchange, case="etf", basket=True, quantity=1_000,
                  gross_limit=300_000, net_limit=200_000)
        result = bot.step(exchange.snapshot())
        self.assertTrue(result["aborted"])
        self.assertFalse(result["serial_reprice"]["finish"])
        self.assertEqual([row["ticker"] for row in exchange.orders], ["BULL", "BULL"])
        self.assertTrue(exchange.is_flat())
        self.assertLess(exchange.realized_pnl_cad, 0)  # one spread + commissions paid to escape

    def test_existing_basket_holds_through_tick_250_and_exits_on_positive_close_now_pnl(self):
        exchange = ETFExchange()
        exchange.state["books"]["RITC"] = {
            "bids": [{"price": 24.39, "quantity": 1_000_000}],
            "asks": [{"price": 24.40, "quantity": 1_000_000}],
        }
        exchange.state["case"]["tick"] = 230
        bot = Bot(exchange, exchange, case="etf", basket=True, quantity=1_000,
                  gross_limit=300_000, net_limit=200_000)
        entry = bot.step(exchange.snapshot())
        self.assertEqual(entry["reason"], "basket filled")
        self.assertNotIn("USD", [row["ticker"] for row in exchange.orders])
        exchange.state["case"]["tick"] = 250
        hold = bot.step(exchange.snapshot())
        self.assertEqual(hold["wait"], "hold convergence basket")
        self.assertIsNotNone(bot.held_basket)
        # Convergence favorable to long RITC / short CAD basket.
        exchange.state["books"]["BULL"] = {"bids": [{"price": 9.80, "quantity": 1_000_000}],
                                             "asks": [{"price": 9.81, "quantity": 1_000_000}]}
        exchange.state["books"]["BEAR"] = {"bids": [{"price": 14.80, "quantity": 1_000_000}],
                                             "asks": [{"price": 14.81, "quantity": 1_000_000}]}
        exchange.state["books"]["RITC"] = {"bids": [{"price": 25.10, "quantity": 1_000_000}],
                                             "asks": [{"price": 25.11, "quantity": 1_000_000}]}
        exit_action = bot.step(exchange.snapshot())
        self.assertEqual(exit_action["reason"], "unwind inventory")
        self.assertIsNone(bot.held_basket)
        for _ in range(5):
            if exchange.is_flat():
                break
            bot.step(exchange.snapshot())
        self.assertTrue(exchange.is_flat())
        self.assertGreater(exchange.realized_pnl_cad, 0)

    def test_new_basket_is_blocked_when_dynamic_window_cannot_fit_at_tick_285(self):
        exchange = ETFExchange()
        exchange.state["books"]["RITC"] = {
            "bids": [{"price": 24.39, "quantity": 1_000_000}],
            "asks": [{"price": 24.40, "quantity": 1_000_000}],
        }
        exchange.state["case"]["tick"] = 285
        bot = Bot(exchange, exchange, case="etf", basket=True, quantity=1_000,
                  gross_limit=300_000, net_limit=200_000)
        result = bot.step(exchange.snapshot())
        self.assertNotEqual(result.get("reason"), "basket filled")
        self.assertEqual(exchange.orders, [])


if __name__ == "__main__":
    unittest.main()
