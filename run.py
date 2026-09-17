import argparse
import json
import time
import os
from typing import Any
from models import etf, volatility
from models.etf_policy import ETFConfig
from models.etf_basket import BasketConfig
from risk import RiskError
from client import Client, RITReadError
from bot import Bot
from environment import configure_case_environment, load_env_file
from execution import Executor
from volatility.logger import StrategyLogger
from volatility.reporting import format_volatility_report, summarize_volatility_result
from volatility.supervisor import SessionBoundaryDetector


def demo(case: str) -> dict[str, Any]:
    """Build synthetic prices plus enough account metadata to exercise pre-trade rules."""
    snapshot = demo_prices(case)
    snapshot["orders"] = []
    snapshot["case"]["period"] = 1
    snapshot["limits"] = []
    for security in snapshot["securities"]:
        ticker = security["ticker"]
        option = ticker.startswith("RTM") and ticker != "RTM"
        name = "options" if option else "stock"
        if ticker == "USD":
            name = "cash"
        security.update(is_tradeable=True, max_trade_size=100 if option else 10000,
                        limits=[{"name": name, "units": .5 if ticker == "RITC" else 1}])
        if name not in {limit["name"] for limit in snapshot["limits"]}:
            snapshot["limits"].append({"name": name, "gross": 0, "net": 0,
                                       "gross_limit": 2500 if option else 300000,
                                       "net_limit": 1000 if option else 200000})
    return snapshot


def demo_prices(case: str) -> dict[str, Any]:
    """Create deterministic, non-executable quotes for a selected case.

    :param case: ``etf`` or ``volatility``.
    :returns: Synthetic case snapshot.
    """
    if case == "volatility":
        rows = [{"ticker": "RTM", "bid": 49.99, "ask": 50.01, "position": 0}]
        for strike in range(48, 53):
            for kind in ("C", "P"):
                price = volatility.bs(50, strike, 1/12, 0, 0.20, kind)[0]
                rows.append({"ticker": f"RTM{strike}{kind}", "bid": max(0, price-.01),
                             "ask": price+.01, "position": 0})
        return {"case": {"tick": 0, "status": "ACTIVE"}, "securities": rows, "news": []}
    prices = {"BULL": 10, "BEAR": 15, "RITC": 24.8, "USD": 1}
    return {"case": {"tick": 1, "status": "ACTIVE"},
            "securities": [{"ticker": t, "position": 0} for t in prices],
            "books": {t: {"bids": [{"price": p-.01, "quantity": 1000000}],
                           "asks": [{"price": p+.01, "quantity": 1000000}]} for t, p in prices.items()},
            "tenders": []}


def format_etf_tender_alert(assessment: dict[str, Any], submitted_tender_id: int | None = None) -> str:
    """Render a conspicuous, operator-readable tender decision for the CLI."""

    estimate = assessment.get("estimated_unwind_profit_cad")
    minimum = assessment.get("minimum_profit_cad")
    estimate_text = "unavailable" if estimate is None else f"C${estimate:,.2f}"
    minimum_text = "n/a" if minimum is None else f"C${minimum:,.2f}"
    quantity = assessment.get("quantity")
    price = assessment.get("price")
    quantity_text = f"{quantity:,}" if isinstance(quantity, (int, float)) else repr(quantity)
    price_text = f"${price:.2f}" if isinstance(price, (int, float)) else repr(price)
    decision = "ACCEPTED" if assessment.get("tender_id") == submitted_tender_id else assessment["decision"]
    reserve = assessment.get("liquidation_reserve") or {}
    route = assessment.get("selected_route") or {}
    staged = route.get("staging")
    staging_text = (f"STAGED FORECAST — liquidity replenishment is NOT guaranteed.\n"
                    f"Frozen-book fallback P&L C${staged['fallback_profit_cad']:,.2f}; "
                    f"loss trigger C${staged['fallback_loss_cap_cad']:,.2f}; "
                    f"child spacing {staged['spacing_ticks']} ticks.\n" if staged else "")
    return (f"\n{'!' * 72}\n"
            f"!!! ETF TENDER #{assessment['tender_id']} — {decision}\n"
            f"Offer: {assessment['action']} {quantity_text} RITC @ {price_text} "
            f"(expires tick {assessment['expires']})\n"
            f"FX-adjusted unwind estimate: {estimate_text}; required buffer: {minimum_text}\n"
            f"Route: {route.get('name', 'none eligible')}; "
            f"execution reserve C${reserve.get('execution_risk_cad', 0):,.2f}; "
            f"FX reserve C${reserve.get('fx_risk_cad', 0):,.2f}\n"
            f"Liquidation budget: {assessment.get('liquidation_budget')}\n"
            f"{staging_text}"
            f"Staging check: {assessment.get('staged_unavailable', 'see selected route')}\n"
            f"Reason: {assessment['reason']}\n"
            f"{'!' * 72}")


