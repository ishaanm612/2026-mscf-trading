"""Inventory-dependent deadlines measured in competition ticks."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from volatility.config import VolatilityConfig
from volatility.market_data import MarketState


@dataclass(frozen=True)
class ExitBudget:
    """Explainable deadline for serial option exits and a final RTM hedge.

    :param child_orders: Option children plus conservative RTM hedge children.
    :param ticks_per_order: Observed cycle duration with a configured floor.
    :param reserve_ticks: Total time reserved including outage headroom.
    :param liquidation_tick: Latest tick to begin reducing this inventory.
    """

    child_orders: int
    ticks_per_order: int
    reserve_ticks: int
    liquidation_tick: int


def order_capacity(state: MarketState, symbol: str) -> int:
    """Read the server child-order cap, failing closed when metadata is absent.

    :param state: Confirmed snapshot with security metadata.
    :param symbol: Instrument to trade.
    :returns: Positive maximum size or zero for invalid metadata.
    """

    row = next((r for r in state.raw.get('securities', []) if r.get('ticker') == symbol), {})
    value = row.get('max_trade_size', 0)
    return int(value) if isinstance(value, (int, float)) and value > 0 else 0


def exit_budget(state: MarketState, positions: Mapping[str, int], cycle_ticks: int,
                config: VolatilityConfig) -> ExitBudget:
    """Reserve serial exit time from inventory and observed complete-cycle delays.

    Each option child reserves an additional hedge opportunity. Final RTM
    capacity covers current shares plus worst-case option delta (one hundred
    shares per contract), so the plan does not assume paired deltas cancel.
    Missing size metadata makes the deadline immediate rather than optimistic.

    :param state: Snapshot supplying exchange order caps.
    :param positions: Confirmed or proposed signed option contract quantities.
    :param cycle_ticks: Conservative observed ticks between decision cycles.
    :param config: Clock, delay floor, and outage allowance.
    :returns: Inventory-dependent liquidation budget.
    """

    children = 0
    for symbol, quantity in positions.items():
        if quantity:
            cap = order_capacity(state, symbol)
            children += math.ceil(abs(quantity) / cap) if cap else config.expiry_tick
    shares = abs(state.rtm_position) + config.contract_multiplier * sum(abs(q) for q in positions.values())
    cap = order_capacity(state, 'RTM')
    hedge_children = math.ceil(shares / cap) if cap else (config.expiry_tick if shares else 0)
    count = 2 * children + hedge_children
    duration = max(config.cycle_ticks_floor, cycle_ticks)
    reserve = count * duration + config.liquidation_buffer_ticks
    return ExitBudget(count, duration, reserve, max(0, config.expiry_tick - 1 - reserve))
