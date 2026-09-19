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
| ETF | `http://flserver.rotman.utoronto.ca:16655/v1` |

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

ETF mode evaluates tenders against the current portfolio, including tenders that offset existing inventory. Add `--basket` to enable three-leg convergence entries. `--quantity` is the target shares **per basket leg** (default 1,000), while `--child-size` caps each equity order (default and maximum 10,000, further bounded by the venue). A larger basket is built in balanced three-leg slices with fresh quotes and intermediate risk checks. Capacity or deteriorating prices can shrink a slice or stop the build below target. Every tender prints its route, profit, separate execution/FX reserves, decision and binding reason. API bot mode also appends these decisions to `data/etf-decisions.jsonl` unless `--decision-log` selects another file. The console keeps ordinary actions compact; `--verbose` prints the full route calculations as well.

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

For an authorized trading heat with a 20,000-share target per basket leg,
10,000-share children, and the aggressive default uncertainty coefficients:

```sh
python3 scripts/supervise_volatility.py --case etf --trade --basket \
  --gross-limit 300000 --net-limit 200000 --quantity 20000 --child-size 10000 \
  --execution-risk-k 0.25 --fx-risk-k 0.5 --manual-wait-ticks 8 \
  --decision-log data/etf-decisions.jsonl --record data/etf-snapshots.jsonl
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

Snapshots are not atomic. Both cases refresh account state before order submission and replan if positions or open orders changed. ETF additionally checks session continuity and refreshed cash/stock limits; a drift during a partial basket latches reduction. ETF execution still requires exclusive account use because the final check and exchange mutation cannot be atomic. Market orders can execute away from observed quotes; thresholds reduce this risk but cannot eliminate it.

A known open account order makes the ETF worker wait without submitting or cancelling orders. When it clears, the worker rechecks confirmed inventory; an interrupted held basket stays latched for reduction. This wait does not clear an ambiguous/partial execution halt. Older workers that already latched the former `Existing open orders must be reconciled` error need to be stopped and restarted after checking/reconciling the account.

## Volatility decisions and units

- An option quote is dollars per underlying share. A position is **contracts**, with 100 shares per contract. Portfolio delta is `RTM shares + sum(contracts × 100 × option delta)`.
- Announcements assign variance to weeks. For a range, the estimate is the mean of squared endpoints. Remaining-time variance weights each week's estimate by its remaining seconds. Unannounced future weeks use a 20% prior, or `--sigma` when supplied; they do not inherit the last print.
- If absolute portfolio delta exceeds 250 shares, hedge RTM first. Proposed trades cannot increase exposure beyond an internal 6,000-share band, below the published 7,000 penalty threshold.
- Option entry uses the bid/ask, fair value, and conservative fee reserves. Buy when fair value exceeds the ask by enough; sell when the bid exceeds fair value. Require a further 0.03/share edge, trade ten contracts, cap each option at 50 contracts and total options at 200 gross contracts.
- Exit when the original signal disappears or reverses. From tick 240, open no new positions and work toward flat inventory. RTM is hedged between option exits. Flat-by-end is a target, not a guarantee under errors or slow execution.
- Pricing reserves two option commissions and an initial RTM hedge commission. It does not forecast every future hedge cost. Delta depends on a model, especially near expiry.

## ETF decisions and units

- Keep RITC and its naturally offsetting USD cash together while equity inventory is worked. Convert only the final net USD balance after the equity position is flat; this avoids paying an unnecessary gross FX round trip.
- Before tender acceptance, compare direct liquidation, manual conversion and mixed routes from the resulting portfolio. ETF Redemption consumes 10,000 RITC and produces 10,000 BULL plus 10,000 BEAR; ETF Creation does the inverse. Each use costs USD 1,500. Creation routes may buy missing BULL/BEAR in legal children before the operator uses the converter; every intermediate stage must fit stock and cash limits. Route value is incremental versus liquidating the current inventory, allowing profitable exposure-reducing tenders.
- A preferred manual route prints `MANUAL WIND/UNWIND REQUIRED` and a deadline. The default wait is eight ticks after preparation, subject to enough remaining time for liquidation. After timeout, direct reduction stays latched until flat. Forced end-of-round or `--flatten-only` liquidation always overrides manual waiting. Set `--manual-wait-ticks 0` to disable manual routes, including converter-dependent tender acceptance. If a manual action is missed or markets move, the direct fallback can realize a loss.
- Otherwise unwind existing equity inventory one child at a time, selecting a risk-legal order with enough visible depth. On restart, existing baskets are treated as inventory to close.
- Accept only fixed-price RITC tenders whose selected route clears the larger of C$0.0025/share or the execution-plus-FX uncertainty reserve. Frozen-book routes consume each displayed level once. Direct RITC depth is not required when a complete legal converter route supplies an exit. Staged direct routes have additional evidence and stress-exit checks below. Every route must fit before case end, with legal child sizes, final FX children, a manual allowance where needed, and a five-tick end buffer. Tender expiry is only the acceptance deadline; a fresh full snapshot must still contain and approve the offer before submission.
- `DIRECT_STAGED` is enabled by default for tenders taken from an equity-flat account. It requires at least four past arrival intervals spanning 12 ticks and at least `--staged-min-active-intervals 2` **nonzero** intervals. Only previously unseen RITC order IDs created since the previous observation and within US$0.05 of the current touch count. Pace children at no more than `--staged-participation 0.5` of the median nonzero arrival rate. Repeated snapshots and old displayed orders do not count as replenishment; history resets each heat and evidence older than two ticks is unavailable. Missing order IDs/timestamps disable this forecast, not ordinary frozen-book trading.
- Staged pricing gives only 50% credit for recovery from depleted-book VWAP toward current child VWAP, with an additional US$0.05 adverse-price allowance on the refreshed reference. This is a forecast, not certain liquidity. The full frozen-book direct exit must remain priceable and its modeled loss must not exceed `--tender-max-fallback-loss 0.10` CAD/share. The schedule must fit `--tender-max-unwind-ticks 60` and the case deadline; all forecast AND fallback cash/stock stages are checked. `--no-staged-tenders` disables the model. Both runner and supervisor accept these controls.

The September 17 direct-route ablation in `analysis/etf_ablation.py` used 14
recorded heats, optimized chronologically on the first nine and held out the
last five. It compares only independent, flat-account direct routes and
crosses later recorded books; it cannot model manual conversions, market
impact, queue position, concurrent offers, or realized server P&L. The
selected robust gate kept the tender reserve coefficients at 0.15/0.25,
required two active intervals, credited 50% participation, and used the
C$0.10/share fallback. It accepted 5 training and 2 held-out offers; the
held-out two simulated outcomes were positive. This is too little data to
claim an optimum or profitability guarantee, and the report remains an offline
counterfactual rather than a live backtest.

For controlled **live practice** comparison, keep basket trading disabled and
launch exactly one tender policy per new heat:

```sh
python3 scripts/run_etf_live_ablation.py --trade \
  --gross-limit 300000 --net-limit 200000