def format_manual_converter_alert(recommendation: dict[str, Any]) -> str:
    """Render an urgent manual-only ETF converter instruction."""

    advantage = recommendation.get("estimated_advantage_cad")
    advantage_text = f"C${advantage:,.2f} per block" if advantage is not None else "direct route lacks full depth"
    return (f"\n{'#' * 72}\n"
            f"### MANUAL {recommendation['manual_action']} REQUIRED — {recommendation['converter']}\n"
            f"Use the RIT Client Assets converter for {recommendation['blocks']} available block(s).\n"
            f"One block converts {recommendation['convert_from']} -> {recommendation['convert_to']}.\n"
            f"Estimated advantage versus direct liquidation: {advantage_text}.\n"
            f"DEADLINE: tick {recommendation.get('deadline_tick', 'n/a')}; automatic unwind follows timeout.\n"
            f"Decision: PAUSE AUTOMATED UNWIND AND PERFORM THE MANUAL CONVERSION.\n"
            f"{'#' * 72}")


def etf_account_before_action(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Capture ETF account fields supplied by the decision snapshot.

    The record deliberately preserves each security's native currency instead
    of inventing a cross-currency account P&L. The snapshot was collected before
    ``Bot.step``; its fields are not a post-action account reconciliation.
    """
    securities: list[dict[str, Any]] = []
    positions: dict[str, Any] = {}
    for row in snapshot.get("securities", []):
        if not isinstance(row, dict) or not isinstance(row.get("ticker"), str):
            continue
        ticker = row["ticker"]
        position = row.get("position")
        positions[ticker] = position
        securities.append({"ticker": ticker, "position": position,
                           "currency": row.get("currency"),
                           "realized_pnl": row.get("realized"),
                           "unrealized_pnl": row.get("unrealized")})
    return {"source_timing": "before_strategy_action", "tick": snapshot.get("case", {}).get("tick"),
            "positions": positions,
            "securities": securities}


def main() -> None:
    """Parse CLI options and run one read-only or explicitly opted-in cycle."""
    load_env_file(".env")
    parser = argparse.ArgumentParser(description="RIT decision support and opt-in practice trading.")
    parser.add_argument("case", choices=["etf", "volatility"])
    parser.add_argument("--source", choices=["demo", "api", "replay"], default="demo")
    parser.add_argument("--file", help="JSONL snapshot file for replay")
    parser.add_argument("--record", help="Append raw API snapshots to this JSONL file")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--exit-on-session-change", action="store_true",
                        help="Exit a watched worker when its active heat stops or resets")
    parser.add_argument("--sigma", type=float, help="Annualized volatility assumption, e.g. 0.25")
    parser.add_argument("--rate", type=float, default=0)
    parser.add_argument("--quantity", type=int, default=1000, help="ETF target shares per basket leg")
    parser.add_argument("--child-size", type=int, default=10000, help="ETF equity child cap, also bounded by server limits")
    parser.add_argument("--execution-risk-k", type=float, default=ETFConfig.execution_k)
    parser.add_argument("--fx-risk-k", type=float, default=ETFConfig.fx_k)
    parser.add_argument("--tender-execution-risk-k", type=float, default=ETFConfig.tender_execution_k)
    parser.add_argument("--tender-fx-risk-k", type=float, default=ETFConfig.tender_fx_k)
    parser.add_argument("--no-staged-tenders", action="store_true", help="Use frozen-book tender routes only")
    parser.add_argument("--tender-max-fallback-loss", type=float, default=ETFConfig.tender_max_fallback_loss,
                        help="Maximum modeled frozen-book loss per staged tender share, CAD")
    parser.add_argument("--tender-max-unwind-ticks", type=int, default=ETFConfig.tender_max_unwind_ticks)
    parser.add_argument("--manual-wait-ticks", type=int, default=8)
    parser.add_argument("--basket-max-hold-ticks", type=int, default=BasketConfig.max_hold_ticks)
    parser.add_argument("--basket-min-hold-ticks", type=int, default=BasketConfig.min_hold_ticks)
    parser.add_argument("--basket-take-profit", type=float, default=BasketConfig.take_profit_cad_per_share)
    parser.add_argument("--basket-stop-loss", type=float, default=BasketConfig.stop_loss_cad_per_share)
    parser.add_argument("--basket-max-quantity", type=int, default=BasketConfig.max_quantity)
    parser.add_argument("--basket-cooldown-ticks", type=int, default=BasketConfig.cooldown_ticks)
    parser.add_argument("--gross-limit", type=int)
    parser.add_argument("--net-limit", type=int)
    parser.add_argument("--plan", action="store_true", help="Report the bot's next action without submitting it")
    parser.add_argument("--trade", action="store_true", help="Submit simulated orders to the configured RIT account")
    parser.add_argument("--flatten-only", action="store_true", help="Close inventory; never open new exposure")
    parser.add_argument("--basket", action="store_true", help="Enable serial ETF basket trades as well as tenders")
    parser.add_argument("--journal", help="Execution journal path; default is data/<case>-<username>-execution.jsonl")
    parser.add_argument("--check", action="store_true", help="Check case, security metadata, orders and limits without trading")
    parser.add_argument("--reconcile", action="store_true", help="Record current account state after checking an interrupted execution")
    parser.add_argument("--decision-log", help="Append structured case decisions; ETF defaults to data/etf-decisions.jsonl")
    parser.add_argument("--no-explainability", action="store_true", help="Omit factor-level rationale from decision logs")
    parser.add_argument("--convergence-model", help="Validated JSON convergence model used as an opt-in entry filter")
    parser.add_argument("--verbose", action="store_true", help="Print raw strategy payloads instead of compact operational reports")
    args = parser.parse_args()
    configure_case_environment(args.case)
    if args.case == "volatility" and args.sigma is None and not (args.plan or args.trade or args.check or args.reconcile):
        parser.error("volatility requires --sigma (use current analyst information)")
    if args.trade and args.plan:
        parser.error("Choose either --plan or --trade")
    if (args.trade or args.check or args.reconcile) and args.source != "api":
        parser.error("--trade, --check and --reconcile require --source api")
    if args.trade and (args.check or args.reconcile):
        parser.error("Account checks/reconciliation cannot run together with --trade")
    if args.case == "etf" and (args.plan or args.trade) and not (args.gross_limit and args.net_limit):
        parser.error("ETF bot requires --gross-limit and --net-limit from your session")
    if args.quantity < 1:
        parser.error("--quantity must be positive")
    try:
        etf_config = ETFConfig(execution_k=args.execution_risk_k, fx_k=args.fx_risk_k,
                               tender_execution_k=args.tender_execution_risk_k,
                               tender_fx_k=args.tender_fx_risk_k,
                               staged_tenders=not args.no_staged_tenders,
                               tender_max_fallback_loss=args.tender_max_fallback_loss,
                               tender_max_unwind_ticks=args.tender_max_unwind_ticks,
                               child_size=args.child_size, manual_wait_ticks=args.manual_wait_ticks)
        basket_config = BasketConfig(max_hold_ticks=args.basket_max_hold_ticks,
                                     min_hold_ticks=args.basket_min_hold_ticks,
                                     take_profit_cad_per_share=args.basket_take_profit,
                                     stop_loss_cad_per_share=args.basket_stop_loss,
                                     max_quantity=args.basket_max_quantity,
                                     cooldown_ticks=args.basket_cooldown_ticks)
    except ValueError as error:
        parser.error(str(error))
    if args.source == "replay" and not args.file:
        parser.error("replay requires --file")
    if args.watch and args.source != "api":
        parser.error("--watch requires --source api")
    if args.exit_on_session_change and not args.watch:
        parser.error("--exit-on-session-change requires --watch")
    client = Client() if args.source == "api" else None
    account = os.environ.get("RIT_USERNAME", "rest").replace("/", "_")
    journal = args.journal or f"data/{args.case}-{account}-execution.jsonl"
    if args.check or args.reconcile:
        snapshot = client.snapshot(args.case, trading=True)
        if args.reconcile:
            Executor.reconcile(journal, snapshot)
        print(json.dumps(snapshot, allow_nan=False))
        return
    executor = Executor(client, journal) if args.trade else None
    bot = Bot(client, executor, case=args.case, sigma=args.sigma, rate=args.rate,
              quantity=args.quantity, gross_limit=args.gross_limit, net_limit=args.net_limit,
              flatten_only=args.flatten_only, basket=args.basket,
              explainability=not args.no_explainability,
              convergence_model_path=args.convergence_model,
              etf_config=etf_config, basket_config=basket_config) if args.plan or args.trade else None
    log_path = args.decision_log or ("data/etf-decisions.jsonl" if args.case == "etf" and args.source == "api" and bot else None)
    decision_logger = StrategyLogger(log_path) if log_path else None
    session_boundary = SessionBoundaryDetector() if args.exit_on_session_change else None
    replay = open(args.file) if args.source == "replay" else None
    halted_error: str | None = None
    try:
        while True:
            try:
                if replay:
                    line = replay.readline()
                    if not line:
                        break
                    snapshot = json.loads(line)
                else:
                    try:
                        snapshot = client.snapshot(args.case, trading=bool(bot)) if client else demo(args.case)
                    except RITReadError as error:
                        if not args.watch:
                            raise
                        print(json.dumps({"analysis": f"market data unavailable; retrying: {error}"}), flush=True)
                        time.sleep(1)
                        continue
                if halted_error is None and session_boundary and session_boundary.observe(snapshot["case"]):
                    print(json.dumps({"case": snapshot["case"], "analysis": "session boundary; worker stopping"}), flush=True)
                    break
                if args.record:
                    with open(args.record, "a") as output:
                        output.write(json.dumps(snapshot) + "\n")
                if snapshot["case"]["status"] == "ACTIVE":
                    result = {"halted": halted_error, "requires_reconciliation": True} if halted_error else bot.step(snapshot) if bot else (volatility.analyze(snapshot, args.sigma, args.rate) if args.case == "volatility"
                              else etf.analyze(snapshot, min(args.quantity, args.child_size), args.gross_limit, args.net_limit))
                    if decision_logger and args.case == "volatility" and isinstance(result, dict) and "decision" in result:
                        decision_logger.write("volatility_decision", result["decision"])
                    elif decision_logger and args.case == "volatility" and isinstance(result, dict):
                        decision_logger.write("execution_wait", {"tick": snapshot["case"]["tick"], **result})
                    elif decision_logger and args.case == "etf":
                        decision_logger.write("etf_decision", {"case": snapshot["case"],
                                              "mode": "trade" if args.trade else "plan",
                                              "account_before": etf_account_before_action(snapshot), **result})
                    displayed = result
                    if args.case == "etf" and bot and not args.verbose:
                        # Full route/depth/reserve details stay in the JSONL
                        # audit log; preserve the loud operator banners here.
                        displayed = {key: value for key, value in result.items() if key in {
                            "wait", "ticker", "quantity", "reason", "tender_id", "basket",
                            "target_quantity", "filled_quantity", "aborted", "halted",
                            "requires_reconciliation", "error", "exit_reason", "inventory_reduction_required"}}
                        holding = result.get("basket_holding") or result.get("basket_holding_before_add")
                        if holding:
                            displayed["basket_status"] = {key: holding[key] for key in (
                                "close_pnl_cad", "risk_adjusted_close_pnl_cad", "age_ticks",
                                "take_profit_cad", "stop_loss_cad", "add_allowed") if key in holding}
                    if args.case == "etf" and bot and snapshot.get("tenders"):
                        positions = {item["ticker"]: item["position"] for item in snapshot["securities"]}
                        submitted_tender_id = result.get("tender_id") if isinstance(result, dict) else None
                        if not args.trade:
                            submitted_tender_id = None
                        refreshed_assessment = (result.get("tender_assessment")
                                                if isinstance(result, dict) else None)
                        for assessment in result.get("tender_assessments", []):
                            if (isinstance(refreshed_assessment, dict)
                                    and refreshed_assessment.get("tender_id") == assessment.get("tender_id")):
                                # Report the same refreshed prices and limits
                                # that authorized or rejected the submission.
                                assessment = refreshed_assessment
                            print(format_etf_tender_alert(assessment, submitted_tender_id), flush=True)
                    if (args.case == "etf" and isinstance(result, dict)
                            and isinstance(result.get("manual_converter"), dict)):
                        print(format_manual_converter_alert(result["manual_converter"]), flush=True)
                    if args.case == "volatility" and bot and not args.verbose and isinstance(result, dict):
                        print(format_volatility_report(summarize_volatility_result(snapshot, result)), flush=True)
                    else:
                        print(json.dumps({"case": snapshot["case"], "analysis": displayed}, allow_nan=False), flush=True)
                else:
                    print(json.dumps({"case": snapshot["case"], "analysis": "inactive"}), flush=True)
                if replay:
                    continue
                if not args.watch:
                    break
                # Reconcile immediately after a confirmed option fill or while
                # a paired leg is pending. Each subsequent order still uses a
                # fresh snapshot and its own preflight and fill confirmation.
                urgent = (halted_error is None and args.trade and args.case == "volatility" and bot
                          and (bot.pending_volatility_trades or
                               (isinstance(result, dict) and str(result.get("ticker", "")).startswith("RTM")
                                and result.get("ticker") != "RTM"))) if snapshot["case"]["status"] == "ACTIVE" else False
                if args.case == "etf" and args.trade and halted_error is None and snapshot["case"]["status"] == "ACTIVE":
                    urgent = (any(key in result for key in ("ticker", "tender_id", "basket"))
                              or bool(result.get("inventory_reduction_required")))
                if urgent and isinstance(result, dict) and result.get("wait") in {
                        "no unwind satisfies depth and risk limits", "no visible depth for net USD child"}:
                    time.sleep(0.25)
                elif not urgent:
                    time.sleep(1)
            except Exception as error:
                if not args.watch:
                    raise
                if (args.case == "etf" and isinstance(error, (RITReadError, RiskError))
                        and getattr(executor, "unresolved_intent", False) is not True):
                    # No uncertain POST: fresh confirmed inventory determines
                    # the next action, including partial but confirmed baskets.
                    message = {"wait": "recoverable preflight failure; replan from fresh state",
                               "error": str(error), "submitted": False}
                    reducing = isinstance(getattr(bot, "etf_reduction_reason", None), str)
                    if reducing:
                        message.update(inventory_reduction_required=True,
                                       exit_reason=bot.etf_reduction_reason,
                                       basket_context=bot.basket_exit_context)
                        message.pop("submitted", None)  # Earlier legs may already be confirmed.
                    if decision_logger:
                        decision_logger.write("execution_wait", message)
                    print(json.dumps(message), flush=True)
                    if not reducing:
                        time.sleep(1)
                    continue
                # Never rerun a failed strategy/execution cycle: an order may
                # already have reached the exchange. Stay observable, read-only,
                # and latched across market resets until operator recovery.
                if halted_error is None:
                    halted_error = f"{type(error).__name__}: {error}"
                    if decision_logger:
                        try:
                            decision_logger.write("worker_halted", {"error": halted_error,
                                                  "requires_reconciliation": True})
                        except OSError:
                            # Console health output remains available if disk logging fails.
                            pass
                print(json.dumps({"analysis": {"halted": halted_error,
                      "requires_reconciliation": True,
                      "message": "worker alive; submissions disabled; inspect state before restart"}}), flush=True)
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if replay:
            replay.close()
        if executor:
            executor.close()


if __name__ == "__main__":
    main()
