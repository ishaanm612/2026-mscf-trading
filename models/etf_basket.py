"""Policy reports for capped, three-leg ETF convergence baskets.

Basket orders are serial and a completed basket is a convergence position, not
an immediately locked arbitrage.  These helpers therefore keep two values
separate: an expected payoff if the parity gap closes, and the executable cash
value of abandoning inventory at the current quotes.  Callers can make the
trade-off explicit without treating the former as a guaranteed liquidation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from models import etf, etf_policy


@dataclass(frozen=True)
class BasketConfig:
    """Capped convergence policy parameters, all CAD except tick counts.

    ``take_profit_cad_per_share`` is deliberately modest: it is the minimum
    realized executable profit after the exit reserve, rather than an assumed
    fraction of a displayed parity gap.  ``stop_loss_cad_per_share`` caps the
    loss accepted while waiting for convergence.
    """

    max_quantity: int = 20_000
    max_hold_ticks: int = 60
    min_hold_ticks: int = 10
    take_profit_cad_per_share: float = 0.02
    stop_loss_cad_per_share: float = 0.30
    cooldown_ticks: int = 5
    entry_buffer_cad_per_share: float = 0.02

    def __post_init__(self) -> None:
        counts = (self.max_quantity, self.max_hold_ticks, self.min_hold_ticks, self.cooldown_ticks)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in counts):
            raise ValueError("basket quantities and tick counts must be integers")
        if self.max_quantity < 1 or self.max_hold_ticks < 1 or self.min_hold_ticks < 0:
            raise ValueError("basket quantity and hold ticks must be positive")
        if self.min_hold_ticks > self.max_hold_ticks:
            raise ValueError("basket minimum hold cannot exceed maximum hold")
        values = (self.take_profit_cad_per_share, self.stop_loss_cad_per_share,
                  self.entry_buffer_cad_per_share)
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("basket CAD thresholds must be finite and nonnegative")
        if self.cooldown_ticks < 0:
            raise ValueError("basket cooldown cannot be negative")


def _quantity(entry_fills: Iterable[Mapping[str, Any]]) -> int:
    return abs(sum(int(row["quantity"]) for row in entry_fills if row["ticker"] == "RITC"))


def _top_midpoint(snapshot: Mapping[str, Any], ticker: str) -> float:
    """Top-of-book midpoint; depth belongs in the executable exit quote."""
    return (etf.vwap(snapshot["books"][ticker], "BUY", 1)
            + etf.vwap(snapshot["books"][ticker], "SELL", 1)) / 2


def _exit_cost(snapshot: Mapping[str, Any], entry_fills: Iterable[Mapping[str, Any]]) -> float:
    """Quote the cost of exiting at a parity-consistent midpoint.

    A current executable reversal also contains the *current* parity gap, so
    it cannot be used as the expected payoff when the gap closes.  This
    calculation retains only exit-side spread, depth and commission costs.
    """
    entry_fills = list(entry_fills)
    quantities = {ticker: sum(int(row["quantity"]) for row in entry_fills if row["ticker"] == ticker)
                  for ticker in etf.WEIGHTS}
    cad_cost, usd_cost = 0.0, 0.0
    for ticker, entry_quantity in quantities.items():
        if not entry_quantity:
            continue
        quantity = abs(entry_quantity)
        exit_quantity = -entry_quantity
        midpoint = _top_midpoint(snapshot, ticker)
        actual = etf.trade_cashflow(ticker, exit_quantity,
                                    etf.vwap(snapshot["books"][ticker],
                                             "BUY" if exit_quantity > 0 else "SELL", quantity))
        # The no-fee midpoint cashflow is the parity-consistent benchmark.
        midpoint_cash = -exit_quantity * midpoint
        if ticker in {"BULL", "BEAR"}:
            cad_cost += midpoint_cash - actual["CAD"]
        else:
            usd_cost += midpoint_cash - actual["USD"]
    return cad_cost + usd_cost * etf.fx_reference_rate(snapshot)


def _execution_reserve(snapshot: Mapping[str, Any], legs: Iterable[tuple[str, int]],
                       etf_config: etf_policy.ETFConfig,
                       sigmas: Mapping[str, float] | None, *, start_offset: float = 0.0) -> dict[str, Any]:
    """Reserve scheduled cashflow variance, not a sum of worst-case moves.

    Independent tick increments and independent instruments are explicit
    modeling assumptions, not confidence bounds. Children of the SAME ticker
    share price shocks: square their remaining aggregate quantity, not each
    child separately. Splitting an order must not manufacture diversification.
    """
    sigmas = sigmas or {}
    legs = list(legs)
    elapsed, variance, children = start_offset, 0.0, []
    rate = etf.fx_reference_rate(snapshot)
    remaining = {t: sum(q for ticker, q in legs if ticker == t) for t in etf.WEIGHTS}
    scales = {}
    for ticker in remaining:
        spread = max(0.0, etf.vwap(snapshot["books"][ticker], "BUY", 1)
                     - etf.vwap(snapshot["books"][ticker], "SELL", 1))
        scales[ticker] = max(etf_config.equity_sigma_floor, sigmas.get(ticker, spread / 4))
        if ticker == "RITC":
            scales[ticker] *= rate
    variance += start_offset * sum((remaining[t] * scales[t]) ** 2 for t in remaining)
    for ticker, quantity in legs:
        elapsed += etf_config.ticks_per_action
        interval_variance = etf_config.ticks_per_action * sum(
            (remaining[t] * scales[t]) ** 2 for t in remaining)
        variance += interval_variance
        remaining[ticker] -= quantity
        children.append({"ticker": ticker, "quantity": quantity, "fill_tick_offset": elapsed,
                         "sigma_per_sqrt_tick": scales[ticker] / (rate if ticker == "RITC" else 1),
                         "interval_variance_cad2": interval_variance})
    return {"reserve_cad": etf_config.execution_k * math.sqrt(variance), "children": children,
            "variance_cad2": variance, "model": "scheduled variance; independent instruments and tick increments",
            "horizon_ticks": elapsed, "execution_k": etf_config.execution_k}


def _partial_inventory_reserve(snapshot: Mapping[str, Any], filled: Iterable[Mapping[str, Any]],
                               horizon_ticks: float, etf_config: etf_policy.ETFConfig,
                               sigmas: Mapping[str, float] | None) -> dict[str, Any]:
    """Reserve movement in confirmed, unpaired legs while later legs are crossed.

    This is separate from the new-order execution reserve: a BULL fill remains
    exposed during the BEAR and RITC orders even though it is not submitted
    again.  The later full-basket exit reserve covers a different interval.
    """
    filled = list(filled)
    positions = {ticker: sum(int(row["quantity"]) for row in filled if row["ticker"] == ticker)
                 for ticker in etf.WEIGHTS}
    sigmas = sigmas or {}
    rate = etf.fx_reference_rate(snapshot)
    variance, children = 0.0, []
    for ticker, quantity in positions.items():
        if not quantity:
            continue
        spread = max(0.0, etf.vwap(snapshot["books"][ticker], "BUY", 1)
                     - etf.vwap(snapshot["books"][ticker], "SELL", 1))
        sigma = max(etf_config.equity_sigma_floor, sigmas.get(ticker, spread / 4))
        value = etf_config.execution_k * abs(quantity) * sigma * math.sqrt(horizon_ticks)
        if ticker == "RITC":
            value *= rate
        variance += value ** 2
        children.append({"ticker": ticker, "quantity": quantity, "holding_ticks": horizon_ticks,
                         "sigma_per_sqrt_tick": sigma, "reserve_cad": value})
    return {"reserve_cad": math.sqrt(variance), "horizon_ticks": horizon_ticks,
            "execution_k": etf_config.execution_k, "children": children}


def _fx_reserve(snapshot: Mapping[str, Any], net_usd: float, horizon: float,
                etf_config: etf_policy.ETFConfig,
                sigmas: Mapping[str, float] | None) -> dict[str, Any]:
    """Price uncertainty in a USD reference or final residual, in CAD."""
    sigmas = sigmas or {}
    sigma = max(etf_config.fx_sigma_floor, sigmas.get("USD", etf_config.fx_sigma_floor))
    value = etf_config.fx_k * abs(net_usd) * sigma * math.sqrt(max(horizon, etf_config.ticks_per_action))
    return {"reserve_cad": value, "net_usd": net_usd, "horizon_ticks": horizon,
            "fx_sigma_per_sqrt_tick": sigma, "fx_k": etf_config.fx_k}


def _ordered_exit_children(snapshot: Mapping[str, Any], fills: Iterable[Mapping[str, Any]],
                           etf_config: etf_policy.ETFConfig) -> list[tuple[str, int]]:
    """Split and order exits exactly as the live largest-weight reducer does."""
    remaining = {ticker: -sum(int(row["quantity"]) for row in fills if row["ticker"] == ticker)
                 for ticker in etf.WEIGHTS}
    children: list[tuple[str, int]] = []
    while any(remaining.values()):
        ticker = max(etf.WEIGHTS, key=lambda item: abs(remaining[item]) * etf.WEIGHTS[item])
        if not remaining[ticker]:
            continue
        cap = etf_policy.child_cap(snapshot, ticker, etf_config)
        child = etf_policy.split(remaining[ticker], cap)[0]
        children.append((ticker, child))
        remaining[ticker] -= child
    return children


def _entry_fills(snapshot: Mapping[str, Any], legs: Iterable[tuple[str, int]]) -> list[dict[str, Any]]:
    return [etf.executable_trade_cashflow(snapshot, ticker, int(quantity)) for ticker, quantity in legs]


def _conditional_value(snapshot: Mapping[str, Any], fills: list[Mapping[str, Any]],
                       etf_config: etf_policy.ETFConfig,
                       sigmas: Mapping[str, float] | None,
                       *, reserve_entry_legs: Iterable[tuple[str, int]] | None = None,
                       partial_fills: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Expected convergence value after exit friction and serial uncertainty."""
    legs = [(str(row["ticker"]), int(row["quantity"])) for row in fills]
    entry_legs = list(reserve_entry_legs) if reserve_entry_legs is not None else legs
    exit_legs = _ordered_exit_children(snapshot, fills, etf_config)
    entry_reserve = _execution_reserve(snapshot, entry_legs, etf_config, sigmas)
    partial_reserve = _partial_inventory_reserve(snapshot, partial_fills or [],
                                                 entry_reserve["horizon_ticks"], etf_config, sigmas)
    # Exit execution is a separate future interval. Starting its clock at the
    # entry horizon charges the entry delay a second time.
    exit_reserve = _execution_reserve(snapshot, exit_legs, etf_config, sigmas)
    entry_mark = etf.basket_entry_value_cad(snapshot, fills)
    exit_cost = _exit_cost(snapshot, fills)
    immediate = etf.basket_close_now(snapshot, {"entry_fills": fills})
    # The future RITC exit price is unknown. Use the current round-trip USD
    # residual as an explicit FX-friction proxy, never the gross financing leg.
    fx_exit_cost = max(0.0, immediate["net_usd_before_fx"] * etf.fx_reference_rate(snapshot)
                       - immediate["fx"]["cad_value"])
    entry_usd = etf.cashflow_totals(_entry_fills(snapshot, entry_legs))["USD"] if entry_legs else 0.0
    entry_fx = _fx_reserve(snapshot, entry_usd, entry_reserve["horizon_ticks"], etf_config, sigmas)
    exit_fx = _fx_reserve(snapshot, immediate["net_usd_before_fx"], exit_reserve["horizon_ticks"],
                          etf_config, sigmas)
    execution_risk = math.sqrt(sum(r["reserve_cad"] ** 2
                                  for r in (entry_reserve, partial_reserve, exit_reserve)))
    fx_risk = math.hypot(entry_fx["reserve_cad"], exit_fx["reserve_cad"])
    total_reserve = execution_risk + fx_risk
    return {"entry_mark_cad": entry_mark, "exit_cost_cad": exit_cost,
            "fx_exit_cost_cad": fx_exit_cost,
            "entry_reserve": entry_reserve, "partial_inventory_reserve": partial_reserve,
            "exit_reserve": exit_reserve,
            "entry_fx_reserve": entry_fx, "exit_fx_reserve": exit_fx,
            "immediate_close": immediate,
            "execution_risk_cad": execution_risk, "fx_risk_cad": fx_risk,
            "reserve_aggregation": "root-sum-square execution phases + root-sum-square FX phases",
            "reserve_cad": total_reserve,
            "conditional_convergence_pnl_cad": entry_mark - exit_cost - fx_exit_cost
                                         - total_reserve}


