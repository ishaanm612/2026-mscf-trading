"""Bounded, evidence-gated staged RITC tender liquidation.

Arrival rates measure new, near-touch order IDs, not total displayed depth or
repeated snapshots of the same orders. They are an estimate, never a promise
that orders will survive until our next child. Frozen-book liquidation remains
the stress exit, and every actual order still needs fresh executable depth.
"""
from __future__ import annotations

import copy
import math
from collections import deque
from typing import Any, Mapping

from models import etf


class LiquidityHistory:
    """Past-only near-touch RITC arrivals, reset on each observed heat boundary."""

    def __init__(self) -> None:
        self.previous = None
        self.intervals = {side: deque(maxlen=10) for side in ("bids", "asks")}
        self.seen: set[int] = set()

    def observe(self, snapshot: Mapping[str, Any]) -> None:
        tick, period = snapshot["case"]["tick"], snapshot["case"].get("period")
        if self.previous and (tick < self.previous[0] or period != self.previous[1]):
            self.__init__()
        if self.previous and tick == self.previous[0]:
            return
        book = snapshot["books"]["RITC"]
        ids = {r["order_id"] for side in self.intervals for r in book[side] if "order_id" in r}
        for side, history in self.intervals.items():
            rows = [r for r in book[side] if r["quantity"] > r.get("quantity_filled", 0)]
            if self.previous:
                dt = tick - self.previous[0]
                # Five cents of near-touch depth; ignore distant new orders
                # that would not support the projected small-child pricing.
                top = (max if side == "bids" else min)((r["price"] for r in rows), default=0)
                new = sum(r["quantity"] - r.get("quantity_filled", 0) for r in rows
                          if "order_id" in r and r["order_id"] not in self.seen
                          and self.previous[0] < r.get("tick", -1) <= tick
                          and abs(r["price"] - top) <= .05 + 1e-9)
                history.append((dt, new / dt))
        self.seen.update(ids)
        self.previous = tick, period

    def estimate(self, snapshot: Mapping[str, Any], side: str) -> dict | None:
        if not self.previous or snapshot["case"].get("period") != self.previous[1]:
            return None
        age = snapshot["case"]["tick"] - self.previous[0]
        history = self.intervals[side]
        if not 0 <= age <= 2 or len(history) < 4 or sum(dt for dt, _ in history) < 12:
            return None
        # Lower quartile, then 50% participation: a few busy ticks cannot
        # justify taking the entire advertised flow for ourselves.
        rate = sorted(value for _, value in history)[(len(history) - 1) // 4] * .5
        if rate <= 0:
            return None
        return {"shares_per_tick": rate, "intervals": len(history),
                "history_ticks": sum(dt for dt, _ in history), "observation_age_ticks": age,
                "participation": .5, "near_touch_band_usd": .05}


def staged_direct(snapshot: Mapping[str, Any], positions: Mapping[str, float],
                  direct: Mapping[str, Any], config: Any, evidence: Mapping[str, Any]) -> dict:
    """Half-credit for replenishment, with a fully priceable frozen-book fallback.

    Only a pure RITC inventory is supported. Every projected child is halfway
    between today's small-child VWAP (plus a five-cent arrival-band haircut)
    and its actual depleted-book VWAP. We never reset depth and call it certain.
    """
    from models.etf_policy import apply_cash, child_cap, split

    if any(positions.get(t, 0) for t in ("BULL", "BEAR")) or not positions.get("RITC"):
        raise ValueError("staging requires a RITC-only portfolio")
    if len(direct["fills"]) < 2:
        raise ValueError("one child does not need staged pricing")
    side = "SELL" if positions["RITC"] > 0 else "BUY"
    cap = max(abs(f["quantity"]) for f in direct["fills"])
    spacing = max(config.ticks_per_action, math.ceil(cap / evidence["shares_per_tick"]))
    p, fills, stages = dict(positions), [], [dict(positions)]
    for i, frozen in enumerate(direct["fills"]):
        q = frozen["quantity"]
        small = etf.vwap(snapshot["books"]["RITC"], side, abs(q))
        # New near-touch arrivals may be five cents worse than the current
        # touch. Do not advertise their full volume at the best observed price.
        adverse = small + (.05 if q > 0 else -.05)
        price = frozen["price"] if i == 0 else ((min if q > 0 else max)(
            frozen["price"], (frozen["price"] + adverse) / 2))
        fill = {"ticker": "RITC", "quantity": q, "price": price,
                "cashflow": etf.trade_cashflow("RITC", q, price),
                "fill_tick_offset": config.ticks_per_action + i * spacing}
        fills.append(fill)
        apply_cash(p, fill)
        stages.append(dict(p))
    fx = etf.net_usd_value_cad(snapshot, p.get("USD", 0))
    fx_children = split(-round(p.get("USD", 0)), child_cap(snapshot, "USD", config))
    horizon = fills[-1]["fill_tick_offset"] + config.ticks_per_action * len(fx_children)
    if horizon > config.tender_max_unwind_ticks:
        raise ValueError("observed replenishment is too slow for staged unwind")
    ticks = math.ceil(horizon + config.end_buffer_ticks)
    return {"name": "DIRECT_STAGED", "converter": None, "blocks": 0,
            "preparation": [], "fills": fills, "stages": stages,
            "fx": fx, "fx_children": fx_children, "net_usd": p.get("USD", 0),
            "total_cad": p.get("CAD", 0) - positions.get("CAD", 0) + fx["cad_value"],
            "budget": {"actions": len(fills) + len(fx_children), "reserve_ticks": ticks,
                       "latest_accept_tick": 298 - ticks},
            "staging": {"spacing_ticks": spacing, "evidence": dict(evidence),
                        "impact_recovery_fraction": .5, "max_wait_ticks": 6,
                        "fallback_loss_cap_cad": abs(positions["RITC"]) * config.tender_max_fallback_loss},
            "fallback_route": copy.deepcopy(direct)}
