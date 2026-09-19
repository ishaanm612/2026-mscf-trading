#!/usr/bin/env python3
"""Run one controlled ETF tender-policy variant on each *fresh* practice heat.

This is an opt-in live experiment runner. It isolates journals, decision logs
and raw snapshots by policy and deliberately leaves ``--basket`` off, so the
comparison measures tender liquidation policy only. It never joins a heat that
was already active when launched: it observes a stop/reset and then requires a
new active heat at tick 0--2 before starting a worker.  A heat contaminated by
another client is retained as an invalid observation and never prevents later,
fresh heats from running.
"""
from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client import Client
from environment import configure_case_environment, load_env_file
from volatility.supervisor import SessionMarker


@dataclass(frozen=True)
class Policy:
    """A conservative policy arm; reserves remain fixed across every arm."""

    name: str
    staged: bool
    active_intervals: int
    participation: float
    fallback_loss: float


# Ten independent heats.  Repetitions are deliberate: one live heat is an
# observation, not evidence that one policy is better than another.  The
# suffix is part of the journal name so no arm can overwrite another's audit.
POLICIES = (
    Policy("frozen_book_01", False, 0, 0.0, .10),
    Policy("staged_selected_01", True, 2, .50, .10),
    Policy("staged_strict_01", True, 3, .50, .075),
    Policy("staged_selected_02", True, 2, .50, .10),
    Policy("staged_half_01", True, 2, .35, .10),
    Policy("staged_selected_03", True, 2, .50, .10),
    Policy("staged_high_flow_01", True, 2, .65, .10),
    Policy("staged_strict_02", True, 3, .50, .075),
    Policy("frozen_book_02", False, 0, 0.0, .10),
    Policy("staged_selected_04", True, 2, .50, .10),
)


def worker_command(args: argparse.Namespace, policy: Policy) -> list[str]:
    """Build a single fresh-heat worker command for a named policy arm."""
    stem = f"data/etf-ablation-{args.run_id}-{policy.name}"
    command = [sys.executable, "run.py", "etf", "--source", "api", "--watch", "--exit-on-session-change",
               "--trade" if args.trade else "--plan", "--gross-limit", str(args.gross_limit),
               "--net-limit", str(args.net_limit), "--child-size", str(args.child_size),
               "--tender-execution-risk-k", str(args.tender_execution_risk_k),
               "--tender-fx-risk-k", str(args.tender_fx_risk_k),
               "--tender-max-fallback-loss", str(policy.fallback_loss),
               "--journal", f"{stem}-execution.jsonl", "--decision-log", f"{stem}-decisions.jsonl",
               "--record", f"{stem}-snapshots.jsonl"]
    if policy.staged:
        command.extend(["--staged-min-active-intervals", str(policy.active_intervals),
                        "--staged-participation", str(policy.participation)])
    else:
        command.append("--no-staged-tenders")
    return command


def wait_for_fresh_heat(client: Any, poll_seconds: float) -> SessionMarker:
    """Wait for a reset after launch, then return only a tick-0--2 active heat."""
    initial = SessionMarker.from_case(client.get("case"))
    saw_boundary = not initial.is_active
    previous = initial
    last_reported = (initial.status, initial.period)
    last_heartbeat = time.monotonic()
    print(f"ablation: waiting for a fresh heat; observed status={initial.status} "
          f"period={initial.period} tick={initial.tick}", flush=True)
    while True:
        try:
            marker = SessionMarker.from_case(client.get("case"))
        except (OSError, RuntimeError, ValueError) as error:
            print(f"ablation: case read failed; retrying: {error}", flush=True)
            time.sleep(poll_seconds)
            continue
        if (not marker.is_active or marker.period != initial.period or marker.tick < previous.tick):
            if not saw_boundary:
                print("ablation: session boundary observed; armed for the next tick-0--2 heat", flush=True)
            saw_boundary = True
        state = (marker.status, marker.period)
        if state != last_reported:
            print(f"ablation: case transition status={marker.status} period={marker.period} "
                  f"tick={marker.tick}", flush=True)
            last_reported = state
        if saw_boundary and marker.is_active and marker.tick <= 2:
            print(f"ablation: fresh heat confirmed period={marker.period} tick={marker.tick}", flush=True)
            return marker
        if time.monotonic() - last_heartbeat >= 15:
            phase = "armed for reset" if saw_boundary else "ignoring current heat"
            print(f"ablation: still waiting ({phase}); status={marker.status} "
                  f"period={marker.period} tick={marker.tick}", flush=True)
            last_heartbeat = time.monotonic()
        previous = marker
        time.sleep(poll_seconds)


