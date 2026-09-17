#!/usr/bin/env python3
"""Launch exactly one selected-case worker for each active RIT practice heat.

The supervisor only responds to confirmed case lifecycle changes.  It never
retries an order, removes an execution lock, or restarts a worker that exits
with an error: those conditions require the normal reconciliation procedure.
"""
from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client import Client
from environment import configure_case_environment, load_env_file
from volatility.supervisor import SessionMarker
from models.etf_policy import ETFConfig
from models.etf_basket import BasketConfig


def worker_command(args: argparse.Namespace) -> list[str]:
    """Build the isolated ``run.py`` command for one active heat.

    :param args: Parsed supervisor options.
    :returns: Arguments for a worker process running from the repository root.
    """

    command = [sys.executable, "run.py", args.case, "--source", "api", "--watch", "--exit-on-session-change"]
    command.append("--trade" if args.trade else "--plan")
    if args.case == "volatility" and args.rate is not None:
        command.extend(["--rate", str(args.rate)])
    if args.flatten_only:
        command.append("--flatten-only")
    if args.journal:
        command.extend(["--journal", args.journal])
    if args.decision_log:
        command.extend(["--decision-log", args.decision_log])
    if args.record:
        command.extend(["--record", args.record])
    if args.case == "volatility" and args.no_explainability:
        command.append("--no-explainability")
    if args.case == "etf":
        command.extend(["--gross-limit", str(args.gross_limit), "--net-limit", str(args.net_limit)])
        for flag, value in (("--quantity", args.quantity), ("--child-size", args.child_size),
                            ("--execution-risk-k", args.execution_risk_k), ("--fx-risk-k", args.fx_risk_k),
                            ("--tender-execution-risk-k", args.tender_execution_risk_k),
                            ("--tender-fx-risk-k", args.tender_fx_risk_k),
                            ("--tender-max-fallback-loss", args.tender_max_fallback_loss),
                            ("--tender-max-unwind-ticks", args.tender_max_unwind_ticks),
                            ("--staged-min-active-intervals", args.staged_min_active_intervals),
                            ("--staged-participation", args.staged_participation),
                            ("--manual-wait-ticks", args.manual_wait_ticks),
                            ("--basket-max-hold-ticks", args.basket_max_hold_ticks),
                            ("--basket-min-hold-ticks", args.basket_min_hold_ticks),
                            ("--basket-take-profit", args.basket_take_profit),
                            ("--basket-stop-loss", args.basket_stop_loss),
                            ("--basket-max-quantity", args.basket_max_quantity),
                            ("--basket-cooldown-ticks", args.basket_cooldown_ticks)):
            command.extend([flag, str(value)])
        if args.basket:
            command.append("--basket")
        if args.no_staged_tenders:
            command.append("--no-staged-tenders")
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
        print(f"supervisor: starting {args.case} worker for period={marker.period} tick={marker.tick}", flush=True)
        worker = subprocess.Popen(command, cwd=ROOT)
        try:
            exit_code = worker.wait()
        except KeyboardInterrupt:
            worker.send_signal(signal.SIGINT)
            worker.wait()
            raise
        if exit_code != 0:
            raise RuntimeError(f"{args.case} worker exited with status {exit_code}; inspect and reconcile before restarting")
        print("supervisor: worker observed a market stop/reset; waiting for the next active heat", flush=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse explicit execution and lifecycle-supervision settings.

    :param argv: Optional argument list used by tests and embedding callers.
    :returns: Validated command-line settings.
    """

    parser = argparse.ArgumentParser(description="Restart a selected-case worker only after an RIT market reset.")
    parser.add_argument("--case", choices=("volatility", "etf"), default="volatility",
                        help="Practice case to supervise (default: volatility)")
    parser.add_argument("--trade", action="store_true", help="Allow the child worker to submit simulated practice orders")
    parser.add_argument("--flatten-only", action="store_true", help="Reduce confirmed inventory; do not open new exposure")
    parser.add_argument("--rate", type=float, help="Annualized risk-free rate supplied to the model")
    parser.add_argument("--journal", help="Shared execution journal retained across heats")
    parser.add_argument("--decision-log", help="Append structured decision records across heats")
    parser.add_argument("--record", help="Append raw API snapshots across heats")
    parser.add_argument("--no-explainability", action="store_true", help="Omit factor-level decision rationale")
    parser.add_argument("--gross-limit", type=int, help="Required ETF session gross limit")
    parser.add_argument("--net-limit", type=int, help="Required ETF session net limit")
    parser.add_argument("--basket", action="store_true", help="Enable serial ETF basket entries; ETF tenders remain enabled")
    parser.add_argument("--quantity", type=int, default=1000, help="ETF target shares per basket leg")
    parser.add_argument("--child-size", type=int, default=10000)
    parser.add_argument("--execution-risk-k", type=float, default=ETFConfig.execution_k)
    parser.add_argument("--fx-risk-k", type=float, default=ETFConfig.fx_k)
    parser.add_argument("--tender-execution-risk-k", type=float, default=ETFConfig.tender_execution_k)
    parser.add_argument("--tender-fx-risk-k", type=float, default=ETFConfig.tender_fx_k)
    parser.add_argument("--no-staged-tenders", action="store_true", help="Use frozen-book tender routes only")
    parser.add_argument("--tender-max-fallback-loss", type=float, default=ETFConfig.tender_max_fallback_loss)
    parser.add_argument("--tender-max-unwind-ticks", type=int, default=ETFConfig.tender_max_unwind_ticks)
    parser.add_argument("--staged-min-active-intervals", type=int, default=ETFConfig.staged_min_active_intervals)
    parser.add_argument("--staged-participation", type=float, default=ETFConfig.staged_participation)
    parser.add_argument("--manual-wait-ticks", type=int, default=8)
    parser.add_argument("--basket-max-hold-ticks", type=int, default=BasketConfig.max_hold_ticks)
    parser.add_argument("--basket-min-hold-ticks", type=int, default=BasketConfig.min_hold_ticks)
    parser.add_argument("--basket-take-profit", type=float, default=BasketConfig.take_profit_cad_per_share)
    parser.add_argument("--basket-stop-loss", type=float, default=BasketConfig.stop_loss_cad_per_share)
    parser.add_argument("--basket-max-quantity", type=int, default=BasketConfig.max_quantity)
    parser.add_argument("--basket-cooldown-ticks", type=int, default=BasketConfig.cooldown_ticks)
    parser.add_argument("--poll-seconds", type=float, default=1.0, help="Read-only case poll interval while waiting (default: 1)")
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    try:
        ETFConfig(execution_k=args.execution_risk_k, fx_k=args.fx_risk_k,
                  tender_execution_k=args.tender_execution_risk_k, tender_fx_k=args.tender_fx_risk_k,
                  staged_tenders=not args.no_staged_tenders,
                  tender_max_fallback_loss=args.tender_max_fallback_loss,
                  tender_max_unwind_ticks=args.tender_max_unwind_ticks,
                  staged_min_active_intervals=args.staged_min_active_intervals,
                  staged_participation=args.staged_participation,
                  child_size=args.child_size, manual_wait_ticks=args.manual_wait_ticks)
        BasketConfig(max_hold_ticks=args.basket_max_hold_ticks,
                     min_hold_ticks=args.basket_min_hold_ticks,
                     take_profit_cad_per_share=args.basket_take_profit,
                     stop_loss_cad_per_share=args.basket_stop_loss,
                     max_quantity=args.basket_max_quantity,
                     cooldown_ticks=args.basket_cooldown_ticks)
    except ValueError as error:
        parser.error(str(error))
    if args.quantity < 1:
        parser.error("--quantity must be positive")
    if args.case == "etf" and (args.gross_limit is None or args.net_limit is None):
        parser.error("--case etf requires --gross-limit and --net-limit from the session")
    if args.case == "volatility" and (args.gross_limit is not None or args.net_limit is not None or args.basket):
        parser.error("ETF limits and --basket require --case etf")
    return args


def main() -> None:
    """Run the selected-case lifecycle supervisor and preserve safe failure behavior."""

    try:
        load_env_file(ROOT / ".env")
        args = parse_args()
        configure_case_environment(args.case)
        supervise(args)
    except KeyboardInterrupt:
        print("supervisor: stopped", flush=True)
    except RuntimeError as error:
        print(f"supervisor: stopped safely: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
