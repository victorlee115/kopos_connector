from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Sequence


MIN_TRAINING_DAYS = 14
MIN_TEST_DAYS = 14
MIN_RELIABLE_DAYS = MIN_TRAINING_DAYS + MIN_TEST_DAYS
MODEL_ORDER = ("same_weekday_seasonal_naive", "trailing_open_day_median")
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
    operating_dates: Sequence[date | datetime | str] | None = None,
    forecast_date: date | datetime | str | None = None,
    safety_stock: Decimal | int | float | str | None = None,
    shelf_life_days: int | None = None,
    shelf_life_cap: Decimal | int | float | str | None = None,
) -> ForecastResult:
    raw_values = tuple(_decimal(value) for value in actuals)
    if operating_days is not None and len(operating_days) != len(raw_values):
        raise ValueError("operating-day flags must match demand observations")
    if operating_dates is not None and len(operating_dates) != len(raw_values):
        raise ValueError("operating dates must match demand observations")
    flags = tuple(operating_days or [True] * len(raw_values))
    values = tuple(value for value, is_open in zip(raw_values, flags) if is_open)
    dates = (
        tuple(_date(value) for value, is_open in zip(operating_dates, flags) if is_open)
        if operating_dates is not None
        else None
    )
    valid_days = len(values)
    reasons: list[str] = []
    if len(values) < MIN_RELIABLE_DAYS or valid_days < MIN_RELIABLE_DAYS:
        reasons.append("at_least_28_post_cutover_operating_days_required")
        return ForecastResult("Not ready", None, None, None, None, None, None, valid_days, tuple(reasons), explanation="Insufficient post-cutover operating days for rolling-origin evaluation")
    if any(value < 0 for value in values):
        reasons.append("negative_demand")
        return ForecastResult("Not ready", None, None, None, None, None, None, valid_days, tuple(reasons), explanation="Demand contains an invalid negative observation")

    evaluations = {model: _rolling_origin(values, model, dates=dates) for model in MODEL_ORDER}
    selected = min(MODEL_ORDER, key=lambda model: (evaluations[model]["mae"], MODEL_ORDER.index(model)))
    selected_metrics = evaluations[selected]
    target_date = _date(forecast_date) if forecast_date is not None else (dates[-1] + timedelta(days=1) if dates else None)
    forecast = _predict(values, selected, dates=dates, target_date=target_date)
    configured_safety = _decimal(safety_stock) if safety_stock is not None else None
    if configured_safety is None:
        reasons.append("safety_stock_not_configured")
    elif configured_safety < selected_metrics["p90"]:
        reasons.append("safety_stock_below_measured_p90")
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


def _rolling_origin(
    values: tuple[Decimal, ...],
    model: str,
    *,
    dates: tuple[date, ...] | None,
) -> dict[str, Decimal | None]:
    observations: list[tuple[Decimal, Decimal]] = []
    for index in range(MIN_TRAINING_DAYS, len(values)):
        history_dates = dates[:index] if dates else None
        target_date = dates[index] if dates else None
        prediction = _predict(
            values[:index],
            model,
            dates=history_dates,
            target_date=target_date,
        )
        error = values[index] - prediction
        observations.append((values[index], error))
    test_observations = observations[-MIN_TEST_DAYS:]
    absolute_errors = [abs(error) for _, error in test_observations]
    mae = sum(absolute_errors, Decimal("0")) / Decimal(len(absolute_errors))
    absolute_actuals = sum((abs(actual) for actual, _ in test_observations), Decimal("0"))
    wape = sum(absolute_errors, Decimal("0")) / absolute_actuals if absolute_actuals else None
    bias = sum((error for _, error in test_observations), Decimal("0")) / Decimal(len(test_observations))
    positive = sorted(error for _, error in test_observations if error > 0)
    if not positive:
        positive = [Decimal("0")]
    p90 = positive[min(len(positive) - 1, max(0, (len(positive) * 9 + 9) // 10 - 1))]
    return {"mae": mae, "wape": wape, "bias": bias, "p90": p90}


def _predict(
    values: tuple[Decimal, ...],
    model: str,
    *,
    dates: tuple[date, ...] | None = None,
    target_date: date | None = None,
) -> Decimal:
    if not values:
        return Decimal("0")
    if model == "same_weekday_seasonal_naive":
        if dates and target_date:
            same_weekday = [
                value for value, observed_on in zip(values, dates)
                if observed_on.weekday() == target_date.weekday()
            ]
            return same_weekday[-1] if same_weekday else values[-1]
        weekday_values = values[-7::7]
        return weekday_values[-1] if weekday_values else values[-1]
    if model == "trailing_open_day_median":
        return Decimal(str(median(values[-7:])))
    raise ValueError(f"unsupported forecast model: {model}")


def _decimal(value: Decimal | int | float | str) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("demand must be finite")
    return result


def _date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
