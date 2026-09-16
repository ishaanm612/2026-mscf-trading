# Running and understanding the MVP

## Connection setup

Run commands from the repository root, with Python 3.10+. There are no pip dependencies. Credentials are environment variables, never source files. Do not run the original scripts in `reference/` alongside this bot.

For DMA, place separate `RIT_ETF_API_MODE`, `RIT_ETF_API_URL`,
`RIT_ETF_USERNAME`, `RIT_ETF_PASSWORD` and `RIT_VOLATILITY_*` values in the
ignored repository `.env` file. The runner selects the matching case values.
Legacy generic `RIT_*` variables remain a fallback. Copy
`.env.example` first if needed. Shell variables take precedence over `.env`.
Use a separate terminal per case. The confirmed practice API URLs are:

| Case | URL |
| --- | --- |
| Volatility | `http://flserver.rotman.utoronto.ca:16595/v1` |
| ETF | `http://flserver.rotman.utoronto.ca:16635/v1` |

Windows RIT server ports 16590/16630 are client login ports, not these DMA API URLs. For client REST, set `RIT_API_MODE=rest`, `RIT_API_KEY`, and the client's local API URL instead.

## Start with a connection check

```sh
python3 run.py volatility --source api --check
python3 run.py etf --source api --check
```

This prints case status, current holdings, news/books, open orders, and server limits. It submits nothing. Verify you connected to the intended case. `--check` output contains account information; do not commit it.

## Preview decisions, then trade

```sh
python3 run.py volatility --source api --plan --watch
python3 run.py volatility --source api --trade --watch --record data/volatility.jsonl
```

The volatility bot reads current announcements; no hardcoded sigma is required. `--sigma 0.25` supplies a fallback assumption only where a current announcement is unavailable. Unrecognized volatility news blocks trading even when a fallback exists. `--rate` defaults to zero; verify the rate announced by your session.

Use confirmed ETF session limits, replacing these observed practice values if needed:

```sh
python3 run.py etf --source api --plan --watch --gross-limit 300000 --net-limit 200000
python3 run.py etf --source api --trade --watch --gross-limit 300000 --net-limit 200000
```

ETF mode starts with tenders and inventory reduction. Add `--basket` to enable three-leg statistical arbitrage. `--quantity` controls ETF unwind/basket child size (default 1,000; maximum 10,000). Tenders larger than 10,000 units are skipped in this MVP. This intentionally leaves some opportunities unused.

Without `--watch`, only one decision is made. With `--watch`, the process polls until Ctrl+C, including through stopped sessions. Ctrl+C stops further submissions; it does not flatten holdings or cancel an order that may already have reached the server. A process lock prevents two instances from using the same journal; run only one bot per account and do not bypass this with alternate journal paths.

### Restart automatically at practice-heat boundaries

Use `scripts/supervise_volatility.py` as the lifecycle supervisor when a
practice server repeatedly stops and starts. Despite its compatibility filename,
`--case volatility` (the default) and `--case etf` are both supported.
It waits for RIT to report `ACTIVE`, launches exactly one worker for that heat,
and starts another only after the worker observes a stop, a changed period, or a
reset tick.  Decision logs, recordings, and execution journals stay append-only
across heats; the dashboard can therefore retain history while separating runs.

```sh
python3 scripts/supervise_volatility.py --trade \
  --decision-log data/volatility-decisions.jsonl \
  --record data/volatility-snapshots.jsonl
```

The same supervisor can manage ETF lifecycle restarts. ETF limits must come
from the active session; `--basket` is still explicit because tender handling
and inventory reduction are the safer default. Omit `--trade` to keep its
child in read-only plan mode; add it only for an authorized simulated heat.

```sh
python3 scripts/supervise_volatility.py --case etf \
  --gross-limit 300000 --net-limit 200000 --basket
```

The supervisor does not restart a worker that exits with an API, execution, or
risk error. Inspect `--check`, resolve open orders, and run `--reconcile` before
starting it again. This prevents a market reset from disguising an ambiguous
order outcome as a safe restart. Use Ctrl+C to stop both the supervisor and its
current worker.

## How one cycle works

1. `client.snapshot` reads case state, securities, news or depth, orders, and limits. A second case read rejects resets or snapshots spanning more than two ticks.
2. `Bot.step` checks session state and requires no open orders.
3. Case helpers select the next action from current holdings. `--plan` reports this action without changing account state or simulating fills.
4. `risk.check` verifies order size, tradeability, local projected risk, and projected server limits using security limit bindings. Existing open orders block new ones.
5. `Executor` persists an intent, submits once, and polls the returned order ID. Only a complete terminal fill allows the next action.

