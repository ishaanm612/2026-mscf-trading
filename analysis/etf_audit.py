"""Read-only ETF decision replay; this does not simulate fills or realized P&L.

Example: python3 -m analysis.etf_audit data/etf-snapshots.jsonl --flat --limit 1005
--flat removes recorded inventory and counters to isolate offer eligibility.
Market risk always uses earlier observations from the same heat only.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter

from bot import Bot
from models.etf_policy import ETFConfig


def audit(path: str, *, flat: bool = False, limit: int | None = None,
          execution_k: float = ETFConfig.execution_k, fx_k: float = ETFConfig.fx_k,
          staged_tenders: bool = True,
          gross_limit: int = 300000, net_limit: int = 200000) -> dict:
    bot = Bot(None, case="etf", gross_limit=gross_limit, net_limit=net_limit,
              etf_config=ETFConfig(execution_k=execution_k, fx_k=fx_k, staged_tenders=staged_tenders))
    decisions, reasons, routes = Counter(), Counter(), Counter()
    offers, accepted = set(), set()
    heat, previous, count = 0, None, 0
    with open(path) as source:
        for index, line in enumerate(source):
            if limit is not None and index >= limit:
                break
            snapshot = json.loads(line)
            marker = snapshot["case"]
            if previous is None or marker["tick"] < previous["tick"] or marker.get("period") != previous.get("period"):
                heat += 1
            previous = marker
            count += 1
            bot.etf_sigmas = bot.etf_market_risk.observe(snapshot)
            bot.etf_liquidity.observe(snapshot)
            if flat:
                for security in snapshot["securities"]:
                    security["position"] = 0
                for row in snapshot["limits"]:
                    row["gross"] = row["net"] = 0
                snapshot["orders"] = []
            for assessment in bot.tender_assessments(snapshot, bot.position_map(snapshot), {}):
                key = (heat, assessment["tender_id"])
                offers.add(key)
                decisions[assessment["decision"]] += 1
                reasons[assessment["reason"]] += 1
                if assessment["decision"] == "ACCEPT":
                    accepted.add(key)
                    routes[assessment["selected_route"]["name"]] += 1
    return {"snapshots": count, "distinct_offers": len(offers), "distinct_eligible_offers": len(accepted),
            "observations_by_decision": dict(decisions), "reasons": dict(reasons),
            "eligible_routes": dict(routes), "counterfactual_flat_account": flat,
            "execution_k": execution_k, "fx_k": fx_k,
            "staged_tenders": staged_tenders,
            "note": "Eligibility replay only; no simulated fills, accepted tenders or realized P&L."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--flat", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--execution-risk-k", type=float, default=ETFConfig.execution_k)
    parser.add_argument("--fx-risk-k", type=float, default=ETFConfig.fx_k)
    parser.add_argument("--no-staged-tenders", action="store_true")
    parser.add_argument("--gross-limit", type=int, default=300000)
    parser.add_argument("--net-limit", type=int, default=200000)
    args = parser.parse_args()
    print(json.dumps(audit(args.path, flat=args.flat, limit=args.limit,
                           execution_k=args.execution_risk_k, fx_k=args.fx_risk_k,
                           staged_tenders=not args.no_staged_tenders,
                           gross_limit=args.gross_limit, net_limit=args.net_limit), indent=2))


if __name__ == "__main__":
    main()
