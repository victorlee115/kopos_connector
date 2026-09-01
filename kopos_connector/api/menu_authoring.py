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
from frappe.utils import cint, cstr, flt, now_datetime

from kopos_connector.api.cafe_units import amount_phrase, unit_words
from kopos_connector.api.catalog import get_item_prices_map
from kopos_connector.api.director_scope import (
    director_pos_profiles,
    director_price_list,
    require_director_role,
    resolve_director_context,
)
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
ITEM_ROLES = {
    "Sellable Drink",
    "Packaged Finished Good",
    "Non-Stock Service",
    "Ingredient",
    "Prep Item",
    "Packaging",
}
SALEABLE_ITEM_ROLES = {
    "Sellable Drink",
    "Packaged Finished Good",
    "Non-Stock Service",
}
STOCK_ITEM_ROLES = {
    "Packaged Finished Good",
    "Ingredient",
    "Prep Item",
    "Packaging",
}
PURCHASE_ITEM_ROLES = {
    "Packaged Finished Good",
    "Ingredient",
    "Prep Item",
    "Packaging",
}
GUIDED_STOCK_ROLES = {"Ingredient", "Packaging"}
GUIDED_CAFE_UNITS = {
    "Bag", "Bottle", "Box", "Canister", "Carton", "Dozen", "Each", "Gram",
    "Kg", "Kilogram", "Litre", "Millilitre", "No", "Nos", "Number", "Packet",
    "Pcs", "Piece", "Sack", "Sleeve", "Tin", "Tub", "Unit",
}
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
def get_menu_page_choices(*, company: str | None = None) -> dict[str, Any]:
    """Return friendly, scoped choices for the beginner Menu page.

    Internal document names remain option values used by the browser.  The
    person using the page sees only café, product, and outlet names.
    """

    _require_company_director("Menu")
    context = resolve_director_context(company=cstr(company).strip() or None)
    result: dict[str, Any] = {
        "status": context.status,
        "selected_company": context.company or None,
        "cafes": [
            {"value": name, "label": name}
            for name in context.allowed_companies
        ],
        "products": [],
        "menu_groups": [],
        "outlets": [],
    }
    if not context.company:
        return result

    priced_items = {
        cstr(value).strip()
        for value in frappe.get_all(
            "Item Price",
            filters={
                "price_list": ["in", list(context.allowed_price_lists) or [""]],
            },
            pluck="item_code",
            limit_page_length=501,
        )
        if cstr(value).strip()
    }
    if len(priced_items) > 500:
        frappe.throw(_("This café has too many products to show safely"), frappe.ValidationError)
    product_rows = (
        frappe.get_all(
            "Item",
            filters={
                "name": ["in", sorted(priced_items)],
                "is_sales_item": 1,
            },
            fields=["name", "item_name", "item_group"],
            order_by="item_name asc, name asc",
            limit_page_length=500,
        )
        if priced_items
        else []
    )
    result["products"] = [
        {
            "value": cstr(row.get("name")).strip(),
            "label": cstr(row.get("item_name")).strip() or cstr(row.get("name")).strip(),
        }
        for row in product_rows
        if cstr(row.get("name")).strip()
    ]
    result["menu_groups"] = _public_menu_group_choices(product_rows)

    profile_names = director_pos_profiles(context)
    profile_rows = (
        frappe.get_all(
            "POS Profile",
            filters={"name": ["in", list(profile_names)]},
            fields=["name", "warehouse"],
            order_by="name asc",
            limit_page_length=500,
        )
        if profile_names
        else []
    )
    warehouse_names = {
        cstr(row.get("warehouse")).strip()
        for row in profile_rows
        if cstr(row.get("warehouse")).strip()
    }
    warehouse_rows = (
        frappe.get_all(
            "Warehouse",
            filters={"name": ["in", sorted(warehouse_names)]},
            fields=["name", "warehouse_name"],
            limit_page_length=500,
        )
        if warehouse_names
        else []
    )
    friendly_warehouses = {
        cstr(row.get("name")).strip(): (
            cstr(row.get("warehouse_name")).strip() or cstr(row.get("name")).strip()
        )
        for row in warehouse_rows
    }
    result["outlets"] = [
        {
            "value": cstr(row.get("name")).strip(),
            "label": friendly_warehouses.get(
                cstr(row.get("warehouse")).strip(),
                cstr(row.get("name")).strip(),
            ),
        }
        for row in profile_rows
        if cstr(row.get("name")).strip()
    ]
    return result


