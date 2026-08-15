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
    raw_quantity = _first_value(row, "stock_qty_decimal", "stock_qty")
    if raw_quantity in (None, ""):
        entered_qty = _mapping_decimal(
            row, "qty_decimal", "qty", f"component {index} quantity"
        )
        conversion = _mapping_decimal(
            row,
            "stock_conversion_factor_decimal",
            "conversion_factor",
            f"component {index} conversion factor",
            default=Decimal("1"),
        )
        loss_percent = _mapping_decimal(
            row,
            "loss_factor_pct_decimal",
            "loss_factor_pct",
            f"component {index} loss factor",
            default=Decimal("0"),
            allow_zero=True,
        )
        if loss_percent < 0:
            raise RecipeCompilerError(f"component {index} loss factor must not be negative")
        raw_quantity = entered_qty * conversion * (Decimal("1") + loss_percent / Decimal("100"))
    quantity = _decimal(raw_quantity, f"component {index} quantity")
    if str(row.get("stock_uom") or row.get("uom") or "").strip() == "":
        raise RecipeCompilerError(f"component {index} is missing a stock UOM")
    return item, quantity


def _modifier_rows(modifiers: Iterable[Mapping[str, Any]]) -> Iterable[tuple[str, Decimal]]:
    for index, modifier in enumerate(modifiers, start=1):
        item = str(modifier.get("item") or modifier.get("stock_item") or "").strip()
        if not item:
            raise RecipeCompilerError(f"modifier {index} is missing an Item")
        raw_quantity = _first_value(modifier, "stock_qty_decimal", "stock_qty")
        if raw_quantity in (None, ""):
            raw_quantity = _mapping_decimal(
                modifier, "qty_decimal", "qty", f"modifier {index} quantity"
            ) * _mapping_decimal(
                modifier,
                "stock_conversion_factor_decimal",
                "conversion_factor",
                f"modifier {index} conversion factor",
                default=Decimal("1"),
            )
        quantity = _decimal(raw_quantity, f"modifier {index} quantity")
        yield item, quantity


def compile_recipe_components(
    recipe: Mapping[str, Any],
    *,
    servings: Any = 1,
    modifiers: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Decimal]:
    """Compile a frozen recipe into deterministic Item totals in stock UOM.

    ``servings`` is the sale quantity expressed in the recipe's default
    serving UOM. Yield is applied exactly once. Modifier rows are additive
    stock-UOM effects and are sorted into the same canonical item-key order.
    """

    compiled: dict[str, Decimal] = {}
    for row in compile_recipe_vector(recipe, servings=servings, modifiers=modifiers):
        item = str(row["item"])
        quantity = _decimal(row["stock_qty"], f"compiled component {item} quantity")
        compiled[item] = compiled.get(item, Decimal("0")) + quantity

    return {
        item: compiled[item].normalize()
        for item in sorted(compiled)
        if compiled[item] > 0
    }


def compile_recipe_vector(
    recipe: Mapping[str, Any],
    *,
    servings: Any = 1,
    modifiers: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, str]]:
    """Return one stable, fully frozen stock-UOM vector for a sale line.

    The recipe's component ``stock_qty`` values represent one full recipe
    yield. The compiler applies yield exactly once, then applies only the
    sale's frozen modifier-effect rows. It intentionally accepts plain data so
    a worker can use it after checkout without reading mutable menu records.
    """

    if not isinstance(recipe, Mapping):
        raise RecipeCompilerError("recipe snapshot must be a mapping")
    yield_qty = _mapping_decimal(
        recipe, "yield_qty_decimal", "yield_qty", "recipe yield"
    )
    serving_qty = _mapping_decimal(
        recipe,
        "default_serving_qty_decimal",
        "default_serving_qty",
        "recipe serving quantity",
        default=Decimal("1"),
    )
    requested = _decimal(servings, "sale servings")
    rows = recipe.get("components")
    if not isinstance(rows, Iterable) or isinstance(rows, (str, bytes, Mapping)):
        raise RecipeCompilerError("recipe components must be a list")

    # Base component quantities describe one full recipe yield.  A selected
    # modifier describes one serving: a cart line with quantity two and an
    # "extra syrup" choice therefore consumes extra syrup twice.  Keeping the
    # two multipliers separate prevents a batch yield from being applied to a
    # modifier effect a second time.
    serving_multiplier = requested / serving_qty
    multiplier = serving_multiplier / yield_qty
    vector: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise RecipeCompilerError(f"component {index} must be a mapping")
        if not bool(row.get("affects_stock", True)):
            continue
        item, quantity = _component_row(row, index)
        stock_uom = str(row.get("stock_uom") or row.get("uom") or "").strip()
        vector.append(
            {
                "item": item,
                "stock_qty": quantity * multiplier,
                "stock_uom": stock_uom,
                "source_type": "Base Recipe",
                "source_reference": str(
                    row.get("substitution_key") or row.get("name") or index
                ),
            }
        )

    for index, effect in enumerate(modifiers, start=1):
        if not isinstance(effect, Mapping):
            raise RecipeCompilerError(f"modifier {index} must be a mapping")
        _apply_modifier_effect(vector, effect, index, serving_multiplier)

    totals: dict[tuple[str, str], Decimal] = {}
    for row in vector:
        item = str(row.get("item") or "").strip()
        stock_uom = str(row.get("stock_uom") or "").strip()
        quantity = _decimal(row.get("stock_qty"), f"compiled component {item} quantity")
        if not item or not stock_uom:
            raise RecipeCompilerError("compiled component is missing an Item or stock UOM")
        key = (item, stock_uom)
        totals[key] = totals.get(key, Decimal("0")) + quantity
    return [
        {
            "item": item,
            "stock_qty": _plain_decimal(quantity),
            "stock_uom": stock_uom,
            "source_type": "Frozen Recipe",
            "source_reference": "recipe-vector",
        }
        for (item, stock_uom), quantity in sorted(totals.items())
        if quantity > 0
    ]


