"""ETF execution plans and explicit uncertainty allowances (CAD throughout).

Quotes, depth and commissions are costs, not uncertainty. The reserve adds
only prospective price movement over the scheduled execution horizon. Default
coefficients are strategy assumptions, not fitted guarantees of profitability.
"""
from __future__ import annotations

import copy
import math
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Mapping

from models import etf


@dataclass(frozen=True)
class ETFConfig:
    execution_k: float = 0.25
    fx_k: float = 0.5
    tender_execution_k: float = 0.15
    tender_fx_k: float = 0.25
    equity_sigma_floor: float = 0.005  # quote-currency dollars / sqrt(tick)
    fx_sigma_floor: float = 0.0001  # CAD/USD / sqrt(tick)
    ticks_per_action: float = 3.0
    manual_wait_ticks: int = 8
    end_buffer_ticks: int = 5
    min_profit_cad_per_share: float = 0.0025
    child_size: int = 10_000
    staged_tenders: bool = True
    tender_max_fallback_loss: float = 0.10  # CAD/share, frozen-book stress scenario
    tender_max_unwind_ticks: int = 60
    staged_min_active_intervals: int = 2
    staged_participation: float = 0.50

    def __post_init__(self) -> None:
        values = (self.execution_k, self.fx_k, self.tender_execution_k, self.tender_fx_k, self.equity_sigma_floor,
                  self.fx_sigma_floor, self.min_profit_cad_per_share, self.tender_max_fallback_loss,
                  self.staged_participation)
        if any(not math.isfinite(x) or x < 0 for x in values):
            raise ValueError("ETF reserve parameters must be finite and nonnegative")
        if not math.isfinite(self.ticks_per_action) or self.ticks_per_action <= 0:
            raise ValueError("ETF action time must be positive")
        if not 1 <= self.child_size <= 10_000 or self.manual_wait_ticks < 0 or self.end_buffer_ticks < 0:
            raise ValueError("Invalid ETF child size or deadline configuration")
        if self.tender_max_unwind_ticks < 1:
            raise ValueError("Tender unwind horizon must be positive")
        if (isinstance(self.staged_min_active_intervals, bool)
                or not isinstance(self.staged_min_active_intervals, int)
                or self.staged_min_active_intervals < 1):
            raise ValueError("Staged tender active-interval count must be a positive integer")


class MarketRisk:
    """Trailing RMS midpoint moves per sqrt(tick), with no future observations."""

    def __init__(self) -> None:
        self.previous = None
        self.moves = {t: deque(maxlen=30) for t in (*etf.WEIGHTS, "USD")}

    def observe(self, snapshot: Mapping[str, Any]) -> dict[str, float]:
        tick, period = snapshot["case"]["tick"], snapshot["case"].get("period")
        prices = {}
        for ticker in self.moves:
            try:
                prices[ticker] = (etf.vwap(snapshot["books"][ticker], "BUY", 1)
                                  + etf.vwap(snapshot["books"][ticker], "SELL", 1)) / 2
            except (ValueError, KeyError):
                pass
        if self.previous:
            old_tick, old_period, old_prices = self.previous
            if period != old_period or tick < old_tick:
                for moves in self.moves.values():
                    moves.clear()
            elif tick > old_tick:
                for ticker, price in prices.items():
                    if ticker in old_prices:
                        self.moves[ticker].append((price - old_prices[ticker]) ** 2 / (tick - old_tick))
            else:
                return self.sigmas()
        self.previous = tick, period, prices
        return self.sigmas()

    def sigmas(self) -> dict[str, float]:
        return {ticker: math.sqrt(sum(values) / len(values))
                for ticker, values in self.moves.items() if values}


def child_cap(snapshot: Mapping[str, Any], ticker: str, config: ETFConfig) -> int:
    security = next(row for row in snapshot["securities"] if row["ticker"] == ticker)
    cap = min(2_500_000 if ticker == "USD" else config.child_size,
              int(security.get("max_trade_size", 0)))
    if cap <= 0:
        raise ValueError(f"{ticker} has no legal child size")
    return cap


def split(quantity: int, cap: int) -> list[int]:
    if cap <= 0:
        raise ValueError("child cap must be positive")
    sign = 1 if quantity > 0 else -1
    remaining, children = abs(quantity), []
    while remaining:
        child = min(cap, remaining)
        children.append(sign * child)
        remaining -= child
    return children


def consume(snapshot: dict, ticker: str, quantity: int) -> dict:
    """Price a child and deplete its displayed depth in a planning copy."""
    row = etf.executable_trade_cashflow(snapshot, ticker, quantity)
    side = "asks" if quantity > 0 else "bids"
    remaining = abs(quantity)
    for level in sorted(snapshot["books"][ticker][side], key=lambda x: x["price"], reverse=side == "bids"):
        available = max(0, level["quantity"] - level.get("quantity_filled", 0))
        used = min(remaining, available)
        level["quantity_filled"] = level.get("quantity_filled", 0) + used
        remaining -= used
        if not remaining:
            break
    return row


