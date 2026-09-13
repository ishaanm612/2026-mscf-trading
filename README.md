# MSCF trading: RITCxCMU 2026

A conservative practice-trading MVP for **Algorithmic ETF Arbitrage** and **Volatility Trading**, with shared REST/DMA access, offline demos, news parsing, opt-in execution, risk checks, and JSONL recording/replay. Python 3.10+; no external packages required for our runner.

```sh
python3 run.py etf
python3 run.py volatility --sigma 0.25
python3 -m unittest discover -s tests -v
```

These commands produce decision-support JSON. No orders, tender acceptances, or conversions are submitted. Demo prices and sigma are synthetic assumptions, not a backtest or calibrated strategy.

## File map

```text
AGENTS.md            Agent instructions and reading order
README.md            Setup and commands
CASES.md             Case rules, sources, assumptions, and next steps
run.py               Thin CLI, demos, recording, and replay
client.py             Shared REST/DMA connection
bot.py                ETF compatibility and execution bridge
risk.py               Pre-trade checks
execution.py          Serial orders, fill confirmation, recovery journal
OPERATIONS.md        Trading commands, design notes, recovery
models/
  etf.py             ETF arbitrage and tender analysis
  volatility.py      Black-Scholes prices, Greeks, and implied volatility
  news.py            Weekly volatility announcement parser
  __init__.py        Model package
volatility/
  config.py          All V1 costs, clocks, thresholds, and capacity settings
  market_data.py     Typed RIT snapshot ingestion
  forecast.py        News parsing and remaining integrated-variance forecast
  signals.py         Executable-edge, ATM straddle, and parity signals
  hedging.py         RTM no-trade-band hedge calculation
  strategy.py        Pure decision orchestration and abstract desired orders
  logger.py          Append-only structured decision logs
  DESIGN.md          Rationale, units, reserves, and calibration plan
analysis/
  reaction.py        Offline SVG chart of market-IV convergence after news
dashboard/
  server.py          Local live GUI for explainable decision logs
tests/               Model, execution, and full-round behavior tests
reference/           Original Rotman scripts, named by case and API
data/                Local recordings (ignored by Git)
pyproject.toml       Python version and project metadata
```

Start with `AGENTS.md` when editing this project. Run commands from the repository root.

## Connect during practice

For the Windows client REST API, enable its API and export the key and URL from your client settings:

```sh
export RIT_API_MODE=rest
export RIT_API_URL=http://localhost:9999/v1
export RIT_API_KEY='your-client-api-key'
python3 run.py etf --source api --watch --record data/etf.jsonl
```

For Mac/browser access, use DMA with the server endpoint and credentials supplied for the **selected case/session**. The website recommends client REST when available. Do not assume a browser UI URL is the API URL.

```sh
export RIT_API_MODE=dma
export RIT_API_URL='http://YOUR_CASE_SERVER:PORT/v1'
export RIT_USERNAME='your-trader-id'
export RIT_PASSWORD='your-password'
python3 run.py volatility --source api --sigma 0.25 --watch --record data/volatility.jsonl
```

Copy `.env.example` to `.env` and replace its placeholders; all project entry
points load that file automatically. Shell variables take precedence, so an
operator can override one setting for a command without editing `.env`. Keep
credentials out of source control. Ctrl+C stops polling. API errors stop the
runner; GET rate-limit retries are bounded.

Replay recorded observations without connecting:

```sh
python3 run.py volatility --source replay --file data/volatility.jsonl --sigma 0.25
```

Record explainable V1 decisions during a plan or an authorized practice heat,
then render the market-maker reaction chart offline:

```sh
python3 run.py volatility --source replay --file data/volatility.jsonl --plan \
  --decision-log data/volatility-decisions.jsonl
python3 -m analysis.reaction data/volatility-decisions.jsonl \
  --output data/market-maker-reaction.svg
```

The chart draws the fair-IV gap by time since news (blue) and by time since a
straddle signal (purple). It shows association, not proof that our trade caused
the quote change. `--no-explainability` retains decision measurements while
omitting the factor-level rationale payload.

For a separate real-time GUI, start the local dashboard and visit the printed
loopback address. It refreshes as the planner appends decisions:

```sh
python3 -m dashboard.server --log data/volatility-decisions.jsonl
```

## What is implemented

- `models/etf.py`: remaining-depth VWAP, FX-adjusted basket comparisons with fees, weighted exposure and sequential hypothetical-fill checks, fixed-price tender unwind estimates. Supply `--gross-limit` and `--net-limit` from the actual session; otherwise risk eligibility is unknown (`null`).
- `volatility/`: V1 uses a typed state, news-triggered integrated-variance forecast, all-option Black-Scholes values and Greeks, executable-price edges after commissions/hedging reserves, ATM straddle selection, hysteresis exits, RTM hedge band, put-call-parity scan, and structured decision data. All thresholds are in `VolatilityConfig`.
- `reference/`: unmodified official starter scripts. These require their own third-party dependencies and some can submit trades; do not use them as the project entry point.
- `CASES.md`: source links, rules, ambiguities, and next implementation steps.

## Trading mode

Read [OPERATIONS.md](OPERATIONS.md) for exact commands and recovery instructions. Default commands remain read-only. `--plan` reports the next bot action; `--trade --source api` enables orders and tender acceptance on the configured simulated account.

Volatility uses weekly news, small option entries, RTM delta hedges, and convergence/time exits. ETF trading prioritizes FX hedging and inventory unwinds, then selective fixed-price tenders. Basket trading is separately enabled with `--basket`. Do not enable `--trade` until a practice heat has been explicitly authorized.

Execution writes intent to disk, submits once, and confirms fills. Partial fills and uncertain outcomes stop the bot. This is a practice MVP, with conservative fixed sizing; it is not a proven profitable strategy. Snapshots are sequential reads, and replay does not simulate fills or P&L.