Snapshots are not atomic. Volatility click trading can change the account while the bot runs; the worker waits for open orders and replans from confirmed inventory. ETF execution still requires exclusive account use. Market orders can execute away from observed quotes; conservative thresholds reduce this risk but cannot eliminate it.

## Volatility decisions and units

- An option quote is dollars per underlying share. A position is **contracts**, with 100 shares per contract. Portfolio delta is `RTM shares + sum(contracts × 100 × option delta)`.
- Announcements assign variance to weeks. For a range, the estimate is the mean of squared endpoints. Remaining-time variance weights each week's estimate by its remaining seconds. Unannounced future weeks use a 20% prior, or `--sigma` when supplied; they do not inherit the last print.
- If absolute portfolio delta exceeds 250 shares, hedge RTM first. Proposed trades cannot increase exposure beyond an internal 6,000-share band, below the published 7,000 penalty threshold.
- Option entry uses the bid/ask, fair value, and conservative fee reserves. Buy when fair value exceeds the ask by enough; sell when the bid exceeds fair value. Require a further 0.03/share edge, trade ten contracts, cap each option at 50 contracts and total options at 200 gross contracts.
- Exit when the original signal disappears or reverses. From tick 240, open no new positions and work toward flat inventory. RTM is hedged between option exits. Flat-by-end is a target, not a guarantee under errors or slow execution.
- Pricing reserves two option commissions and an initial RTM hedge commission. It does not forecast every future hedge cost. Delta depends on a model, especially near expiry.

## ETF decisions and units

- First reduce actual USD cash exposure. FX child orders use current account inventory, not predicted cash from a submitted order.
- Unwind existing equity inventory one child at a time, selecting a risk-legal order with enough visible depth. On restart, existing baskets are treated as inventory to close.
- Accept only fixed-price RITC tenders with sufficient visible unwind depth, at least three ticks remaining, enough projected risk capacity, and a 0.05 USD/unit cushion beyond modeled unwind fees.
- Optional baskets enter only while equity inventory is flat and the executable CAD edge clears a 0.10/unit serial-execution reserve. The signal crosses BULL/BEAR/RITC/USD visible depth, includes all three equity fees, and rounds the indicative USD funding amount up to cover RITC plus its fee. Each leg must fully fill; inventory is reread before the next and the actual USD position is hedged after the RITC fill. Hold until the entry-direction edge disappears, then unwind. No automated converters are used.
- From tick 250, take no new tenders/baskets. Gross risk counts RITC twice. Server `limits[].units` expresses instrument units per risk unit: a binding of 0.5 means a reciprocal weight of 2.
- A basket is a statistical convergence trade with sequential execution. Partial legs create directional exposure, and any failure stops the runner for reconciliation. Tender estimates are static depth calculations, not promises about the eventual unwind.

## Failure and recovery

Execution journals default to `data/<case>-<username>-execution.jsonl`. Entries are flushed to disk before requests. No password is recorded. An HTTP error, timeout, malformed response, partial fill, or unconfirmed cancellation stops execution. POST/DELETE are never automatically retried.

1. Stop any other process using the account. Inspect `--check` and the last journal events.
2. Resolve any open orders. Use the API/client as permitted by the session rules; automated trading cases may prohibit manual orders. Preserve the journal for review.
3. After a hard process crash, a `.lock` file can remain. Remove only that stale lock after confirming its process is no longer running.
4. Run `python3 run.py <case> --source api --reconcile` to record the actual account state. This refuses accounts with open orders. It acknowledges current holdings and does not assume an interrupted order failed.
5. Restart with `--trade --watch --flatten-only` (and ETF limits if needed) to reduce holdings before resuming new entries. Volatility still needs a valid forecast to calculate risk; unknown news must be resolved first.

## Validation and limitations

```sh
python3 -m unittest discover -s tests -v
python3 run.py etf
python3 run.py volatility --sigma 0.25
python3 run.py volatility --plan --sigma 0.25
```

Tests exercise pricing identities, actual news formats, inverse risk-unit weights, tender unwinds, partial/uncertain fills, and a mocked 300-tick volatility round. The mock verifies behavior, not market realism or profitability. Local recordings are ignored by Git. Replay recalculates signals; it is not a fills/P&L backtester.

