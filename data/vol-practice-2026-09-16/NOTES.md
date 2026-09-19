# Volatility practice day — 2026-09-16 (six heats, trader qubr-1)

Raw calibration data from the practice server. Each heat has a full per-tick
market recording (`market-*.jsonl`: case, every security row, verbatim news)
and the bot's decision journal (`decisions-*.jsonl`). Replay any recording
with `vol/review.py <file>`. No account-identifying data; timestamps are
file-name suffixes (HHMMSS local).

## Heat-by-heat

| Heat | Log suffix | Vol path (wk1-4) | Result | What happened |
|---|---|---|---|---|
| 1 | 191158 (first part) | 29/15/18/22ish | ~$41k | v1: two good entries; ~$9k fines from naked-RTM exits; week-4 "hold for gamma" bled ~$20k against an overshot MM |
| 2 | 191158 (after reset) | 18/36/17/36 | ~$194k | v1: five clean round trips incl. stale tick-1 MM; ~$16k fines (exit-leaves-RTM bug); RTM hedge peaked 46.6k/50k |
| 3 | 192639 + 193052 | 25/11/13/17 | ~$95k | v2: fines ~$7k (serial unwind windows); $40 entry floor blocked a $15-26 overshoot scalp; MM began pre-positioning at range midpoints |
| 4 | (RIT client only) | 29/16/28/18 | ~$143k | v2.1 ($15 floor): 4th on leaderboard, ~8% behind leader |
| 5 | 194308 | 33/31/27/23 | ~−$5k | v3 (max-gross book): +$73k at t150, then exit STARVED by hedge-priority bug; rode to close-out |
| 6 | 194935 + 195625 | 12/33/20/23 | large loss | v3.1: exit deadlocked on net-limit 400 rejections (alphabetical leg order), book unhedged for ~40+ ticks; v3.2 fix flattened it at t291 |

## Verified findings (also in repo CLAUDE.md)

- MM stale at tick 1 every heat -> free trade, worth full size instantly.
- MM convergence accelerates within a heat and pre-positions at range
  midpoints; edge ~ distance of announcement from midpoint. Overshoots
  2-5 pts after converging (tradable from flat).
- Exchange 400-rejects orders that transiently breach net limit ->
  sequence limit-reducing legs first; skip rejected chunks, never freeze.
- Fines are per-second and cluster in unbalanced multi-tick sequences.
- Realized vol tracked announcements within ~1-2.5 pts.

## Deep-dive TODO

- Per-event capture ratio: gap-at-event x vega x contracts vs banked P&L,
  for all ~20 events across six heats (data sufficient in market logs).
- MM overshoot statistics: magnitude/half-life -> re-entry rule tuning.
- Hedge cost vs fine tradeoff: optimal trigger from replayed paths.
- Reconcile RIT blotters (screenshots in chat) against decision logs for
  heats 5-6 exact loss attribution.