def entry_report(snapshot: Mapping[str, Any], direction: int, quantity: int, config: BasketConfig,
                 etf_config: etf_policy.ETFConfig,
                 sigmas: Mapping[str, float] | None = None) -> dict[str, Any]:
    """Evaluate a fresh capped-convergence basket before any order is submitted."""
    if quantity < 1 or quantity > config.max_quantity:
        return {"eligible": False, "reason": "basket quantity exceeds capped convergence policy"}
    try:
        opportunity = etf.basket_opportunity(snapshot, direction, quantity, entry_buffer_cad=0.0)
        fills = _entry_fills(snapshot, opportunity["legs"])
        value = _conditional_value(snapshot, fills, etf_config, sigmas)
        immediate = value["immediate_close"]
        required = max(config.take_profit_cad_per_share * quantity,
                       config.entry_buffer_cad_per_share * quantity)
        value.update({"direction": opportunity["direction"], "legs": opportunity["legs"],
                      "quantity": quantity, "required_convergence_pnl_cad": required,
                      "conditional_profit_cad": value["conditional_convergence_pnl_cad"],
                      "immediate_close_pnl_cad": immediate["pnl_cad"],
                      "reserve": {"entry": value["entry_reserve"],
                                  "partial_inventory": value["partial_inventory_reserve"],
                                  "entry_fx": value["entry_fx_reserve"],
                                  "exit": value["exit_reserve"], "exit_fx": value["exit_fx_reserve"],
                                  "reserve_cad": value["reserve_cad"]},
                      "eligible": (value["conditional_convergence_pnl_cad"] >= required
                                   and immediate["pnl_cad"] > -config.stop_loss_cad_per_share * quantity)})
        if immediate["pnl_cad"] <= -config.stop_loss_cad_per_share * quantity:
            value["reason"] = "current round-trip trading cost already exhausts basket loss budget"
        elif value["conditional_convergence_pnl_cad"] < required:
            value["reason"] = "convergence payoff after exit costs and uncertainty is below profit target"
        else:
            value["reason"] = "conditional convergence value clears exit costs and reserve"
        return value
    except (ValueError, KeyError) as error:
        return {"eligible": False, "reason": f"basket cannot be priced: {error}"}


