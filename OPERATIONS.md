# Running and understanding the MVP

## Connection setup

Run commands from the repository root, with Python 3.10+. There are no pip dependencies. Credentials are environment variables, never source files. Do not run the original scripts in `reference/` alongside this bot.

For DMA, place `RIT_API_MODE=dma`, `RIT_USERNAME`, `RIT_PASSWORD`, and the
appropriate `RIT_API_URL` in the ignored repository `.env` file. Copy
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

Use the lifecycle supervisor when a practice server repeatedly stops and starts.
It waits for RIT to report `ACTIVE`, launches exactly one worker for that heat,
and starts another only after the worker observes a stop, a changed period, or a
reset tick.  Decision logs, recordings, and execution journals stay append-only
across heats; the dashboard can therefore retain history while separating runs.

```sh
python3 scripts/supervise_volatility.py --trade \
  --decision-log data/volatility-decisions.jsonl \
  --record data/volatility-snapshots.jsonl
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

Snapshots are not atomic. No other trader or program should change this account while the bot runs. Market orders can execute away from observed quotes; conservative thresholds reduce this risk but cannot eliminate it.

## Volatility decisions and units

- An option quote is dollars per underlying share. A position is **contracts**, with 100 shares per contract. Portfolio delta is `RTM shares + sum(contracts × 100 × option delta)`.
- Announcements assign variance to weeks. For a range, the estimate is the mean of squared endpoints. Remaining-time variance weights each week's estimate by its remaining seconds. Unannounced future weeks inherit the most recent known estimate; this is a modeling assumption.
- If absolute portfolio delta exceeds 250 shares, hedge RTM first. Proposed trades cannot increase exposure beyond an internal 6,000-share band, below the published 7,000 penalty threshold.
- Option entry uses the bid/ask, fair value, and conservative fee reserves. Buy when fair value exceeds the ask by enough; sell when the bid exceeds fair value. Require a further 0.03/share edge, trade ten contracts, cap each option at 50 contracts and total options at 200 gross contracts.
- Exit when the original signal disappears or reverses. From tick 240, open no new positions and work toward flat inventory. RTM is hedged between option exits. Flat-by-end is a target, not a guarantee under errors or slow execution.
- Pricing reserves two option commissions and an initial RTM hedge commission. It does not forecast every future hedge cost. Delta depends on a model, especially near expiry.

## ETF decisions and units

- First reduce actual USD cash exposure. FX child orders use current account inventory, not predicted cash from a submitted order.
- Unwind existing equity inventory one child at a time, selecting a risk-legal order with enough visible depth. On restart, existing baskets are treated as inventory to close.
- Accept only fixed-price RITC tenders with sufficient visible unwind depth, at least three ticks remaining, enough projected risk capacity, and a 0.05 USD/unit cushion beyond modeled unwind fees.
- Optional baskets enter only while equity inventory is flat and estimated CAD edge exceeds 0.10/unit after modeled fees. Each leg must fully fill; inventory is reread before the next. Hold until the entry-direction edge disappears, then unwind. No automated converters are used.
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
