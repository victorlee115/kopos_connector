"""One stock-capacity calculation for availability holds and overlays.

The availability contract is about whether a sellable recipe can be made, not
whether the sellable Item itself happens to have a ``Bin`` row.  This module
keeps the calculation small and shared: callers load the active frozen recipe
and current ERPNext stock evidence here, then use the same result for holds,
manager warnings, and the catalog overlay.

No BOM is expanded here.  A published ``FB Recipe`` already contains the
frozen component vector; a prepared component in that vector is one stocked
input and its standard BOM is owned by ERPNext manufacturing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any

import frappe
from frappe.utils import cstr, get_datetime, now_datetime

from kopos_connector.kopos.services.inventory_autopilot.recipe_compiler import (
    RecipeCompilerError,
    compile_recipe_components,
    compile_recipe_vector,
)
from kopos_connector.kopos.doctype.fb_modifier_group.fb_modifier_group import (
    filter_visible_allowed_modifier_groups,
)
from kopos_connector.kopos.services.recipe.modifier_bounds import (
    ModifierBoundsError,
    resolve_effective_modifier_bounds,
)


@dataclass(frozen=True)
class CapacityResult:
    """Capacity and evidence state for one availability target."""

    target_type: str
    target_id: str
    capacity: Decimal | None
    reliable: bool
    reason: str
    requirements: dict[str, Decimal]
    limiting_item: str | None = None


def calculate_capacity(
    requirements: Mapping[str, Decimal],
    evidence: Mapping[str, Mapping[str, Any]],
    *,
    target_type: str = "Item",
    target_id: str = "",
) -> CapacityResult:
    """Purely calculate sellable portions from component stock evidence.

    ``requirements`` is one serving's frozen stock-UOM vector.  Each
    component limits the number of portions by ``usable / required`` and the
    lowest whole number is the safe capacity.  Missing or non-current
    evidence is never treated as zero: it returns an unreliable result so an
    automatic hold cannot be created from an outage.
    """

    normalized: dict[str, Decimal] = {}
    for item, raw_required in requirements.items():
        item_code = cstr(item).strip()
        required = _finite_decimal(raw_required)
        if not item_code or required is None or required <= 0:
            return CapacityResult(
                target_type,
                target_id,
                None,
                False,
                "The frozen recipe has an invalid stock component quantity",
                {},
            )
        normalized[item_code] = normalized.get(item_code, Decimal("0")) + required

    if not normalized:
        return CapacityResult(
            target_type,
            target_id,
            None,
            True,
            "This target has no stock-affecting recipe components",
            {},
        )

    capacities: list[tuple[Decimal, str]] = []
    for item_code, required in sorted(normalized.items()):
        row = evidence.get(item_code)
        if not isinstance(row, Mapping) or row.get("current") is not True:
            return CapacityResult(
                target_type,
                target_id,
                None,
                False,
                cstr((row or {}).get("reason") if isinstance(row, Mapping) else "").strip()
                or "Current stock evidence is unavailable for a recipe component",
                normalized,
            )
        usable = _finite_decimal(row.get("usable"))
        if usable is None:
            return CapacityResult(
                target_type,
                target_id,
                None,
                False,
                "Current stock evidence is incomplete for a recipe component",
                normalized,
            )
        capacities.append((max(usable, Decimal("0")) / required, item_code))

    limiting_capacity, limiting_item = min(capacities, key=lambda row: (row[0], row[1]))
    whole_capacity = limiting_capacity.to_integral_value(rounding=ROUND_FLOOR)
    return CapacityResult(
        target_type,
        target_id,
        max(whole_capacity, Decimal("0")),
        True,
        "Current stock evidence covers the frozen recipe components",
        normalized,
        limiting_item,
    )


def target_capacity(
    *,
    target_type: str,
    target_id: str,
    company: str,
    warehouse: str,
    at_time: Any | None = None,
    parent_item_id: str | None = None,
) -> CapacityResult:
    """Load one target's active frozen recipe and calculate its capacity.

    A non-stock sellable Item is evaluated from its active ``FB Recipe``.
    Stock Items without a recipe use their own Bin as a direct stocked target;
    this keeps prepared components and packaged goods usable without inventing
    a second recipe.  Modifier targets are evaluated against the frozen
    effect in each active recipe that exposes them, conservatively using the
    lowest capacity across those contexts.
    """

    resolved_type = cstr(target_type).strip()
    resolved_id = cstr(target_id).strip()
    resolved_company = cstr(company).strip()
    resolved_warehouse = cstr(warehouse).strip()
    if not resolved_type or not resolved_id or not resolved_company or not resolved_warehouse:
        return _not_ready(resolved_type, resolved_id, "This stock check is missing its company or outlet stock location")

    policy = _policy(resolved_company, resolved_warehouse)
    if not policy or not cstr(policy.get("cutover_token")).strip() or not policy.get("cutover_at"):
        return _not_ready(resolved_type, resolved_id, "Stock tracking has not started for this outlet")

    current_time = at_time or now_datetime()
    try:
        if resolved_type == "Item":
            return _item_capacity(
                item=resolved_id,
                company=resolved_company,
                warehouse=resolved_warehouse,
                current_time=current_time,
                cutover_at=policy.get("cutover_at"),
            )
        if resolved_type == "Modifier":
            return _modifier_capacity(
                modifier=resolved_id,
                parent_item=cstr(parent_item_id).strip(),
                company=resolved_company,
                warehouse=resolved_warehouse,
                current_time=current_time,
                cutover_at=policy.get("cutover_at"),
            )
    except (RecipeCompilerError, InvalidOperation, TypeError, ValueError) as error:
        return _not_ready(resolved_type, resolved_id, f"The approved recipe has a problem: {error}")
    except Exception:
        # Availability is optional to checkout.  A database/compiler failure
        # must fail closed for automation and be visible as a plain status,
        # rather than becoming a zero-capacity hold.
        return _not_ready(resolved_type, resolved_id, "JiJi could not read the approved recipe or current stock amount")
    return _not_ready(resolved_type, resolved_id, "JiJi cannot check stock for this type of item")


def _item_capacity(
    *,
    item: str,
    company: str,
    warehouse: str,
    current_time: Any,
    cutover_at: Any,
) -> CapacityResult:
    item_values = frappe.db.get_value(
        "Item", item, ["name", "stock_uom", "is_stock_item"], as_dict=True
    ) or {}
    is_stock_item = _truthy(_value(item_values, "is_stock_item"))
    recipe = _active_recipe(item, company, current_time)
    if recipe is not None:
        requirements = _recipe_requirements(recipe)
    elif is_stock_item:
        requirements = {item: Decimal("1")}
    else:
        return _not_ready("Item", item, "This made-to-order item has no current approved recipe")
    evidence = _stock_evidence(
        requirements,
        warehouse=warehouse,
        cutover_at=cutover_at,
    )
    return calculate_capacity(requirements, evidence, target_type="Item", target_id=item)


def _modifier_capacity(
    *,
    modifier: str,
    parent_item: str,
    company: str,
    warehouse: str,
    current_time: Any,
    cutover_at: Any,
) -> CapacityResult:
    modifier_values = frappe.db.get_value(
        "FB Modifier", modifier, ["name", "active", "affects_stock"], as_dict=True
    ) or {}
    if not modifier_values:
        return _not_ready("Modifier", modifier, "This customer choice was not found")
    if not _truthy(_value(modifier_values, "active")):
        return CapacityResult("Modifier", modifier, None, True, "This customer choice is not in use", {})
    if not _truthy(_value(modifier_values, "affects_stock")):
        return CapacityResult(
            "Modifier", modifier, None, True, "This customer choice changes instructions only and does not use stock", {}
        )

    if parent_item:
        contextual_recipe = _active_recipe(parent_item, company, current_time)
        recipes = [contextual_recipe] if contextual_recipe is not None else []
    else:
        recipes = _active_recipes_for_modifier(modifier, company, current_time)
    if not recipes:
        return _not_ready("Modifier", modifier, "No current approved recipe uses this customer choice")

    results: list[CapacityResult] = []
    for recipe in recipes:
        effect = next(
            (
                row
                for row in _rows(recipe, "recipe_modifier_effects")
                if cstr(_value(row, "modifier")).strip() == modifier
            ),
            None,
        )
        if effect is None:
            continue
        effects = _effects_for_choice(recipe, modifier, effect)
        requirements = _recipe_requirements(recipe, modifiers=effects)
        evidence = _stock_evidence(requirements, warehouse=warehouse, cutover_at=cutover_at)
        results.append(
            calculate_capacity(requirements, evidence, target_type="Modifier", target_id=modifier)
        )
    if not results:
        return _not_ready("Modifier", modifier, "JiJi could not calculate the ingredient change for this customer choice")
    if any(not result.reliable for result in results):
        first_unreliable = next(result for result in results if not result.reliable)
        return CapacityResult(
            "Modifier",
            modifier,
            None,
            False,
            first_unreliable.reason,
            first_unreliable.requirements,
            first_unreliable.limiting_item,
        )
    capacities = [result.capacity for result in results if result.capacity is not None]
    if not capacities:
        return CapacityResult(
            "Modifier", modifier, None, True, "This customer choice does not change any ingredient or supply amount", {}
        )
    limiting_result = min(
        (result for result in results if result.capacity is not None),
        key=lambda result: (result.capacity, result.target_id),
    )
    return CapacityResult(
        "Modifier",
        modifier,
        min(capacities),
        True,
        "Current stock information covers the ingredient change for this customer choice",
        limiting_result.requirements,
        limiting_result.limiting_item,
    )


def active_recipe_tracking_items(
    *, item: str, company: str, at_time: Any | None = None
) -> set[str]:
    """Return every physical input used by any valid choice for one product.

    The same frozen recipe compiler used for sale posting performs the work.
    This function only unions its exact vectors; it does not introduce another
    recipe interpretation.
    """

    recipe = _active_recipe(cstr(item).strip(), cstr(company).strip(), at_time or now_datetime())
    if recipe is None:
        return set()
    effects = _rows(recipe, "recipe_modifier_effects")
    contexts: list[list[Any]] = [_default_effects(recipe)]
    contexts.extend(
        _effects_for_choice(recipe, cstr(_value(effect, "modifier")).strip(), effect)
        for effect in effects
        if cstr(_value(effect, "modifier")).strip()
    )
    if not contexts:
        contexts = [[]]
    tracked: set[str] = set()
    for context in contexts:
        tracked.update(_recipe_requirements(recipe, modifiers=context))
    return tracked


def resolve_made_item_waste(
    *,
    item: str,
    company: str,
    quantity: Any,
    selected_modifiers: list[str] | tuple[str, ...],
    at_time: Any | None = None,
) -> dict[str, Any]:
    """Resolve one made product to the same frozen vector used by a sale.

    The tablet sends only the product, quantity, and customer-choice ids. It
    cannot send or alter ingredient lines. Defaults from unselected groups are
    applied exactly as they are for availability and sale projection.
    """

    recipe = _active_recipe(
        cstr(item).strip(), cstr(company).strip(), at_time or now_datetime()
    )
    if recipe is None:
        raise ValueError("This product has no approved ingredient list")
    frozen_effects = {
        cstr(_value(effect, "modifier")).strip(): effect
        for effect in _rows(recipe, "recipe_modifier_effects")
        if cstr(_value(effect, "modifier")).strip()
    }
    selected_names = [cstr(value).strip() for value in selected_modifiers]
    if any(not name for name in selected_names) or len(selected_names) != len(
        set(selected_names)
    ):
        raise ValueError("Choose each customer choice once")
    unknown = [name for name in selected_names if name not in frozen_effects]
    if unknown:
        raise ValueError("One customer choice is not approved for this product")

    allowed_rows = _rows(recipe, "allowed_modifier_groups")
    allowed_by_group = {
        cstr(_value(row, "modifier_group")).strip(): row
        for row in allowed_rows
        if cstr(_value(row, "modifier_group")).strip()
    }
    for name in selected_names:
        group_name = cstr(_value(frozen_effects[name], "modifier_group")).strip()
        if not group_name or group_name not in allowed_by_group:
            raise ValueError("One customer choice is not approved for this product")

    # Apply a visible group's saved default only when the tablet did not send a
    # choice for that group. A default can reveal another group, so resolve to a
    # fixed point before validating the same effective bounds used by orders.
    resolved_names = list(selected_names)
    for _index in range(len(allowed_rows) + 1):
        visible_rows = filter_visible_allowed_modifier_groups(
            allowed_rows, set(resolved_names)
        )
        visible_group_names = {
            cstr(_value(row, "modifier_group")).strip() for row in visible_rows
        }
        selected_group_names = {
            cstr(_value(frozen_effects[name], "modifier_group")).strip()
            for name in resolved_names
        }
        defaults_to_add = [
            cstr(_value(row, "default_modifier")).strip()
            for row in visible_rows
            if cstr(_value(row, "modifier_group")).strip()
            not in selected_group_names
            and cstr(_value(row, "default_modifier")).strip()
        ]
        invalid_default = next(
            (name for name in defaults_to_add if name not in frozen_effects), None
        )
        if invalid_default:
            raise ValueError("A saved default choice is incomplete for this product")
        additions = [name for name in defaults_to_add if name not in resolved_names]
        if not additions:
            break
        resolved_names.extend(additions)
    else:
        raise ValueError("The customer choices for this product cannot be resolved")

    visible_rows = filter_visible_allowed_modifier_groups(
        allowed_rows, set(resolved_names)
    )
    visible_group_names = {
        cstr(_value(row, "modifier_group")).strip() for row in visible_rows
    }
    selections_by_group: dict[str, list[str]] = {}
    for name in resolved_names:
        group_name = cstr(_value(frozen_effects[name], "modifier_group")).strip()
        if group_name not in visible_group_names:
            raise ValueError("One customer choice is not available with the other choices")
        selections_by_group.setdefault(group_name, []).append(name)

    for row in visible_rows:
        group_name = cstr(_value(row, "modifier_group")).strip()
        group = frappe.get_cached_doc("FB Modifier Group", group_name)
        try:
            bounds = resolve_effective_modifier_bounds(
                selection_type=_value(group, "selection_type"),
                group_is_required=_value(group, "is_required"),
                group_min_selection=_value(group, "min_selection"),
                group_max_selection=_value(group, "max_selection"),
                recipe_required=_value(row, "required"),
                override_min_selection=_value(row, "override_min_selection"),
                override_max_selection=_value(row, "override_max_selection"),
            )
        except ModifierBoundsError as error:
            raise ValueError(
                "The customer choice rules for this product need fixing"
            ) from error
        selected_count = len(selections_by_group.get(group_name, []))
        if selected_count < bounds.min_selection:
            raise ValueError("Choose the required customer choices for this product")
        if bounds.max_selection and selected_count > bounds.max_selection:
            raise ValueError("Too many customer choices were selected for this product")

    modifiers = [frozen_effects[name] for name in resolved_names]
    vector = compile_recipe_vector(
        _recipe_mapping(recipe),
        servings=quantity,
        modifiers=[_modifier_mapping(effect) for effect in modifiers],
    )
    if not vector:
        raise ValueError("This product has no physical ingredients to record")
    return {
        "recipe": cstr(getattr(recipe, "name", "")).strip(),
        "recipe_hash": cstr(getattr(recipe, "canonical_hash", "")).strip(),
        "selected_modifiers": resolved_names,
        "lines": [
            {
                "item": cstr(row.get("item")).strip(),
                "qty": cstr(row.get("stock_qty")).strip(),
                "qty_decimal": cstr(row.get("stock_qty")).strip(),
                "uom": cstr(row.get("stock_uom")).strip(),
            }
            for row in vector
        ],
    }


def _recipe_requirements(recipe: Any, *, modifiers: list[Any] | None = None) -> dict[str, Decimal]:
    recipe_mapping = _recipe_mapping(recipe)
    rows = compile_recipe_components(
        recipe_mapping,
        modifiers=[_modifier_mapping(row) for row in modifiers or ()],
    )
    return {cstr(item).strip(): _finite_decimal(quantity) or Decimal("0") for item, quantity in rows.items()}


def _stock_evidence(
    requirements: Mapping[str, Decimal], *, warehouse: str, cutover_at: Any
) -> dict[str, dict[str, Any]]:
    item_codes = sorted(cstr(item).strip() for item in requirements if cstr(item).strip())
    if not item_codes:
        return {}
    if not frappe.db.exists("DocType", "Bin"):
        return {item: {"current": False, "reason": "Outlet stock checking is not installed"} for item in item_codes}
    meta = frappe.get_meta("Bin")
    fields = ["item_code"]
    for fieldname in ("actual_qty", "reserved_qty"):
        if meta.has_field(fieldname):
            fields.append(fieldname)
    rows = frappe.get_all(
        "Bin",
        filters={"warehouse": warehouse, "item_code": ["in", item_codes]},
        fields=fields,
        limit_page_length=len(item_codes),
    )
    by_item = {cstr(_value(row, "item_code")).strip(): row for row in rows}
    evidence: dict[str, dict[str, Any]] = {}
    for item in item_codes:
        row = by_item.get(item)
        if row is None or _value(row, "actual_qty") in (None, ""):
            evidence[item] = {"current": False, "reason": "This item has no complete stock record for this location"}
            continue
        actual = _finite_decimal(_value(row, "actual_qty"))
        reserved = _finite_decimal(_value(row, "reserved_qty")) or Decimal("0")
        unposted = _unposted_consumption(item=item, warehouse=warehouse, cutover_at=cutover_at)
        if actual is None or unposted is None:
            evidence[item] = {"current": False, "reason": "Current stock or unposted ingredient consumption is incomplete"}
            continue
        evidence[item] = {
            "current": True,
            "usable": max(actual - max(reserved, Decimal("0")) - max(unposted, Decimal("0")), Decimal("0")),
        }
    return evidence


def _unposted_consumption(*, item: str, warehouse: str, cutover_at: Any) -> Decimal | None:
    if not cutover_at:
        return Decimal("0")
    if not frappe.db.exists("DocType", "FB Resolved Sale") or not frappe.db.exists("DocType", "FB Resolved Component"):
        return Decimal("0")
    try:
        quantity_expression = _resolved_component_quantity_expression("rc")
        rows = frappe.db.sql(
            f"""
            SELECT COALESCE(SUM({quantity_expression}), 0) AS quantity
            FROM `tabFB Resolved Sale` rs
            INNER JOIN `tabFB Resolved Component` rc ON rc.parent = rs.name
            WHERE rs.booth_warehouse = %s
              AND rs.creation >= %s
              AND rs.stock_entry_issue IS NULL
              AND rc.item = %s
              AND rc.affects_stock = 1
              AND {quantity_expression} > 0
            """,
            (warehouse, cutover_at, item),
            as_dict=True,
        ) or []
    except Exception:
        return None
    return _finite_decimal(_value(rows[0], "quantity") if rows else "0")


def _active_recipe(item: str, company: str, at_time: Any) -> Any | None:
    rows = _active_recipe_rows(item=item, company=company)
    return _first_effective_recipe(rows, at_time)


def active_recipe_name(*, item: str, company: str, at_time: Any | None = None) -> str:
    """Which recipe is in force for this drink right now.

    Exposed because the menu desk needs the same answer this module already
    uses to decide what a tablet may sell. A second reading of "active" would
    let a director cost one recipe while the counter pours another.
    """
    document = _active_recipe(item, company, at_time or now_datetime())
    return cstr(getattr(document, "name", "")).strip()


def _active_recipes_for_modifier(modifier: str, company: str, at_time: Any) -> list[Any]:
    rows = frappe.get_all(
        "FB Recipe",
        filters={"company": company, "status": "Active"},
        fields=["name", "effective_from", "effective_to"],
        order_by="version_no desc, name asc",
        limit_page_length=500,
    )
    recipes: list[Any] = []
    for row in rows:
        if not _is_effective(row, at_time):
            continue
        doc = _load_recipe(row)
        if any(cstr(_value(effect, "modifier")).strip() == modifier for effect in _rows(doc, "recipe_modifier_effects")):
            recipes.append(doc)
    return recipes


def _active_recipe_rows(*, item: str, company: str) -> list[dict[str, Any]]:
    return list(
        frappe.get_all(
            "FB Recipe",
            filters={"sellable_item": item, "company": company, "status": "Active"},
            fields=["name", "effective_from", "effective_to", "version_no"],
            order_by="version_no desc, name asc",
            limit_page_length=100,
        )
        or []
    )


def _first_effective_recipe(rows: list[dict[str, Any]], at_time: Any) -> Any | None:
    for row in rows:
        if _is_effective(row, at_time):
            return _load_recipe(row)
    return None


def _load_recipe(row: Mapping[str, Any]) -> Any:
    name = cstr(_value(row, "name")).strip()
    return frappe.get_cached_doc("FB Recipe", name) if name else row


def _is_effective(row: Mapping[str, Any], at_time: Any) -> bool:
    current = get_datetime(at_time)
    start = _optional_datetime(_value(row, "effective_from"))
    end = _optional_datetime(_value(row, "effective_to"))
    if start and getattr(start, "tzinfo", None) and not getattr(current, "tzinfo", None):
        current = current.replace(tzinfo=start.tzinfo)
    elif start and not getattr(start, "tzinfo", None) and getattr(current, "tzinfo", None):
        start = start.replace(tzinfo=current.tzinfo)
    if end and getattr(end, "tzinfo", None) and not getattr(current, "tzinfo", None):
        current = current.replace(tzinfo=end.tzinfo)
    elif end and not getattr(end, "tzinfo", None) and getattr(current, "tzinfo", None):
        end = end.replace(tzinfo=current.tzinfo)
    return not (start and current < start) and not (end and current > end)


def _default_effects(recipe: Any) -> list[Any]:
    default_names = {
        cstr(_value(row, "default_modifier")).strip()
        for row in _rows(recipe, "allowed_modifier_groups")
        if cstr(_value(row, "default_modifier")).strip()
    }
    return [
        row
        for row in _rows(recipe, "recipe_modifier_effects")
        if cstr(_value(row, "modifier")).strip() in default_names
    ]


def _effects_for_choice(recipe: Any, modifier: str, selected_effect: Any) -> list[Any]:
    """Apply defaults from other groups, then the selected customer choice."""

    selected_group = cstr(
        frappe.db.get_value("FB Modifier", modifier, "modifier_group")
    ).strip()
    default_names = {
        cstr(_value(row, "default_modifier")).strip()
        for row in _rows(recipe, "allowed_modifier_groups")
        if cstr(_value(row, "default_modifier")).strip()
        and (
            not selected_group
            or cstr(_value(row, "modifier_group")).strip() != selected_group
        )
    }
    defaults = [
        row
        for row in _rows(recipe, "recipe_modifier_effects")
        if cstr(_value(row, "modifier")).strip() in default_names
    ]
    return [*defaults, selected_effect]


def _recipe_mapping(recipe: Any) -> dict[str, Any]:
    return {
        "yield_qty": _value(recipe, "yield_qty"),
        "yield_qty_decimal": _value(recipe, "yield_qty_decimal"),
        "default_serving_qty": _value(recipe, "default_serving_qty") or 1,
        "default_serving_qty_decimal": _value(recipe, "default_serving_qty_decimal"),
        "components": [
            {
                fieldname: _value(row, fieldname)
                for fieldname in (
                    "item", "qty", "qty_decimal", "uom", "stock_qty", "stock_qty_decimal",
                    "stock_uom", "stock_conversion_factor", "stock_conversion_factor_decimal",
                    "loss_factor_pct", "loss_factor_pct_decimal",
                    "affects_stock", "substitution_key", "name",
                )
            }
            for row in _rows(recipe, "components")
        ],
    }


def _modifier_mapping(row: Any) -> dict[str, Any]:
    return {
        fieldname: _value(row, fieldname)
        for fieldname in (
            "modifier", "kind", "target_substitution_key", "target_item", "new_item",
            "qty_delta", "qty_delta_decimal", "qty_uom", "stock_qty_delta", "stock_qty_delta_decimal", "stock_uom",
            "stock_conversion_factor", "stock_conversion_factor_decimal", "scale_percent", "scale_percent_decimal",
            "affects_stock", "affects_recipe",
        )
    }


def _rows(value: Any, fieldname: str) -> list[Any]:
    rows = _value(value, fieldname)
    return list(rows) if isinstance(rows, (list, tuple)) else []


def _value(value: Any, fieldname: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(fieldname)
    return getattr(value, fieldname, None)


def _resolved_component_quantity_expression(alias: str) -> str:
    """Prefer exact resolved-component text, retaining the historical fallback."""

    field = "stock_qty_decimal" if frappe.get_meta("FB Resolved Component").has_field("stock_qty_decimal") else None
    return f"COALESCE(NULLIF({alias}.{field}, ''), {alias}.stock_qty)" if field else f"{alias}.stock_qty"


def _policy(company: str, warehouse: str) -> dict[str, Any] | None:
    rows = frappe.get_all(
        "FB Inventory Policy",
        filters={"company": company, "warehouse": warehouse},
        fields=["name", "cutover_token", "cutover_at"],
        limit_page_length=1,
    )
    return dict(rows[0]) if rows else None


def _not_ready(target_type: str, target_id: str, reason: str) -> CapacityResult:
    return CapacityResult(target_type, target_id, None, False, reason, {})


def _finite_decimal(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _optional_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return get_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
