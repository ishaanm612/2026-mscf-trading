# MSCF trading: RITCxCMU 2026

Python starters for **Algorithmic ETF Arbitrage** and **Volatility Trading**, with shared REST/DMA access, offline demos, JSONL recording/replay, and model tests. Python 3.10+; no external packages required for our runner.

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
run.py               Entry point, demos, recording, and replay
client.py            Shared REST/DMA connection
etf.py               ETF arbitrage and tender analysis
volatility.py        Option pricing and portfolio hedging analysis
tests/test_models.py Model regression tests
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

Environment variables are read directly; `.env` files are not automatically loaded. Keep credentials out of source control. Ctrl+C stops polling. API errors stop the runner; GET rate-limit retries are bounded.

Replay recorded observations without connecting:

```sh
python3 run.py volatility --source replay --file data/volatility.jsonl --sigma 0.25
```

## What is implemented

- `etf.py`: remaining-depth VWAP, FX-adjusted basket comparisons with fees, weighted exposure and sequential hypothetical-fill checks, fixed-price tender unwind estimates. Supply `--gross-limit` and `--net-limit` from the actual session; otherwise risk eligibility is unknown (`null`).
- `volatility.py`: Black-Scholes fair values, delta/vega, numerical implied volatility, bid/ask signals with cost reserves, portfolio exposure, and RTM hedge suggestions. `--sigma` is an explicit annualized forecast assumption; news is displayed but not automatically interpreted. `--rate` defaults to zero.
- `reference/`: unmodified official starter scripts. These require their own third-party dependencies and some can submit trades; do not use them as the project entry point.
- `CASES.md`: source links, rules, ambiguities, and next implementation steps.

The next ETF milestone is an execution engine with position/order reconciliation, tender inventory unwinds and FX hedging. Volatility needs a news-driven variance forecast and trade sizing. Current outputs are independent signals, not a jointly risk-approved order plan. Snapshots use sequential API reads; displayed depth and prices can change. Replay recalculates signals only and does not simulate fills or P&L.
