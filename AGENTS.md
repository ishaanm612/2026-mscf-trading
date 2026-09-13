# Project guidance

This repository prepares the two RITCxCMU 2026 simulated trading cases: ETF arbitrage and volatility trading.

- Keep shared API transport in `rit_algo/client.py` and case logic separate.
- Default to offline demos or read-only decision support. Order execution is a future feature requiring explicit implementation and validation, not an existing capability.
- Preserve official scripts in `reference/` as downloaded, with attribution. They are educational references, not the production runner.
- Never commit API keys, trader credentials, `.env`, or private recorded account data.
- Distinguish published case rules from strategy assumptions and session-specific settings. Follow `docs/cases.md` sources; do not invent missing limits.
- ETF exposure counts RITC twice. Validate intermediate fills, outstanding orders, tender exposure, and FX before enabling execution.
- Options are contracts of 100 shares. Portfolio delta includes existing RTM shares. Use executable quotes, commissions, and remaining case time.
- Automated converters are not supported by this case. Multi-leg trades are not atomic.
- Keep the starter dependency-free unless a dependency materially helps. Python 3.10+.
- Run `python3 -m unittest discover -s tests -v` and both demo commands after changing pricing or risk logic.
- Do not claim practice-server validation unless a session was actually tested.
