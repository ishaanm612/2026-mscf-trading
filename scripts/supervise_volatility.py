#!/usr/bin/env python3
"""Launch exactly one volatility worker for each active RIT practice heat.

The supervisor only responds to confirmed case lifecycle changes.  It never
retries an order, removes an execution lock, or restarts a worker that exits
with an error: those conditions require the normal reconciliation procedure.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client import Client
from environment import load_env_file
from volatility.supervisor import SessionMarker


def worker_command(args: argparse.Namespace) -> list[str]:
    """Build the isolated ``run.py`` command for one active heat.

    :param args: Parsed supervisor options.
    :returns: Arguments for a worker process running from the repository root.
    """

    command = [sys.executable, "run.py", "volatility", "--source", "api", "--watch", "--exit-on-session-change"]
    command.append("--trade" if args.trade else "--plan")
    if args.rate is not None:
        command.extend(["--rate", str(args.rate)])
    if args.flatten_only:
        command.append("--flatten-only")
    if args.journal:
        command.extend(["--journal", args.journal])
    if args.decision_log:
        command.extend(["--decision-log", args.decision_log])
    if args.record:
        command.extend(["--record", args.record])
    if args.no_explainability:
        command.append("--no-explainability")
    return command


def wait_for_active_case(client: Client, poll_seconds: float) -> SessionMarker:
    """Poll read-only case state until RIT reports an active heat.

    Transient read failures are reported and retried because no mutation has
    occurred.  The worker itself remains responsible for all execution errors.

    :param client: Configured RIT API client.
    :param poll_seconds: Delay between lifecycle checks.
    :returns: First active case marker.
    """

    last_report: tuple[str, int | None] | None = None
    while True:
        try:
            marker = SessionMarker.from_case(client.get("case"))
        except (OSError, RuntimeError, ValueError) as error:
            print(f"supervisor: case read failed; retrying: {error}", flush=True)
            time.sleep(poll_seconds)
            continue
        report = (marker.status, marker.period)
        if report != last_report:
            print(f"supervisor: case status={marker.status} period={marker.period} tick={marker.tick}", flush=True)
            last_report = report
        if marker.is_active:
            return marker
        time.sleep(poll_seconds)


def supervise(args: argparse.Namespace) -> None:
    """Run one child worker per RIT heat until interrupted or an error occurs.

    :param args: Parsed supervisor options.
    :raises RuntimeError: If a worker exits unsuccessfully.
    """

    client = Client()
    command = worker_command(args)
    while True:
        marker = wait_for_active_case(client, args.poll_seconds)
        print(f"supervisor: starting volatility worker for period={marker.period} tick={marker.tick}", flush=True)
        worker = subprocess.Popen(command, cwd=ROOT)
        try:
            exit_code = worker.wait()
        except KeyboardInterrupt:
            worker.terminate()
            worker.wait()
            raise
        if exit_code != 0:
            raise RuntimeError(f"volatility worker exited with status {exit_code}; inspect and reconcile before restarting")
        print("supervisor: worker observed a market stop/reset; waiting for the next active heat", flush=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse explicit execution and lifecycle-supervision settings.

    :param argv: Optional argument list used by tests and embedding callers.
    :returns: Validated command-line settings.
    """

    parser = argparse.ArgumentParser(description="Restart a volatility worker only after an RIT market reset.")
    parser.add_argument("--trade", action="store_true", help="Allow the child worker to submit simulated practice orders")
    parser.add_argument("--flatten-only", action="store_true", help="Reduce confirmed inventory; do not open new exposure")
    parser.add_argument("--rate", type=float, help="Annualized risk-free rate supplied to the model")
    parser.add_argument("--journal", help="Shared execution journal retained across heats")
    parser.add_argument("--decision-log", help="Append structured decision records across heats")
    parser.add_argument("--record", help="Append raw API snapshots across heats")
    parser.add_argument("--no-explainability", action="store_true", help="Omit factor-level decision rationale")
    parser.add_argument("--poll-seconds", type=float, default=1.0, help="Read-only case poll interval while waiting (default: 1)")
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    return args


def main() -> None:
    """Run the volatility lifecycle supervisor and preserve safe failure behavior."""

    try:
        load_env_file(ROOT / ".env")
        supervise(parse_args())
    except KeyboardInterrupt:
        print("supervisor: stopped", flush=True)
    except RuntimeError as error:
        print(f"supervisor: stopped safely: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