def apply_cash(positions: dict, fill: Mapping[str, Any]) -> None:
    positions[fill["ticker"]] = positions.get(fill["ticker"], 0) + fill["quantity"]
    for currency, value in fill["cashflow"].items():
        positions[currency] = positions.get(currency, 0) + value


def route_plan(snapshot: Mapping[str, Any], positions: Mapping[str, float], config: ETFConfig,
               converter: str | None = None, blocks: int = 0) -> dict:
    """Build child orders, optional stock purchases, manual conversion and exits.

    Every priced child consumes distinct current book depth. Future liquidity
    replenishment is never assumed. Conversion is a human action only.
    """
    book = copy.deepcopy(snapshot)
    p = dict(positions)
    base_cad = p.get("CAD", 0)
    stages, preparation, fills = [dict(p)], [], []
    units = blocks * etf.CONVERTER_BLOCK
    if converter == "ETF-Creation":
        for ticker in ("BULL", "BEAR"):
            need = max(0, units - int(p.get(ticker, 0)))
            for quantity in split(need, child_cap(snapshot, ticker, config)):
                fill = consume(book, ticker, quantity)
                preparation.append(fill)
                apply_cash(p, fill)
                stages.append(dict(p))
        p["BULL"] -= units
        p["BEAR"] -= units
        p["RITC"] += units
    elif converter == "ETF-Redemption":
        if p.get("RITC", 0) < units:
            raise ValueError("insufficient RITC for redemption")
        p["RITC"] -= units
        p["BULL"] = p.get("BULL", 0) + units
        p["BEAR"] = p.get("BEAR", 0) + units
    if converter:
        p["USD"] = p.get("USD", 0) - blocks * etf.CONVERTER_COST_USD
        stages.append(dict(p))
    # This order matches the live reducer: largest weighted holding first.
    while any(p.get(t, 0) for t in etf.WEIGHTS):
        ticker = max(etf.WEIGHTS, key=lambda t: abs(p.get(t, 0)) * etf.WEIGHTS[t])
        quantity = split(-int(p[ticker]), child_cap(snapshot, ticker, config))[0]
        fill = consume(book, ticker, quantity)
        fills.append(fill)
        apply_cash(p, fill)
        stages.append(dict(p))
    fx = etf.net_usd_value_cad(book, p.get("USD", 0))
    fx_children = split(-int(math.copysign(round(abs(p.get("USD", 0))), p.get("USD", 0))),
                        child_cap(snapshot, "USD", config))
    # Final FX cash is valued once, at cumulative depth, not repeatedly at top.
    actions = len(preparation) + len(fills) + len(fx_children)
    manual_ticks = config.manual_wait_ticks if converter else 0
    ticks = math.ceil(actions * config.ticks_per_action + manual_ticks + config.end_buffer_ticks)
    return {"name": converter or "DIRECT", "converter": converter, "blocks": blocks,
            "preparation": preparation, "fills": fills, "stages": stages,
            "fx": fx, "fx_children": fx_children,
            "total_cad": p.get("CAD", 0) - base_cad + fx["cad_value"],
            "budget": {"actions": actions, "reserve_ticks": ticks,
                       "latest_accept_tick": 298 - ticks},
            "net_usd": p.get("USD", 0)}


def reserve(snapshot: Mapping[str, Any], route: Mapping[str, Any], config: ETFConfig,
            sigmas: Mapping[str, float] | None = None) -> dict:
    """Execution reserve = k * sum(child shares * sigma * sqrt(fill time)).

    FX reserve = k_fx * absolute final USD * sigma_FX * sqrt(horizon).
    VWAP already charges spread, depth and commissions; none is charged again.
    Missing history uses explicit floors and one-quarter of the current spread
    as a startup proxy. These are assumptions, not fitted confidence bounds.
    """
    sigmas = sigmas or {}
    rate = etf.fx_reference_rate(snapshot)
    execution, elapsed, details = 0.0, 0.0, []
    for index, fill in enumerate([*route["preparation"], *route["fills"]]):
        if index == len(route["preparation"]) and route["converter"]:
            elapsed += config.manual_wait_ticks
        elapsed += config.ticks_per_action
        elapsed = max(elapsed, fill.get("fill_tick_offset", elapsed))
        ticker = fill["ticker"]
        spread = max(0.0, etf.vwap(snapshot["books"][ticker], "BUY", 1)
                     - etf.vwap(snapshot["books"][ticker], "SELL", 1))
        sigma = max(config.equity_sigma_floor, sigmas.get(ticker, spread / 4))
        value = config.execution_k * abs(fill["quantity"]) * sigma * math.sqrt(elapsed)
        if ticker == "RITC":
            value *= rate
        execution += value
        details.append({"ticker": ticker, "quantity": fill["quantity"], "sigma_per_sqrt_tick": sigma,
                        "sigma_source": "observed RMS with floor" if ticker in sigmas else "startup quarter-spread with floor",
                        "fill_tick_offset": elapsed, "reserve_cad": value})
    horizon = max(config.ticks_per_action, route["budget"]["reserve_ticks"] - config.end_buffer_ticks)
    fx_sigma = max(config.fx_sigma_floor, sigmas.get("USD", config.fx_sigma_floor))
    fx = config.fx_k * abs(route["net_usd"]) * fx_sigma * math.sqrt(horizon)
    return {"reserve_cad": execution + fx, "execution_risk_cad": execution,
            "fx_risk_cad": fx, "execution_k": config.execution_k, "fx_k": config.fx_k,
            "fx_sigma_per_sqrt_tick": fx_sigma, "horizon_ticks": horizon,
            "fx_sigma_source": "observed RMS with floor" if "USD" in sigmas else "startup floor",
            "child_orders": len(details), "children": details}


