"""Pure Decimal recipe compiler used by projection and planning.

The compiler intentionally accepts plain mappings so it can be tested without
Frappe. Callers pass the sale-time recipe snapshot and modifier effects; the
compiler returns one stable stock-UOM component vector.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any


class RecipeCompilerError(ValueError):
    """Raised when a frozen recipe snapshot cannot be compiled safely."""


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise RecipeCompilerError(f"{label} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RecipeCompilerError(f"{label} must be a finite decimal") from exc
    if not result.is_finite() or result <= 0:
        raise RecipeCompilerError(f"{label} must be greater than zero")
    return result


def _component_row(row: Mapping[str, Any], index: int) -> tuple[str, Decimal]:
    item = str(row.get("item") or "").strip()
    if not item:
        raise RecipeCompilerError(f"component {index} is missing an Item")
    quantity = _decimal(row.get("stock_qty", row.get("qty")), f"component {index} quantity")
    if str(row.get("stock_uom") or row.get("uom") or "").strip() == "":
        raise RecipeCompilerError(f"component {index} is missing a stock UOM")
    return item, quantity


def _modifier_rows(modifiers: Iterable[Mapping[str, Any]]) -> Iterable[tuple[str, Decimal]]:
    for index, modifier in enumerate(modifiers, start=1):
        item = str(modifier.get("item") or modifier.get("stock_item") or "").strip()
        if not item:
            raise RecipeCompilerError(f"modifier {index} is missing an Item")
        quantity = _decimal(
            modifier.get("stock_qty", modifier.get("qty")),
            f"modifier {index} quantity",
        )
        yield item, quantity


def compile_recipe_components(
    recipe: Mapping[str, Any],
    *,
    servings: Any = 1,
    modifiers: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Decimal]:
    """Compile a frozen recipe into a deterministic stock-UOM vector.

    ``servings`` is the sale quantity expressed in the recipe's default
    serving UOM. Yield is applied exactly once. Modifier rows are additive
    stock-UOM effects and are sorted into the same canonical item-key order.
    """

    if not isinstance(recipe, Mapping):
        raise RecipeCompilerError("recipe snapshot must be a mapping")
    yield_qty = _decimal(recipe.get("yield_qty"), "recipe yield")
    serving_qty = _decimal(recipe.get("default_serving_qty", 1), "recipe serving quantity")
    requested = _decimal(servings, "sale servings")
    rows = recipe.get("components")
    if not isinstance(rows, Iterable) or isinstance(rows, (str, bytes, Mapping)):
        raise RecipeCompilerError("recipe components must be a list")

    multiplier = requested / serving_qty / yield_qty
    compiled: dict[str, Decimal] = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise RecipeCompilerError(f"component {index} must be a mapping")
        if not bool(row.get("affects_stock", True)):
            continue
        item, quantity = _component_row(row, index)
        compiled[item] = compiled.get(item, Decimal("0")) + quantity * multiplier

    for item, quantity in _modifier_rows(modifiers):
        compiled[item] = compiled.get(item, Decimal("0")) + quantity

    return {
        item: compiled[item].normalize()
        for item in sorted(compiled)
        if compiled[item] > 0
    }
