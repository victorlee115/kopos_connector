"""Small, explainable promotion economics calculator.

This is intentionally reporting-only in v1. It does not estimate causal lift or
trigger purchasing; a director-entered scenario can only create a Review First
planning suggestion.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable


class PromotionEconomicsError(ValueError):
    pass


def calculate_promotion_economics(
    *,
    items: Iterable[dict[str, Any]],
    scenarios: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = list(items)
    if not rows:
        raise PromotionEconomicsError("at least one promotion Item is required")
    revenue = Decimal("0")
    cogs = Decimal("0")
    baseline_revenue = Decimal("0")
    baseline_cogs = Decimal("0")
    ingredient_demand: dict[str, Decimal] = {}
    for index, row in enumerate(rows, start=1):
        units = _positive(row.get("units"), f"items[{index}].units")
        price = _sen(row.get("promoted_price_sen"), f"items[{index}].promoted_price_sen")
        baseline_price = _sen(row.get("baseline_price_sen"), f"items[{index}].baseline_price_sen")
        cost_value = row.get("cogs_sen")
        if cost_value in (None, ""):
            raise PromotionEconomicsError(f"items[{index}].cogs_sen is required; stale or missing cost blocks publication")
        cost = _sen(cost_value, f"items[{index}].cogs_sen")
        revenue += units * price
        cogs += units * cost
        baseline_revenue += units * baseline_price
        baseline_cogs += units * cost
        for component in row.get("components", []) or []:
            if not isinstance(component, dict):
                raise PromotionEconomicsError(f"items[{index}].components contains an invalid row")
            item = str(component.get("item") or "").strip()
            qty = _positive(component.get("qty"), f"items[{index}].components.qty")
            if not item:
                raise PromotionEconomicsError(f"items[{index}].components.item is required")
            ingredient_demand[item] = ingredient_demand.get(item, Decimal("0")) + units * qty
    gross_profit = revenue - cogs
    baseline_profit = baseline_revenue - baseline_cogs
    margin = _ratio(gross_profit, revenue)
    baseline_margin = _ratio(baseline_profit, baseline_revenue)
    scenarios_result: list[dict[str, int]] = []
    for label, raw_units in (scenarios or {}).items():
        units = _positive(raw_units, f"scenarios.{label}")
        scenario_revenue = sum(
            units * _sen(row.get("promoted_price_sen"), "promoted_price_sen") for row in rows
        )
        scenario_cogs = sum(
            units * _sen(row.get("cogs_sen"), "cogs_sen") for row in rows
        )
        scenarios_result.append({
            "label": str(label),
            "units": _int(units),
            "revenue_sen": _int(scenario_revenue),
            "cogs_sen": _int(scenario_cogs),
            "gross_profit_sen": _int(scenario_revenue - scenario_cogs),
        })
    break_even_units = (baseline_profit - gross_profit) / (gross_profit / sum(_positive(row.get("units"), "units") for row in rows)) if gross_profit > 0 else None
    return {
        "revenue_sen": _int(revenue),
        "cogs_sen": _int(cogs),
        "gross_profit_sen": _int(gross_profit),
        "margin_percent": _percent(margin),
        "baseline_gross_profit_sen": _int(baseline_profit),
        "baseline_margin_percent": _percent(baseline_margin),
        "margin_change_percent": _percent(margin - baseline_margin),
        "break_even_additional_units": None if break_even_units is None else max(0, _int(break_even_units)),
        "ingredient_demand": {key: _plain_decimal(value) for key, value in sorted(ingredient_demand.items())},
        "scenarios": scenarios_result,
        "planning_mode": "Review First",
    }


def _sen(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PromotionEconomicsError(f"{label} must be an integer sen value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise PromotionEconomicsError(f"{label} must be an integer sen value") from error
    if not result.is_finite() or result != result.to_integral_value() or result < 0:
        raise PromotionEconomicsError(f"{label} must be a non-negative integer sen value")
    return result


def _positive(value: Any, label: str) -> Decimal:
    result = _sen(value, label)
    if result <= 0:
        raise PromotionEconomicsError(f"{label} must be greater than zero")
    return result


def _int(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    return numerator / denominator if denominator else Decimal("0")


def _percent(value: Decimal) -> str:
    return str((value * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _plain_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
