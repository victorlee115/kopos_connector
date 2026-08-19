"""Guided Company Director menu and recipe authoring endpoints.

The page in this module is intentionally a thin authoring assistant.  Standard
Item, FB Recipe, BOM, modifier, and Promotion documents remain the authorities;
this module only validates bounded input, derives previews from ERP values, and
coordinates the immutable recipe publish transition.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

import frappe
from frappe import _
from frappe.utils import cint, cstr

from kopos_connector.kopos.doctype.fb_recipe.fb_recipe import (
    _canonical_recipe_hash,
)
from kopos_connector.kopos.services.inventory_autopilot.menu_authoring import (
    MAX_GUIDED_COMPONENTS,
    MAX_GUIDED_MODIFIER_EFFECTS,
    MAX_GUIDED_MODIFIER_GROUPS,
    build_guided_recipe_preview,
    draft_recipe_code,
)


RECIPE_TYPES = {"Finished Drink", "Add-On", "Prep Batch", "Packaging Assembly"}
COMPONENT_TYPES = {"Ingredient", "Prep Item", "Packaging", "Tool Usage"}
ITEM_ROLES = {"Sellable Drink", "Ingredient", "Prep Item", "Packaging"}
RECIPE_COMPONENT_FIELDS = (
    "item",
    "component_type",
    "qty",
    "uom",
    "is_optional",
    "is_substitutable",
    "substitution_key",
    "affects_stock",
    "affects_cogs",
    "loss_factor_pct",
    "sort_order",
    "remarks",
)
ALLOWED_MODIFIER_FIELDS = (
    "modifier_group",
    "required",
    "override_min_selection",
    "override_max_selection",
    "default_modifier",
    "display_order",
    "always_prompt",
)


@frappe.whitelist(methods=["GET"])
def get_menu_recipe_editor(
    *,
    company: str | None = None,
    sellable_item: str | None = None,
    recipe: str | None = None,
    warehouse: str | None = None,
) -> dict[str, Any]:
    """Return bounded data needed by the one-page guided editor."""

    _require_company_director("Menu recipe editor")
    recipe_doc = _load_recipe(recipe) if cstr(recipe).strip() else None
    selected_company = cstr(company or getattr(recipe_doc, "company", None)).strip()
    selected_item = cstr(
        sellable_item or getattr(recipe_doc, "sellable_item", None)
    ).strip()
    if not selected_company or not selected_item:
        frappe.throw(_("Choose a company and menu Item before opening the editor"), frappe.ValidationError)
    _require_existing_company(selected_company)
    item_details = _load_item_details(
        {selected_item}, warehouse=cstr(warehouse).strip() or None
    )
    item = item_details.get(selected_item)
    if not item or cint(item.get("disabled")) or not cint(item.get("is_sales_item")):
        frappe.throw(_("Choose an enabled saleable Item"), frappe.ValidationError)

    components = _recipe_components(recipe_doc) if recipe_doc else []
    component_items = {cstr(row.get("item")).strip() for row in components if cstr(row.get("item")).strip()}
    item_details.update(
        _load_item_details(component_items, warehouse=cstr(warehouse).strip() or None)
    )
    modifier_groups = _recipe_modifier_groups(recipe_doc) if recipe_doc else []
    modifier_group_names = {
        cstr(row.get("modifier_group")).strip()
        for row in modifier_groups
        if cstr(row.get("modifier_group")).strip()
    }
    modifier_effects = _load_modifier_effects(modifier_group_names)
    prepared = _load_prepared_boms(component_items)
    editor_recipe = _serialize_recipe(recipe_doc) if recipe_doc else {
        "name": None,
        "recipe_code": None,
        "recipe_name": item.get("item_name") or selected_item,
        "sellable_item": selected_item,
        "company": selected_company,
        "recipe_type": "Finished Drink",
        "status": "Draft",
        "version_no": _next_version(selected_item, selected_company),
        "yield_qty": "1",
        "yield_uom": item.get("stock_uom") or "Nos",
        "default_serving_qty": "1",
        "default_serving_uom": item.get("stock_uom") or "Nos",
        "effective_from": None,
        "effective_to": None,
        "components": [],
        "allowed_modifier_groups": [],
    }
    preview = _preview_from_values(
        {
            **editor_recipe,
            "warehouse": cstr(warehouse).strip(),
            "components": components,
            "modifier_groups": modifier_groups,
        },
        item_details=item_details,
        prepared_boms=prepared,
        modifier_effects=modifier_effects,
    )
    return {
        "status": "ok",
        "schema_version": "jiji-menu-guided-editor-v1",
        "company": selected_company,
        "sellable_item": selected_item,
        "warehouse": cstr(warehouse).strip() or None,
        "recipe": editor_recipe,
        "item": _public_item_detail(item),
        "items": _public_item_choices(
            _load_item_choices(item_details)
        ),
        "uoms": _uom_choices(),
        "modifier_groups": _modifier_group_choices(),
        "modifier_effects": _public_modifier_effects(modifier_effects),
        "prepared_boms": prepared,
        "preview": _public_preview(preview),
    }


@frappe.whitelist(methods=["POST"])
def create_menu_item_draft(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Create one disabled standard Item from the required-first flow.

    Frappe has no Item Draft state.  A newly commissioned Item is therefore
    created disabled and remains unusable until its standard form has the
    remaining accounting/supplier details and a reviewed recipe.
    """

    _require_company_director("Menu Item drafting")
    if not frappe.has_permission("Item", ptype="create"):
        frappe.throw(_("Menu Item drafting requires Item create permission"), frappe.PermissionError)
    value = _parse_payload(payload, "Menu Item draft payload")
    company = _require(value.get("company"), "Company")
    item_name = _require(value.get("item_name"), "Item name")
    item_group = _require(value.get("item_group"), "Item group")
    stock_uom = _require(value.get("stock_uom"), "Stock UOM")
    role = _require(value.get("item_role"), "Item classification")
    if role not in ITEM_ROLES:
        frappe.throw(_("Choose a valid Item classification"), frappe.ValidationError)
    _require_existing_company(company)
    if not frappe.db.exists("Item Group", item_group):
        frappe.throw(_("Choose an existing Item Group"), frappe.ValidationError)
    if not frappe.db.exists("UOM", stock_uom):
        frappe.throw(_("Choose an existing stock UOM"), frappe.ValidationError)
    item_code = cstr(value.get("item_code")).strip()
    if item_code and frappe.db.exists("Item", item_code):
        frappe.throw(_("That Item Code already exists; choose another one"), frappe.ValidationError)
    meta = frappe.get_meta("Item")
    if not meta.has_field("custom_fb_item_role"):
        frappe.throw(_("Run the inventory migration before classifying a new Item"), frappe.ValidationError)
    item_payload: dict[str, Any] = {
        "doctype": "Item",
        "item_name": item_name,
        "item_group": item_group,
        "stock_uom": stock_uom,
        "is_sales_item": 1 if role == "Sellable Drink" else 0,
        "is_purchase_item": 1 if role in {"Ingredient", "Prep Item", "Packaging"} else 0,
        "is_stock_item": 0 if role == "Sellable Drink" else 1,
        "disabled": 1,
        "custom_fb_item_role": role,
    }
    if item_code:
        item_payload["item_code"] = item_code
    item = frappe.get_doc(item_payload)
    item.insert(ignore_permissions=True)
    return {
        "status": "ok",
        "item": cstr(getattr(item, "name", "") or getattr(item, "item_code", "")),
        "item_code": cstr(getattr(item, "item_code", "") or getattr(item, "name", "")),
        "disabled": True,
        "item_role": role,
        "next_step": "Complete the standard Item form, add supplier/accounting details where applicable, then create and review its measured recipe before enabling it.",
    }


