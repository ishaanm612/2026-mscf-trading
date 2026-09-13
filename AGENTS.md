# Project guidance

This repository prepares the two RITCxCMU 2026 simulated trading cases: ETF arbitrage and volatility trading.

## File map and reading order

1. `README.md`: setup, commands, and current capabilities.
2. `CASES.md`: published rules, source links, model assumptions, and missing features.
3. `run.py`: CLI, synthetic demo snapshots, recording/replay, and polling loop.
4. `client.py`: shared read-only REST/DMA transport and API snapshot collection.
5. `etf.py` or `volatility.py`: independent case analysis; each exposes `analyze(snapshot, ...)` and returns a JSON-serializable report.
6. `tests/test_models.py`: pricing and risk regression tests.

`reference/{etf,volatility}_{rest,dma}.py` contains original external examples. Read only when checking upstream API behavior; edit the root modules for project changes. `data/` holds local recordings and is not source code.

## Validation

```sh
python3 -m unittest discover -s tests -v
python3 run.py etf
python3 run.py volatility --sigma 0.25
```

## Implementation rules

- Keep shared API transport in `client.py` and case logic separate.
- Default to offline demos or read-only decision support. Order execution is a future feature requiring explicit implementation and validation, not an existing capability.
- Preserve official scripts in `reference/` as downloaded, with attribution. They are educational references, not the production runner.
- Never commit API keys, trader credentials, `.env`, or private recorded account data.
- Distinguish published case rules from strategy assumptions and session-specific settings. Follow `CASES.md` sources; do not invent missing limits.
- ETF exposure counts RITC twice. Validate intermediate fills, outstanding orders, tender exposure, and FX before enabling execution.
- Options are contracts of 100 shares. Portfolio delta includes existing RTM shares. Use executable quotes, commissions, and remaining case time.
- Automated converters are not supported by this case. Multi-leg trades are not atomic.
- Keep the starter dependency-free unless a dependency materially helps. Python 3.10+.
- Run `python3 -m unittest discover -s tests -v` and both demo commands after changing pricing or risk logic.
- Do not claim practice-server validation unless a session was actually tested.