def serial_report(snapshot: Mapping[str, Any], filled: Iterable[Mapping[str, Any]],
                  remaining_legs: Iterable[tuple[str, int]], config: BasketConfig,
                  etf_config: etf_policy.ETFConfig,
                  sigmas: Mapping[str, float] | None = None) -> dict[str, Any]:
    """Choose completion only when its conditional value beats an executable abort.

    Completing can have a negative conditional value and still be preferable
    when reversing the partial position is worse.  The policy bounds that case
    by the basket loss limit instead of imposing the invalid ``finish >= 0``
    test used by the original serial path.
    """
    filled_rows = list(filled)
    remaining_legs = list(remaining_legs)
    try:
        remaining = _entry_fills(snapshot, remaining_legs)
        complete = [*filled_rows, *remaining]
        value = _conditional_value(snapshot, complete, etf_config, sigmas,
                                   reserve_entry_legs=[(str(ticker), int(quantity))
                                                       for ticker, quantity in remaining_legs],
                                   partial_fills=filled_rows)
        abort = etf.basket_abort_now(snapshot, filled_rows)
        immediate = value["immediate_close"]
        quantity = _quantity(complete)
        loss_cap = config.stop_loss_cad_per_share * quantity
        finish = (value["conditional_convergence_pnl_cad"] >= abort["abort_pnl_cad"]
                  and value["conditional_convergence_pnl_cad"] >= -loss_cap
                  and immediate["pnl_cad"] > -loss_cap)
        value.update({"finish": finish, "abort_pnl_cad": abort["abort_pnl_cad"],
                      "abort_exit_fills": abort["abort_exit_fills"],
                      "projected_remaining_fills": remaining,
                      "conditional_profit_cad": value["conditional_convergence_pnl_cad"],
                      "immediate_close_pnl_cad": immediate["pnl_cad"],
                      "reserve": {"entry": value["entry_reserve"],
                                  "partial_inventory": value["partial_inventory_reserve"],
                                  "entry_fx": value["entry_fx_reserve"],
                                  "exit": value["exit_reserve"], "exit_fx": value["exit_fx_reserve"],
                                  "reserve_cad": value["reserve_cad"]},
                      "loss_cap_cad": loss_cap})
        value["reason"] = ("completion has the better capped conditional value" if finish else
                           "abort has better executable value or completion exceeds loss cap")
        return value
    except (ValueError, KeyError) as error:
        return {"finish": False, "reason": f"serial basket cannot be repriced: {error}"}


