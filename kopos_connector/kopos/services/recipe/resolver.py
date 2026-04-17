from __future__ import annotations

from copy import deepcopy
from importlib import import_module
from typing import Protocol, Sequence, cast

frappe = import_module("frappe")
frappe_utils = import_module("frappe.utils")

cint = frappe_utils.cint
flt = frappe_utils.flt
get_datetime = frappe_utils.get_datetime


ResolvedComponent = dict[str, object]
ModifierInput = object


class RecipeDocument(Protocol):
    name: str
    version_no: object
    sellable_item: str
    default_serving_qty: object
    effective_from: object
    effective_to: object

    def get(self, key: str) -> object: ...


class ModifierDocument(Protocol):
    name: str
    modifier_group: object
    kind: object
    active: object
    affects_recipe: object
    affects_stock: object
    price_adjustment: object
    instruction_text: object
    display_order: object
    new_item: object
    target_item: object
    qty_delta: object
    qty_uom: object
    target_substitution_key: object
    scale_percent: object


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    return []


def resolve_sale_line(
    item_code: str,
    qty: float,
    modifiers: list[ModifierInput] | None,
    warehouse: str | None,
) -> dict[str, object]:
    if not item_code:
        frappe.throw("Item Code is required to resolve a sale line")

    line_qty = flt(qty)
    if line_qty <= 0:
        frappe.throw("Qty must be greater than 0")

    recipe = _get_active_recipe(item_code)
    recipe_modifiers = _normalize_modifiers(modifiers or [])
    default_modifiers = apply_defaults(_as_list(recipe.get("allowed_modifier_groups")))
    selected_modifiers = _merge_modifiers(default_modifiers, recipe_modifiers)
    resolved_components = resolve_components(recipe, selected_modifiers)

    serving_qty = flt(recipe.default_serving_qty) or 1.0
    scale_factor = line_qty / serving_qty
    scaled_components = [
        _scale_component(component, scale_factor, warehouse)
        for component in resolved_components
    ]

    return {
        "recipe": recipe.name,
        "recipe_version": cint(recipe.version_no),
        "sellable_item": recipe.sellable_item,
        "qty": line_qty,
        "selected_modifiers": [
            _serialize_modifier(modifier) for modifier in selected_modifiers
        ],
        "resolved_components": scaled_components,
    }


def apply_defaults(modifier_groups: list[object]) -> list[ModifierDocument]:
    resolved_defaults: list[ModifierDocument] = []
    seen_groups: set[str] = set()

    for row in modifier_groups or []:
        modifier_group = getattr(row, "modifier_group", None)
        if not modifier_group or modifier_group in seen_groups:
            continue
        seen_groups.add(modifier_group)

        default_modifier_name = getattr(row, "default_modifier", None)
        group_doc = _get_modifier_group_doc(modifier_group)

        if default_modifier_name:
            default_modifier = _get_modifier_doc(default_modifier_name)
            if default_modifier and cint(default_modifier.active):
                resolved_defaults.append(default_modifier)
                continue

        if (
            getattr(group_doc, "default_resolution_policy", None)
            != "Auto Apply Default"
        ):
            continue

        default_modifier_name = frappe.db.get_value(
            "FB Modifier",
            {
                "modifier_group": modifier_group,
                "is_default": 1,
                "active": 1,
            },
            "name",
            order_by="display_order asc, modifier_name asc",
        )
        if not default_modifier_name:
            continue

        default_modifier = _get_modifier_doc(default_modifier_name)
        if default_modifier:
            resolved_defaults.append(default_modifier)

    return resolved_defaults


