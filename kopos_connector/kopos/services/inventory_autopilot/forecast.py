from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Sequence


MIN_TRAINING_DAYS = 14
MIN_TEST_DAYS = 14
MIN_RELIABLE_DAYS = MIN_TRAINING_DAYS + MIN_TEST_DAYS
MODEL_ORDER = ("same_weekday_seasonal_naive", "trailing_open_day_median", "ewma")
ALGORITHM_VERSION = "inventory-autopilot-forecast-v1"


@dataclass(frozen=True)
class ForecastResult:
    state: str
    selected_model: str | None
    forecast: Decimal | None
    mae: Decimal | None
    wape: Decimal | None
    signed_bias: Decimal | None
    positive_underforecast_p90: Decimal | None
    valid_operating_days: int
    reasons: tuple[str, ...]
    algorithm_version: str = ALGORITHM_VERSION
    training_days: int = MIN_TRAINING_DAYS
    test_days: int = MIN_TEST_DAYS
    explanation: str = ""


def evaluate_forecast(
    actuals: Sequence[Decimal | int | float | str],
    *,
    operating_days: Sequence[bool] | None = None,
    shelf_life_days: int | None = None,
    shelf_life_cap: Decimal | int | float | str | None = None,
) -> ForecastResult:
    values = tuple(_decimal(value) for value in actuals)
    valid_days = sum(1 for value in (operating_days or [True] * len(values)) if value)
    reasons: list[str] = []
    if len(values) < MIN_RELIABLE_DAYS or valid_days < MIN_RELIABLE_DAYS:
        reasons.append("at_least_28_post_cutover_operating_days_required")
        return ForecastResult("Not ready", None, None, None, None, None, None, valid_days, tuple(reasons), explanation="Insufficient post-cutover operating days for rolling-origin evaluation")
    if any(value < 0 for value in values):
        reasons.append("negative_demand")
        return ForecastResult("Not ready", None, None, None, None, None, None, valid_days, tuple(reasons), explanation="Demand contains an invalid negative observation")

    evaluations = {model: _rolling_origin(values, model) for model in MODEL_ORDER}
    selected = min(MODEL_ORDER, key=lambda model: (evaluations[model]["mae"], MODEL_ORDER.index(model)))
    selected_metrics = evaluations[selected]
    forecast = _predict(values, selected)
    if shelf_life_days is not None and shelf_life_days <= 0:
        reasons.append("invalid_shelf_life")
    if shelf_life_cap is not None:
        cap = _decimal(shelf_life_cap)
        if forecast > cap:
            forecast = cap
            reasons.append("shelf_life_cap_applied")
        if cap < selected_metrics["p90"]:
            reasons.append("shelf_life_cap_below_measured_uncertainty")
    state = "Reliable" if not reasons else "Please check"
    return ForecastResult(
        state,
        selected,
        forecast,
        selected_metrics["mae"],
        selected_metrics["wape"],
        selected_metrics["bias"],
        selected_metrics["p90"],
        valid_days,
        tuple(reasons),
        explanation=(
            f"Selected {selected} from {MIN_TRAINING_DAYS} training days and {MIN_TEST_DAYS} "
            f"out-of-sample days; forecast={forecast}, MAE={selected_metrics['mae']}, "
            f"WAPE={selected_metrics['wape']}, signed_bias={selected_metrics['bias']}, "
            f"positive_underforecast_p90={selected_metrics['p90']}"
        ),
    )


def _rolling_origin(values: tuple[Decimal, ...], model: str) -> dict[str, Decimal]:
    errors: list[Decimal] = []
    absolute_actuals = Decimal("0")
    signed_error = Decimal("0")
    for index in range(MIN_TRAINING_DAYS, len(values)):
        prediction = _predict(values[:index], model)
        error = values[index] - prediction
        errors.append(abs(error))
        signed_error += error
        absolute_actuals += abs(values[index])
    test_errors = errors[-MIN_TEST_DAYS:]
    mae = sum(test_errors, Decimal("0")) / Decimal(len(test_errors))
    wape = mae / (absolute_actuals / Decimal(len(values) - MIN_TRAINING_DAYS)) if absolute_actuals else Decimal("0")
    bias = signed_error / Decimal(len(errors))
    positive = sorted(error for error in test_errors if error > 0)
    if not positive:
        positive = [Decimal("0")]
    p90 = positive[min(len(positive) - 1, max(0, (len(positive) * 9 + 9) // 10 - 1))]
    return {"mae": mae, "wape": wape, "bias": bias, "p90": p90}


def _predict(values: tuple[Decimal, ...], model: str) -> Decimal:
    if not values:
        return Decimal("0")
    if model == "same_weekday_seasonal_naive":
        weekday_values = values[-7::7]
        return weekday_values[-1] if weekday_values else values[-1]
    if model == "trailing_open_day_median":
        return Decimal(str(median(values[-7:])))
    if model == "ewma":
        result = values[0]
        alpha = Decimal("0.3")
        for value in values[1:]:
            result = alpha * value + (Decimal("1") - alpha) * result
        return result
    raise ValueError(f"unsupported forecast model: {model}")


def _decimal(value: Decimal | int | float | str) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("demand must be finite")
    return result