@frappe.whitelist(methods=["POST"])
def preview_menu_recipe(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Return a server-calculated quantity and director-only cost preview."""

    _require_company_director("Recipe preview")
    value = _parse_payload(payload, "Recipe preview payload")
    _validate_recipe_identity(value)
    components = _normalized_components(value.get("components"))
    item_codes = {cstr(value.get("sellable_item")).strip()}
    item_codes.update(cstr(row.get("item")).strip() for row in components)
    item_details = _load_item_details(
        {item for item in item_codes if item},
        warehouse=cstr(value.get("warehouse")).strip() or None,
    )
    groups = _normalized_modifier_groups(value.get("modifier_groups"))
    effects = _load_modifier_effects(
        {cstr(row.get("modifier_group")).strip() for row in groups}
    )
    prepared = _load_prepared_boms(
        {cstr(row.get("item")).strip() for row in components if cstr(row.get("item")).strip()}
    )
    preview = _preview_from_values(
        {**value, "components": components, "modifier_groups": groups},
        item_details=item_details,
        prepared_boms=prepared,
        modifier_effects=effects,
        require_prepared_bom=False,
    )
    return {
        "status": "ok" if preview["valid"] else "blocked",
        "preview": _public_preview(preview),
        "modifier_effects": _public_modifier_effects(effects),
        "prepared_boms": prepared,
    }


@frappe.whitelist(methods=["POST"])
def save_menu_recipe_draft(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Save the guided rows into one standard ``FB Recipe`` Draft."""

    _require_company_director("Recipe drafting")
    value = _parse_payload(payload, "Recipe draft payload")
    _validate_recipe_identity(value)
    components = _normalized_components(value.get("components"))
    groups = _normalized_modifier_groups(value.get("modifier_groups"))
    item_codes = {cstr(value.get("sellable_item")).strip()}
    item_codes.update(cstr(row.get("item")).strip() for row in components)
    item_details = _load_item_details(
        {item for item in item_codes if item},
        warehouse=cstr(value.get("warehouse")).strip() or None,
    )
    effects = _load_modifier_effects(
        {cstr(row.get("modifier_group")).strip() for row in groups}
    )
    prepared = _load_prepared_boms(
        {cstr(row.get("item")).strip() for row in components if cstr(row.get("item")).strip()}
    )
    preview = _preview_from_values(
        {**value, "components": components, "modifier_groups": groups},
        item_details=item_details,
        prepared_boms=prepared,
        modifier_effects=effects,
        require_prepared_bom=False,
    )
    if not preview["valid"]:
        frappe.throw(
            _("Recipe draft has unresolved rows: {0}").format(
                "; ".join(error["message"] for error in preview["errors"][:8])
            ),
            frappe.ValidationError,
        )

    recipe_doc = _load_recipe(value.get("recipe")) if cstr(value.get("recipe")).strip() else None
    if recipe_doc and cstr(getattr(recipe_doc, "status", "")).strip() != "Draft":
        frappe.throw(_("Published recipes are immutable; use Copy / revise"), frappe.ValidationError)
    if recipe_doc:
        if cstr(getattr(recipe_doc, "company", "")).strip() != cstr(value.get("company")).strip() or cstr(getattr(recipe_doc, "sellable_item", "")).strip() != cstr(value.get("sellable_item")).strip():
            frappe.throw(_("The Draft recipe company and menu Item cannot be changed"), frappe.ValidationError)
    else:
        recipe_doc = frappe.get_doc(
            {
                "doctype": "FB Recipe",
                "recipe_code": _recipe_code(value),
                "recipe_name": _recipe_name(value),
                "sellable_item": cstr(value.get("sellable_item")).strip(),
                "recipe_type": _recipe_type(value),
                "status": "Draft",
                "version_no": _next_version(
                    cstr(value.get("sellable_item")).strip(),
                    cstr(value.get("company")).strip(),
                ),
            }
        )
    _set_recipe_fields(recipe_doc, value, components, groups, preview)
    if getattr(recipe_doc, "is_new", lambda: True)():
        recipe_doc.insert(ignore_permissions=True)
    else:
        recipe_doc.save(ignore_permissions=True)
    return {
        "status": "ok",
        "recipe": recipe_doc.name,
        "recipe_code": cstr(getattr(recipe_doc, "recipe_code", "")),
        "preview": _public_preview(preview),
        "next_step": "Review the measured rows, then select this Draft in Publish selected recipes.",
    }


@frappe.whitelist(methods=["POST"])
def copy_menu_recipe_revision(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Copy an immutable recipe into the next standard Draft version."""

    _require_company_director("Recipe revision")
    value = _parse_payload(payload, "Recipe revision payload")
    source_name = cstr(value.get("recipe")).strip()
    if not source_name:
        frappe.throw(_("Choose a recipe to copy"), frappe.ValidationError)
    source = _load_recipe(source_name)
    company = cstr(getattr(source, "company", "")).strip()
    item = cstr(getattr(source, "sellable_item", "")).strip()
    version = _next_version(item, company)
    company_abbr = cstr(frappe.db.get_value("Company", company, "abbr")).strip()
    code = cstr(value.get("recipe_code")).strip() or draft_recipe_code(
        sellable_item=item,
        company_abbr=company_abbr,
        version_no=version,
    )
    if frappe.db.exists("FB Recipe", code):
        frappe.throw(_("The next recipe code already exists; enter a different code"), frappe.ValidationError)
    copied = frappe.get_doc(
        {
            "doctype": "FB Recipe",
            "recipe_code": code,
            "recipe_name": cstr(value.get("recipe_name")).strip() or cstr(getattr(source, "recipe_name", "")),
            "sellable_item": item,
            "recipe_type": cstr(getattr(source, "recipe_type", "Finished Drink")) or "Finished Drink",
            "status": "Draft",
            "version_no": version,
            "yield_qty": getattr(source, "yield_qty", 1),
            "yield_uom": getattr(source, "yield_uom", None),
            "default_serving_qty": getattr(source, "default_serving_qty", 1),
            "default_serving_uom": getattr(source, "default_serving_uom", None),
            "effective_from": getattr(source, "effective_from", None),
            "effective_to": getattr(source, "effective_to", None),
            "company": company,
            "station": getattr(source, "station", None),
            "notes": "Copied from {0}. Review measured rows before publishing.".format(source.name),
            "components": [_child_dict(row, RECIPE_COMPONENT_FIELDS) for row in getattr(source, "components", None) or []],
            "allowed_modifier_groups": [_child_dict(row, ALLOWED_MODIFIER_FIELDS) for row in getattr(source, "allowed_modifier_groups", None) or []],
        }
    )
    copied.insert(ignore_permissions=True)
    return {"status": "ok", "recipe": copied.name, "recipe_code": code, "version_no": version}


@frappe.whitelist(methods=["GET"])
def get_menu_recipe_publish_queue(*, company: str | None = None) -> dict[str, Any]:
    """List a bounded, director-only queue of Draft recipes ready for review."""

    _require_company_director("Recipe publishing")
    selected_company = cstr(company).strip()
    if not selected_company:
        frappe.throw(_("Choose a Company before reviewing Draft recipes"), frappe.ValidationError)
    rows = frappe.get_all(
        "FB Recipe",
        filters={"company": selected_company, "status": "Draft"},
        fields=["name", "recipe_code", "recipe_name", "sellable_item", "version_no", "modified"],
        order_by="modified desc",
        limit_page_length=100,
    )
    queue: list[dict[str, Any]] = []
    for row in rows:
        doc = frappe.get_doc("FB Recipe", row["name"])
        preview = _preview_for_doc(doc)
        queue.append(
            {
                **{key: row.get(key) for key in ("name", "recipe_code", "recipe_name", "sellable_item", "version_no", "modified")},
                "valid": bool(preview["valid"]),
                "errors": [error["message"] for error in preview["errors"][:3]],
                "component_count": len(getattr(doc, "components", None) or []),
            }
        )
    return {"status": "ok", "company": selected_company, "recipes": queue}


@frappe.whitelist(methods=["POST"])
def publish_menu_recipe_selection(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Publish selected Draft recipes as one all-or-nothing transition.

    Every selected recipe is fully validated before any document is saved.  A
    database savepoint additionally protects against a late standard-document
    validation or database failure during the write phase.
    """

    _require_company_director("Recipe publishing")
    value = _parse_payload(payload, "Recipe publish payload")
    names = _recipe_selection(value.get("recipes") or value.get("selected"))
    documents = [_load_recipe(name) for name in names]
    prepared: list[Any] = []
    failures: list[dict[str, str]] = []
    for document in documents:
        try:
            _prepare_recipe_for_publish(document)
            prepared.append(document)
        except Exception as error:
            failures.append({"recipe": cstr(getattr(document, "name", "")), "message": cstr(error)[:500]})
    if failures:
        frappe.throw(
            _("Nothing was published. Fix every selected recipe first: {0}").format(
                "; ".join(f"{row['recipe']}: {row['message']}" for row in failures[:6])
            ),
            frappe.ValidationError,
        )

    savepoint = "kopos_menu_recipe_publish"
    frappe.db.savepoint(savepoint)
    try:
        for document in prepared:
            document.save(ignore_permissions=True)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise
    return {
        "status": "ok",
        "published": [cstr(document.name) for document in prepared],
        "count": len(prepared),
    }


def _preview_for_doc(document: Any) -> dict[str, Any]:
    groups = _recipe_modifier_groups(document)
    components = _recipe_components(document)
    item_codes = {cstr(getattr(document, "sellable_item", "")).strip()}
    item_codes.update(cstr(row.get("item")).strip() for row in components)
    item_details = _load_item_details({item for item in item_codes if item}, warehouse=None)
    effects = _load_modifier_effects(
        {cstr(row.get("modifier_group")).strip() for row in groups}
    )
    prepared = _load_prepared_boms(
        {cstr(row.get("item")).strip() for row in components if cstr(row.get("item")).strip()}
    )
    return _preview_from_values(
        {
            "company": getattr(document, "company", None),
            "sellable_item": getattr(document, "sellable_item", None),
            "yield_qty": getattr(document, "yield_qty", None),
            "yield_uom": getattr(document, "yield_uom", None),
            "default_serving_qty": getattr(document, "default_serving_qty", None),
            "default_serving_uom": getattr(document, "default_serving_uom", None),
            "components": components,
            "modifier_groups": groups,
        },
        item_details=item_details,
        prepared_boms=prepared,
        modifier_effects=effects,
    )


def _prepare_recipe_for_publish(document: Any) -> None:
    if cstr(getattr(document, "status", "")).strip() != "Draft":
        raise ValueError("recipe is not a Draft")
    if not cstr(getattr(document, "effective_from", "")).strip():
        raise ValueError("effective_from is required before publishing a recipe")
    if cstr(getattr(document, "canonical_hash", "")).strip():
        raise ValueError("recipe already has a canonical published hash")
    preview = _preview_for_doc(document)
    if not preview["valid"]:
        raise ValueError("; ".join(error["message"] for error in preview["errors"][:8]))
    document.status = "Active"
    document.freeze_stock_component_conversions()
    document.freeze_modifier_effects()
    document.canonical_hash = _canonical_recipe_hash(document)
    document.validate()


def _preview_from_values(
    value: Mapping[str, Any],
    *,
    item_details: Mapping[str, Mapping[str, Any]],
    prepared_boms: Mapping[str, Mapping[str, Any]],
    modifier_effects: list[Mapping[str, Any]],
    require_prepared_bom: bool = True,
) -> dict[str, Any]:
    sellable = cstr(value.get("sellable_item")).strip()
    sellable_detail = item_details.get(sellable, {})
    yield_uom = cstr(value.get("yield_uom")).strip()
    serving_uom = cstr(value.get("default_serving_uom")).strip()
    yield_factor = _item_uom_factor(sellable_detail, yield_uom)
    serving_factor = _item_uom_factor(sellable_detail, serving_uom)
    preview = build_guided_recipe_preview(
        yield_qty=value.get("yield_qty"),
        yield_uom=yield_uom,
        default_serving_qty=value.get("default_serving_qty"),
        default_serving_uom=serving_uom,
        components=list(value.get("components") or []),
        item_details=item_details,
        modifier_effects=modifier_effects,
        prepared_components=prepared_boms,
        yield_conversion_factor=yield_factor,
        serving_conversion_factor=serving_factor,
        require_prepared_bom=require_prepared_bom,
    )
    if yield_factor is None and yield_uom:
        preview["errors"].append({"field": "yield_uom", "message": f"No ERP conversion for {yield_uom} on {sellable}"})
        preview["valid"] = False
    if serving_factor is None and serving_uom:
        preview["errors"].append({"field": "default_serving_uom", "message": f"No ERP conversion for {serving_uom} on {sellable}"})
        preview["valid"] = False
    return preview


def _set_recipe_fields(document: Any, value: Mapping[str, Any], components: list[dict[str, Any]], groups: list[dict[str, Any]], preview: Mapping[str, Any]) -> None:
    document.recipe_name = _recipe_name(value)
    document.recipe_type = _recipe_type(value)
    document.status = "Draft"
    document.version_no = cint(getattr(document, "version_no", None) or _next_version(cstr(value.get("sellable_item")).strip(), cstr(value.get("company")).strip()))
    document.yield_qty = _positive(value.get("yield_qty"), "Yield quantity")
    document.yield_uom = _require(value.get("yield_uom"), "Yield UOM")
    document.default_serving_qty = _positive(value.get("default_serving_qty"), "Serving quantity")
    document.default_serving_uom = _require(value.get("default_serving_uom"), "Serving UOM")
    document.company = _require(value.get("company"), "Company")
    document.effective_from = value.get("effective_from") or None
    document.effective_to = value.get("effective_to") or None
    document.set("components", [_component_document_row(row, preview) for row in components])
    document.set("allowed_modifier_groups", groups)
    document.set("recipe_modifier_effects", [])
    document.canonical_hash = ""
    document.notes = cstr(value.get("notes")).strip() or "Guided Draft from JiJi Menu & Recipes. Review measured rows before publishing."


def _component_document_row(row: Mapping[str, Any], preview: Mapping[str, Any]) -> dict[str, Any]:
    item = cstr(row.get("item")).strip()
    matched = next((candidate for candidate in preview.get("components", []) if candidate.get("item") == item and candidate.get("entered_uom") == cstr(row.get("uom")).strip()), {})
    result = {field: row.get(field) for field in RECIPE_COMPONENT_FIELDS if field in row}
    result.update(
        {
            "item": item,
            "component_type": cstr(row.get("component_type") or "Ingredient"),
            "qty": _positive(row.get("qty"), "Component quantity"),
            "uom": _require(row.get("uom"), "Component UOM"),
            "stock_qty": matched.get("stock_qty_per_batch") or _positive(row.get("qty"), "Component stock quantity"),
            "stock_uom": matched.get("stock_uom") or cstr(row.get("uom")),
            "stock_conversion_factor": matched.get("conversion_factor") or "1",
            "affects_stock": 0 if cstr(row.get("component_type")) == "Tool Usage" else cint(row.get("affects_stock", 1)),
            "affects_cogs": 0 if cstr(row.get("component_type")) == "Tool Usage" else cint(row.get("affects_cogs", 1)),
            "loss_factor_pct": row.get("loss_factor_pct") or 0,
        }
    )
    return result


def _recipe_selection(raw: Any) -> list[str]:
    if not isinstance(raw, list) or not raw or len(raw) > 100:
        frappe.throw(_("Select between 1 and 100 Draft recipes"), frappe.ValidationError)
    names: list[str] = []
    for row in raw:
        if isinstance(row, str):
            name = row.strip()
        elif isinstance(row, Mapping):
            if cstr(row.get("doctype")).strip() not in {"", "FB Recipe"}:
                frappe.throw(_("Only FB Recipe records can be published here"), frappe.ValidationError)
            name = cstr(row.get("name")).strip()
        else:
            name = ""
        if not name or len(name) > 140 or name in names:
            frappe.throw(_("Recipe selection contains a missing or duplicate name"), frappe.ValidationError)
        names.append(name)
    return names


def _normalized_components(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > MAX_GUIDED_COMPONENTS:
        frappe.throw(_("A recipe can contain at most {0} component rows").format(MAX_GUIDED_COMPONENTS), frappe.ValidationError)
    rows: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, Mapping):
            frappe.throw(_("Every component row must be an object"), frappe.ValidationError)
        component_type = cstr(row.get("component_type") or "Ingredient").strip()
        if component_type not in COMPONENT_TYPES:
            frappe.throw(_("Choose a valid component type"), frappe.ValidationError)
        normalized = {field: row.get(field) for field in RECIPE_COMPONENT_FIELDS if field in row}
        normalized["item"] = _require(row.get("item"), "Component Item")
        normalized["component_type"] = component_type
        normalized["qty"] = _positive(row.get("qty"), "Component quantity")
        normalized["uom"] = _require(row.get("uom"), "Component UOM")
        normalized.setdefault("affects_stock", 0 if component_type == "Tool Usage" else 1)
        normalized.setdefault("affects_cogs", 0 if component_type == "Tool Usage" else 1)
        normalized["loss_factor_pct"] = _non_negative(row.get("loss_factor_pct", 0), "Loss factor")
        rows.append(normalized)
    return rows


def _normalized_modifier_groups(raw: Any) -> list[dict[str, Any]]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_GUIDED_MODIFIER_GROUPS:
        frappe.throw(_("A recipe can include at most {0} modifier groups").format(MAX_GUIDED_MODIFIER_GROUPS), frappe.ValidationError)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in raw:
        if isinstance(row, str):
            row = {"modifier_group": row}
        if not isinstance(row, Mapping):
            frappe.throw(_("Every modifier group row must be an object"), frappe.ValidationError)
        group = _require(row.get("modifier_group"), "Modifier group")
        if group in seen:
            frappe.throw(_("A modifier group can only appear once"), frappe.ValidationError)
        seen.add(group)
        result.append({field: row.get(field) for field in ALLOWED_MODIFIER_FIELDS if field in row} | {"modifier_group": group})
    return result


def _validate_recipe_identity(value: Mapping[str, Any]) -> None:
    _require(value.get("company"), "Company")
    item = _require(value.get("sellable_item"), "Menu Item")
    _require_existing_company(cstr(value.get("company")).strip())
    item_row = frappe.db.get_value("Item", item, ["is_sales_item", "disabled"], as_dict=True)
    if not item_row or cint(item_row.get("disabled")) or not cint(item_row.get("is_sales_item")):
        frappe.throw(_("Choose an enabled saleable Item"), frappe.ValidationError)
    if cstr(value.get("recipe_type") or "Finished Drink") not in RECIPE_TYPES:
        frappe.throw(_("Choose a valid recipe type"), frappe.ValidationError)


def _load_recipe(name: Any) -> Any:
    recipe_name = _require(name, "Recipe")
    if not frappe.db.exists("FB Recipe", recipe_name):
        frappe.throw(_("Recipe {0} was not found").format(recipe_name), frappe.DoesNotExistError)
    return frappe.get_doc("FB Recipe", recipe_name)


def _load_item_details(items: set[str], *, warehouse: str | None) -> dict[str, dict[str, Any]]:
    if len(items) > MAX_GUIDED_COMPONENTS + 1:
        frappe.throw(_("The guided editor can resolve at most {0} Items").format(MAX_GUIDED_COMPONENTS + 1), frappe.ValidationError)
    details: dict[str, dict[str, Any]] = {}
    item_meta = frappe.get_meta("Item")
    item_fields = ["name", "item_name", "stock_uom", "is_stock_item", "is_sales_item", "disabled"]
    if item_meta.has_field("valuation_rate"):
        item_fields.append("valuation_rate")
    for row in frappe.get_all("Item", filters={"name": ["in", sorted(items)]}, fields=item_fields, limit_page_length=MAX_GUIDED_COMPONENTS + 1):
        details[cstr(row.get("name")).strip()] = {
            **dict(row),
            "conversion_factors": _conversion_factors(cstr(row.get("name")).strip()),
            "valuation_rate": _valuation_rate(cstr(row.get("name")).strip(), warehouse),
        }
    return details


def _load_item_choices(existing: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return a bounded Item lookup for the editor datalist.

    The editor still resolves and validates the selected rows server-side.  A
    small lookup makes entry easier without placing the entire Item master or
    any valuation data in the browser.
    """

    result = dict(existing)
    rows = frappe.get_all(
        "Item",
        filters={"disabled": 0},
        fields=["name", "item_name", "stock_uom", "is_stock_item", "is_sales_item", "disabled"],
        order_by="item_name asc, name asc",
        limit_page_length=500,
    )
    for row in rows:
        name = cstr(row.get("name")).strip()
        if name and name not in result:
            result[name] = dict(row)
    return result


def _conversion_factors(item: str) -> dict[str, Any]:
    rows = frappe.get_all("UOM Conversion Detail", filters={"parent": item}, fields=["uom", "conversion_factor"], limit_page_length=100)
    return {cstr(row.get("uom")).strip(): row.get("conversion_factor") for row in rows if cstr(row.get("uom")).strip()}


def _valuation_rate(item: str, warehouse: str | None) -> Any:
    if warehouse and frappe.db.exists("Bin", {"item_code": item, "warehouse": warehouse}):
        value = frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "valuation_rate")
        if value not in (None, ""):
            return value
    if frappe.get_meta("Item").has_field("valuation_rate"):
        return frappe.db.get_value("Item", item, "valuation_rate")
    return None


def _load_prepared_boms(items: set[str]) -> dict[str, dict[str, Any]]:
    prepared_items = sorted(item for item in items if item)
    if not prepared_items or not frappe.db.exists("DocType", "BOM"):
        return {}
    meta = frappe.get_meta("BOM")
    fields = ["name", "item", "quantity", "docstatus", "is_active", "is_default"]
    custom_fields = (
        "custom_kopos_autoprep_enabled",
        "custom_kopos_batch_qty",
        "custom_kopos_min_ready_qty",
        "custom_kopos_preparation_lead_minutes",
        "custom_kopos_preparation_instructions",
    )
    fields.extend(field for field in custom_fields if meta.has_field(field))
    rows = frappe.get_all(
        "BOM",
        filters={"item": ["in", prepared_items], "docstatus": 1, "is_active": 1},
        fields=fields,
        order_by="is_default desc, modified desc",
        limit_page_length=len(prepared_items),
    )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = cstr(row.get("item")).strip()
        if item and item not in result:
            result[item] = {
                "name": cstr(row.get("name")),
                "item": item,
                "status": "Submitted",
                "ready": True,
                "quantity": row.get("quantity"),
                "autoprep_enabled": cint(row.get("custom_kopos_autoprep_enabled")),
                "batch_qty": row.get("custom_kopos_batch_qty") or row.get("quantity"),
                "min_ready_qty": row.get("custom_kopos_min_ready_qty") or row.get("custom_kopos_batch_qty") or row.get("quantity"),
                "preparation_lead_minutes": row.get("custom_kopos_preparation_lead_minutes"),
                "instructions": cstr(row.get("custom_kopos_preparation_instructions"))[:2_000],
            }
    return result


def _load_modifier_effects(groups: set[str]) -> list[dict[str, Any]]:
    group_names = sorted(group for group in groups if group)
    if not group_names:
        return []
    rows = frappe.get_all(
        "FB Modifier",
        filters={"modifier_group": ["in", group_names], "active": 1},
        fields=["name", "modifier_group", "modifier_name", "kind", "target_item", "new_item", "qty_delta", "qty_uom", "scale_percent", "affects_stock", "affects_recipe"],
        order_by="modifier_group asc, display_order asc, name asc",
        limit_page_length=MAX_GUIDED_MODIFIER_EFFECTS,
    )
    return [dict(row) for row in rows]


def _modifier_group_choices() -> list[dict[str, Any]]:
    rows = frappe.get_all(
        "FB Modifier Group",
        filters={"active": 1},
        fields=["name", "group_code", "group_name", "selection_type", "is_required", "min_selection", "max_selection"],
        order_by="group_name asc, name asc",
        limit_page_length=MAX_GUIDED_MODIFIER_GROUPS,
    )
    return [dict(row) for row in rows]


def _uom_choices() -> list[str]:
    return [cstr(row.get("name")).strip() for row in frappe.get_all("UOM", filters={"enabled": 1}, fields=["name"], order_by="name asc", limit_page_length=200) if cstr(row.get("name")).strip()]


def _public_item_choices(details: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [_public_item_detail(details[name]) for name in sorted(details)]


def _public_item_detail(detail: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": cstr(detail.get("name")),
        "item_name": cstr(detail.get("item_name")) or cstr(detail.get("name")),
        "stock_uom": cstr(detail.get("stock_uom")),
        "is_stock_item": cint(detail.get("is_stock_item")),
        "is_sales_item": cint(detail.get("is_sales_item")),
    }


def _public_modifier_effects(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: row.get(key)
            for key in ("name", "modifier_group", "modifier_name", "kind", "target_item", "new_item", "qty_delta", "qty_uom", "scale_percent", "affects_stock", "affects_recipe")
        }
        for row in rows[:MAX_GUIDED_MODIFIER_EFFECTS]
    ]


def _public_preview(preview: Mapping[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(preview, default=str))
    for row in result.get("components", []):
        row["valuation_rate_sen"] = _money_to_sen(row.pop("valuation_rate", None))
        row["cost_per_batch_sen"] = _money_to_sen(row.pop("cost_per_batch", None))
    result["cost_per_batch_sen"] = _money_to_sen(result.pop("cost_per_batch", None))
    result["cost_per_serving_sen"] = _money_to_sen(result.pop("cost_per_serving", None))
    return result


def _serialize_recipe(document: Any) -> dict[str, Any]:
    return {
        "name": cstr(getattr(document, "name", "")),
        "recipe_code": cstr(getattr(document, "recipe_code", "")),
        "recipe_name": cstr(getattr(document, "recipe_name", "")),
        "sellable_item": cstr(getattr(document, "sellable_item", "")),
        "company": cstr(getattr(document, "company", "")),
        "recipe_type": cstr(getattr(document, "recipe_type", "")) or "Finished Drink",
        "status": cstr(getattr(document, "status", "")) or "Draft",
        "version_no": cint(getattr(document, "version_no", 0)),
        "yield_qty": getattr(document, "yield_qty", None),
        "yield_uom": cstr(getattr(document, "yield_uom", "")),
        "default_serving_qty": getattr(document, "default_serving_qty", None),
        "default_serving_uom": cstr(getattr(document, "default_serving_uom", "")),
        "effective_from": cstr(getattr(document, "effective_from", "")),
        "effective_to": cstr(getattr(document, "effective_to", "")),
        "components": _recipe_components(document),
        "allowed_modifier_groups": _recipe_modifier_groups(document),
    }


def _recipe_components(document: Any | None) -> list[dict[str, Any]]:
    return [_child_dict(row, RECIPE_COMPONENT_FIELDS) for row in getattr(document, "components", None) or []]


def _recipe_modifier_groups(document: Any | None) -> list[dict[str, Any]]:
    return [_child_dict(row, ALLOWED_MODIFIER_FIELDS) for row in getattr(document, "allowed_modifier_groups", None) or []]


def _child_dict(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field in fields:
        if isinstance(row, Mapping):
            value = row.get(field)
        else:
            value = getattr(row, field, None)
        if value not in (None, ""):
            values[field] = value
    return values


def _recipe_code(value: Mapping[str, Any]) -> str:
    explicit = cstr(value.get("recipe_code")).strip()
    if explicit:
        if len(explicit) > 140:
            frappe.throw(_("Recipe code must be 140 characters or fewer"), frappe.ValidationError)
        if frappe.db.exists("FB Recipe", explicit):
            frappe.throw(_("That recipe code already exists"), frappe.ValidationError)
        return explicit
    return draft_recipe_code(
        sellable_item=cstr(value.get("sellable_item")).strip(),
        company_abbr=cstr(frappe.db.get_value("Company", value.get("company"), "abbr")).strip(),
        version_no=_next_version(cstr(value.get("sellable_item")).strip(), cstr(value.get("company")).strip()),
    )


def _recipe_name(value: Mapping[str, Any]) -> str:
    name = cstr(value.get("recipe_name") or value.get("sellable_item")).strip()
    if not name or len(name) > 140:
        frappe.throw(_("Recipe name is required and must be 140 characters or fewer"), frappe.ValidationError)
    return name


def _recipe_type(value: Mapping[str, Any]) -> str:
    value_type = cstr(value.get("recipe_type") or "Finished Drink").strip()
    if value_type not in RECIPE_TYPES:
        frappe.throw(_("Choose a valid recipe type"), frappe.ValidationError)
    return value_type


def _next_version(item: str, company: str) -> int:
    rows = frappe.get_all("FB Recipe", filters={"sellable_item": item, "company": company}, fields=["version_no"], order_by="version_no desc", limit_page_length=1)
    return cint(rows[0].get("version_no")) + 1 if rows else 1


def _item_uom_factor(detail: Mapping[str, Any], uom: str) -> Decimal | None:
    if not uom:
        return None
    stock_uom = cstr(detail.get("stock_uom")).strip()
    if uom == stock_uom:
        return Decimal("1")
    raw = (detail.get("conversion_factors") or {}).get(uom)
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() and value > 0 else None


def _money_to_sen(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return int((parsed * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _parse_payload(payload: str | dict[str, Any], label: str) -> dict[str, Any]:
    value: Any = payload
    if isinstance(payload, str):
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as error:
            frappe.throw(_("{0} is not valid JSON").format(label), frappe.ValidationError)
            raise AssertionError("frappe.throw must raise") from error
    if not isinstance(value, dict):
        frappe.throw(_("{0} must be an object").format(label), frappe.ValidationError)
    return value


def _require(value: Any, label: str) -> str:
    text = cstr(value).strip()
    if not text:
        frappe.throw(_("{0} is required").format(label), frappe.ValidationError)
    if len(text) > 140:
        frappe.throw(_("{0} must be 140 characters or fewer").format(label), frappe.ValidationError)
    return text


def _positive(value: Any, label: str) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        frappe.throw(_("{0} must be a positive number").format(label), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if not parsed.is_finite() or parsed <= 0:
        frappe.throw(_("{0} must be a positive number").format(label), frappe.ValidationError)
    return format(parsed.normalize(), "f")


def _non_negative(value: Any, label: str) -> str:
    if value in (None, ""):
        return "0"
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        frappe.throw(_("{0} must be zero or greater").format(label), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if not parsed.is_finite() or parsed < 0:
        frappe.throw(_("{0} must be zero or greater").format(label), frappe.ValidationError)
    return format(parsed.normalize(), "f")


def _require_existing_company(company: str) -> None:
    if not frappe.db.exists("Company", company):
        frappe.throw(_("Choose an existing Company"), frappe.ValidationError)


def _require_company_director(action: str) -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    if "Company Director" not in roles and "System Manager" not in roles:
        frappe.throw(_("{0} requires Company Director permission").format(action), frappe.PermissionError)
