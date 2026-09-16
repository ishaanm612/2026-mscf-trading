"""Configuration objects for the RIT volatility strategy."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class VolatilityConfig:
    """Parameters governing the baseline volatility strategy.

    :param contract_multiplier: Underlying shares represented by one option.
    :param expiry_tick: Case tick at which options expire.
    :param ticks_per_trading_year: Tick count represented by one trading year.
    :param risk_free_rate: Continuously compounded annual risk-free rate.
    :param hedge_threshold: Delta magnitude below which RTM is not traded.
    :param max_safe_delta: Internal boundary below the competition delta limit.
    :param option_commission: Commission charged per option contract.
    :param rtm_commission_per_share: Commission charged per RTM share.
    :param entry_edge_per_contract: Minimum net edge required to enter.
    :param exit_edge_per_contract: Net edge below which an open straddle exits.
    :param max_option_position_fraction: Fraction of server option capacity V1 uses.
    :param max_straddle_contracts: Absolute per-strike concentration cap.
    :param max_portfolio_gamma: Internal cap on absolute portfolio gamma.
    :param max_portfolio_vega: Internal cap on absolute portfolio vega.
    :param edge_for_full_risk: Net edge that earns the full available risk budget.
    :param news_entry_window_ticks: Maximum release age for reevaluating an unused news signal.
    :param convergence_min_expected_pnl: Minimum learned expected P&L per straddle to enter.
    :param close_tick: Optional operator override forcing an earlier liquidation.
    :param cycle_ticks_floor: Conservative startup ticks per complete decision/order cycle.
    :param liquidation_buffer_ticks: Extra ticks reserved for transient API delays.
    :param minimum_holding_ticks: Time a new position must have before liquidation.
    :param safety_margin_per_contract: Additional edge required for model and fill error.
    :param explainability_enabled: Include factor-level rationale in decision logs.
    """

    contract_multiplier: int = 100
    expiry_tick: int = 300
    ticks_per_trading_year: int = 3600
    risk_free_rate: float = 0.0
    hedge_threshold: int = 3000
    max_safe_delta: int = 6000
    option_commission: float = 2.0
    rtm_commission_per_share: float = 0.02
    entry_edge_per_contract: float = 4.0
    exit_edge_per_contract: float = 1.0
    max_option_position_fraction: float = 0.60
    max_straddle_contracts: int = 300
    max_portfolio_gamma: float = 1500.0
    max_portfolio_vega: float = 80000.0
    edge_for_full_risk: float = 32.0
    news_entry_window_ticks: int = 10
    convergence_min_expected_pnl: float = 0.0
    close_tick: int | None = None
    cycle_ticks_floor: int = 3
    liquidation_buffer_ticks: int = 5
    minimum_holding_ticks: int = 10
    safety_margin_per_contract: float = 1.0
    explainability_enabled: bool = True

    def as_log_fields(self) -> dict[str, Any]:
        """Return a JSON-safe representation for a decision log.

        :returns: Configuration fields as a dictionary.
        """

        return asdict(self)