def holding_report(snapshot: Mapping[str, Any], held: Mapping[str, Any], config: BasketConfig,
                   etf_config: etf_policy.ETFConfig,
                   sigmas: Mapping[str, float] | None = None) -> dict[str, Any]:
    """State whether a completed convergence basket should hold or exit now."""
    fills = list(held["entry_fills"])
    quantity = _quantity(fills)
    tick = int(snapshot["case"]["tick"])
    age = max(0, tick - int(held["entry_tick"]))
    take_profit = config.take_profit_cad_per_share * quantity
    stop_loss = config.stop_loss_cad_per_share * quantity
    try:
        close = etf.basket_close_now(snapshot, held)
        close_legs = _ordered_exit_children(snapshot, fills, etf_config)
        execution_reserve = _execution_reserve(snapshot, close_legs, etf_config, sigmas)
        fx_reserve = _fx_reserve(snapshot, close["net_usd_before_fx"],
                                 execution_reserve["horizon_ticks"], etf_config, sigmas)
        reserve = {**execution_reserve, "execution_risk_cad": execution_reserve["reserve_cad"],
                   "fx": fx_reserve, "fx_risk_cad": fx_reserve["reserve_cad"],
                   "reserve_cad": execution_reserve["reserve_cad"] + fx_reserve["reserve_cad"]}
        pnl = close["pnl_cad"]
        adjusted = pnl - reserve["reserve_cad"]
        signed_ritc = sum(int(row["quantity"]) for row in fills if row["ticker"] == "RITC")
        direction = 1 if signed_ritc > 0 else -1
        remaining_gap = direction * (_top_midpoint(snapshot, "BULL") + _top_midpoint(snapshot, "BEAR")
                                    - _top_midpoint(snapshot, "RITC") * etf.fx_reference_rate(snapshot))
        if pnl <= -stop_loss:
            action, reason = "EXIT", "stop loss reached"
        elif age >= config.max_hold_ticks:
            action, reason = "EXIT", "maximum basket holding time reached"
        elif remaining_gap <= 0:
            action, reason = "EXIT", "basket parity gap converged or reversed"
        elif adjusted >= take_profit:
            action, reason = "EXIT", "executable profit clears take-profit and exit reserve"
        else:
            action, reason = "HOLD", "within basket loss, age and profit limits"
        return {"action": action, "reason": reason, "quantity": quantity, "age_ticks": age,
                "close": close, "close_pnl_cad": pnl, "close_reserve": reserve,
                "risk_adjusted_close_pnl_cad": adjusted, "take_profit_cad": take_profit,
                "remaining_gap_cad_per_share": remaining_gap,
                "stop_loss_cad": stop_loss, "max_hold_ticks": config.max_hold_ticks,
                "exit_reason": reason if action == "EXIT" else None,
                # Growing an underwater convergence bet was the principal
                # source of avoidable loss in the observed heat.
                "add_allowed": action == "HOLD" and pnl >= 0}
    except (ValueError, KeyError) as error:
        reason = f"basket close cannot be priced: {error}"
        return {"action": "EXIT", "reason": reason, "exit_reason": reason,
                "add_allowed": False, "quantity": quantity, "age_ticks": age,
                "max_hold_ticks": config.max_hold_ticks}
