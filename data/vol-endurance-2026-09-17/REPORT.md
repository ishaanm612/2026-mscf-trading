# Vol bot 15-heat endurance run — 2026-09-17 (v3.2, code frozen)

Fifteen consecutive practice heats on one frozen build. Raw logs alongside
this file; replay with `vol/review.py`.

## Headline

**15 of 15 heats profitable.** Realized+unrealized proxy: total +$2.16M,
mean +$144k, median +$144k, best +$239k, worst +$35k. Fines are not in the
proxy, so actual account P&L ≈ proxy − fines ≈ **$132k/heat average**.
Yesterday's best heat ($194k) would be merely average today; yesterday's
leaderboard winners ($155-165k) would be beaten by 7 of these 15 heats.

## What worked (keep frozen)

- **Tick-1 free trade confirmed at scale**: MM stale by 4-14 vol pts
  (mean 8.0) at the open of 13/13 measurable heats. Entered by tick ~3
  every time.
- **Announcement capture**: 56 announcement round trips, +$1.77M total,
  51/56 winners, mean +$32k. Losses were all small ($3-6k) instant-
  reversal entries.
- **Overshoot scalps** (re-entry after convergence, away from
  announcements): 18 trips, +$273k, 16/18 winners, mean +$15k — the $15
  entry floor is earning its keep; do not raise it.
- **Directional exits**: no repeat of the heat-5 (9/16) starvation or
  deadlock; every exit landed within ~1 vol pt of forecast.
- **Robustness**: zero unparsed news, zero order-rejection freezes, zero
  crashed heats across 15 heats and ~80 entries/exits.

## Leaks, ranked by dollars

1. **Fines $182k total (~$12k/heat), heat 6 alone $48k.** Heat 6's burst
   is structural, not a tuning issue: near expiry with spot far from the
   held strike, a full-size book's delta (~±85k shares) exceeds the 50k
   RTM limit — the bot maxes its hedge and then sits unhedgeable at
   ~$2.6k/sec until exit. Needs a hedgeability guard (below).
2. **Wing/ATM collision.** This server lists strikes 48-52 only. When the
   ATM is 48 or 52, the far wing shares the ATM strike and self-cancels:
   ~1/3 of entries ran at gross 1,700 instead of 2,400 (≈30% less vega on
   exactly the trades that happened at extreme spots).
3. **Commissions ~$57k/heat** are already netted in the P&L and are
   mostly the unavoidable price of 5-7 full-size round trips; no change
   recommended beyond not adding churn.

## Improvement areas for competition day, in order

1. **Hedgeability guard (fixes the heat-6 fine burst).** Each tick, if
   |portfolio delta| > (RTM_LIMIT − |rtm|) + DELTA_BAND, close option
   chunks until the remaining delta is hedgeable. Caps the worst heat.
2. **Wing selection at the chain edge.** Wings must exclude the ATM
   strike: if ATM is the highest strike, put both wings in puts (and vice
   versa), or size 700 on the single available wing. Restores full vega
   on ~1/3 of entries.
3. **Late-heat entry sizing.** t225+ entries were the only systematic
   small losers and carry the deep-ITM fine risk; consider half size
   (or a vega-scaled size) after tick ~260.
4. **Ops hardening (done during the run, keep):** bot runs as
   `python3 -m bot` (immune to loose pkill patterns); never use blanket
   `pkill -f "python3 bot.py"` with two bots up; keep live logs OUTSIDE
   OneDrive-synced folders — OneDrive created sync-conflict copies of the
   actively-written log mid-run.
5. **Port discipline:** case/server ports changed overnight (vol moved
   16655 → 16595). Verify the port against `GET /case` (case name) at
   startup rather than assuming; a wrong-case connection should refuse to
   trade. (The bot currently trades whatever RTM-shaped case it sees.)

## Reference numbers for tuning

- Convergence: MM pre-positions at range midpoints; edge scales with the
  announcement's distance from the midpoint. Overshoots of 2-5 pts remain
  common and tradable.
- Best trips held 40-120 ticks (t75/t150 entries with big
  midpoint-misses); worst trips were 4-6-tick instant reversals.
- Realized vol continues to track announcements within ~1-2.5 pts.
