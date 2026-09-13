# Volatility V1 design decisions

This document records decisions that are easy to lose when tuning a strategy.
They are starting assumptions for practice, not claims that the chosen values
are optimal.

## System boundary

`strategy.py` is pure: it turns a complete snapshot into `DesiredTrade`
objects. It does not send HTTP requests or assume a submitted order fills.
`bot.py` validates one desired trade against a fresh snapshot, submits it via
the journalled executor only in explicit trading mode, and waits for confirmed
fills before proceeding. A straddle is therefore serial rather than atomic;
after the call or put fills, the next snapshot is reconciled before the other
leg is considered. This prevents an uncertain API result from producing a
duplicate order, but means temporary directional exposure is possible.

## Forecast representation

The forecast stores **annualized variance** for each news-derived regime. For
a regime with annualized volatility `sigma` and duration `dt` ticks, its
contribution is `dt * sigma^2`. Remaining volatility is calculated only at
the end:

```text
remaining_sigma = sqrt(sum(dt * sigma^2) / sum(dt))
```

For a published range, V1 uses the mean of endpoint variances. A 20–30% range
therefore becomes `(0.20^2 + 0.30^2) / 2`, rather than `0.25^2`.

News is parsed from its wording (`this week`, `next week`, or an explicit
week), never from an assumed release tick. If a volatility-related message is
ambiguous, the strategy returns no forecast and cannot enter. When a later
regime is not yet announced, the latest known variance is carried forward and
logged as an assumption. This gives a usable V1 forecast while making the
assumption visible in replay data.

## Pricing and units

Black-Scholes price and Greeks are per **option share**. The strategy converts
to contract dollars and portfolio Greeks with `contract_multiplier`, currently
100. Time to expiry uses `(expiry_tick - current_tick) / ticks_per_trading_year`.
Neither the risk-free rate nor the clock convention is hardcoded into pricing;
both live in `VolatilityConfig`.

The portfolio delta is:

```text
RTM shares + sum(option contracts * 100 * option delta)
```

Gamma, vega, and theta are aggregated using the same multiplier. They are
logged now even though V1’s hedge threshold is delta-only, so later practice
analysis can introduce a gamma-aware band from observed RTM moves.

## Edge and reserve calculation

The fair-price comparison always uses the price that can actually be traded:

```text
buy edge before reserve  = (fair price - ask) * 100
sell edge before reserve = (bid - fair price) * 100
```

The bid/ask choice already incorporates the entry spread. V1 subtracts a
deliberately conservative per-contract reserve:

```text
2 * option commission
+ 2 * abs(option delta) * 100 * RTM commission per share
+ safety margin
```

The first `2` covers entering and exiting the option. The second covers one
initial RTM delta hedge and one closing hedge. This is intentionally a simple
model: it does not predict intraperiod rehedges or portfolio netting. When two
legs form a straddle, V1 sums the two leg reserves even though their deltas
offset. That biases the system toward skipping marginal opportunities, which
is the preferred V1 failure mode. Practice logs should later support a
portfolio-level hedge-cost estimator.

## Instrument and entry choice

V1 uses the listed strike nearest the RTM midpoint and requires both its call
and put to have positive cost-adjusted edge on the same side. Buying both is a
long-volatility straddle; selling both is a short-volatility straddle. This
starts near delta-neutral and emphasizes vega/gamma exposure rather than a
directional RTM view.

An entry is only considered when a newly observed analyst/news record appears.
Existing positions can still be hedged or exited on every snapshot. This
separates the informational event from normal polling and records
`time_since_latest_news` for estimating how quickly the market maker absorbs
information.

Size uses coarse edge buckets and hard V1 caps. It intentionally uses less
than server capacity. `max_option_position_fraction` is retained as the
configuration boundary for server-limit-aware sizing; the current conservative
bucket implementation also applies `max_straddle_contracts`. Practice results
should determine whether capacity, vega, gamma, and available delta headroom
justify a more complete sizing model.

## Exits and risk priority

The decision priority is: inactive/expiry handling, expiry-window reduction,
hard delta safety hedge, ordinary delta hedge, convergence exit, then new
entry. Once the expiry window begins, the strategy may reduce inventory or
hedge RTM but can never open a fresh straddle, including while flat. Entry and
exit thresholds are separate. This hysteresis avoids rapidly opening and
closing on a small noisy edge.

`hedge_threshold` creates a no-trade band because RTM commissions make tiny
hedges expensive. `max_safe_delta` is lower than the competition boundary and
forces an RTM hedge before the strategy approaches the penalty region. V1 does
not yet adjust this band for gamma; that is deferred until practice data can
estimate plausible short-horizon RTM moves.

## Static arbitrage scanner

Put-call parity is logged independently of the volatility signal. It uses
executable option quotes and an RTM-equivalent forward difference, then
subtracts two option commissions and an RTM commission reserve. V1 does not
execute parity signals yet because the required multi-leg sequence has more
execution risk than the baseline straddle. Logged observations are the basis
for deciding whether that execution path is worthwhile.
