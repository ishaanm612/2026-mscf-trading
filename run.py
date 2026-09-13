import argparse
import json
import time
import etf
import volatility
from client import Client


def demo(case):
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


def main():
    parser = argparse.ArgumentParser(description="Read-only RIT decision support; never submits orders.")
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
    args = parser.parse_args()
    if args.case == "volatility" and args.sigma is None:
        parser.error("volatility requires --sigma (use current analyst information)")
    if args.source == "replay" and not args.file:
        parser.error("replay requires --file")
    if args.watch and args.source != "api":
        parser.error("--watch requires --source api")
    client = Client() if args.source == "api" else None
    replay = open(args.file) if args.source == "replay" else None
    try:
        while True:
            if replay:
                line = replay.readline()
                if not line:
                    break
                snapshot = json.loads(line)
            else:
                snapshot = client.snapshot(args.case) if client else demo(args.case)
            if args.record:
                with open(args.record, "a") as output:
                    output.write(json.dumps(snapshot) + "\n")
            if snapshot["case"]["status"] == "ACTIVE":
                result = (volatility.analyze(snapshot, args.sigma, args.rate) if args.case == "volatility"
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


if __name__ == "__main__":
    main()
