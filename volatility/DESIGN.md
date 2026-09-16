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
ambiguous, the strategy returns no forecast and cannot enter. Unannounced
future weeks use a 20% annualized prior (`unannounced_sigma`), or `--sigma`
when the operator supplies one. Last week's print is not carried forward.
That prior is a strategy assumption, logged as `used_unannounced_prior`.

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
hard delta safety hedge, ordinary delta hedge for complete inventory, then
exits, then new entry. Incomplete one-leg inventory skips the ordinary
3,000-share band so the second straddle leg can fill; the 6,000-share safety
hedge still fires. Once the expiry window begins, the strategy may reduce
inventory or hedge RTM but can never open a fresh straddle, including while
flat. Entry and exit thresholds are separate. Remaining edge below
`exit_edge_per_contract` still exits. Remaining edge below
`take_profit_remaining_fraction` of the entry edge also exits, freeing capital
for later news. A complete straddle whose ATM side has flipped inside the
news window exits even if hysteresis has not yet been reached.

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

## September 14 heat investigation and corrections

The investigated heat held 69 RTM50 calls and puts, peaked at $11,385 at
 tick 58, and finished flat at $1,601.81. These are sums of the securities'
realized and unrealized fields, excluding separately reported penalties.
The record has a gap from ticks 115 to 165; its cause is not established.

Exit evaluation now groups confirmed inventory by its held strike, independently
of the strike selected for a new ATM entry. Long inventory uses fair-minus-ask;
short inventory uses bid-minus-fair. Both subtract the existing conservative
reserve. Negative scores therefore trigger exits on signal reversals as well
as convergence. Unequal legs are weighted by quantity relative to the largest
leg. The score retains the entry reserve for consistent hysteresis; it is not
liquidation P&L and does not charge past commissions a second time.

Forecasts resolve overlapping regimes in publication order: the latest
applicable announcement wins for each tick. A revision published midweek
replaces only its remaining interval. Unannounced remaining ticks use
`unannounced_sigma` (default 20%) or an operator `--sigma` fallback; they do
not inherit the last printed week. Only news published by the snapshot tick is
eligible. Integration is performed on the case's integer tick grid, summing
annualized variance times ticks.

Offline reevaluation of the recorded inventory first requests closing both
69-contract RTM50 legs at tick 37, instead of the historical tick 230. This is
a decision regression, not a counterfactual P&L backtest: the recorded inventory
and subsequent market prices were generated by the old strategy. Future fills
and profitability have not been verified. The tick-240 entry cutoff remains
unchanged pending calibration. Process outages and serial-leg execution remain
separate operational concerns; these signal corrections do not resolve them.

## Inventory-dependent expiry timing

The default tick-240 cutoff is replaced by separate computed entry and
liquidation deadlines. `close_tick` is now an optional earlier operator override.
The final submission boundary remains expiry minus one tick, matching the
execution bridge's existing end-of-case guard.

Let C be the number of option child orders required by confirmed quantities
and each security's maximum trade size. Let H be the number of RTM children
needed for current shares plus 100 times gross option contracts (a conservative
worst-case delta allowance). Reserve N = 2C + H cycles: C option exits, C hedge
opportunities, and H final hedge children. At cycle duration D the liquidation
reserve is N*D + B, where B is extra outage headroom. Liquidation starts at
expiry - 1 - reserve. Missing server size metadata forces an immediate deadline.

D is the maximum positive tick gap across the most recent 32 observations,
with a three-tick startup floor. These gaps include polling, snapshot collection,
previous execution/fill confirmation, and retries. They are an observed
end-to-end duration proxy, not a fill-latency estimator. A large outage advances
the deadline; liquidation latches for the rest of the heat, so shrinking
inventory cannot cause it to resume entries. B defaults to five ticks. Neither
floor nor outage allowance guarantees completion during an extended outage.

A proposed entry must fit two entry cycles, ten ticks of minimum holding time,
and its own projected exit reserve. Entry sizes fit one server child per leg.
For 69 contracts on each leg, flat RTM, 100-contract option caps, a 10,000-share
RTM cap, and D=3, there are six reserved cycles: liquidation starts at tick 276
and the entry deadline is tick 260. D=6 advances those to 258 and 236. These
are operational budgets; the ten-tick holding allowance is configurable and
has not been calibrated as a profitable holding horizon.

Mandatory liquidation cancels queued strategy entry intentions locally, then
replans from confirmed positions each cycle. Option and RTM reductions respect
child-order sizes. Existing risk gates and unresolved-fill stops still apply;
no unconfirmed exchange order is retried. Explainability logs include the
observed cycle budget and the candidate entry deadline when entry is evaluated.

## Paired execution regression correction

A 6,000-delta safety hedge may interrupt the queued pair without deleting its
remaining leg. Ordinary band hedges wait until both legs are confirmed, so a
50–70 contract first fill is not immediately offset in RTM and then unwound.
After confirmed safety-hedge completion, the remaining entry leg is rechecked
against fresh executable prices and the current forecast. It must retain
positive cost-adjusted edge in its original direction. If that fails, or the
current strategy requests an exit, confirmed option inventory is explicitly
queued for unwind. Manual account changes, risk rejection, and mandatory
liquidation can still invalidate queued intentions. Every subsequent order
uses fresh account state and its own risk checks; pairs remain non-atomic.

The watched trading runner skips its ordinary one-second sleep after an option
fill or while a paired leg is pending. It still collects a new snapshot and
performs preflight before the next order. This removes an avoidable delay but
cannot guarantee execution at the original quote. Filled journal events now
retain the complete API order response and submission-to-confirmation duration
in seconds; historical fills cannot be retroactively enriched.

Recognized news remains eligible for entry for ten ticks after publication.
An event with no executable edge is reevaluated as quotes change inside that
window. Once an entry is proposed, that recognized-news set is consumed to
avoid repeated churn after exits or rejections. Old news seen on restart does
not receive a fresh ten-tick window. The duration is a configurable hypothesis,
not a calibrated estimate of market-maker learning speed.

## Supervised convergence filter

The optional convergence filter estimates one-straddle executable P&L over a
configured future horizon. Its features are current model edge, absolute
fair/market-IV gap, news age, ticks remaining, and paired spread. Its label is
the later bid close for a long straddle or later ask close for a short
straddle, less the original executable entry price. It excludes actual position
size, realized account P&L, and future analyst information.

Training uses chronological whole-heat splits. The final three heats are held
out, so nearby ticks from one heat never appear in both train and validation.
The implementation is ridge regression with versioned JSON coefficients. It is
not an execution simulator: delayed fills, hedges, unobserved orders, and
policy counterfactuals are outside its label.

The filter is opt-in through `--convergence-model`. It may reject an otherwise
eligible entry when predicted post-cost P&L is below the configured threshold;
it cannot create an entry or change risk handling. Review held-out directional
accuracy, absolute error, heat coverage, and economic stability before use in
practice trading.
