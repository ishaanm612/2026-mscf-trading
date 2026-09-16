# vol/ — RITC volatility-case bot

A deliberately simple bot for the volatility case: four short files, no
framework, every line readable. The API transport is adapted from the partner
repo ([ishaanm612/2026-mscf-trading](https://github.com/ishaanm612/2026-mscf-trading)).

## Files

| File | What it does |
|---|---|
| `bot.py` | Tick loop: poll case/securities/news, decide, trade, hedge, explain |
| `pricing.py` | Black-Scholes price/delta/vega, implied vol, T from tick |
| `news.py` | Parse the three news templates; weekly vol forecast |
| `rit.py` | Minimal REST transport (adapted from partner repo) |
| `test_vol.py` | Parity, IV round-trip, clock, news, and sizing tests |

## The strategy in one paragraph

The analyst news tells us realized vol exactly, week by week. The market
maker quotes a flat IV surface that **lags** each announcement. So: parse the
news into a forecast sigma (exact announcements only, latest carried
forward — ranges are a direction hint, never a trade trigger), price every
option with Black-Scholes at that sigma, and when the ATM straddle is
mispriced by more than costs + threshold, buy (or sell) straddles up to the
net limit. Exit when the MM's IV converges to the forecast; from tick 225
(week 4) hold through expiry and collect gamma instead. Keep portfolio delta
inside the penalty band by trading RTM shares.

## What you see each tick

```
[tick  76 | ACTIVE] spot 49.87 | T 0.0622y | r 2.00%
  news: exact [wk1 28%, wk2 15%] | ranges (hint only) [wk2 10%-20%]
  forecast σ 15.0% | ATM K=50 MM IV 27.9%
  straddle fair $2.31 vs mkt 4.05/4.07 | edge/straddle: BUY -186$, SELL +164$ (after $10 costs)
  position: opts gross 0/2500 net +0/±1000 | RTM +0 | delta +0 (hedge at ±5000, band ±7000)
  >> enter: SELL 500 straddles @ K=50, edge $164/straddle > $10 threshold
  sent: SELL 100 RTM50C @ market
  ...
```

Every run writes two timestamped files under `logs/`:

- `decisions-<ts>.jsonl` — what the bot thought each tick: forecast σ, ATM
  IVs, IV gap, edges, delta, the decision string, and any trades sent.
- `market-<ts>.jsonl` — the **full raw recording**: every security row
  (quotes, positions, realized/unrealized, nlv) plus the verbatim news.
  Written before any decision logic runs, so it survives bot crashes.
  This is the shared calibration data; it contains no names.

After a session, mine the recording:

```bash
python3 review.py logs/market-<timestamp>.jsonl
```

`review.py` prints: every news message verbatim with how the parser read it
(anything `!! UNPARSED` means fix `news.py`), whether the MM was stale at the
start, the MM's IV convergence speed after each announcement (tune
`CONVERGED_IV` and exit timing from this), realized vol per week vs announced
(gamma check), and the P&L trajectory with per-instrument attribution.

## Decision priority (in `process_tick`)

1. **Case inactive / expired** → wait.
2. **No parsed exact vol yet** → wait (never trade on a guess).
3. **|delta| ≥ 5,000** → hedge RTM back toward zero (penalty band is ±7,000).
4. **Holding options, before tick 225, MM IV within 1 vol pt of forecast** →
   close everything (the edge has converged).
5. **Holding options otherwise** → hold and keep hedging.
6. **Flat and ATM straddle edge > $10/straddle after $10 cost reserve** →
   enter at K nearest spot, up to 500 straddles (= the ±1,000 net limit).
7. Otherwise → wait, printing the exact edge that was insufficient.

## Deliberate simplifications (vs. the full plan in CLAUDE.md)

- **500 straddles max, one strike** — uses net ±1,000 but only 1,000/2,500
  gross. The full plan adds ~750 far-OTM contracts the other way to max
  gross; add that only once this version is boringly reliable.
- **Exit reads ATM IV only** — fine because the MM surface is flat.
- **No fill polling** — market orders against an unlimited-depth MM; positions
  are re-read from the API every tick, so partials self-correct.
- **Binary sizing** — full size or nothing past the threshold; the printed
  edge makes threshold tuning easy after practice sessions.

## Run

```bash
cd vol
python3 -m unittest test_vol -v      # before every session
python3 bot.py --dry-run             # watch decisions, no orders
python3 bot.py                       # live
```

Environment: `RIT_API_URL` (default `http://localhost:9999/v1`) and
`RIT_API_KEY` (default `Rotman`).

## Open questions to verify on the practice server

- Is the MM stale at tick 1 (free trade in week 1)?
- Shape/speed of MM IV convergence after a shock → tune `CONVERGED_IV`.
- Exact week-4 announcement tick (expected 225) → confirm `WEEK4_TICK`.
- Exact news wording → run `--dry-run` and watch for `!! UNPARSED` lines.
