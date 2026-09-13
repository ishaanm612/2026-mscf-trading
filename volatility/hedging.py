"""Delta hedge decisions kept separate from volatility signals."""
from __future__ import annotations


def calculate_hedge_order(portfolio_delta: float, hedge_threshold: int) -> int:
    """Return RTM shares that bring a portfolio toward zero delta.

    :param portfolio_delta: Portfolio delta measured in RTM shares.
    :param hedge_threshold: No-trade-band half-width in RTM shares.
    :returns: Signed RTM quantity; positive buys RTM and negative sells it.
    :raises ValueError: If the threshold is negative.
    """

    if hedge_threshold < 0:
        raise ValueError("hedge_threshold must be non-negative")
    return 0 if abs(portfolio_delta) < hedge_threshold else -round(portfolio_delta)
