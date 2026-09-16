"""Compact human-readable volatility runner reports."""
from __future__ import annotations

from typing import Any, Mapping


def _round(value: Any, digits: int = 2) -> float | None:
    """Round a finite numeric value for one-line operational output.

    :param value: Candidate numeric value.
    :param digits: Decimal places to retain.
    :returns: Rounded value, or ``None`` for absent/non-numeric input.
    """

    return round(float(value), digits) if isinstance(value, (int, float)) else None


def summarize_volatility_result(snapshot: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce a raw bot result into the facts needed for an operator decision.

    Raw all-option data remains in the JSONL decision journal. This report is
    intentionally limited to the selected straddle, proposed/submitted order,
    forecast, risk, and any gate that prevented an action.

    :param snapshot: Confirmed RIT snapshot used for the cycle.
    :param result: Bot action or wait result.
    :returns: JSON-compatible concise operational report.
    """

    decision = result.get("decision")
    report: dict[str, Any] = {"tick": snapshot["case"].get("tick"),
                               "status": snapshot["case"].get("status"),
                               "action": "WAIT"}
    if not isinstance(decision, Mapping):
        report["reason"] = result.get("wait") or result.get("halted") or "no strategy decision"
        if result.get("risk_rejection"):
            report["risk_rejection"] = result["risk_rejection"]
        return report
    forecast = decision.get("forecast", {})
    portfolio = decision.get("portfolio", {})
    straddle = decision.get("straddle")
    report.update(reason=decision.get("reason", result.get("wait")),
                  fair_volatility=_round(forecast.get("sigma"), 4),
                  news_age_ticks=decision.get("time_since_latest_news"),
                  portfolio_delta=_round(portfolio.get("delta")),
                  portfolio_gamma=_round(portfolio.get("gamma"), 1),
                  portfolio_vega=_round(portfolio.get("vega"), 1))
    if isinstance(straddle, Mapping):
        report["straddle"] = {"strike": straddle.get("strike"), "side": straddle.get("side"),
                               "edge_per_straddle": _round(straddle.get("edge"))}
    explanation = decision.get("explanation", {})
    convergence = explanation.get("convergence_model") if isinstance(explanation, Mapping) else None
    if isinstance(convergence, Mapping):
        report["convergence"] = {"expected_pnl": _round(convergence.get("expected_pnl_per_straddle")),
                                 "horizon_ticks": convergence.get("horizon_ticks"),
                                 "holdout_accuracy": _round(convergence.get("holdout_directional_accuracy"), 3)}
    if "ticker" in result:
        report["action"] = "SUBMITTED" if result.get("quantity") else "PLANNED"
        report["order"] = {"symbol": result.get("ticker"), "quantity": result.get("quantity"),
                           "reason": result.get("reason")}
    elif result.get("risk_rejection"):
        report["risk_rejection"] = result["risk_rejection"]
    return report


def format_volatility_report(report: Mapping[str, Any]) -> str:
    """Render one concise console line from a summarized decision report.

    :param report: Mapping returned by :func:`summarize_volatility_result`.
    :returns: Operator-readable status line without raw option payloads.
    """

    fields = [f"tick {report.get('tick')}", str(report.get("action", "WAIT"))]
    order = report.get("order")
    if isinstance(order, Mapping):
        fields.append(f"{order.get('symbol')} {order.get('quantity'):+} ({order.get('reason')})")
    straddle = report.get("straddle")
    if isinstance(straddle, Mapping):
        fields.append(f"{straddle.get('side')} {straddle.get('strike')} straddle edge ${straddle.get('edge_per_straddle')}")
    volatility = report.get("fair_volatility")
    if isinstance(volatility, (int, float)):
        fields.append(f"fair IV {volatility:.2%}")
    delta = report.get("portfolio_delta")
    if isinstance(delta, (int, float)):
        fields.append(f"delta {delta:+.0f}")
    convergence = report.get("convergence")
    if isinstance(convergence, Mapping) and isinstance(convergence.get("expected_pnl"), (int, float)):
        fields.append(f"model ${convergence['expected_pnl']:.2f}/{convergence.get('horizon_ticks')}t")
    fields.append(str(report.get("reason", "")))
    return " | ".join(fields)