def routes(snapshot: Mapping[str, Any], positions: Mapping[str, float], config: ETFConfig) -> list[dict]:
    candidates = [(None, 0)]
    ritc = int(positions.get("RITC", 0))
    name = "ETF-Redemption" if ritc > 0 else "ETF-Creation"
    candidates.extend((name, n) for n in range(1, abs(ritc) // etf.CONVERTER_BLOCK + 1))
    result = []
    for converter, blocks in candidates:
        try:
            result.append(route_plan(snapshot, positions, config, converter, blocks))
        except (ValueError, KeyError):
            continue
    return result


def tender_report(snapshot: Mapping[str, Any], offer: Mapping[str, Any], config: ETFConfig,
                  sigmas: Mapping[str, float] | None = None, liquidity: Any = None) -> dict:
    report = {"tender_id": offer.get("tender_id"), "decision": "REVIEW"}
    try:
        if offer.get("ticker") != "RITC" or not offer.get("is_fixed_bid"):
            raise ValueError("only fixed-price RITC tenders supported")
        quantity = offer["quantity"]
        if (isinstance(quantity, bool) or not isinstance(quantity, (int, float))
                or not math.isfinite(quantity) or quantity <= 0 or int(quantity) != quantity
                or offer.get("action") not in ("BUY", "SELL")
                or not math.isfinite(offer["price"]) or offer["price"] <= 0):
            raise ValueError("invalid tender price, action or quantity")
        signed = int(quantity) * (1 if offer["action"] == "BUY" else -1)
        positions = {s["ticker"]: float(s["position"]) for s in snapshot["securities"]}
        baseline = etf.liquidation_value(snapshot, positions)
        after = dict(positions)
        after["RITC"] = after.get("RITC", 0) + signed
        after["USD"] = after.get("USD", 0) - signed * offer["price"]
        candidates = routes(snapshot, after, config)
        direct = next((r for r in candidates if not r["converter"]), None)
        # Staged forecasts never replace an unavailable stress exit, nor price
        # a partially offset portfolio against an incompatible baseline.
        if config.staged_tenders and liquidity is not None and direct and not any(
                positions.get(t, 0) for t in etf.WEIGHTS):
            evidence = liquidity.estimate(snapshot, "bids" if after["RITC"] > 0 else "asks")
            if evidence:
                from models.etf_liquidity import staged_direct
                try:
                    staged = staged_direct(snapshot, after, direct, config, evidence)
                    fallback_profit = direct["total_cad"] - baseline["total_cad"]
                    staged["staging"]["fallback_profit_cad"] = fallback_profit
                    staged["staging"]["baseline_total_cad"] = positions.get("CAD", 0) + baseline["total_cad"]
                    if fallback_profit >= -staged["staging"]["fallback_loss_cap_cad"]:
                        candidates.append(staged)
                    else:
                        report["staged_unavailable"] = "frozen-book fallback exceeds tender loss cap"
                except (ValueError, KeyError) as error:
                    report["staged_unavailable"] = str(error)
            else:
                report["staged_unavailable"] = "insufficient recent near-touch replenishment evidence"
        for route in candidates:
            # Tenders have a fixed negotiated entry price. Their risk appetite
            # is configurable independently of speculative basket entries.
            tender_config = replace(config, execution_k=config.tender_execution_k,
                                    fx_k=config.tender_fx_k)
            allowance = reserve(snapshot, route, tender_config, sigmas)
            route["profit_cad"] = route["total_cad"] - baseline["total_cad"]
            route["reserve"] = allowance
            route["minimum_profit_cad"] = max(config.min_profit_cad_per_share * quantity,
                                               allowance["reserve_cad"])
            route["surplus_cad"] = route["profit_cad"] - route["minimum_profit_cad"]
        if not candidates:
            raise ValueError("no complete direct or converter route has executable depth")
        best = max(candidates, key=lambda r: r["surplus_cad"])
        report.update(routes=candidates, projected_positions=after, selected_route=best,
                      estimated_unwind_profit_cad=best["profit_cad"],
                      estimated_unwind_profit_usd=(direct["net_usd"] - baseline["net_usd_before_fx"]
                                                   if direct else None),
                      liquidation_reserve_cad=best["minimum_profit_cad"],
                      liquidation_reserve=best["reserve"],
                      baseline_liquidation_cad=baseline["total_cad"],
                      risk_reducing=etf.exposure(after)["gross"] < etf.exposure(positions)["gross"])
    except (ValueError, KeyError) as error:
        report["skip"] = str(error)
    return report