def resolve_components(
    recipe: RecipeDocument, modifiers: Sequence[object] | None
) -> list[ResolvedComponent]:
    resolved_components = [
        _component_from_row(row)
        for row in _as_list(recipe.get("components"))
        if getattr(row, "item", None)
    ]

    for modifier in _normalize_modifiers(modifiers or []):
        if not _modifier_affects_recipe(modifier):
            continue

        kind = getattr(modifier, "kind", None)
        if kind == "Add":
            _apply_add_modifier(resolved_components, modifier)
        elif kind == "Replace":
            _apply_replace_modifier(resolved_components, modifier)
        elif kind == "Remove":
            _apply_remove_modifier(resolved_components, modifier)
        elif kind == "Scale":
            _apply_scale_modifier(resolved_components, modifier)

    return [
        component for component in resolved_components if flt(component.get("qty")) > 0
    ]


def calculate_stock_qty(qty: float, uom: str | None, item: str | None) -> float:
    quantity = flt(qty)
    if quantity == 0 or not item:
        return quantity

    item_values = frappe.db.get_value("Item", item, ["stock_uom"], as_dict=True)
    if not item_values:
        return quantity

    stock_uom = item_values.stock_uom
    if not uom or not stock_uom or uom == stock_uom:
        return quantity

    conversion_factor = frappe.db.get_value(
        "UOM Conversion Detail",
        {"parent": item, "uom": uom},
        "conversion_factor",
    )
    if conversion_factor:
        return quantity * flt(conversion_factor)

    return quantity


def _get_active_recipe(item_code: str) -> RecipeDocument:
    candidate_names = frappe.get_all(
        "FB Recipe",
        filters={"sellable_item": item_code, "status": "Active"},
        pluck="name",
        order_by="version_no desc, modified desc",
    )

    current_time = get_datetime()
    for recipe_name in candidate_names:
        recipe = frappe.get_cached_doc("FB Recipe", recipe_name)
        if _is_recipe_active(recipe, current_time):
            return cast(RecipeDocument, recipe)

    frappe.throw(f"No active FB Recipe found for item {item_code}")
    raise RuntimeError(f"No active FB Recipe found for item {item_code}")


def _is_recipe_active(recipe: RecipeDocument, at_time: object) -> bool:
    effective_from = (
        get_datetime(recipe.effective_from)
        if getattr(recipe, "effective_from", None)
        else None
    )
    effective_to = (
        get_datetime(recipe.effective_to)
        if getattr(recipe, "effective_to", None)
        else None
    )
    current_time = get_datetime(at_time)
    if effective_from and current_time < effective_from:
        return False
    if effective_to and current_time > effective_to:
        return False
    return getattr(recipe, "status", None) == "Active"


def _normalize_modifiers(modifiers: Sequence[object]) -> list[ModifierDocument]:
    normalized: list[ModifierDocument] = []
    seen_names: set[str] = set()

    for modifier in modifiers:
        modifier_doc = _coerce_modifier(modifier)
        if not modifier_doc:
            continue
        modifier_name = modifier_doc.name
        if modifier_name in seen_names:
            continue
        seen_names.add(modifier_name)
        normalized.append(modifier_doc)

    return normalized


def _coerce_modifier(modifier: ModifierInput) -> ModifierDocument | None:
    if not modifier:
        return None
    if (
        hasattr(modifier, "doctype")
        and getattr(modifier, "doctype", None) == "FB Modifier"
    ):
        return cast(ModifierDocument, modifier)
    if isinstance(modifier, str):
        return _get_modifier_doc(modifier)
    if isinstance(modifier, dict):
        modifier_name = str(
            modifier.get("modifier")
            or modifier.get("name")
            or modifier.get("modifier_code")
            or ""
        ).strip()
        if modifier_name:
            return _get_modifier_doc(modifier_name)
    return None


def _get_modifier_doc(modifier_name: str) -> ModifierDocument | None:
    if not modifier_name:
        return None
    if frappe.db.exists("FB Modifier", modifier_name):
        return frappe.get_cached_doc("FB Modifier", modifier_name)

    resolved_name = frappe.db.get_value(
        "FB Modifier", {"modifier_code": modifier_name}, "name"
    )
    if resolved_name:
        return frappe.get_cached_doc("FB Modifier", resolved_name)
    return None


