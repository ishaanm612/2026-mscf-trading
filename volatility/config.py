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
    :param max_straddle_contracts: Further V1 cap for one strike straddle.
    :param close_tick: Tick at which V1 starts reducing inventory for expiry.
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
    max_straddle_contracts: int = 50
    close_tick: int = 240

    def as_log_fields(self) -> dict[str, Any]:
        """Return a JSON-safe representation for a decision log.

        :returns: Configuration fields as a dictionary.
        """

        return asdict(self)
