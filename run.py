import argparse
import json
import time
import os
from typing import Any
from models import etf, volatility
from client import Client
from bot import Bot
from execution import Executor


def demo(case: str) -> dict[str, Any]:
    """Build synthetic prices plus enough account metadata to exercise pre-trade rules."""
    snapshot = demo_prices(case)
    snapshot["orders"] = []
    snapshot["case"]["period"] = 1
    snapshot["limits"] = []
    for security in snapshot["securities"]:
        ticker = security["ticker"]
        option = ticker.startswith("RTM") and ticker != "RTM"
        name = "options" if option else "stock"
        if ticker == "USD":
            name = "cash"
        security.update(is_tradeable=True, max_trade_size=100 if option else 10000,
                        limits=[{"name": name, "units": .5 if ticker == "RITC" else 1}])
        if name not in {limit["name"] for limit in snapshot["limits"]}:
            snapshot["limits"].append({"name": name, "gross": 0, "net": 0,
                                       "gross_limit": 2500 if option else 300000,
                                       "net_limit": 1000 if option else 200000})
    return snapshot


def demo_prices(case: str) -> dict[str, Any]:
    """Create deterministic, non-executable quotes for a selected case.

    :param case: ``etf`` or ``volatility``.
    :returns: Synthetic case snapshot.
    """
    if case == "volatility":
        rows = [{"ticker": "RTM", "bid": 49.99, "ask": 50.01, "position": 0}]
        for strike in range(48, 53):
            for kind in ("C", "P"):
                price = volatility.bs(50, strike, 1/12, 0, 0.20, kind)[0]
                rows.append({"ticker": f"RTM{strike}{kind}", "bid": max(0, price-.01),
                             "ask": price+.01, "position": 0})
        return {"case": {"tick": 0, "status": "ACTIVE"}, "securities": rows, "news": []}
    prices = {"BULL": 10, "BEAR": 15, "RITC": 24.8, "USD": 1}
    return {"case": {"tick": 1, "status": "ACTIVE"},
            "securities": [{"ticker": t, "position": 0} for t in prices],
            "books": {t: {"bids": [{"price": p-.01, "quantity": 1000000}],
                           "asks": [{"price": p+.01, "quantity": 1000000}]} for t, p in prices.items()},
            "tenders": []}


def main() -> None:
    """Parse CLI options and run one read-only or explicitly opted-in cycle."""
    parser = argparse.ArgumentParser(description="RIT decision support and opt-in practice trading.")
    parser.add_argument("case", choices=["etf", "volatility"])
    parser.add_argument("--source", choices=["demo", "api", "replay"], default="demo")
    parser.add_argument("--file", help="JSONL snapshot file for replay")
    parser.add_argument("--record", help="Append raw API snapshots to this JSONL file")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--sigma", type=float, help="Annualized volatility assumption, e.g. 0.25")
    parser.add_argument("--rate", type=float, default=0)
    parser.add_argument("--quantity", type=int, default=1000)
    parser.add_argument("--gross-limit", type=int)
    parser.add_argument("--net-limit", type=int)
    parser.add_argument("--plan", action="store_true", help="Report the bot's next action without submitting it")
    parser.add_argument("--trade", action="store_true", help="Submit simulated orders to the configured RIT account")
    parser.add_argument("--flatten-only", action="store_true", help="Close inventory; never open new exposure")
    parser.add_argument("--basket", action="store_true", help="Enable serial ETF basket trades as well as tenders")
    parser.add_argument("--journal", help="Execution journal path; default is data/<case>-<username>-execution.jsonl")
    parser.add_argument("--check", action="store_true", help="Check case, security metadata, orders and limits without trading")
    parser.add_argument("--reconcile", action="store_true", help="Record current account state after checking an interrupted execution")
    args = parser.parse_args()
    if args.case == "volatility" and args.sigma is None and not (args.plan or args.trade or args.check or args.reconcile):
        parser.error("volatility requires --sigma (use current analyst information)")
    if args.trade and args.plan:
        parser.error("Choose either --plan or --trade")
    if (args.trade or args.check or args.reconcile) and args.source != "api":
        parser.error("--trade, --check and --reconcile require --source api")
    if args.trade and (args.check or args.reconcile):
        parser.error("Account checks/reconciliation cannot run together with --trade")
    if args.case == "etf" and (args.plan or args.trade) and not (args.gross_limit and args.net_limit):
        parser.error("ETF bot requires --gross-limit and --net-limit from your session")
    if not 1 <= args.quantity <= 10000:
        parser.error("--quantity must be 1..10000")
    if args.source == "replay" and not args.file:
        parser.error("replay requires --file")
    if args.watch and args.source != "api":
        parser.error("--watch requires --source api")
    client = Client() if args.source == "api" else None
    account = os.environ.get("RIT_USERNAME", "rest").replace("/", "_")
    journal = args.journal or f"data/{args.case}-{account}-execution.jsonl"
    if args.check or args.reconcile:
        snapshot = client.snapshot(args.case, trading=True)
        if args.reconcile:
            Executor.reconcile(journal, snapshot)
        print(json.dumps(snapshot, allow_nan=False))
        return
    executor = Executor(client, journal) if args.trade else None
    bot = Bot(client, executor, case=args.case, sigma=args.sigma, rate=args.rate,
              quantity=args.quantity, gross_limit=args.gross_limit, net_limit=args.net_limit,
              flatten_only=args.flatten_only, basket=args.basket) if args.plan or args.trade else None
    replay = open(args.file) if args.source == "replay" else None
    try:
        while True:
            if replay:
                line = replay.readline()
                if not line:
                    break
                snapshot = json.loads(line)
            else:
                snapshot = client.snapshot(args.case, trading=bool(bot)) if client else demo(args.case)
            if args.record:
                with open(args.record, "a") as output:
                    output.write(json.dumps(snapshot) + "\n")
            if snapshot["case"]["status"] == "ACTIVE":
                result = bot.step(snapshot) if bot else (volatility.analyze(snapshot, args.sigma, args.rate) if args.case == "volatility"
                          else etf.analyze(snapshot, args.quantity, args.gross_limit, args.net_limit))
                print(json.dumps({"case": snapshot["case"], "analysis": result}, allow_nan=False), flush=True)
            else:
                print(json.dumps({"case": snapshot["case"], "analysis": "inactive"}), flush=True)
            if replay:
                continue
            if not args.watch:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if replay:
            replay.close()
        if executor:
            executor.close()


if __name__ == "__main__":
    main()