def _get_modifier_group_doc(modifier_group: str) -> object | None:
    if not modifier_group:
        return None
    if frappe.db.exists("FB Modifier Group", modifier_group):
        return frappe.get_cached_doc("FB Modifier Group", modifier_group)

    resolved_name = frappe.db.get_value(
        "FB Modifier Group", {"group_code": modifier_group}, "name"
    )
    if resolved_name:
        return frappe.get_cached_doc("FB Modifier Group", resolved_name)
    return None


def _merge_modifiers(
    defaults: list[ModifierDocument], selected: list[ModifierDocument]
) -> list[ModifierDocument]:
    selected_groups = {
        getattr(modifier, "modifier_group", None)
        for modifier in selected
        if getattr(modifier, "modifier_group", None)
    }
    merged = list(selected)
    for modifier in defaults:
        modifier_group = getattr(modifier, "modifier_group", None)
        if modifier_group and modifier_group in selected_groups:
            continue
        merged.append(modifier)
    return merged


def _component_from_row(row: object) -> ResolvedComponent:
    uom = _as_optional_str(getattr(row, "uom", None))
    item_code = _as_optional_str(getattr(row, "item", None))
    component: ResolvedComponent = {
        "item": item_code,
        "source_type": "Base Recipe",
        "qty": flt(getattr(row, "qty", 0)),
        "uom": uom,
        "stock_qty": flt(getattr(row, "stock_qty", 0)),
        "stock_uom": getattr(row, "stock_uom", None),
        "source_reference": getattr(row, "name", None) or getattr(row, "idx", None),
        "affects_stock": cint(getattr(row, "affects_stock", 1)),
        "affects_cogs": cint(getattr(row, "affects_cogs", 1)),
        "remarks": getattr(row, "remarks", None),
        "substitution_key": getattr(row, "substitution_key", None),
        "is_substitutable": cint(getattr(row, "is_substitutable", 0)),
        "component_type": getattr(row, "component_type", None),
    }
    if not component["stock_qty"]:
        component["stock_qty"] = calculate_stock_qty(
            flt(component["qty"]),
            uom,
            item_code,
        )
    if not component["stock_uom"]:
        if item_code:
            component["stock_uom"] = frappe.db.get_value("Item", item_code, "stock_uom")
    return component


def _modifier_affects_recipe(modifier: ModifierDocument) -> bool:
    kind = getattr(modifier, "kind", None)
    if kind == "Instruction Only":
        return False
    if cint(getattr(modifier, "active", 0)) == 0:
        return False
    if kind in {"Add", "Replace", "Remove", "Scale"}:
        return True
    return cint(getattr(modifier, "affects_recipe", 0)) == 1


def _apply_add_modifier(
    components: list[ResolvedComponent], modifier: ModifierDocument
) -> None:
    item_code = getattr(modifier, "new_item", None) or getattr(
        modifier, "target_item", None
    )
    qty = flt(getattr(modifier, "qty_delta", 0))
    if not item_code or qty <= 0:
        return

    component: ResolvedComponent = {
        "item": item_code,
        "source_type": "Modifier Add",
        "qty": qty,
        "uom": getattr(modifier, "qty_uom", None)
        or frappe.db.get_value("Item", item_code, "stock_uom"),
        "stock_qty": 0.0,
        "stock_uom": frappe.db.get_value("Item", item_code, "stock_uom"),
        "source_reference": getattr(modifier, "name", None),
        "affects_stock": cint(getattr(modifier, "affects_stock", 0)),
        "affects_cogs": 1,
        "remarks": getattr(modifier, "instruction_text", None),
        "substitution_key": getattr(modifier, "target_substitution_key", None),
        "is_substitutable": 0,
        "component_type": "Ingredient",
    }
    component["stock_qty"] = calculate_stock_qty(
        flt(component["qty"]),
        _as_optional_str(component.get("uom")),
        item_code,
    )
    components.append(component)