def _apply_modifier_effect(
    vector: list[dict[str, Any]],
    effect: Mapping[str, Any],
    index: int,
    serving_multiplier: Decimal,
) -> None:
    if not bool(effect.get("affects_recipe", True)):
        return
    if not bool(effect.get("affects_stock", True)):
        return
    kind = str(effect.get("kind") or "Add").strip()
    if kind == "Instruction Only":
        return
    if kind == "Add":
        item = str(
            effect.get("new_item") or effect.get("target_item") or effect.get("item") or effect.get("stock_item") or ""
        ).strip()
        stock_uom = str(effect.get("stock_uom") or effect.get("uom") or effect.get("qty_uom") or "").strip()
        if not stock_uom:
            stock_uom = _vector_item_stock_uom(vector, item)
        raw_quantity = _first_value(effect, "stock_qty_delta_decimal", "stock_qty_delta")
        if raw_quantity in (None, ""):
            raw_quantity = effect.get("stock_qty")
        if raw_quantity in (None, ""):
            raw_quantity = _first_value(effect, "qty_decimal", "qty")
            if raw_quantity not in (None, ""):
                raw_quantity = _mapping_decimal(
                    effect, "qty_decimal", "qty", f"modifier {index} quantity"
                ) * _mapping_decimal(
                    effect,
                    "stock_conversion_factor_decimal",
                    "conversion_factor",
                    f"modifier {index} conversion factor",
                    default=Decimal("1"),
                )
        quantity = _decimal(raw_quantity, f"modifier {index} quantity")
        if not item or not stock_uom:
            raise RecipeCompilerError(f"modifier {index} is missing an Item or stock UOM")
        vector.append(
            {
                "item": item,
                "stock_qty": quantity * serving_multiplier,
                "stock_uom": stock_uom,
                "source_type": "Modifier Add",
                "source_reference": str(effect.get("modifier") or index),
            }
        )
        return

    matching = _matching_components(vector, effect)
    if not matching:
        raise RecipeCompilerError(f"modifier {index} does not match a recipe component")
    if kind == "Remove":
        for row in matching:
            vector.remove(row)
        return
    if kind == "Scale":
        scale_multiplier = _mapping_decimal(
            effect,
            "scale_percent_decimal",
            "scale_percent",
            f"modifier {index} scale percent",
        ) / Decimal("100")
        for row in matching:
            row["stock_qty"] = _decimal(
                row.get("stock_qty"), f"modifier {index} target quantity"
            ) * scale_multiplier
            row["source_type"] = "Modifier Scale"
        return
    if kind == "Replace":
        new_item = str(effect.get("new_item") or "").strip()
        stock_uom = str(effect.get("stock_uom") or "").strip()
        if not new_item or not stock_uom:
            raise RecipeCompilerError(f"modifier {index} replacement is missing an Item or stock UOM")
        raw_delta = _first_value(effect, "stock_qty_delta_decimal", "stock_qty_delta")
        delta = (
            Decimal("0")
            if raw_delta in (None, "", 0, "0", "0.0")
            else _decimal(raw_delta, f"modifier {index} stock quantity delta")
        )
        for row in matching:
            if str(row.get("stock_uom") or "").strip() != stock_uom:
                raise RecipeCompilerError(
                    f"modifier {index} replacement changes stock UOM; publish an explicit Remove and Add instead"
                )
            row["item"] = new_item
            row["stock_qty"] = _decimal(
                row.get("stock_qty"), f"modifier {index} target quantity"
            ) + delta * serving_multiplier
            row["source_type"] = "Modifier Replace"
            row["source_reference"] = str(effect.get("modifier") or index)
        return
    raise RecipeCompilerError(f"modifier {index} has unsupported effect kind {kind}")


def _matching_components(
    vector: list[dict[str, Any]], effect: Mapping[str, Any]
) -> list[dict[str, Any]]:
    substitution_key = str(effect.get("target_substitution_key") or "").strip()
    target_item = str(effect.get("target_item") or "").strip()
    if substitution_key:
        return [
            row
            for row in vector
            if str(row.get("source_reference") or "").strip() == substitution_key
        ]
    if target_item:
        return [row for row in vector if str(row.get("item") or "").strip() == target_item]
    return []


def _vector_item_stock_uom(vector: list[dict[str, Any]], item: str) -> str:
    matching_uoms = {
        str(row.get("stock_uom") or "").strip()
        for row in vector
        if str(row.get("item") or "").strip() == item
    }
    return next(iter(matching_uoms)) if len(matching_uoms) == 1 else ""


def _plain_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized != 0 else "0"


def _first_value(mapping: Mapping[str, Any], exact_key: str, legacy_key: str) -> Any:
    exact = mapping.get(exact_key)
    return exact if exact not in (None, "") else mapping.get(legacy_key)


def _mapping_decimal(
    mapping: Mapping[str, Any],
    exact_key: str,
    legacy_key: str,
    label: str,
    *,
    default: Decimal | None = None,
    allow_zero: bool = False,
) -> Decimal:
    raw = _first_value(mapping, exact_key, legacy_key)
    if raw in (None, "") and default is not None:
        return default
    if allow_zero:
        if isinstance(raw, bool) or raw is None:
            raise RecipeCompilerError(f"{label} must be a finite decimal")
        try:
            result = Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise RecipeCompilerError(f"{label} must be a finite decimal") from exc
        if not result.is_finite() or result < 0:
            raise RecipeCompilerError(f"{label} must be zero or greater")
        return result
    return _decimal(raw, label)
