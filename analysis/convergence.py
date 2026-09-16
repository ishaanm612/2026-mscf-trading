"""Train an executable-return convergence model from saved decision logs."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterable, Mapping

from execution import _read_events
from volatility.convergence import ConvergenceSample, features_for_straddle, fit_ridge


def _runs(records: Iterable[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """Split append-only records into competition heats at a tick reset.

    :param records: Parsed decision records.
    :returns: Chronologically ordered heat record groups.
    """

    result: list[list[Mapping[str, Any]]] = []
    for record in records:
        tick = record.get("tick")
        if not isinstance(tick, int):
            continue
        if not result or tick < result[-1][-1]["tick"]:
            result.append([])
        result[-1].append(record)
    return result


def _pair(record: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    """Find the selected call and put quotes in one decision record.

    :param record: Structured volatility decision record.
    :returns: Selected call and put records, or ``None`` when unavailable.
    """

    selected = record.get("straddle")
    if not isinstance(selected, Mapping):
        return None
    strike = selected.get("strike")
    options = record.get("options", [])
    call = next((option for option in options if option.get("symbol") == f"RTM{strike:g}C"), None)
    put = next((option for option in options if option.get("symbol") == f"RTM{strike:g}P"), None)
    return (call, put) if isinstance(call, Mapping) and isinstance(put, Mapping) else None


def samples_from_records(records: Iterable[Mapping[str, Any]], horizon_ticks: int) -> list[ConvergenceSample]:
    """Label candidate straddles with a later executable close outcome.

    The label assumes a one-contract entry at the decision's executable price
    and a close at the first available quote at least ``horizon_ticks`` later.
    It does not claim to reproduce the historical bot's fills or hedges.

    :param records: Decision records from one or more heats.
    :param horizon_ticks: Minimum future quote horizon.
    :returns: Supervised samples with whole-heat identifiers.
    """

    samples: list[ConvergenceSample] = []
    for heat, run in enumerate(_runs(records)):
        for index, record in enumerate(run):
            selected = record.get("straddle")
            forecast = record.get("forecast")
            if not isinstance(selected, Mapping) or not isinstance(forecast, Mapping):
                continue
            side, tick, sigma = selected.get("side"), record.get("tick"), forecast.get("sigma")
            pair = _pair(record)
            if side not in {"BUY", "SELL"} or not isinstance(tick, int) or not isinstance(sigma, (int, float)) or pair is None:
                continue
            future = next((item for item in run[index + 1:] if item["tick"] >= tick + horizon_ticks), None)
            if future is None:
                continue
            future_pair = _pair({**future, "straddle": selected})
            if future_pair is None:
                continue
            call, put = pair
            later_call, later_put = future_pair
            entry = float(call["ask"] + put["ask"]) if side == "BUY" else float(call["bid"] + put["bid"])
            exit_value = float(later_call["bid"] + later_put["bid"]) if side == "BUY" else float(later_call["ask"] + later_put["ask"])
            realized = (exit_value - entry) * 100.0 * (1 if side == "BUY" else -1)
            features = features_for_straddle(float(selected["edge"]), float(sigma), call.get("market_iv"), put.get("market_iv"),
                                              record.get("time_since_latest_news"), tick,
                                              float(call["ask"] - call["bid"]), float(put["ask"] - put["bid"]))
            samples.append(ConvergenceSample(features, realized, heat, tick))
    return samples


def main() -> None:
    """Fit and persist an opt-in convergence model from a decision journal."""

    parser = argparse.ArgumentParser(description="Train an executable straddle convergence model.")
    parser.add_argument("input", help="Decision JSONL written by --decision-log")
    parser.add_argument("--output", default="data/convergence-model.json", help="Model JSON output path")
    parser.add_argument("--horizon", type=int, default=10, help="Target exit horizon in ticks")
    args = parser.parse_args()
    if args.horizon <= 0:
        parser.error("--horizon must be positive")
    samples = samples_from_records(_read_events(Path(args.input)), args.horizon)
    model = fit_ridge(samples, args.horizon)
    model.save(args.output)
    print(f"saved {args.output}: train={model.training_samples}, holdout_mae={model.holdout_mae:.2f}, "
          f"holdout_directional_accuracy={model.holdout_directional_accuracy:.1%}")


if __name__ == "__main__":
    main()