def _apply_replace_modifier(
    components: list[ResolvedComponent], modifier: ModifierDocument
) -> None:
    new_item = getattr(modifier, "new_item", None)
    if not new_item:
        return

    matches = _find_matching_components(components, modifier)
    for component in matches:
        component["item"] = new_item
        component["source_type"] = "Modifier Replace"
        component["source_reference"] = getattr(modifier, "name", None)
        component["remarks"] = getattr(
            modifier, "instruction_text", None
        ) or component.get("remarks")
        component["stock_uom"] = frappe.db.get_value("Item", new_item, "stock_uom")
        component["stock_qty"] = calculate_stock_qty(
            flt(component.get("qty")),
            _as_optional_str(component.get("uom")),
            new_item,
        )


def _apply_remove_modifier(
    components: list[ResolvedComponent], modifier: ModifierDocument
) -> None:
    matches = _find_matching_components(components, modifier)
    if not matches:
        return
    match_ids = {id(component) for component in matches}
    components[:] = [
        component for component in components if id(component) not in match_ids
    ]


def _apply_scale_modifier(
    components: list[ResolvedComponent], modifier: ModifierDocument
) -> None:
    scale_percent = flt(getattr(modifier, "scale_percent", 0))
    if scale_percent <= 0:
        return

    matches = _find_matching_components(
        components, modifier, include_all_when_unscoped=True
    )
    scale_factor = scale_percent / 100.0
    for component in matches:
        component["qty"] = flt(component.get("qty")) * scale_factor
        component["stock_qty"] = flt(component.get("stock_qty")) * scale_factor
        component["source_type"] = "Modifier Scale"
        component["source_reference"] = getattr(modifier, "name", None)
        component["remarks"] = getattr(
            modifier, "instruction_text", None
        ) or component.get("remarks")


def _find_matching_components(
    components: list[ResolvedComponent],
    modifier: ModifierDocument,
    include_all_when_unscoped: bool = False,
) -> list[ResolvedComponent]:
    substitution_key = getattr(modifier, "target_substitution_key", None)
    target_item = getattr(modifier, "target_item", None)

    matches: list[ResolvedComponent] = []
    for component in components:
        if substitution_key and component.get("substitution_key") == substitution_key:
            matches.append(component)
            continue
        if target_item and component.get("item") == target_item:
            matches.append(component)

    if matches or not include_all_when_unscoped:
        return matches
    if substitution_key or target_item:
        return matches
    return [
        component
        for component in components
        if cint(component.get("affects_stock", 1)) == 1
    ]


def _scale_component(
    component: ResolvedComponent,
    scale_factor: float,
    warehouse: str | None,
) -> ResolvedComponent:
    scaled = deepcopy(component)
    scaled["qty"] = flt(scaled.get("qty")) * scale_factor
    item_code = _as_optional_str(scaled.get("item"))
    uom = _as_optional_str(scaled.get("uom"))
    scaled["stock_qty"] = calculate_stock_qty(flt(scaled.get("qty")), uom, item_code)
    if item_code and not scaled.get("stock_uom"):
        scaled["stock_uom"] = frappe.db.get_value("Item", item_code, "stock_uom")
    scaled["warehouse"] = warehouse
    scaled.pop("substitution_key", None)
    scaled.pop("is_substitutable", None)
    scaled.pop("component_type", None)
    return scaled


def _serialize_modifier(modifier: ModifierDocument) -> dict[str, object]:
    return {
        "modifier_group": getattr(modifier, "modifier_group", None),
        "modifier": getattr(modifier, "name", None),
        "price_adjustment": flt(getattr(modifier, "price_adjustment", 0)),
        "instruction_text": getattr(modifier, "instruction_text", None),
        "sort_order": cint(getattr(modifier, "display_order", 0)),
        "affects_stock": cint(getattr(modifier, "affects_stock", 0)),
        "affects_recipe": cint(getattr(modifier, "affects_recipe", 0)),
    }


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return None
