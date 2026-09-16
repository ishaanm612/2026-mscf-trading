"""Interpretable supervised estimate of executable straddle convergence."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


FEATURE_NAMES = ("intercept", "model_edge", "iv_gap", "news_age", "ticks_remaining", "spread")


@dataclass(frozen=True)
class ConvergenceSample:
    """One historical opportunity and its later executable straddle outcome.

    :param features: Stable feature vector beginning with an intercept.
    :param realized_pnl: Later executable P&L per straddle in dollars.
    :param heat: Chronological heat identifier used for holdout splits.
    :param tick: Opportunity tick within the heat.
    """

    features: tuple[float, ...]
    realized_pnl: float
    heat: int
    tick: int


@dataclass(frozen=True)
class ConvergenceModel:
    """Ridge-regression model with an explicit feature contract.

    :param horizon_ticks: Target exit horizon used during training.
    :param coefficients: Coefficients matching :data:`FEATURE_NAMES`.
    :param training_samples: Number of observations used for fitting.
    :param holdout_mae: Mean absolute error on chronological holdout heats.
    :param holdout_directional_accuracy: Fraction of correctly signed holdout predictions.
    """

    horizon_ticks: int
    coefficients: tuple[float, ...]
    training_samples: int
    holdout_mae: float
    holdout_directional_accuracy: float

    def predict(self, features: Sequence[float]) -> float:
        """Predict post-cost P&L per straddle over the trained horizon.

        :param features: Vector produced by :func:`features_for_straddle`.
        :returns: Expected executable P&L in dollars per straddle.
        :raises ValueError: If the model and feature vector disagree.
        """

        if len(features) != len(self.coefficients):
            raise ValueError("Convergence feature vector does not match model")
        return sum(weight * value for weight, value in zip(self.coefficients, features))

    def save(self, path: str | Path) -> None:
        """Persist a versioned, inspectable model without pickle.

        :param path: JSON model destination.
        """

        Path(path).write_text(json.dumps({"version": 1, "feature_names": FEATURE_NAMES,
                                           "horizon_ticks": self.horizon_ticks,
                                           "coefficients": self.coefficients,
                                           "training_samples": self.training_samples,
                                           "holdout_mae": self.holdout_mae,
                                           "holdout_directional_accuracy": self.holdout_directional_accuracy},
                                          indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "ConvergenceModel":
        """Load a validated JSON model created by :meth:`save`.

        :param path: JSON model source.
        :returns: Validated supervised model.
        :raises ValueError: If the model contract is unsupported or malformed.
        """

        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if raw.get("version") != 1 or tuple(raw.get("feature_names", ())) != FEATURE_NAMES:
            raise ValueError("Unsupported convergence model contract")
        coefficients = tuple(float(value) for value in raw["coefficients"])
        if len(coefficients) != len(FEATURE_NAMES) or not all(math.isfinite(value) for value in coefficients):
            raise ValueError("Invalid convergence coefficients")
        return cls(int(raw["horizon_ticks"]), coefficients, int(raw["training_samples"]),
                   float(raw["holdout_mae"]), float(raw["holdout_directional_accuracy"]))


def features_for_straddle(model_edge: float, fair_sigma: float, call_iv: float | None,
                          put_iv: float | None, news_age: int | None, tick: int,
                          call_spread: float, put_spread: float) -> tuple[float, ...]:
    """Build bounded, human-readable features for a paired opportunity.

    :param model_edge: Current cost-adjusted edge per straddle.
    :param fair_sigma: Forecast remaining annualized volatility.
    :param call_iv: Call midpoint implied volatility.
    :param put_iv: Put midpoint implied volatility.
    :param news_age: Ticks since the analyst event, if known.
    :param tick: Current competition tick.
    :param call_spread: Call ask minus bid in dollars per share.
    :param put_spread: Put ask minus bid in dollars per share.
    :returns: Feature vector matching :data:`FEATURE_NAMES`.
    """

    market_ivs = [value for value in (call_iv, put_iv) if isinstance(value, (int, float))]
    average_iv = sum(market_ivs) / len(market_ivs) if market_ivs else fair_sigma
    return (1.0, model_edge, abs(fair_sigma - average_iv), float(news_age or 0),
            float(max(0, 300 - tick)), (call_spread + put_spread) * 100.0)


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> tuple[float, ...]:
    """Solve a square system with partial-pivot Gaussian elimination.

    :param matrix: Non-singular square coefficient matrix.
    :param vector: Right-hand-side vector.
    :returns: Solution vector.
    :raises ValueError: If the system is numerically singular.
    """

    width = len(vector)
    augmented = [row[:] + [value] for row, value in zip(matrix, vector)]
    for column in range(width):
        pivot = max(range(column, width), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("Singular convergence training system")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(width):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [value - factor * pivot_value
                              for value, pivot_value in zip(augmented[row], augmented[column])]
    return tuple(augmented[row][-1] for row in range(width))


def fit_ridge(samples: Sequence[ConvergenceSample], horizon_ticks: int, ridge: float = 10.0,
              holdout_heats: int = 3) -> ConvergenceModel:
    """Fit ridge regression and score it on the final chronological heats.

    :param samples: Time-ordered labelled historical opportunities.
    :param horizon_ticks: Target exit horizon represented by the labels.
    :param ridge: Positive L2 regularization applied except to the intercept.
    :param holdout_heats: Final whole heats held out from fitting.
    :returns: Fitted model with held-out metrics.
    :raises ValueError: If there is insufficient independent heat history.
    """

    heat_ids = sorted({sample.heat for sample in samples})
    if len(heat_ids) <= holdout_heats:
        raise ValueError("Need more heats than the requested holdout")
    train_heats = set(heat_ids[:-holdout_heats])
    train = [sample for sample in samples if sample.heat in train_heats]
    holdout = [sample for sample in samples if sample.heat not in train_heats]
    if len(train) < len(FEATURE_NAMES) or not holdout:
        raise ValueError("Insufficient convergence samples")
    width = len(FEATURE_NAMES)
    matrix = [[0.0] * width for _ in range(width)]
    vector = [0.0] * width
    for sample in train:
        for row in range(width):
            vector[row] += sample.features[row] * sample.realized_pnl
            for column in range(width):
                matrix[row][column] += sample.features[row] * sample.features[column]
    for index in range(1, width):
        matrix[index][index] += ridge
    coefficients = _solve_linear_system(matrix, vector)
    predictions = [sum(weight * value for weight, value in zip(coefficients, sample.features)) for sample in holdout]
    mae = sum(abs(prediction - sample.realized_pnl) for prediction, sample in zip(predictions, holdout)) / len(holdout)
    accuracy = sum((prediction >= 0) == (sample.realized_pnl >= 0)
                   for prediction, sample in zip(predictions, holdout)) / len(holdout)
    return ConvergenceModel(horizon_ticks, coefficients, len(train), mae, accuracy)
