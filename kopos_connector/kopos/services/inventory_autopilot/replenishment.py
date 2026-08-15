from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_UP
from typing import Iterable


@dataclass(frozen=True)
class ReplenishmentInput:
    item: str
    warehouse: str
    current_stock: Decimal
    reservations: Decimal
    unposted_consumption: Decimal
    open_supply: Decimal
    forecast_through_lead_time: Decimal
    safety_stock: Decimal
    supplier_pack: Decimal
    supplier_minimum: Decimal
    shelf_life_cap: Decimal | None


@dataclass(frozen=True)
class ReplenishmentLine:
    item: str
    warehouse: str
    quantity: Decimal
    reason: str


REQUIRED_AUTOMATION_GATES = (
    "policy_active",
    "input_hash_match",
    "no_unresolved_count",
    "devices_current",
    "projection_backlog_clear",
    "recipe_uom_complete",
    "forecast_reliable",
    "source_current",
    "quantity_ceiling",
    "shelf_life_cap",
    "intent_not_open",
)


def build_replenishment_plan(inputs: Iterable[ReplenishmentInput]) -> tuple[ReplenishmentLine, ...]:
    lines: list[ReplenishmentLine] = []
    for value in inputs:
        available = value.current_stock - value.reservations - value.unposted_consumption + value.open_supply
        target = value.forecast_through_lead_time + value.safety_stock
        deficit = target - available
        if deficit <= 0:
            continue
        quantity = max(deficit, value.supplier_minimum)
        if value.supplier_pack > 0:
            quantity = (quantity / value.supplier_pack).quantize(Decimal("1"), rounding=ROUND_UP) * value.supplier_pack
        if value.shelf_life_cap is not None:
            quantity = min(quantity, value.shelf_life_cap)
        if quantity <= 0:
            continue
        lines.append(ReplenishmentLine(value.item, value.warehouse, quantity, "forecast_through_lead_time_and_safety_stock"))
    return tuple(sorted(lines, key=lambda line: (line.warehouse, line.item)))


def evaluate_automation_gates(
    gates: dict[str, bool],
    *,
    require_complete: bool = False,
) -> tuple[bool, tuple[str, ...]]:
    expected = set(REQUIRED_AUTOMATION_GATES) if require_complete else set(gates)
    failed = tuple(sorted(name for name in expected if gates.get(name) is not True))
    return not failed, failed