def verify_flat_new_heat(client: Any, marker: SessionMarker) -> None:
    """Require a flat, order-free account before an independent policy arm.

    CAD P&L is allowed to carry across heats, but equity and USD inventory
    would contaminate an arm's tender decisions. This check never cancels or
    closes anything; the operator must resolve a non-flat account first.
    """
    snapshot = client.snapshot("etf", trading=True)
    current = SessionMarker.from_case(snapshot["case"])
    if (not current.is_active or current.period != marker.period or current.tick > 2):
        raise RuntimeError("fresh heat advanced during account preflight; no worker started")
    if snapshot.get("orders"):
        raise RuntimeError("open account orders; no ablation worker started")
    positions = {row["ticker"]: float(row["position"]) for row in snapshot["securities"]}
    nonflat = {ticker: positions.get(ticker, 0.0) for ticker in ("BULL", "BEAR", "RITC", "USD")
               if abs(positions.get(ticker, 0.0)) >= 1}
    if nonflat:
        raise RuntimeError(f"account inventory is not flat; no ablation worker started: {nonflat}")


def verify_worker_flat(args: argparse.Namespace, policy: Policy) -> None:
    """Reject an arm whose final recorded account state still has inventory.

    A clean child exit only means the session ended.  It does *not* prove that
    all liquidation orders completed.  The decision log is written from the
    same account snapshot used for every action, so require its final observed
    BULL/BEAR/RITC/USD inventory to be flat before counting an arm.  This also
    catches orders placed by another client during an experiment; those must
    never be silently attributed to an ablation policy.
    """
    path = ROOT / f"data/etf-ablation-{args.run_id}-{policy.name}-decisions.jsonl"
    try:
        last = json.loads(path.read_text().splitlines()[-1])
        positions = last["account_before"]["positions"]
    except (OSError, IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"policy {policy.name} has no readable final decision snapshot") from error
    nonflat = {ticker: float(positions.get(ticker, 0)) for ticker in ("BULL", "BEAR", "RITC", "USD")
               if abs(float(positions.get(ticker, 0))) >= 1}
    if nonflat:
        raise RuntimeError(f"policy {policy.name} did not finish flat: {nonflat}")


def run(args: argparse.Namespace) -> None:
    """Run every requested arm; skip only a completed-but-contaminated heat.

    An unjournaled account mutation is unsafe to trade through, but a practice
    heat reset returns the account to a clean baseline.  Therefore a flatness
    failure invalidates that *one* observation rather than silently ending a
    multi-heat experiment.  A nonzero worker exit still stops immediately: it
    can mean an ambiguous order or unresolved execution intent.
    """
    client = Client()
    for policy in POLICIES[:args.max_heats]:
        print(f"ablation: queued policy={policy.name} staged={policy.staged} "
              f"active_intervals={policy.active_intervals} participation={policy.participation:.2f} "
              f"fallback_loss=C${policy.fallback_loss:.3f}/share", flush=True)
        marker = wait_for_fresh_heat(client, args.poll_seconds)
        verify_flat_new_heat(client, marker)
        print(f"ablation: account preflight passed for policy={policy.name}", flush=True)
        command = worker_command(args, policy)
        print(f"ablation: starting policy={policy.name} period={marker.period} tick={marker.tick}", flush=True)
        worker = subprocess.Popen(command, cwd=ROOT)
        try:
            status = worker.wait()
        except KeyboardInterrupt:
            worker.send_signal(signal.SIGINT)
            worker.wait()
            raise
        if status:
            raise RuntimeError(f"policy {policy.name} worker exited with status {status}; inspect its journal")
        try:
            verify_worker_flat(args, policy)
        except RuntimeError as error:
            print(f"ablation: policy={policy.name} INVALID ({error}); awaiting clean next heat", flush=True)
            continue
        print(f"ablation: policy={policy.name} completed; awaiting the next fresh heat", flush=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse explicit session limits and experimental controls."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade", action="store_true", help="Submit simulated practice orders; omit for dry-run workers")
    parser.add_argument("--gross-limit", type=int, required=True)
    parser.add_argument("--net-limit", type=int, required=True)
    parser.add_argument("--child-size", type=int, default=10_000)
    parser.add_argument("--tender-execution-risk-k", type=float, default=.15)
    parser.add_argument("--tender-fx-risk-k", type=float, default=.25)
    parser.add_argument("--max-heats", type=int, default=len(POLICIES), choices=range(1, len(POLICIES) + 1))
    parser.add_argument("--run-id", required=True,
                        help="Unique audit namespace, e.g. 2026-09-17-live-a; existing logs are never reused")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.gross_limit <= 0 or args.net_limit <= 0 or not 1 <= args.child_size <= 10_000:
        parser.error("limits must be positive and child size must be 1..10000")
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    return args


def main() -> None:
    load_env_file(ROOT / ".env")
    configure_case_environment("etf")
    try:
        run(parse_args())
    except KeyboardInterrupt:
        print("ablation: stopped", flush=True)
    except RuntimeError as error:
        print(f"ablation: stopped safely: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