### Adaptive volatility deadlines

Volatility now computes the entry and liquidation deadlines from confirmed
inventory, server child-order limits, and recent observed decision-cycle tick
gaps. It no longer stops by default at tick 240. Defaults and optional earlier
`close_tick` override live in `volatility/config.py`; formulas and assumptions
are documented in `volatility/DESIGN.md` under inventory-dependent expiry timing.
The decision explanation's `risk_factors.execution_timing` records the budget.
These changes apply when a new worker starts; they do not hot-reload a worker.

### Volatility click trading

Open UI orders pause new bot submissions until they fill or are cancelled.
The bot never cancels UI orders. A resting limit order therefore keeps it
paused, including automated hedges; close or cancel that UI order to resume.
Confirmed manual fills become portfolio inventory that the strategy can hedge
or exit under its normal rules. They are not exempt from risk management.

Pending strategy legs are discarded when account quantities differ from the
expected result of the bot's last confirmed fill, or open orders are observed.
A second account snapshot before submission catches concurrent UI activity.
There remains a race between the last read and submission because the API does
not provide an atomic account lock. Uncertain or partial fills of the bot's
own orders still halt for journal reconciliation; this feature does not bypass
that safeguard. Account waits are recorded as execution_wait events.

### Rejected volatility candidates

A local or fresh-account pre-trade RiskError is an ordinary no-trade result.
The worker logs `risk_rejection` (symbol, signed quantity, rejection reason,
and `submitted: false`), drops dependent queued legs, and replans on the next
snapshot. Fresh hedge decisions take precedence over queued option legs.
Expired snapshots and failed read-only final case checks also return waits.
Persistent constraints may continue to block trading; the bot never weakens
limits to force an order through. Unknown outcomes after submission and partial
fills still halt through the execution journal; these are not retried.

### Worker failure policy

During --watch, rejected volatility candidates and known transient snapshot
failures are recoverable waits. GET retries include 408/429/500/502/503/504,
connection/protocol failures, and malformed JSON, with three bounded attempts.
Authentication/configuration errors are not endlessly retried as transient errors.

Any unexpected cycle failure, including an ambiguous execution outcome, latches
the runner into read-only mode. It continues account polling and health output,
records a worker_halted event when logging is available, and does not call the
strategy again. This halt survives market resets in the running process. Stop
the worker, inspect the account and journal, resolve outstanding execution, and
explicitly restart after recovery. Never clear an execution journal to bypass
an unresolved intent. The supervisor does not start a second worker while the
halted worker remains alive.

This runtime policy does not bypass startup validation: an occupied journal
lock, unresolved prior intent, or invalid startup configuration still prevents
trading startup. Process termination and unavailable stdout/disk can also stop
the worker; keeping the process alive is not a guarantee of trading availability.

Paired-leg update: hedges now preserve pending option legs for revalidation.
Watched volatility trading immediately requests fresh state between option
fills, rather than sleeping the normal polling interval. Unused analyst news
is eligible for a configurable ten-tick post-publication entry window.
Execution journals now include the returned filled order and elapsed execution
seconds. Restart is required to load these changes; they do not hot-reload.

### Supervised convergence filter

Train a candidate model from recorded decisions after several complete heats:

```sh
python3 -m analysis.convergence data/volatility-decisions.jsonl \
  --output data/convergence-model.json --horizon 10
```

The trainer labels each candidate with the executable bid/ask outcome ten or
more ticks later, trains only on earlier complete heats, and reports mean
absolute error and directional accuracy on the final three heats. It does not
reconstruct actual fills, hedges, or counterfactual P&L.

Use a reviewed model explicitly; it remains disabled unless passed to the
runner:

```sh
python3 run.py volatility --source api --plan --watch \
  --convergence-model data/convergence-model.json
```

The filter can only reject entries. It never overrides forecast parsing, risk,
hedging, exits, or execution confirmation. Logs include its prediction,
horizon, training count, and holdout metrics.

### Console decision log

Volatility `--plan` and `--trade` output one readable line per cycle, for
example `tick 37 | WAIT | BUY 50 straddle edge $18.40 | fair IV 31.00% |
delta +422 | wait: existing option inventory is being held and risk-managed`.
The full all-option record remains in `--decision-log` for replay and model
training. Pass `--verbose` only when inspecting the raw JSON payload.
