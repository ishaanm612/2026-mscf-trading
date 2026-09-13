# Project guidance

This repository prepares the two RITCxCMU 2026 simulated trading cases: ETF arbitrage and volatility trading.

## File map and reading order

1. `README.md`: setup, commands, and current capabilities.
2. `CASES.md`: published rules, source links, model assumptions, and missing features.
3. `run.py`: CLI, synthetic demo snapshots, recording/replay, and polling loop.
4. `volatility/`: volatility V1 state, forecast, signals, hedging, logging, and pure strategy decisions.
5. `bot.py`: execution bridge; it submits at most one fresh, validated desired trade.
6. `risk.py` then `execution.py`: pre-trade gates, fill confirmation, and recovery journal.
7. `client.py`: shared REST/DMA transport and API snapshot collection.
8. `models/`: ETF math and reusable Black-Scholes pricing/Greeks.
9. `tests/`: pricing, risk, V1, execution failure, and mocked full-round tests.
10. `OPERATIONS.md`: live commands, strategy assumptions, and recovery procedure.

`reference/{etf,volatility}_{rest,dma}.py` contains original external examples. Read only when checking upstream API behavior; edit the project modules for project changes. `data/` holds local recordings and is not source code.

## Validation

```sh
python3 -m unittest discover -s tests -v
python3 run.py etf
python3 run.py volatility --sigma 0.25
```

## Implementation rules

- Keep shared API transport in `client.py` and case logic separate.
- Default to offline demos or read-only decision support. `--trade --source api` explicitly enables simulated account mutations; `--plan` uses the same strategy without submitting.
- Preserve small, named helpers and explain financial units, execution sequencing, and failure behavior in docstrings. Keep operational instructions in `OPERATIONS.md`.
- Never automatically retry mutations. Leave unresolved intents in the execution journal and stop on ambiguous/partial fills. Risk rejections and transport failures must be distinct exception types.
- Validate server limits as well as local limits: API security `limits[].units` is instrument units per risk unit, so 0.5 corresponds to 2x weighting. Verify this against actual fills when validating new cases.
- Preserve official scripts in `reference/` as downloaded, with attribution. They are educational references, not the production runner.
- Never commit API keys, trader credentials, `.env`, or private recorded account data.
- Distinguish published case rules from strategy assumptions and session-specific settings. Follow `CASES.md` sources; do not invent missing limits.
- ETF exposure counts RITC twice. Validate intermediate fills, outstanding orders, tender exposure, and FX before enabling execution.
- Options are contracts of 100 shares. Portfolio delta includes existing RTM shares. Use executable quotes, commissions, and remaining case time. Volatility forecasts aggregate integrated variance, never a direct average of volatilities.
- Automated converters are not supported by this case. Multi-leg trades are not atomic.
- Keep the starter dependency-free unless a dependency materially helps. Python 3.10+.
- Run `python3 -m unittest discover -s tests -v` and both demo commands after changing pricing or risk logic.
- Do not claim practice-server validation unless a session was actually tested.