def _public_menu_group_choices(
    product_rows: list[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Return only front-of-house groups already used by this café.

    Item groups are site-wide records, so listing every group would expose
    ingredients and other internal setup on a beginner page. The director
    sees groups already used by this café's products. A new café gets one safe
    default chosen behind the scenes.
    """

    names = {
        cstr(row.get("item_group")).strip()
        for row in product_rows
        if cstr(row.get("item_group")).strip()
    }
    default_group = ""
    if not names:
        default_group = _default_menu_item_group()
        if default_group:
            names.add(default_group)
    if not names:
        frappe.throw(_("Ask Support to add a menu group first"), frappe.ValidationError)
    rows = frappe.get_all(
        "Item Group",
        filters={"name": ["in", sorted(names)], "is_group": 0},
        fields=["name", "item_group_name"],
        order_by="item_group_name asc, name asc",
        limit_page_length=50,
    )
    choices = []
    for row in rows:
        name = cstr(row.get("name")).strip()
        if not name:
            continue
        label = cstr(row.get("item_group_name")).strip()
        if name == default_group and label.casefold() in {
            "products",
            "all item groups",
        }:
            label = _("Other")
        choices.append({"value": name, "label": label or _("Other")})
    return choices


def _default_menu_item_group() -> str:
    """Pick one valid leaf group without asking a café user about setup."""

    for candidate in ("Drinks", "Beverages", "Products"):
        row = frappe.db.get_value(
            "Item Group", candidate, ["name", "is_group"], as_dict=True
        )
        if row and not cint(row.get("is_group")):
            return cstr(row.get("name")).strip()
    rows = frappe.get_all(
        "Item Group",
        filters={"is_group": 0},
        fields=["name"],
        order_by="lft asc, name asc",
        limit_page_length=50,
    )
    unsuitable = {"ingredient", "raw material", "packaging", "consumable"}
    for row in rows:
        name = cstr(row.get("name")).strip()
        if name and not any(word in name.casefold() for word in unsuitable):
            return name
    return ""


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
    requested_company = cstr(company).strip()
    if recipe_doc is None and cstr(sellable_item).strip() and requested_company:
        # Opening on a drink with no recipe named should show the drink's own
        # recipe, not an empty form. The capacity owner already answers which
        # one is active, so there is no second definition of "active" here.
        from kopos_connector.kopos.services.inventory_autopilot.availability_capacity import (
            active_recipe_name,
        )

        selected_context = resolve_director_context(company=requested_company)
        active = active_recipe_name(
            item=cstr(sellable_item).strip(), company=selected_context.company
        )
        if active:
            recipe_doc = _load_recipe(active)
    selected_company = cstr(company or getattr(recipe_doc, "company", None)).strip()
    if (
        recipe_doc is not None
        and cstr(getattr(recipe_doc, "company", None)).strip() != selected_company
    ):
        frappe.throw(_("This recipe belongs to a different café"), frappe.PermissionError)
    selected_item = cstr(
        sellable_item or getattr(recipe_doc, "sellable_item", None)
    ).strip()
    if not selected_company or not selected_item:
        frappe.throw(_("Choose a café and something on the menu first"), frappe.ValidationError)
    _require_existing_company(selected_company)
    # Costs and capacity are read against an outlet's own stock. A director
    # who has not named one is asking about the outlet they already have, and
    # one owner answers that for every desk page.
    context = resolve_director_context(
        company=selected_company,
        warehouse=cstr(warehouse).strip() or None,
    )
    resolved_warehouse = context.warehouse
    item_details = _load_item_details({selected_item}, warehouse=resolved_warehouse or None)
    item = item_details.get(selected_item)
    if not item or not cint(item.get("is_sales_item")):
        frappe.throw(_("Choose something that is for sale"), frappe.ValidationError)
    _require_item_in_company(selected_item, context)

    components = _recipe_components(recipe_doc) if recipe_doc else []
    component_items = {cstr(row.get("item")).strip() for row in components if cstr(row.get("item")).strip()}
    item_details.update(
        _load_item_details(component_items, warehouse=resolved_warehouse or None)
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
            "warehouse": resolved_warehouse,
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
        "warehouse": resolved_warehouse or None,
        "recipe": editor_recipe,
        "item": _public_item_detail(item),
        "items": _public_item_choices(
            _load_item_choices(
                item_details,
                company=context.company,
                allowed_warehouses=context.allowed_warehouses,
            )
        ),
        "units": _friendly_uom_choices(),
        "modifier_groups": _modifier_group_choices(modifier_group_names),
        "modifier_effects": _public_modifier_effects(modifier_effects),
        "prepared_boms": prepared,
        "preview": _public_preview(
            preview,
            price_sen=_selling_price_sen(selected_item, selected_company, item_details),
        ),
        "can_make": _stock_can_make(selected_item, selected_company, resolved_warehouse),
    }


def _stock_can_make(item: str, company: str, warehouse: str) -> dict[str, Any]:
    """How many of this drink today's stock could make.

    Read from the capacity owner the availability holds and the tablet overlay
    already use, so the desk cannot quote a different number than the counter
    refuses to sell past.
    """
    if not item or not company or not warehouse:
        return {"value": None, "reason": _("Choose an outlet to see what your stock can make")}
    try:
        from kopos_connector.kopos.services.inventory_autopilot.availability_capacity import (
            target_capacity,
        )

        result = target_capacity(
            target_type="Item", target_id=item, company=company, warehouse=warehouse
        )
    except Exception:
        frappe.log_error(title="JiJi menu could not read stock capacity")
        return {"value": None, "reason": _("JiJi could not read your stock just now")}
    if result.capacity is None or not result.reliable:
        return {
            "value": None,
            "reason": _human_menu_message(cstr(result.reason))
            if cstr(result.reason).strip()
            else _("Stock information is not complete enough"),
        }
    return {"value": int(result.capacity), "reason": ""}


@frappe.whitelist(methods=["POST"])
def create_menu_item_draft(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Create the standard records behind the first product-setup step.

    The business user supplies a name, menu group, price, and sale mode. JiJi
    owns the identifier, stock flags, internal counting unit, and price record.
    The product stays unavailable to staff until its setup is published.
    """

    _require_company_director("Product setup")
    value = _parse_payload(payload, "Product details")
    company = _require(value.get("company"), "Café")
    context = resolve_director_context(company=company)
    item_name = _require(value.get("item_name"), "Product name")
    item_group = _require(value.get("item_group"), "Menu group")
    role = _require(value.get("item_role"), "How this product is sold")
    if role not in ITEM_ROLES:
        frappe.throw(_("Choose how this item is made"), frappe.ValidationError)
    stock_uom = cstr(value.get("stock_uom")).strip() or _default_stock_uom(role)
    price_sen = (
        _non_negative_integer(value.get("price_sen"), "Selling price")
        if role in SALEABLE_ITEM_ROLES and value.get("price_sen") not in (None, "")
        else None
    )
    _require_existing_company(company)
    if role in SALEABLE_ITEM_ROLES:
        allowed_groups = {
            row["value"]
            for row in _public_menu_group_choices(
                _company_saleable_product_rows(context)
            )
        }
        if item_group not in allowed_groups:
            frappe.throw(_("Choose one of the menu groups shown"), frappe.ValidationError)
    elif not frappe.db.exists("Item Group", item_group):
        frappe.throw(_("Choose a group that exists"), frappe.ValidationError)
    if not frappe.db.exists("UOM", stock_uom):
        frappe.throw(_("Choose one of the measuring units shown"), frappe.ValidationError)
    meta = frappe.get_meta("Item")
    if not meta.has_field("custom_fb_item_role"):
        frappe.throw(_("Ask whoever set up JiJi to finish the stock setup first"), frappe.ValidationError)
    item_payload: dict[str, Any] = {
        "doctype": "Item",
        "item_code": _new_internal_item_code("PRODUCT", item_name),
        "item_name": item_name,
        "item_group": item_group,
        "stock_uom": stock_uom,
        **_item_role_flags(role),
        "disabled": 1,
        "custom_fb_item_role": role,
    }
    image = cstr(value.get("image")).strip()
    if image:
        item_payload["image"] = image
    item = frappe.get_doc(item_payload)
    item.insert(ignore_permissions=True)
    resolved_item = cstr(getattr(item, "name", "") or getattr(item, "item_code", ""))
    if role in SALEABLE_ITEM_ROLES and price_sen is not None:
        _create_selling_price(
            item=resolved_item,
            price_sen=price_sen,
            context=context,
            stock_uom=stock_uom,
        )
    return {
        "status": "ok",
        # The next guided step consumes this value without displaying it as a
        # code or asking the director to understand it.
        "item": resolved_item,
        "product_name": item_name,
        "disabled": True,
        "item_role": role,
        "next_step": (
            "Add what goes into one, then check and publish it."
            if role == "Sellable Drink"
            else "Check the product, then make it available to staff."
        ),
    }


def _company_saleable_product_rows(context: Any) -> list[dict[str, Any]]:
    """Bounded product rows used only to validate a guided group choice."""

    prices = frappe.get_all(
        "Item Price",
        filters={"price_list": ["in", list(context.allowed_price_lists) or [""]]},
        pluck="item_code",
        limit_page_length=501,
    )
    item_names = sorted(
        {cstr(value).strip() for value in prices if cstr(value).strip()}
    )
    if len(item_names) > 500:
        frappe.throw(_("This café has too many products to show safely"), frappe.ValidationError)
    if not item_names:
        return []
    return [
        dict(row)
        for row in frappe.get_all(
            "Item",
            filters={"name": ["in", item_names], "is_sales_item": 1},
            fields=["name", "item_name", "item_group"],
            order_by="item_name asc, name asc",
            limit_page_length=500,
        )
    ]


@frappe.whitelist(methods=["POST"])
def create_guided_stock_item(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Create an ingredient or supply without exposing the Item form."""

    _require_company_director("Ingredient setup")
    value = _parse_payload(payload, "Ingredient setup payload")
    company = _require(value.get("company"), "Café")
    context = resolve_director_context(company=company)
    if not context.warehouse:
        frappe.throw(_("Finish the outlet setup before adding stock"), frappe.ValidationError)
    name = _require(value.get("name"), "Name")
    role = _require(value.get("kind"), "Type")
    if role not in GUIDED_STOCK_ROLES:
        frappe.throw(_("Choose Ingredient or Supply"), frappe.ValidationError)
    unit = _require(value.get("unit"), "How it is counted")
    allowed_units = {row["value"] for row in _friendly_uom_choices()}
    if unit not in allowed_units:
        frappe.throw(_("Choose one of the café counting units"), frappe.ValidationError)
    meta = frappe.get_meta("Item")
    if not meta.has_field("custom_fb_item_role"):
        frappe.throw(_("Ask support to finish the stock setup first"), frappe.ValidationError)
    item = frappe.get_doc(
        {
            "doctype": "Item",
            "item_code": _new_internal_item_code("STOCK", name),
            "item_name": name,
            "item_group": _default_item_group(role),
            "stock_uom": unit,
            "is_sales_item": 0,
            "is_purchase_item": 1,
            "is_stock_item": 1,
            "disabled": 0,
            "custom_fb_item_role": role,
            "item_defaults": [
                {
                    "company": context.company,
                    "default_warehouse": context.warehouse,
                }
            ],
        }
    )
    item.insert(ignore_permissions=True)
    result = _public_item_detail(
        {
            "name": cstr(getattr(item, "name", "") or getattr(item, "item_code", "")),
            "item_name": name,
            "stock_uom": unit,
            "is_stock_item": 1,
            "is_sales_item": 0,
            "item_role": role,
        }
    )
    return {"status": "ok", "item": result}


def _new_internal_item_code(kind: str, name: str) -> str:
    readable = "-".join(
        part for part in "".join(
            character.upper() if character.isalnum() else " " for character in name
        ).split() if part
    )[:72]
    return "JIJI-{0}-{1}-{2}".format(
        kind,
        readable or "ITEM",
        frappe.generate_hash(length=8).upper(),
    )


def _default_item_group(role: str) -> str:
    candidates = (
        ("Raw Material", "Ingredients", "Products")
        if role == "Ingredient"
        else ("Consumable", "Packaging", "Products")
    )
    for candidate in candidates:
        if frappe.db.exists("Item Group", candidate):
            return candidate
    rows = frappe.get_all(
        "Item Group",
        filters={"is_group": 0},
        fields=["name"],
        order_by="lft asc, name asc",
        limit_page_length=1,
    )
    if not rows:
        frappe.throw(_("Ask support to add a product group first"), frappe.ValidationError)
    return cstr(rows[0].get("name")).strip()


@frappe.whitelist(methods=["POST"])
def create_guided_customer_choice(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Create one product-specific choice group and attach it to a Draft."""

    _require_company_director("Customer choice setup")
    value = _parse_payload(payload, "Customer choice payload")
    recipe = _load_recipe(value.get("recipe"))
    if cstr(getattr(recipe, "status", "")).strip() != "Draft":
        frappe.throw(_("Start a product change before adding customer choices"), frappe.ValidationError)
    company = _require(value.get("company"), "Café")
    if cstr(getattr(recipe, "company", "")).strip() != company:
        frappe.throw(_("This product belongs to a different café"), frappe.PermissionError)
    context = resolve_director_context(company=company)
    _require_item_in_company(cstr(getattr(recipe, "sellable_item", "")).strip(), context)
    group_name = _require(value.get("name"), "Customer choice name")
    mode = cstr(value.get("selection_mode")).strip()
    if mode not in {"one", "many"}:
        frappe.throw(_("Choose whether customers pick one or any"), frappe.ValidationError)
    required = 1 if cint(value.get("required")) else 0
    raw_options = value.get("options")
    if not isinstance(raw_options, list) or not raw_options or len(raw_options) > 20:
        frappe.throw(_("Add between 1 and 20 choices"), frappe.ValidationError)
    names: set[str] = set()
    default_count = 0
    for raw in raw_options:
        if not isinstance(raw, Mapping):
            frappe.throw(_("One customer choice is not readable"), frappe.ValidationError)
        option_name = _require(raw.get("name"), "Choice name")
        normalized_name = option_name.casefold()
        if normalized_name in names:
            frappe.throw(_("Give every customer choice a different name"), frappe.ValidationError)
        names.add(normalized_name)
        default_count += 1 if cint(raw.get("is_default")) else 0
    if default_count > 1:
        frappe.throw(_("Choose only one usual choice"), frappe.ValidationError)

    component_items = {
        cstr(getattr(row, "item", "")).strip()
        for row in getattr(recipe, "components", None) or []
        if cstr(getattr(row, "item", "")).strip()
    }
    allowed_items = _allowed_component_item_names(
        company=context.company,
        allowed_warehouses=context.allowed_warehouses,
    ) | component_items
    referenced_items = set(component_items)
    for raw in raw_options:
        if isinstance(raw, Mapping):
            referenced_items.update(
                cstr(raw.get(fieldname)).strip()
                for fieldname in ("ingredient", "replacement")
                if cstr(raw.get(fieldname)).strip()
            )
    item_details = _load_item_details(
        referenced_items,
        warehouse=context.warehouse or None,
    )
    group_code = f"JIJI-G-{frappe.generate_hash(length=12).upper()}"
    savepoint = "kopos_guided_customer_choice"
    frappe.db.savepoint(savepoint)
    try:
        group = frappe.get_doc(
            {
                "doctype": "FB Modifier Group",
                "group_code": group_code,
                "group_name": group_name,
                "selection_type": "Single" if mode == "one" else "Multiple",
                "is_required": required,
                "min_selection": 1 if required else 0,
                "max_selection": 1 if mode == "one" else len(raw_options),
                "active": 1,
                "default_resolution_policy": (
                    "Auto Apply Default" if default_count else "Require Explicit Selection"
                ),
            }
        )
        group.insert(ignore_permissions=True)
        resolved_group = cstr(getattr(group, "name", "") or group_code).strip()
        default_modifier = ""
        for index, raw in enumerate(raw_options, start=1):
            effect = _guided_choice_effect(
                raw,
                recipe=recipe,
                allowed_items=allowed_items,
                item_details=item_details,
            )
            option = frappe.get_doc(
                {
                    "doctype": "FB Modifier",
                    "modifier_code": f"JIJI-C-{frappe.generate_hash(length=12).upper()}",
                    "modifier_name": _require(raw.get("name"), "Choice name"),
                    "modifier_group": resolved_group,
                    "price_adjustment": format(
                        Decimal(_non_negative_integer(raw.get("price_sen", 0), "Extra price"))
                        / Decimal("100"),
                        ".2f",
                    ),
                    "is_default": 1 if cint(raw.get("is_default")) else 0,
                    "display_order": index,
                    "active": 1,
                    **effect,
                }
            )
            option.insert(ignore_permissions=True)
            if cint(raw.get("is_default")):
                default_modifier = cstr(
                    getattr(option, "name", "") or getattr(option, "modifier_code", "")
                ).strip()
        recipe.append(
            "allowed_modifier_groups",
            {
                "modifier_group": resolved_group,
                "required": required,
                "override_min_selection": 1 if required else 0,
                "override_max_selection": 1 if mode == "one" else len(raw_options),
                "default_modifier": default_modifier or None,
                "always_prompt": 0 if default_modifier else 1,
            },
        )
        recipe.save(ignore_permissions=True)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise
    return {
        "status": "ok",
        "choice": {
            "name": resolved_group,
            "group_name": group_name,
            "selection_type": "Single" if mode == "one" else "Multiple",
        },
        "recipe": cstr(getattr(recipe, "name", "")),
    }


def _default_stock_uom(role: str) -> str:
    del role
    # Drinks and services are not physical stock. Ready-made products are
    # counted as whole products. Business users therefore never need to choose
    # the ERP's whole-product unit here.
    return "Nos"


def _create_selling_price(
    *, item: str, price_sen: int, context: Any, stock_uom: str
) -> None:
    price_lists = tuple(getattr(context, "allowed_price_lists", ()) or ())
    if len(price_lists) != 1:
        frappe.throw(
            _("Choose one outlet with one selling price before adding a product"),
            frappe.ValidationError,
        )
    price = frappe.get_doc(
        {
            "doctype": "Item Price",
            "item_code": item,
            "price_list": price_lists[0],
            "uom": stock_uom,
            "price_list_rate": format(Decimal(price_sen) / Decimal("100"), ".2f"),
        }
    )
    price.insert(ignore_permissions=True)


def _item_role_flags(role: str) -> dict[str, int]:
    """Standard Item flags that make each plain-language role true."""
    if role not in ITEM_ROLES:
        raise ValueError("unsupported menu item role")
    return {
        "is_sales_item": 1 if role in SALEABLE_ITEM_ROLES else 0,
        "is_purchase_item": 1 if role in PURCHASE_ITEM_ROLES else 0,
        "is_stock_item": 1 if role in STOCK_ITEM_ROLES else 0,
    }


@frappe.whitelist(methods=["POST"])
def set_menu_item_role(
    *,
    company: str,
    item: str,
    item_role: str,
) -> dict[str, Any]:
    """Classify one existing saleable item without opening the ERP Item form."""
    _require_company_director("Menu Item setup")
    selected_company = _require(company, "Café")
    item_name = _require(item, "Product")
    role = _require(item_role, "How this product is sold")
    if role not in SALEABLE_ITEM_ROLES:
        frappe.throw(_("Choose how this menu item is sold"), frappe.ValidationError)

    context = resolve_director_context(company=selected_company)
    price_lists = list(context.allowed_price_lists)
    if not price_lists or not frappe.db.exists(
        "Item Price",
        {"item_code": item_name, "price_list": ["in", price_lists]},
    ):
        frappe.throw(_("This menu item does not belong to your café"), frappe.PermissionError)

    item_doc = frappe.get_doc("Item", item_name)
    if cint(getattr(item_doc, "disabled", 0)) or not cint(
        getattr(item_doc, "is_sales_item", 0)
    ):
        frappe.throw(_("Choose an active item that staff can sell"), frappe.ValidationError)
    expected_stock = _item_role_flags(role)["is_stock_item"]
    actual_stock = cint(getattr(item_doc, "is_stock_item", 0))
    if expected_stock != actual_stock:
        message = (
            _("This item is not tracked in stock yet. Ask the person who set up JiJi to finish its stock details first.")
            if expected_stock
            else _("This item is tracked in stock. Choose Packaged item, or ask the person who set up JiJi to change its stock details.")
        )
        frappe.throw(message, frappe.ValidationError)
    if not frappe.get_meta("Item").has_field("custom_fb_item_role"):
        frappe.throw(_("Ask whoever set up JiJi to finish the stock setup first"), frappe.ValidationError)

    item_doc.custom_fb_item_role = role
    item_doc.save()
    return {
        "status": "ok",
        "item": cstr(getattr(item_doc, "name", item_name)),
        "item_role": role,
    }


@frappe.whitelist(methods=["POST"])
def publish_product_without_recipe(*, company: str, item: str) -> dict[str, Any]:
    """Make a ready-made product or non-stock service available to staff."""

    _require_company_director("Product publishing")
    context = resolve_director_context(company=_require(company, "Café"))
    item_name = _require(item, "Product")
    _require_item_in_company(item_name, context)
    document = frappe.get_doc("Item", item_name)
    role = cstr(getattr(document, "custom_fb_item_role", None)).strip()
    if role not in {"Packaged Finished Good", "Non-Stock Service"}:
        frappe.throw(
            _("Add the ingredients before publishing this product"),
            frappe.ValidationError,
        )
    document.disabled = 0
    document.save(ignore_permissions=True)
    return {
        "status": "ok",
        "item": item_name,
        "message": _("Product published"),
    }


@frappe.whitelist(methods=["POST"])
def preview_menu_recipe(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Return a server-calculated quantity and director-only cost preview."""

    _require_company_director("Product check")
    value = _guided_recipe_values(_parse_payload(payload, "Product details"))
    _validate_recipe_identity(value)
    components = _normalized_components(value.get("components"))
    source_recipe = None
    if cint(value.get("human_guided")):
        source_recipe = _validate_guided_component_scope(value, components)
    item_codes = {cstr(value.get("sellable_item")).strip()}
    item_codes.update(cstr(row.get("item")).strip() for row in components)
    item_details = _load_item_details(
        {item for item in item_codes if item},
        warehouse=cstr(value.get("warehouse")).strip() or None,
    )
    if cint(value.get("human_guided")):
        components = _guided_components(
            components,
            item_details,
            source_recipe=source_recipe,
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
    sellable = cstr(value.get("sellable_item")).strip()
    return {
        "status": "ok" if preview["valid"] else "blocked",
        "preview": _public_preview(
            preview,
            price_sen=_selling_price_sen(sellable, cstr(value.get("company")).strip(), item_details),
        ),
        "modifier_effects": _public_modifier_effects(effects),
        "prepared_boms": prepared,
    }


@frappe.whitelist(methods=["POST"])
def save_menu_recipe_draft(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Save the guided rows into one standard ``FB Recipe`` Draft."""

    _require_company_director("Product setup")
    value = _guided_recipe_values(_parse_payload(payload, "Product details"))
    _validate_recipe_identity(value)
    components = _normalized_components(value.get("components"))
    source_recipe = None
    if cint(value.get("human_guided")):
        source_recipe = _validate_guided_component_scope(value, components)
    groups = _normalized_modifier_groups(value.get("modifier_groups"))
    item_codes = {cstr(value.get("sellable_item")).strip()}
    item_codes.update(cstr(row.get("item")).strip() for row in components)
    item_details = _load_item_details(
        {item for item in item_codes if item},
        warehouse=cstr(value.get("warehouse")).strip() or None,
    )
    if cint(value.get("human_guided")):
        components = _guided_components(
            components,
            item_details,
            source_recipe=source_recipe,
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
            _("This product still needs attention: {0}").format(
                "; ".join(
                    _human_menu_message(error["message"])
                    for error in preview["errors"][:8]
                )
            ),
            frappe.ValidationError,
        )

    recipe_doc = _load_recipe(value.get("recipe")) if cstr(value.get("recipe")).strip() else None
    if recipe_doc and cstr(getattr(recipe_doc, "status", "")).strip() != "Draft":
        frappe.throw(_("Start a product change before editing what staff already use"), frappe.ValidationError)
    if recipe_doc:
        if cstr(getattr(recipe_doc, "company", "")).strip() != cstr(value.get("company")).strip() or cstr(getattr(recipe_doc, "sellable_item", "")).strip() != cstr(value.get("sellable_item")).strip():
            frappe.throw(_("This saved change belongs to a different product"), frappe.ValidationError)
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
        "next_step": "Check the amounts, then publish this product when it is ready.",
    }


@frappe.whitelist(methods=["POST"])
def copy_menu_recipe_revision(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Copy an immutable recipe into the next standard Draft version."""

    _require_company_director("Recipe revision")
    value = _parse_payload(payload, "Recipe revision payload")
    source_name = cstr(value.get("recipe")).strip()
    if not source_name:
        frappe.throw(_("Choose a product to change"), frappe.ValidationError)
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
        frappe.throw(_("JiJi could not start this change. Try again."), frappe.ValidationError)
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
                "notes": "Product change started from the currently published version.",
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
        frappe.throw(_("Choose a café before looking at saved recipes"), frappe.ValidationError)
    resolve_director_context(company=selected_company)
    rows = frappe.get_all(
        "FB Recipe",
        filters={"company": selected_company, "status": "Draft"},
        fields=["name", "recipe_code", "recipe_name", "sellable_item", "version_no", "modified"],
        order_by="modified desc",
        limit_page_length=100,
    )
    item_names = {
        cstr(row.get("sellable_item")).strip()
        for row in rows
        if cstr(row.get("sellable_item")).strip()
    }
    product_names = {
        cstr(row.get("name")).strip(): cstr(row.get("item_name")).strip()
        for row in (
            frappe.get_all(
                "Item",
                filters={"name": ["in", sorted(item_names)]},
                fields=["name", "item_name"],
                limit_page_length=100,
            )
            if item_names
            else []
        )
    }
    queue: list[dict[str, Any]] = []
    for row in rows:
        doc = frappe.get_doc("FB Recipe", row["name"])
        preview = _preview_for_doc(doc)
        queue.append(
            {
                **{key: row.get(key) for key in ("name", "recipe_code", "recipe_name", "sellable_item", "version_no", "modified")},
                "valid": bool(preview["valid"]),
                "errors": [
                    _human_menu_message(error["message"])
                    for error in preview["errors"][:3]
                ],
                "component_count": len(getattr(doc, "components", None) or []),
                "product_name": product_names.get(
                    cstr(row.get("sellable_item")).strip(), ""
                )
                or cstr(row.get("recipe_name")).strip()
                or _("Product"),
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
            _("Nothing was published. Fix every selected product first: {0}").format(
                "; ".join(
                    _human_menu_message(row["message"])
                    for row in failures[:6]
                )
            ),
            frappe.ValidationError,
        )

    savepoint = "kopos_menu_recipe_publish"
    frappe.db.savepoint(savepoint)
    try:
        for document in prepared:
            document.save(ignore_permissions=True)
        for item in {
            cstr(getattr(document, "sellable_item", "")).strip()
            for document in prepared
        }:
            if item and cint(frappe.db.get_value("Item", item, "disabled")):
                frappe.db.set_value("Item", item, "disabled", 0, update_modified=True)
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
        # Immediate publication is the only date choice on the beginner page.
        # Scheduled cutovers remain a support operation.
        document.effective_from = now_datetime()
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
        unit_name = yield_uom
        preview["errors"].append({"field": "yield_uom", "message": f"JiJi cannot use {unit_name} for this product. Choose another unit."})
        preview["valid"] = False
    if serving_factor is None and serving_uom:
        unit_name = serving_uom
        preview["errors"].append({"field": "default_serving_uom", "message": f"JiJi cannot use {unit_name} for one serving. Choose another unit."})
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
                frappe.throw(_("Only recipes can be put on sale here"), frappe.ValidationError)
            name = cstr(row.get("name")).strip()
        else:
            name = ""
        if not name or len(name) > 140 or name in names:
            frappe.throw(_("Recipe selection contains a missing or duplicate name"), frappe.ValidationError)
        names.append(name)
    return names


def _guided_recipe_values(value: Mapping[str, Any]) -> dict[str, Any]:
    """Fill technical one-serving defaults for the human product editor."""

    result = dict(value)
    if not cint(result.get("human_guided")):
        return result
    item = _require(result.get("sellable_item"), "Product")
    details = frappe.db.get_value(
        "Item", item, ["item_name", "stock_uom"], as_dict=True
    )
    if not details:
        frappe.throw(_("Choose a product that exists"), frappe.ValidationError)
    internal_unit = cstr(details.get("stock_uom")).strip() or "Nos"
    result.update(
        {
            "recipe_name": cstr(details.get("item_name")).strip() or item,
            "recipe_code": "",
            "recipe_type": "Finished Drink",
            "yield_qty": "1",
            "yield_uom": internal_unit,
            "default_serving_qty": "1",
            "default_serving_uom": internal_unit,
            "effective_from": None,
            "effective_to": None,
        }
    )
    # The outlet is resolved from director scope. A browser may not substitute
    # another stock location into the cost preview.
    result["warehouse"] = resolve_director_context(
        company=cstr(result.get("company")).strip()
    ).warehouse
    return result


def _guided_components(
    rows: list[dict[str, Any]],
    item_details: Mapping[str, Mapping[str, Any]],
    *,
    source_recipe: Any | None = None,
) -> list[dict[str, Any]]:
    """Derive new rows while preserving hidden behavior on unchanged ingredients."""

    normalized: list[dict[str, Any]] = []
    remaining_source = _recipe_components(source_recipe)
    for row in rows:
        item = cstr(row.get("item")).strip()
        detail = item_details.get(item) or {}
        if not cint(detail.get("is_stock_item")):
            frappe.throw(
                _("Choose an ingredient, prepared item, or supply for every row"),
                frappe.ValidationError,
            )
        role = cstr(detail.get("item_role")).strip()
        component_type = {
            "Prep Item": "Prep Item",
            "Packaging": "Packaging",
        }.get(role, "Ingredient")
        source_index = next(
            (
                index
                for index, candidate in enumerate(remaining_source)
                if cstr(candidate.get("item")).strip() == item
                and cstr(candidate.get("uom")).strip() == cstr(row.get("uom")).strip()
            ),
            None,
        )
        if source_index is None:
            source_index = next(
                (
                    index
                    for index, candidate in enumerate(remaining_source)
                    if cstr(candidate.get("item")).strip() == item
                ),
                None,
            )
        source = remaining_source.pop(source_index) if source_index is not None else {}
        hidden = {
            field: source[field]
            for field in RECIPE_COMPONENT_FIELDS
            if field not in {"item", "qty", "uom"} and field in source
        }
        normalized.append(
            {
                **row,
                "component_type": hidden.pop("component_type", component_type),
                "affects_stock": hidden.pop("affects_stock", 1),
                "affects_cogs": hidden.pop("affects_cogs", 1),
                "loss_factor_pct": hidden.pop("loss_factor_pct", "0"),
                **hidden,
            }
        )
    return normalized


def _guided_choice_effect(
    raw: Mapping[str, Any],
    *,
    recipe: Any,
    allowed_items: set[str],
    item_details: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Translate one plain-language stock change into the existing effect."""

    change = cstr(raw.get("change_type") or "instructions_only").strip()
    if change == "instructions_only":
        return {
            "kind": "Instruction Only",
            "instruction_text": _require(raw.get("name"), "Choice name"),
            "affects_stock": 0,
            "affects_recipe": 0,
        }

    def scoped_item(value: Any, label: str) -> str:
        item = _require(value, label)
        detail = item_details.get(item) or {}
        if item not in allowed_items or not cint(detail.get("is_stock_item")):
            frappe.throw(
                _("Choose {0} from this café's ingredients and supplies").format(label.lower()),
                frappe.ValidationError,
            )
        return item

    components = list(getattr(recipe, "components", None) or [])

    def matching_component(item: str) -> Any:
        matches = [
            row
            for row in components
            if cstr(_row_value(row, "item")).strip() == item
        ]
        if len(matches) != 1:
            frappe.throw(
                _("Choose an ingredient that appears once in this product"),
                frappe.ValidationError,
            )
        return matches[0]

    if change == "add":
        item = scoped_item(raw.get("ingredient"), "Ingredient to add")
        amount = _positive(raw.get("amount"), "Amount to add")
        unit = _require(raw.get("unit"), "How the added amount is measured")
        if _item_uom_factor(item_details.get(item) or {}, unit) is None:
            frappe.throw(
                _("That ingredient cannot be measured in the chosen unit"),
                frappe.ValidationError,
            )
        return {
            "kind": "Add",
            "new_item": item,
            "qty_delta": amount,
            "qty_uom": unit,
            "affects_stock": 1,
            "affects_recipe": 1,
        }

    target = scoped_item(raw.get("ingredient"), "Ingredient to change")
    matching = matching_component(target)
    if change == "remove":
        return {
            "kind": "Remove",
            "target_item": target,
            "affects_stock": 1,
            "affects_recipe": 1,
        }
    if change == "replace":
        replacement = scoped_item(raw.get("replacement"), "Ingredient to use instead")
        if cstr((item_details.get(target) or {}).get("stock_uom")).strip() != cstr(
            (item_details.get(replacement) or {}).get("stock_uom")
        ).strip():
            frappe.throw(
                _("Those two ingredients are counted differently. Add one choice to leave the first out and another to add the replacement."),
                frappe.ValidationError,
            )
        return {
            "kind": "Replace",
            "target_item": target,
            "new_item": replacement,
            "affects_stock": 1,
            "affects_recipe": 1,
        }
    if change == "change_amount":
        amount = Decimal(_positive(raw.get("amount"), "New amount"))
        unit = _require(raw.get("unit"), "How the new amount is measured")
        factor = _item_uom_factor(item_details.get(target) or {}, unit)
        if factor is None:
            frappe.throw(
                _("That ingredient cannot be measured in the chosen unit"),
                frappe.ValidationError,
            )
        raw_base = _row_value(matching, "stock_qty_decimal") or _row_value(
            matching, "stock_qty"
        )
        try:
            base = Decimal(cstr(raw_base).strip())
        except (InvalidOperation, TypeError, ValueError) as error:
            frappe.throw(
                _("Save the product's ingredient amounts before changing this amount"),
                frappe.ValidationError,
            )
            raise AssertionError("frappe.throw must raise") from error
        if not base.is_finite() or base <= 0:
            frappe.throw(
                _("Save the product's ingredient amounts before changing this amount"),
                frappe.ValidationError,
            )
        percent = (amount * factor / base) * Decimal("100")
        return {
            "kind": "Scale",
            "target_item": target,
            "scale_percent": format(percent.normalize(), "f"),
            "affects_stock": 1,
            "affects_recipe": 1,
        }
    frappe.throw(_("Choose what this customer choice changes"), frappe.ValidationError)
    raise AssertionError("frappe.throw must raise")


def _row_value(row: Any, fieldname: str) -> Any:
    return row.get(fieldname) if isinstance(row, Mapping) else getattr(row, fieldname, None)


def _normalized_components(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > MAX_GUIDED_COMPONENTS:
        frappe.throw(_("A product can use at most {0} ingredients and supplies").format(MAX_GUIDED_COMPONENTS), frappe.ValidationError)
    rows: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, Mapping):
            frappe.throw(_("One of the ingredient rows is not readable"), frappe.ValidationError)
        component_type = cstr(row.get("component_type") or "Ingredient").strip()
        if component_type not in COMPONENT_TYPES:
            frappe.throw(_("Choose a valid component type"), frappe.ValidationError)
        normalized = {field: row.get(field) for field in RECIPE_COMPONENT_FIELDS if field in row}
        normalized["item"] = _require(row.get("item"), "Ingredient or supply")
        normalized["component_type"] = component_type
        normalized["qty"] = _positive(row.get("qty"), "Amount")
        normalized["uom"] = _require(row.get("uom"), "Measuring unit")
        normalized.setdefault("affects_stock", 0 if component_type == "Tool Usage" else 1)
        normalized.setdefault("affects_cogs", 0 if component_type == "Tool Usage" else 1)
        normalized["loss_factor_pct"] = _non_negative(row.get("loss_factor_pct", 0), "Loss factor")
        rows.append(normalized)
    return rows


def _normalized_modifier_groups(raw: Any) -> list[dict[str, Any]]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_GUIDED_MODIFIER_GROUPS:
        frappe.throw(_("A recipe can hold at most {0} customer choices").format(MAX_GUIDED_MODIFIER_GROUPS), frappe.ValidationError)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in raw:
        if isinstance(row, str):
            row = {"modifier_group": row}
        if not isinstance(row, Mapping):
            frappe.throw(_("One of the customer choice rows is not readable"), frappe.ValidationError)
        group = _require(row.get("modifier_group"), "Customer choice")
        if group in seen:
            frappe.throw(_("The same customer choice is listed twice"), frappe.ValidationError)
        seen.add(group)
        result.append({field: row.get(field) for field in ALLOWED_MODIFIER_FIELDS if field in row} | {"modifier_group": group})
    return result


def _validate_recipe_identity(value: Mapping[str, Any]) -> None:
    company = _require(value.get("company"), "Café")
    resolve_director_context(
        company=company,
        warehouse=cstr(value.get("warehouse")).strip() or None,
    )
    item = _require(value.get("sellable_item"), "Product")
    _require_existing_company(cstr(value.get("company")).strip())
    item_row = frappe.db.get_value("Item", item, ["is_sales_item", "disabled"], as_dict=True)
    if not item_row or not cint(item_row.get("is_sales_item")):
        frappe.throw(_("Choose something that is for sale"), frappe.ValidationError)
    _require_item_in_company(item, resolve_director_context(company=company))
    if cstr(value.get("recipe_type") or "Finished Drink") not in RECIPE_TYPES:
        frappe.throw(_("JiJi could not read how this product is made. Refresh and try again."), frappe.ValidationError)


def _require_item_in_company(item: str, context: Any) -> None:
    """A global ERP Item belongs here only through this café's price list."""

    price_lists = tuple(getattr(context, "allowed_price_lists", ()) or ())
    if not price_lists or not frappe.db.exists(
        "Item Price",
        {"item_code": item, "price_list": ["in", list(price_lists)]},
    ):
        frappe.throw(_("This product does not belong to your café"), frappe.PermissionError)


def _load_recipe(name: Any) -> Any:
    recipe_name = _require(name, "Saved product change")
    if not frappe.db.exists("FB Recipe", recipe_name):
        frappe.throw(_("This saved product change was not found. Refresh and try again."), frappe.DoesNotExistError)
    recipe = frappe.get_doc("FB Recipe", recipe_name)
    company = cstr(getattr(recipe, "company", None)).strip()
    resolve_director_context(company=company)
    return recipe


def _load_item_details(items: set[str], *, warehouse: str | None) -> dict[str, dict[str, Any]]:
    maximum = MAX_GUIDED_COMPONENTS + MAX_GUIDED_MODIFIER_EFFECTS + 1
    if len(items) > maximum:
        frappe.throw(_("This product has too many ingredients and choices to open safely"), frappe.ValidationError)
    details: dict[str, dict[str, Any]] = {}
    item_meta = frappe.get_meta("Item")
    item_fields = ["name", "item_name", "stock_uom", "is_stock_item", "is_sales_item", "disabled"]
    if item_meta.has_field("custom_fb_item_role"):
        item_fields.append("custom_fb_item_role")
    if item_meta.has_field("valuation_rate"):
        item_fields.append("valuation_rate")
    for row in frappe.get_all(
        "Item",
        filters={"name": ["in", sorted(items)]},
        fields=item_fields,
        limit_page_length=maximum,
    ):
        details[cstr(row.get("name")).strip()] = {
            **dict(row),
            "item_role": cstr(row.get("custom_fb_item_role")).strip(),
            "conversion_factors": _conversion_factors(cstr(row.get("name")).strip()),
            "valuation_rate": _valuation_rate(cstr(row.get("name")).strip(), warehouse),
        }
    return details


def _load_item_choices(
    existing: Mapping[str, Mapping[str, Any]],
    *,
    company: str,
    allowed_warehouses: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """Return a bounded Item lookup for the editor datalist.

    The editor still resolves and validates the selected rows server-side.  A
    small lookup makes entry easier without placing the entire Item master or
    any valuation data in the browser.
    """

    result = dict(existing)
    candidate_items = sorted(
        _allowed_component_item_names(
            company=company,
            allowed_warehouses=allowed_warehouses,
        )
    )
    if not candidate_items:
        return result
    item_meta = frappe.get_meta("Item")
    fields = ["name", "item_name", "stock_uom", "is_stock_item", "is_sales_item", "disabled"]
    if item_meta.has_field("custom_fb_item_role"):
        fields.append("custom_fb_item_role")
    rows = frappe.get_all(
        "Item",
        filters={
            "name": ["in", candidate_items],
            "disabled": 0,
            "is_stock_item": 1,
        },
        fields=fields,
        order_by="item_name asc, name asc",
        limit_page_length=500,
    )
    for row in rows:
        name = cstr(row.get("name")).strip()
        if name and name not in result:
            result[name] = {
                **dict(row),
                "item_role": cstr(row.get("custom_fb_item_role")).strip(),
            }
    return result


def _allowed_component_item_names(
    *, company: str, allowed_warehouses: tuple[str, ...]
) -> set[str]:
    """Stock Items tied to this café by stock or a saved outlet default."""

    if not allowed_warehouses:
        return set()
    candidates = {
        cstr(value).strip()
        for value in frappe.get_all(
            "Bin",
            filters={"warehouse": ["in", list(allowed_warehouses)]},
            pluck="item_code",
            limit_page_length=501,
        )
        if cstr(value).strip()
    }
    if frappe.db.exists("DocType", "Item Default"):
        candidates.update(
            cstr(value).strip()
            for value in frappe.get_all(
                "Item Default",
                filters={
                    "company": company,
                    "default_warehouse": ["in", list(allowed_warehouses)],
                },
                pluck="parent",
                limit_page_length=501,
            )
            if cstr(value).strip()
        )
    if len(candidates) > 500:
        frappe.throw(
            _("This café has too many stock products to show safely"),
            frappe.ValidationError,
        )
    return candidates


def _validate_guided_component_scope(
    value: Mapping[str, Any], components: list[Mapping[str, Any]]
) -> Any | None:
    context = resolve_director_context(company=cstr(value.get("company")).strip())
    allowed = _allowed_component_item_names(
        company=context.company,
        allowed_warehouses=context.allowed_warehouses,
    )
    recipe_name = cstr(value.get("recipe")).strip()
    current = None
    if recipe_name:
        current = _load_recipe(recipe_name)
        if (
            cstr(getattr(current, "company", None)).strip() != context.company
            or cstr(getattr(current, "sellable_item", None)).strip()
            != cstr(value.get("sellable_item")).strip()
        ):
            frappe.throw(
                _("This saved change belongs to a different product"),
                frappe.PermissionError,
            )
        allowed.update(
            cstr(_row_value(row, "item")).strip()
            for row in getattr(current, "components", None) or []
            if cstr(_row_value(row, "item")).strip()
        )
    outside = [
        cstr(row.get("item")).strip()
        for row in components
        if cstr(row.get("item")).strip() not in allowed
    ]
    if outside:
        frappe.throw(
            _("Choose ingredients and supplies from this café only"),
            frappe.PermissionError,
        )
    return current


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


def _modifier_group_choices(group_names: set[str]) -> list[dict[str, Any]]:
    if not group_names:
        return []
    rows = frappe.get_all(
        "FB Modifier Group",
        filters={"active": 1, "name": ["in", sorted(group_names)]},
        fields=["name", "group_code", "group_name", "selection_type", "is_required", "min_selection", "max_selection"],
        order_by="group_name asc, name asc",
        limit_page_length=MAX_GUIDED_MODIFIER_GROUPS,
    )
    return [
        {
            **dict(row),
            "group_name": cstr(row.get("group_name")).strip()
            or _("Customer choice"),
        }
        for row in rows
    ]


def _uom_choices() -> list[str]:
    return [cstr(row.get("name")).strip() for row in frappe.get_all("UOM", filters={"enabled": 1}, fields=["name"], order_by="name asc", limit_page_length=200) if cstr(row.get("name")).strip()]


def _friendly_uom_choices() -> list[dict[str, str]]:
    """Internal unit values paired with words shown in the guided editor."""

    choices: list[dict[str, str]] = []
    for name in _uom_choices():
        if name not in GUIDED_CAFE_UNITS:
            continue
        singular, plural = unit_words(name)
        label = singular if singular == plural else f"{singular} / {plural}"
        choices.append({"value": name, "label": label or name.lower()})
    return choices


def _public_item_choices(details: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        _public_item_detail(details[name])
        for name in sorted(details)
        if cint(details[name].get("is_stock_item"))
    ]


def _public_item_detail(detail: Mapping[str, Any]) -> dict[str, Any]:
    safe_name = cstr(detail.get("item_name")).strip()
    if not safe_name:
        safe_name = (
            _("Product")
            if cint(detail.get("is_sales_item"))
            else _("Ingredient or supply")
        )
    return {
        "name": cstr(detail.get("name")),
        "item_name": safe_name,
        "stock_uom": cstr(detail.get("stock_uom")),
        "is_stock_item": cint(detail.get("is_stock_item")),
        "is_sales_item": cint(detail.get("is_sales_item")),
        "item_role": cstr(detail.get("item_role")),
    }


def _public_modifier_effects(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows[:MAX_GUIDED_MODIFIER_EFFECTS]:
        public = {
            key: row.get(key)
            for key in ("name", "modifier_group", "kind", "target_item", "new_item", "qty_delta", "qty_uom", "scale_percent", "affects_stock", "affects_recipe")
        }
        public["modifier_name"] = (
            cstr(row.get("modifier_name")).strip() or _("Customer choice")
        )
        result.append(public)
    return result


def _selling_price_sen(
    item: str, company: str, item_details: Mapping[str, Mapping[str, Any]]
) -> int | None:
    """What one of these sells for, from the list the tablet charges from.

    A recipe cost on its own answers "is this valid". Next to the price it
    answers the question a director actually has, which is whether the drink
    still makes money. Quoting a second price here would let the desk and the
    counter disagree, so this reads the catalog owner.
    """
    if not item or not company:
        return None
    price_list = director_price_list(company)
    if not price_list:
        return None
    stock_uom = cstr((item_details.get(item) or {}).get("stock_uom")).strip()
    prices = get_item_prices_map([item], price_list, {item: stock_uom} if stock_uom else None)
    return _money_to_sen(prices.get(item))


def _human_menu_message(value: Any) -> str:
    """Fail closed when an internal validation detail reaches a beginner page."""

    message = cstr(value).strip()
    lowered = message.casefold()
    if not message:
        return _("This product needs one more detail before it can be published.")
    if "stock tracking has not started" in lowered:
        return _("Start stock tracking to see what today's stock can make.")
    if "at least one measured component" in lowered or "no component rows" in lowered:
        return _("Add at least one ingredient or supply.")
    if "at most" in lowered and ("component" in lowered or "recipe" in lowered):
        return _("This product has too many ingredient rows. Remove the extras.")
    if "modifier" in lowered:
        return _("One customer choice needs attention. Open it and check each option.")
    if "prepared" in lowered and "bom" in lowered:
        return _("Save how this prepared ingredient is made before publishing the product.")
    if "valuation" in lowered or "current cost" in lowered:
        return _("Add a current cost for each ingredient to see what the product costs.")
    if "conversion" in lowered or "uom" in lowered or "measuring unit" in lowered:
        return _("Choose a measuring unit that matches how the ingredient is counted.")
    if "quantity" in lowered or ".qty" in lowered or "amount" in lowered:
        return _("Enter an amount greater than zero for every ingredient.")
    if "item" in lowered and ("not found" in lowered or "required" in lowered):
        return _("Choose an ingredient or supply for every row.")
    if "draft" in lowered or "canonical" in lowered or "hash" in lowered:
        return _("Start a new product change, then try again.")
    if "stock" in lowered and ("missing" in lowered or "unavailable" in lowered):
        return _("Some stock information is incomplete. Check this product's ingredients.")
    return _("This product needs one more detail before it can be published.")


def _public_preview(preview: Mapping[str, Any], *, price_sen: int | None = None) -> dict[str, Any]:
    result = json.loads(json.dumps(preview, default=str))
    for key in ("errors", "warnings"):
        result[key] = [
            {"message": _human_menu_message(row.get("message"))}
            for row in result.get(key, [])
            if isinstance(row, Mapping)
        ]
    for row in result.get("components", []):
        row["valuation_rate_sen"] = _money_to_sen(row.pop("valuation_rate", None))
        row["cost_per_batch_sen"] = _money_to_sen(row.pop("cost_per_batch", None))
        row["cost_per_serving_sen"] = _money_to_sen(row.pop("cost_per_serving", None))
        # "180 Millilitre" is what the ERP stores; "180 ml" is what a café
        # says. One owner words every amount the desk shows.
        row["amount"] = amount_phrase(
            flt(row.get("entered_qty_per_serving")),
            cstr(row.get("entered_uom")),
        )
    result["cost_per_batch_sen"] = _money_to_sen(result.pop("cost_per_batch", None))
    result["cost_per_serving_sen"] = _money_to_sen(result.pop("cost_per_serving", None))
    result["price_sen"] = price_sen
    # Integer sen throughout, so the margin cannot drift from the two numbers
    # it is drawn from.
    cost_sen = result["cost_per_serving_sen"]
    result["keeps_sen"] = (
        price_sen - cost_sen if price_sen is not None and cost_sen is not None else None
    )
    result["tracked_items"] = [
        {
            "name": cstr(row.get("item_name")).strip()
            or _("Ingredient or supply"),
            "amount": cstr(row.get("amount")),
            "kind": (
                "Prepared item"
                if cstr(row.get("component_type")) == "Prep Item"
                else "Supply"
                if cstr(row.get("component_type")) == "Packaging"
                else "Ingredient"
            ),
        }
        for row in result.get("components", [])
        if row.get("affects_stock")
    ]
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


def _non_negative_integer(value: Any, label: str) -> int:
    text = cstr(value).strip()
    if not text or not text.isdigit():
        frappe.throw(_("{0} must be a whole number of sen").format(label), frappe.ValidationError)
    parsed = int(text)
    if parsed > 999_999_999:
        frappe.throw(_("{0} is too large").format(label), frappe.ValidationError)
    return parsed


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
        frappe.throw(_("Choose a café that exists"), frappe.ValidationError)


def _require_company_director(action: str) -> None:
    del action
    require_director_role()