```

It waits for a stop/reset after launch and starts only at tick 0--2 of the
next heat. The three arms are frozen-book baseline, selected staged policy,
and stricter staged policy. Each has separate ignored journal, decision-log,
and raw-snapshot files under `data/etf-ablation-*`; a worker error halts the
experiment rather than proceeding to another arm. The script does not enable
`--basket`, does not test low-reserve settings, and never joins the currently
active heat.
- After a staged acceptance, new tenders and conversions are deferred. Submit one confirmed child per slot only when fresh depth meets its forecast price bound. Wait at most six additional ticks for a missed slot, never past the route deadline. Lost fallback depth, changed inventory, the loss trigger, or a missed deadline latches direct liquidation through final USD. No mutation is retried. The loss trigger is **not** a guaranteed maximum realized loss: quotes can gap and market orders can slip. Console alerts explicitly label staged forecasts and show frozen-book fallback P&L.
- Execution reserve in CAD is `k_exec × sum(abs(child shares) × sigma_price × sqrt(time to child fill) × currency conversion)`. FX reserve is `k_fx × abs(final net USD) × sigma_FX × sqrt(route horizon)`. Price sigmas are trailing RMS midpoint changes per square root of tick, using up to 30 past intervals from the current heat. Startup uses a quarter-spread proxy and explicit floors (0.005 equity quote-currency dollars and 0.0001 CAD/USD per square root of tick). Three ticks per action is the initial scheduling assumption. Spread, depth and commissions are already in route cashflows and are not charged again as uncertainty.
- Basket coefficients remain `--execution-risk-k 0.25 --fx-risk-k 0.5`. Tender reserves use independent `--tender-execution-risk-k 0.15 --tender-fx-risk-k 0.25` defaults in both runner and supervisor. These are risk preferences, not statistical guarantees. The September 17 logged heat (ticks 82–298) contained six distinct tenders; five never showed a positive executable route, while tender 3131 at tick 203 showed a three-block redemption profit of C$8,530.32 against the former C$10,192.46 reserve. The revised weights reduce that route's allowance to C$5,602.28. This is a counterfactual eligibility calculation on recorded decisions, not a simulated fill or realized-P&L backtest. The route still requires manual conversion and all existing risk/time checks.
- There is no fixed initial cash reserve. Each route recomputes its CAD profit hurdle from quantity, quote spreads, serial fill times, and observed volatility. At worker startup the history is empty: equity sigma is `max(0.005, spread/4)` and FX sigma is 0.0001 CAD/USD/sqrt(tick), replaced by trailing observed RMS with the same floors. For a direct RITC unwind at FX midpoint 1, a two-cent equity spread and near-flat final USD, the new startup hurdle is about C$25 for 10,000 shares (the minimum-profit floor binds), and C$292 for 100,000 shares in ten children. Wider spreads or observed price movement raise it. Decision reserve details identify whether startup or observed volatility supplied each sigma. Restarting a worker resets this history; it is not a way to recalibrate risk.
- `--basket` explicitly enables **capped convergence positions**, including sizes smaller than a converter block. These are not locked arbitrages. The configured basket target is bounded by `--basket-max-quantity` (default 20,000 shares per leg), legal child caps, current depth and intermediate risk. No minimum 10,000-unit position is forced simply to qualify for a manual converter. A feasible, cheaper manual route can still be recommended for eligible whole blocks; automated converters remain unsupported.
- Basket entry records both immediate executable liquidation P&L and a **conditional** payoff if the parity gap closes. It requires the latter to cover modeled exit spreads/depth, exit commissions, entry/exit execution reserves, FX uncertainty and C$0.02/share profit (or the take-profit target when larger). The former extra C$0.10/share hurdle is removed. Exit friction is measured against the top-of-book midpoint, preserving depth costs. FX uncertainty in establishing the entry reference and on the final net USD is explicit; no gross USD round trip is submitted. Reject an entry whose immediate liquidation loss already meets/exceeds its loss budget. These assumptions are not a forecast that convergence will occur.
- Basket execution uncertainty now uses scheduled cashflow variance: for each action interval, square the remaining aggregate quantity of each ticker times its CAD price sigma, sum across tickers, and multiply by interval length. Take the square root of total variance and multiply by `k_exec`. Children of the same ticker are correlated through their shared remaining exposure; splitting cannot create fake diversification. Entry and exit have separate execution clocks. Combine execution phases by root-sum-square, combine FX phases similarly, then add the execution and FX allowances. Independent instruments/tick increments are explicit approximations, not calibrated covariance estimates or confidence bounds. Holding/convergence risk remains controlled by the separate loss, age and size rules; this execution reserve does not guarantee convergence.
- Value the held basket before any addition or tender. Never add while executable basket P&L is negative; exits take precedence over additions. Historical fill cashflows and the **original** entry tick survive successful additions. Default exit triggers: executable profit above C$0.02/share plus exit uncertainty reserve, executable loss of C$0.30/share, 60 ticks since the first entry, a parity gap that has converged/reversed, or the inventory-dependent liquidation deadline. Profit can be taken immediately. A reversed gap exits even if past costs leave the position slightly negative; do not wait just to recover sunk costs. The loss threshold initiates market liquidation and is not a guaranteed fill price or maximum realized loss.
- Basket controls are `--basket-max-hold-ticks 60`, `--basket-min-hold-ticks 10`, `--basket-take-profit 0.02`, `--basket-stop-loss 0.30`, `--basket-max-quantity 20000`, and `--basket-cooldown-ticks 5`; the runner and supervisor both accept them. CAD thresholds are per basket unit (one share of each equity), not per individual leg. Minimum hold ticks reserve an opportunity window **before entry**, not a prohibition on early profit-taking. A new slice must fit entry actions, that opportunity window, all projected liquidation children and the end buffer; there is no fixed tick-250 cutoff. Additions must also fit inside the original basket's remaining age limit.
- After each confirmed serial fill, price completion again using the next leg's fresh snapshot. Compare the buffered **conditional** completion value with the executable abort loss, permit negative completion value only inside the configured loss budget, and bound the immediate full-basket liquidation loss too. Remaining-leg execution uncertainty and price risk on already-filled partial inventory are separate allowances. This is a risk-taking decision, not a claim that a conditional value is guaranteed cash. A vanished book, expired preflight or read failure after confirmed fills latches direct inventory reduction; incomplete/ambiguous mutations still halt for reconciliation. Never send remaining entry or abort legs after observing a session boundary. An aborted addition triggers reduction of the existing basket as well.
- Stops, timeouts, inventory/FX mismatches and partial-entry recovery stay latched through final net USD liquidation, deferring tenders and manual conversions until flat. A five-tick cooldown follows completed reduction. Manual preparation also defers new tenders. On restart, basis is not reconstructed automatically from a mutation journal: existing positions are handled as inventory, never silently adopted as a newly opened convergence basket.
- ETF decision logs include pre-action account positions and the server's per-security realized/unrealized P&L in their reported native currency. Missing fields remain null; cross-currency amounts are not summed into a fabricated account P&L. Basket records include entry basis, conditional and executable values, uncertainty reserves, holding age and exit cause; the compact CLI prints held P&L and exit reasons. Use `--record` as well to retain raw books for a full replay.

Offline tender eligibility comparison (no API calls, fills or realized-P&L simulation):

```sh
python3 -m analysis.etf_audit data/etf-snapshots.jsonl --flat --no-staged-tenders
python3 -m analysis.etf_audit data/etf-snapshots.jsonl --flat
```

The local September 17 audit of 1,605 recorded snapshots and 94 distinct offers
found 9 eligible offers with frozen-book routes and 10 with staged routes.
`--flat` is a counterfactual account assumption; these counts do not show that
the strategy would have filled those offers or earned a profit. Most recorded
offers still failed economic/risk gates. New model code is loaded by the next
worker, not by an already running worker.
- A tender can qualify late only when its size-aware liquidation budget fits before case end. Gross risk counts RITC twice. Server `limits[].units` expresses instrument units per risk unit: a binding of 0.5 means a reciprocal weight of 2. Tender estimates are static depth calculations, not promises about the eventual unwind.

## Failure and recovery

Execution journals default to `data/<case>-<username>-execution.jsonl`. Entries are flushed to disk before requests. No password is recorded. A mutation failure, partial fill, or read failure while confirming an outstanding mutation stops execution. ETF read failures or known risk rejections before submission replan from fresh inventory; confirmed partial basket sequences become inventory to manage. POST/DELETE are never automatically retried.

Tender acceptance can be acknowledged before the securities endpoint reflects
its inventory effect. The executor submits exactly once, then checks up to 12
times, separated by 0.25 seconds (plus API latency). Only an unchanged
pre-tender position is allowed to wait. A partial/unexpected position, session
change, read failure or timeout keeps the journal unresolved and halts. The
journal records the offer, expected position, acknowledgement and confirmation
outcome. This fixes the old single-read false halt; it does **not** automatically
clear journals left by an older worker or retry their acceptance requests.

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

Audit ETF eligibility without contacting the server:

```sh
python3 -m analysis.etf_audit data/etf-snapshots.jsonl --flat --limit 1005
```

Omit `--flat` to retain recorded account inventory and server counters. Both
modes use only earlier quotes for the risk estimates. No fills are simulated.

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
