"""Walk-forward tender-policy ablation on recorded ETF books.

This is an offline *counterfactual* fill simulation. It accepts a tender at its
recorded fixed price, crosses each planned RITC child against the first
subsequent recorded book at/after the scheduled tick, and converts final USD
at that book. It deliberately does not model manual conversions, queue
position, other participants reacting to our order, or concurrent offers.
Consequently it is useful for comparing conservative direct-route settings,
not for claiming live P&L or fitting a production optimum.

Run from the repository root:
    python3 -m analysis.etf_ablation data/etf-snapshots.jsonl
"""
from __future__ import annotations

import argparse
import copy
import json
import math
from collections import Counter
from dataclasses import dataclass
from dataclasses import replace
from statistics import mean
from typing import Any, Iterable, Mapping

from models import etf, etf_policy
from models.etf_liquidity import LiquidityHistory


@dataclass(frozen=True)
class Settings:
    """Direct tender policy settings evaluated without future book data."""

    execution_k: float
    fx_k: float
    active_intervals: int
    participation: float
    fallback_loss: float


class TunableLiquidity(LiquidityHistory):
    """Same past-only arrival observations with explicit ablation gates."""

    def __init__(self, active_intervals: int, participation: float) -> None:
        super().__init__()
        self.active_intervals = active_intervals
        self.participation = participation

    def estimate(self, snapshot: Mapping[str, Any], side: str) -> dict | None:
        if not self.previous or snapshot["case"].get("period") != self.previous[1]:
            return None
        age = snapshot["case"]["tick"] - self.previous[0]
        values = [value for _, value in self.intervals[side] if value > 0]
        if not 0 <= age <= 2 or len(self.intervals[side]) < 4 or sum(dt for dt, _ in self.intervals[side]) < 12:
            return None
        if len(values) < self.active_intervals:
            return None
        # Median of nonzero intervals avoids treating intermittent arrivals as
        # literally zero liquidity, while the participation cap remains below
        # the observed flow. This is the parameter being ablated, not a claim
        # that all future displayed depth is ours.
        rate = sorted(values)[(len(values) - 1) // 2] * self.participation
        if rate <= 0:
            return None
        return {"shares_per_tick": rate, "intervals": len(self.intervals[side]),
                "active_intervals": len(values), "history_ticks": sum(dt for dt, _ in self.intervals[side]),
                "observation_age_ticks": age, "participation": self.participation,
                "near_touch_band_usd": .05, "estimator": "nonzero-median"}


def heats(path: str) -> list[list[dict]]:
    """Load complete recorded heats, retaining only active snapshots with books."""
    result: list[list[dict]] = []
    current: list[dict] = []
    previous: Mapping[str, Any] | None = None
    with open(path) as source:
        for line in source:
            snapshot = json.loads(line)
            case = snapshot.get("case", {})
            if not snapshot.get("books") or case.get("status") != "ACTIVE":
                continue
            if previous and (case["tick"] < previous["tick"] or case.get("period") != previous.get("period")):
                if current:
                    result.append(current)
                current = []
            current.append(snapshot)
            previous = case
    if current:
        result.append(current)
    return result


def flat(snapshot: Mapping[str, Any]) -> dict:
    """Return a copied account-free snapshot for independent-offer scoring."""
    value = copy.deepcopy(snapshot)
    for security in value["securities"]:
        security["position"] = 0
    for limit in value.get("limits", []):
        limit["gross"] = limit["net"] = 0
    value["orders"] = []
    return value


def later(heat: list[dict], index: int, tick: float) -> dict | None:
    """First recorded book at or after a planned child tick."""
    for snapshot in heat[index:]:
        if snapshot["case"]["tick"] >= tick:
            return snapshot
    return None


def execute_direct(heat: list[dict], index: int, offer: Mapping[str, Any], route: Mapping[str, Any]) -> dict:
    """Apply a direct route to later recorded books without looking ahead at entry.

    A staged child whose later quote misses its model price is allowed its
    documented six-tick wait, then uses the current quote as the route's direct
    fallback. The simulated action still consumes only one current snapshot's
    displayed depth and never assumes a fill at the planned forecast price.
    """
    entry = heat[index]
    signed = int(offer["quantity"]) * (1 if offer["action"] == "BUY" else -1)
    positions = {ticker: 0.0 for ticker in (*etf.WEIGHTS, "CAD", "USD")}
    positions["RITC"] = signed
    positions["USD"] = -signed * float(offer["price"])
    fills, last = [], entry
    staged = route["name"] == "DIRECT_STAGED"
    for planned in route["fills"]:
        target = entry["case"]["tick"] + planned.get("fill_tick_offset", 0)
        actual = later(heat, index, target)
        if actual is None:
            return {"status": "unfilled", "reason": "no later recorded book"}
        try:
            row = etf.executable_trade_cashflow(actual, planned["ticker"], planned["quantity"])
        except (ValueError, KeyError) as error:
            return {"status": "unfilled", "reason": f"no executable child: {error}"}
        if staged:
            favorable = row["price"] <= planned["price"] if row["quantity"] > 0 else row["price"] >= planned["price"]
            if not favorable:
                retry = later(heat, index, target + route["staging"]["max_wait_ticks"])
                if retry is None:
                    return {"status": "unfilled", "reason": "staged child missed without fallback book"}
                try:
                    row = etf.executable_trade_cashflow(retry, planned["ticker"], planned["quantity"])
                    actual = retry
                except (ValueError, KeyError) as error:
                    return {"status": "unfilled", "reason": f"staged fallback has no depth: {error}"}
        etf_policy.apply_cash(positions, row)
        fills.append({**row, "tick": actual["case"]["tick"]})
        last = actual
    try:
        fx = etf.net_usd_value_cad(last, positions["USD"])
    except (ValueError, KeyError) as error:
        return {"status": "unfilled", "reason": f"no final FX depth: {error}"}
    return {"status": "filled", "pnl_cad": positions["CAD"] + fx["cad_value"],
            "entry_tick": entry["case"]["tick"], "exit_tick": last["case"]["tick"],
            "fills": fills, "route": route["name"]}


def direct_report(snapshot: Mapping[str, Any], offer: Mapping[str, Any], config: etf_policy.ETFConfig,
                  sigmas: Mapping[str, float], liquidity: TunableLiquidity) -> dict:
    """Fast flat-account direct-route scorer used by the ablation.

    Converter routes are intentionally excluded: their manual execution is not
    present in the recordings, so fabricating immediate conversions would give
    an invalid P&L comparison. Production still evaluates those routes.
    """
    quantity = int(offer["quantity"])
    signed = quantity if offer["action"] == "BUY" else -quantity
    after = {ticker: 0.0 for ticker in (*etf.WEIGHTS, "CAD", "USD")}
    after["RITC"] = signed
    after["USD"] = -signed * float(offer["price"])
    try:
        direct = etf_policy.route_plan(snapshot, after, config)
    except (ValueError, KeyError) as error:
        return {"skip": str(error)}
    candidates = [direct]
    evidence = liquidity.estimate(snapshot, "bids" if signed > 0 else "asks")
    if config.staged_tenders and evidence:
        from models.etf_liquidity import staged_direct
        try:
            staged = staged_direct(snapshot, after, direct, config, evidence)
            fallback = direct["total_cad"]
            staged["staging"]["fallback_profit_cad"] = fallback
            if fallback >= -staged["staging"]["fallback_loss_cap_cad"]:
                candidates.append(staged)
        except (ValueError, KeyError):
            pass
    tender_config = replace(config, execution_k=config.tender_execution_k, fx_k=config.tender_fx_k)
    for route in candidates:
        allowance = etf_policy.reserve(snapshot, route, tender_config, sigmas)
        route["profit_cad"] = route["total_cad"]
        route["reserve"] = allowance
        route["minimum_profit_cad"] = max(config.min_profit_cad_per_share * quantity,
                                           allowance["reserve_cad"])
        route["surplus_cad"] = route["profit_cad"] - route["minimum_profit_cad"]
    return {"selected_route": max(candidates, key=lambda route: route["surplus_cad"])}


def evaluate(heat: list[dict], heat_id: int, settings: Settings) -> list[dict]:
    """Score each tender once using only preceding observations in its heat."""
    config = etf_policy.ETFConfig(tender_execution_k=settings.execution_k,
                                  tender_fx_k=settings.fx_k,
                                  tender_max_fallback_loss=settings.fallback_loss,
                                  manual_wait_ticks=0)
    risk = etf_policy.MarketRisk()
    liquidity = TunableLiquidity(settings.active_intervals, settings.participation)
    seen: set[int] = set()
    results = []
    for index, original in enumerate(heat):
        sigmas = risk.observe(original)
        liquidity.observe(original)
        snapshot = flat(original)
        for offer in snapshot.get("tenders", []):
            tender_id = int(offer["tender_id"])
            if tender_id in seen:
                continue
            seen.add(tender_id)
            report = direct_report(snapshot, offer, config, sigmas, liquidity)
            route = report.get("selected_route")
            selected = route and route["name"] in {"DIRECT", "DIRECT_STAGED"}
            accepted = bool(selected and route["surplus_cad"] >= 0
                            and snapshot["case"]["tick"] <= route["budget"]["latest_accept_tick"])
            item = {"heat": heat_id, "tender_id": tender_id, "tick": snapshot["case"]["tick"],
                    "route": route["name"] if route else None, "accepted": accepted,
                    "modeled_surplus_cad": route["surplus_cad"] if route else None,
                    "staged_unavailable": report.get("skip")}
            if accepted:
                item.update(execute_direct(heat, index, offer, route))
            results.append(item)
    return results


def summary(rows: Iterable[Mapping[str, Any]], settings: Settings) -> dict:
    """Summarize independent-tender counterfactual P&L and tail behavior."""
    accepted = [r for r in rows if r["accepted"]]
    filled = [r for r in accepted if r.get("status") == "filled"]
    pnl = [r["pnl_cad"] for r in filled]
    ordered = sorted(pnl)
    return {"settings": settings.__dict__, "offers": len(list(rows)) if not isinstance(rows, list) else len(rows),
            "accepted": len(accepted), "filled": len(filled), "unfilled": len(accepted) - len(filled),
            "total_pnl_cad": sum(pnl), "mean_pnl_cad": mean(pnl) if pnl else 0.0,
            "win_rate": sum(x > 0 for x in pnl) / len(pnl) if pnl else 0.0,
            "worst_pnl_cad": min(pnl) if pnl else 0.0,
            "median_pnl_cad": ordered[len(ordered) // 2] if ordered else 0.0,
            "staged_accepted": sum(r.get("route") == "DIRECT_STAGED" for r in accepted)}


def grid() -> Iterable[Settings]:
    for execution_k in (.05, .10, .15, .20):
        for fx_k in (.10, .25, .40):
            for active_intervals in (1, 2, 3):
                for participation in (.25, .50, .75):
                    for fallback_loss in (.05, .10, .15, .20):
                        yield Settings(execution_k, fx_k, active_intervals, participation, fallback_loss)


def rank(rows: list[dict]) -> tuple:
    """Prefer P&L, then positive outlier resistance and actual sample size."""
    return (rows["total_pnl_cad"], rows["worst_pnl_cad"], rows["filled"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--train-heats", type=int, default=9,
                        help="Chronological training heats; later heats are held out")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    all_heats = heats(args.path)
    if not 1 <= args.train_heats < len(all_heats):
        parser.error("--train-heats must leave at least one held-out heat")
    reports = []
    for settings in grid():
        train_rows = [row for heat_id, heat in enumerate(all_heats[:args.train_heats])
                      for row in evaluate(heat, heat_id, settings)]
        test_rows = [row for heat_id, heat in enumerate(all_heats[args.train_heats:], args.train_heats)
                     for row in evaluate(heat, heat_id, settings)]
        reports.append({"train": summary(train_rows, settings), "test": summary(test_rows, settings)})
    reports.sort(key=lambda r: rank(r["train"]), reverse=True)
    output = {"heats": len(all_heats), "train_heats": args.train_heats,
              "test_heats": len(all_heats) - args.train_heats,
              "route_scope": "independent flat-account DIRECT/DIRECT_STAGED tenders only",
              "assumptions": "recorded-book market fills; no manual conversion, queue impact, concurrent offers or market reaction",
              "top_by_train": reports[:args.top]}
    print(json.dumps(output, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
