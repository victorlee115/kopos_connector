# pyright: reportMissingImports=false

"""Idempotent Moonleaf café fixture for test and training sites only.

The restored-data and disposable-site runners intentionally have different
safety guards, but consume one fictional catalogue.  This runner keeps the
lighter operational setup needed by a disposable site; restored commissioning
adds the accounting- and evidence-bound documents around the same Items.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import cint, cstr, now_datetime

from kopos_connector.demo.moonleaf_catalog import (
    CAFE_NAME,
    INGREDIENTS as MOONLEAF_INGREDIENTS,
    MATCHA_LATTE_ITEM as MOONLEAF_MATCHA_LATTE_ITEM,
    MODIFIER_GROUPS as MOONLEAF_CUSTOMER_CHOICES,
    OPENING_PACKS as MOONLEAF_OPENING_PACKS,
    PREPARED_COMPONENTS as MOONLEAF_PREPARATIONS,
    SELLABLES as MOONLEAF_SELLABLES,
    SELLING_PRICE_LIST as MOONLEAF_SELLING_PRICE_LIST,
    SUPPLIER as MOONLEAF_SUPPLIER,
    SUPPLIER_NAME as MOONLEAF_SUPPLIER_NAME,
)

DEMO_PREFIX = "INV-ACCEPT-MOONLEAF"
DEMO_COMPANY = CAFE_NAME
DEMO_ABBR = "MLF"
DEMO_SUPPLIER = MOONLEAF_SUPPLIER
DEMO_DIRECTOR = "moonleaf.director@example.test"
DEMO_POS_PROFILE = "Moonleaf Counter Tablet"
DEMO_CUSTOMER = "Moonleaf Walk-in Customer"
DEMO_POLICY_MARKER = "moonleaf-demo-policy-v5"
MAX_RESET_RECIPE_DRAFTS = 100
DEMO_STAFF = (
    ("moonleaf.manager@example.test", "Kai", "Rahman", "Male", date(2001, 4, 12)),
    ("moonleaf.cashier@example.test", "Maya", "Lee", "Female", date(2003, 9, 8)),
)

STOCK_ITEMS = tuple(
    (spec["code"], spec["name"], spec["stock_uom"], spec["role"])
    for spec in MOONLEAF_INGREDIENTS
)

_STOCK_BY_CODE = {spec["code"]: spec for spec in MOONLEAF_INGREDIENTS}
_PREPARATION_BY_CODE = {spec["code"]: spec for spec in MOONLEAF_PREPARATIONS}
_COMPONENT_UOM_BY_CODE = {
    **{code: spec["stock_uom"] for code, spec in _STOCK_BY_CODE.items()},
    **{code: "Millilitre" for code in _PREPARATION_BY_CODE},
}
MENU_ITEMS = tuple(
    (
        spec["code"],
        spec["name"],
        format(spec["price"], "f"),
        tuple(
            (
                component["item"],
                cstr(component["qty"]),
                cstr(_COMPONENT_UOM_BY_CODE[component["item"]]),
                cstr(component["component_type"]),
                cstr(component.get("loss") or "0"),
                cstr(component.get("substitution_key") or ""),
            )
            for component in spec["components"]
        ),
        tuple(spec["modifier_groups"]),
        cstr(spec["menu_group"]),
    )
    for spec in MOONLEAF_SELLABLES
)

PURCHASE_UOMS = {
    spec["code"]: (spec["purchase_uom"], cstr(spec["conversion"]))
    for spec in MOONLEAF_INGREDIENTS
}

OPENING_STOCK = {
    spec["code"]: (
        cstr(MOONLEAF_OPENING_PACKS[spec["code"]] * spec["conversion"]),
        cstr(spec["conversion"]),
        cstr(spec["valuation_rate"]),
    )
    for spec in MOONLEAF_INGREDIENTS
}


def setup_matcha_cafe_demo_json(
    reset: bool = False,
    *,
    commit_transaction: bool = True,
    confirm_offline_orders_cleared: bool = False,
) -> dict[str, Any]:
    """Create missing demo authorities; reset only rewrites mutable demo drafts."""

    _require_safe_demo_site()
    reset_requested = _as_bool(reset)
    if reset_requested and not _as_bool(confirm_offline_orders_cleared):
        frappe.throw(
            "Reset requires confirmation that every demo tablet has uploaded or discarded its offline test orders.",
            frappe.ValidationError,
        )
    savepoint = "matcha_cafe_demo_setup"
    frappe.db.savepoint(savepoint)
    try:
        result = _setup(reset=reset_requested)
        verification = verify_matcha_cafe_demo_json()
        if not verification["pass"]:
            frappe.throw(
                "The café demo did not pass its readiness check: "
                + ", ".join(name for name, passed in verification["assertions"].items() if not passed),
                frappe.ValidationError,
            )
        result["verification"] = verification
        if commit_transaction:
            frappe.db.commit()
        return result
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


def reset_matcha_cafe_demo_json(confirm_offline_orders_cleared: bool = False) -> dict[str, Any]:
    return setup_matcha_cafe_demo_json(
        reset=True,
        confirm_offline_orders_cleared=confirm_offline_orders_cleared,
    )


def verify_matcha_cafe_demo_json() -> dict[str, Any]:
    company_exists = bool(frappe.db.exists("Company", DEMO_COMPANY))
    main_warehouse = _warehouse_name("Moonleaf Counter")
    source_warehouse = _warehouse_name("Moonleaf Storeroom")
    expected_recipe_codes = [
        f"{DEMO_PREFIX}-RECIPE-{code.removeprefix(f'{DEMO_PREFIX}-')}-V2"
        for code, *_ in MENU_ITEMS
    ]
    recipes = frappe.get_all(
        "FB Recipe",
        filters={"recipe_code": ["in", expected_recipe_codes], "status": "Active"},
        pluck="name",
        limit_page_length=len(expected_recipe_codes),
    )
    frozen_choice_effects = sum(
        len(getattr(frappe.get_doc("FB Recipe", name), "recipe_modifier_effects", None) or [])
        for name in recipes
    )
    expected_choice_codes = [
        option["code"]
        for group in MOONLEAF_CUSTOMER_CHOICES
        for option in group["options"]
    ]
    customer_choices = frappe.get_all(
        "FB Modifier",
        filters={"modifier_code": ["in", expected_choice_codes], "active": 1},
        pluck="name",
        limit_page_length=len(expected_choice_codes),
    )
    menu_prices = frappe.get_all(
        "Item Price",
        filters={
            "price_list": MOONLEAF_SELLING_PRICE_LIST,
            "item_code": ["in", [code for code, *_ in MENU_ITEMS]],
        },
        pluck="name",
        limit_page_length=len(MENU_ITEMS),
    )
    menu_item_rows = frappe.get_all(
        "Item",
        filters={"name": ["in", [code for code, *_ in MENU_ITEMS]]},
        fields=["name", "item_group"],
        limit_page_length=len(MENU_ITEMS),
    )
    expected_menu_groups = {
        code: menu_group
        for code, _name, _price, _components, _choice_groups, menu_group in MENU_ITEMS
    }
    actual_menu_groups = {
        cstr(row.get("name")): cstr(row.get("item_group"))
        for row in menu_item_rows
    }
    outlet_profile = frappe.db.get_value(
        "POS Profile",
        {
            "name": DEMO_POS_PROFILE,
            "company": DEMO_COMPANY,
            "warehouse": main_warehouse,
            "selling_price_list": MOONLEAF_SELLING_PRICE_LIST,
            "disabled": 0,
        },
        "name",
    ) if main_warehouse else None
    director_permission = frappe.db.get_value(
        "User Permission",
        {
            "user": DEMO_DIRECTOR,
            "allow": "Company",
            "for_value": DEMO_COMPANY,
        },
        "name",
    )
    staff_candidates = frappe.get_all(
        "Employee",
        filters={
            "company": DEMO_COMPANY,
            "status": "Active",
            "user_id": ["in", [email for email, *_ in DEMO_STAFF]],
        },
        pluck="name",
        limit_page_length=len(DEMO_STAFF),
    )
    supplier_name = cstr(
        frappe.db.get_value("Supplier", _supplier_id(), "supplier_name")
    ).strip()
    preparation_boms = frappe.get_all(
        "BOM",
        filters={
            "item": ["in", [spec["code"] for spec in MOONLEAF_PREPARATIONS]],
            "docstatus": 1,
            "is_active": 1,
        },
        pluck="name",
        limit_page_length=len(MOONLEAF_PREPARATIONS),
    )
    plan = frappe.get_all("FB Inventory Plan", filters={"input_hash": _plan_hash()}, pluck="name", limit_page_length=1)
    purchase_order = frappe.get_all(
        "Purchase Order",
        filters={"company": DEMO_COMPANY, "supplier": _supplier_id(), "docstatus": 0},
        fields=["name", "docstatus"],
        limit_page_length=1,
    )
    projection_logs = frappe.get_all("FB Projection Log", filters={"projection_id": ["like", f"{DEMO_PREFIX}-PROJECTION-%"]}, pluck="name", limit_page_length=10)
    count_tasks = frappe.get_all("FB Inventory Count Task", filters={"schedule_key": f"{DEMO_PREFIX}:weekly-count"}, pluck="name", limit_page_length=1)
    waste_example = frappe.db.get_value(
        "FB Waste Event",
        {"waste_id": f"{DEMO_PREFIX}-MADE-ITEM-WASTE-V1"},
        ["docstatus", "stock_entry", "source_type", "recipe", "resolved_lines_json"],
        as_dict=True,
    )
    stocked_bins = frappe.get_all(
        "Bin",
        filters={
            "warehouse": main_warehouse,
            "item_code": ["in", [code for code, *_ in STOCK_ITEMS]],
            "actual_qty": [">", 0],
        },
        pluck="name",
        limit_page_length=20,
    ) if main_warehouse else []
    source_bins = frappe.get_all(
        "Bin",
        filters={"warehouse": source_warehouse, "item_code": ["like", f"{DEMO_PREFIX}-%"], "actual_qty": [">", 0]},
        pluck="name",
        limit_page_length=20,
    ) if source_warehouse else []
    prepared_bins = frappe.get_all(
        "Bin",
        filters={
            "warehouse": main_warehouse,
            "item_code": ["in", [spec["code"] for spec in MOONLEAF_PREPARATIONS]],
            "actual_qty": [">", 0],
        },
        pluck="name",
        limit_page_length=len(MOONLEAF_PREPARATIONS),
    ) if main_warehouse else []
    assertions = {
        "company_exists": company_exists,
        "two_stock_locations_exist": bool(main_warehouse and source_warehouse),
        "fictional_stock_items_exist": sum(bool(frappe.db.exists("Item", code)) for code, *_ in STOCK_ITEMS) == len(STOCK_ITEMS),
        "fictional_menu_items_exist": sum(bool(frappe.db.exists("Item", code)) for code, *_ in MENU_ITEMS) == len(MENU_ITEMS),
        "active_recipes_exist": len(recipes) == len(MENU_ITEMS),
        "customer_choices_are_recipe_bound": (
            len(customer_choices) == len(expected_choice_codes)
            and frozen_choice_effects >= len(expected_choice_codes)
        ),
        "every_drink_has_a_price": len(menu_prices) == len(MENU_ITEMS),
        "menu_items_use_counter_groups": actual_menu_groups == expected_menu_groups,
        "counter_is_ready_for_business_pages": bool(outlet_profile),
        "director_can_open_the_cafe": bool(director_permission),
        "staff_candidates_exist": len(staff_candidates) == len(DEMO_STAFF),
        "supplier_has_a_friendly_name": supplier_name == MOONLEAF_SUPPLIER_NAME,
        "preparation_recipes_exist": len(preparation_boms) == len(MOONLEAF_PREPARATIONS),
        "opening_stock_balances_exist": len(stocked_bins) == len(STOCK_ITEMS),
        "storeroom_stock_balances_exist": len(source_bins) == len(STOCK_ITEMS),
        "prepared_batches_exist": len(prepared_bins) == len(MOONLEAF_PREPARATIONS),
        "made_item_waste_used_the_recipe": bool(
            waste_example
            and cint(waste_example.get("docstatus")) == 1
            and cstr(waste_example.get("stock_entry"))
            and cstr(waste_example.get("source_type")) == "Made Item"
            and cstr(waste_example.get("recipe"))
            and cstr(waste_example.get("resolved_lines_json"))
        ),
        "restock_plan_exists": bool(plan),
        "supplier_order_is_draft": bool(purchase_order and cint(purchase_order[0].get("docstatus")) == 0),
        "success_and_failure_update_examples_exist": len(projection_logs) >= 2,
        "count_assignment_exists": bool(count_tasks),
    }
    digest = hashlib.sha256(json.dumps(assertions, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"status": "passed" if all(assertions.values()) else "failed", "pass": all(assertions.values()), "assertions": assertions, "business_digest": digest}


def _setup(*, reset: bool) -> dict[str, Any]:
    _ensure_uoms()
    _ensure_company()
    removed_recipe_drafts = _reset_recipe_drafts() if reset else 0
    _ensure_fiscal_year()
    director = _ensure_director()
    staff_candidates = _ensure_staff_candidates()
    _ensure_item_groups()
    main_warehouse = _ensure_warehouse("Moonleaf Counter")
    source_warehouse = _ensure_warehouse("Moonleaf Storeroom")
    _ensure_supplier()
    for code, name, uom, group in STOCK_ITEMS:
        _ensure_item(code, name, uom, group, business_role=group)
    preparations = _ensure_preparations(main_warehouse)
    customer_choices = _ensure_customer_choices()
    price_list = _ensure_selling_price_list()
    for code, name, price, components, choice_groups, menu_group in MENU_ITEMS:
        _ensure_item(
            code,
            name,
            "Nos",
            menu_group,
            stock_item=False,
            business_role="Sellable Drink",
        )
        _ensure_item_price(code, price_list, price)
        _ensure_recipe(code, name, components, choice_groups, customer_choices)
    pos_profile = _ensure_pos_profile(main_warehouse, price_list)
    opening_stock = _ensure_opening_stock(main_warehouse)
    storeroom_stock = _ensure_storeroom_stock(source_warehouse)
    prepared_stock = _ensure_prepared_stock(main_warehouse, preparations)
    waste_example = _ensure_made_item_waste(main_warehouse)
    policy = _ensure_policy(
        main_warehouse,
        source_warehouse,
        opening_stock=opening_stock,
        director=director,
        reset=reset,
    )
    purchase_order = _ensure_draft_purchase_order(main_warehouse, reset=reset)
    plan = _ensure_plan(main_warehouse, purchase_order, reset=reset)
    _ensure_projection_examples(policy)
    count_task = _ensure_count_task(main_warehouse, reset=reset)
    return {
        "status": "reset" if reset else "ready",
        "company": DEMO_COMPANY,
        "warehouse": main_warehouse,
        "source_warehouse": source_warehouse,
        "selling_price_list": price_list,
        "pos_profile": pos_profile,
        "opening_stock_reconciliation": opening_stock,
        "storeroom_stock_receipt": storeroom_stock,
        "prepared_stock": prepared_stock,
        "made_item_waste": waste_example,
        "stock_director": director,
        "staff_candidates": staff_candidates,
        "policy": policy,
        "plan": plan,
        "draft_purchase_order": purchase_order,
        "count_task": count_task,
        "removed_recipe_drafts": removed_recipe_drafts,
        "supplier_delivery": "Use the seeded draft supplier order for Director review; submit a copied test order before practising delivery receipt.",
        "transfer": "Use Moonleaf Storeroom as the source and Moonleaf Counter as the destination.",
        "supplier_send": "outside_jiji_only",
    }


def _reset_recipe_drafts() -> int:
    """Remove only unpublished Moonleaf recipe edits during an explicit demo reset."""

    menu_item_codes = [code for code, *_ in MENU_ITEMS]
    drafts = frappe.get_all(
        "FB Recipe",
        filters={
            "company": DEMO_COMPANY,
            "sellable_item": ["in", menu_item_codes],
            "status": "Draft",
        },
        pluck="name",
        limit_page_length=MAX_RESET_RECIPE_DRAFTS + 1,
    )
    if len(drafts) > MAX_RESET_RECIPE_DRAFTS:
        frappe.throw(
            "The sample café has too many unpublished recipe changes to reset safely.",
            frappe.ValidationError,
        )
    for name in drafts:
        frappe.delete_doc("FB Recipe", name, ignore_permissions=True)
    return len(drafts)


def _require_safe_demo_site() -> None:
    site = cstr(getattr(getattr(frappe, "local", None), "site", ""))
    developer_mode = _as_bool(getattr(getattr(frappe, "conf", None), "developer_mode", 0))
    allow_tests = _as_bool(getattr(getattr(frappe, "conf", None), "allow_tests", 0))
    if developer_mode or allow_tests or site == "localhost" or site.endswith(".localhost"):
        return
    frappe.throw("The sample café can only be started on a training or test site. Nothing was created.", frappe.ValidationError)


def _as_bool(value: Any) -> bool:
    return cstr(value).strip().lower() in {"1", "true", "yes", "on"}


def _ensure_company() -> None:
    if frappe.db.exists("Company", DEMO_COMPANY):
        return
    document = frappe.new_doc("Company")
    document.company_name = DEMO_COMPANY
    document.abbr = DEMO_ABBR
    document.default_currency = "MYR"
    document.country = "Malaysia"
    document.insert(ignore_permissions=True)


def _ensure_fiscal_year() -> None:
    from erpnext.accounts.utils import get_fiscal_year

    posting_date = now_datetime().date()
    if get_fiscal_year(
        posting_date,
        company=DEMO_COMPANY,
        as_dict=True,
        raise_on_missing=False,
    ):
        return
    fiscal_year_name = f"{posting_date.year}-{DEMO_ABBR}"
    if frappe.db.exists("Fiscal Year", fiscal_year_name):
        frappe.throw(
            f"Fiscal Year {fiscal_year_name} exists but is not active for the sample café.",
            frappe.ValidationError,
        )
    document = frappe.new_doc("Fiscal Year")
    document.year = fiscal_year_name
    document.year_start_date = date(posting_date.year, 1, 1)
    document.year_end_date = date(posting_date.year, 12, 31)
    document.disabled = 0
    document.append("companies", {"company": DEMO_COMPANY})
    document.insert(ignore_permissions=True)


def _ensure_director() -> str:
    if frappe.db.exists("User", DEMO_DIRECTOR):
        document = frappe.get_doc("User", DEMO_DIRECTOR)
    else:
        document = frappe.new_doc("User")
        document.email = DEMO_DIRECTOR
        document.first_name = "Amina"
        document.last_name = "Demo Stock Director"
        document.enabled = 1
        document.send_welcome_email = 0
        document.insert(ignore_permissions=True)
    roles = {cstr(getattr(row, "role", None)).strip() for row in (getattr(document, "roles", None) or [])}
    if "Company Director" not in roles:
        document.append("roles", {"role": "Company Director"})
        document.save(ignore_permissions=True)
    _ensure_user_permission(DEMO_DIRECTOR, "Company", DEMO_COMPANY)
    from kopos_connector.install.install import ensure_jiji_business_module_profile

    ensure_jiji_business_module_profile()
    return DEMO_DIRECTOR


def _ensure_staff_candidates() -> list[str]:
    """Give the Staff page fictional people it can add to a tablet."""

    employees: list[str] = []
    for email, first_name, last_name, gender, birth_date in DEMO_STAFF:
        if not frappe.db.exists("Gender", gender):
            gender_document = frappe.new_doc("Gender")
            gender_document.gender = gender
            gender_document.insert(ignore_permissions=True)
        if frappe.db.exists("User", email):
            user = frappe.get_doc("User", email)
        else:
            user = frappe.new_doc("User")
            user.email = email
            user.first_name = first_name
            user.last_name = last_name
            user.enabled = 1
            user.send_welcome_email = 0
            user.user_type = "Website User"
            user.insert(ignore_permissions=True)
        existing = frappe.db.get_value("Employee", {"user_id": email}, "name")
        employee = frappe.get_doc("Employee", existing) if existing else frappe.new_doc("Employee")
        employee.first_name = first_name
        employee.last_name = last_name
        employee.employee_name = f"{first_name} {last_name}"
        employee.company = DEMO_COMPANY
        employee.status = "Active"
        employee.gender = gender
        employee.date_of_birth = birth_date
        employee.date_of_joining = date(2026, 1, 1)
        employee.user_id = email
        employee.save(ignore_permissions=True) if existing else employee.insert(ignore_permissions=True)
        employees.append(cstr(employee.name))
    return employees


def _ensure_user_permission(user: str, allow: str, for_value: str) -> str:
    existing = frappe.db.get_value(
        "User Permission",
        {"user": user, "allow": allow, "for_value": for_value},
        "name",
    )
    if existing:
        return cstr(existing)
    document = frappe.new_doc("User Permission")
    document.user = user
    document.allow = allow
    document.for_value = for_value
    document.apply_to_all_doctypes = 1
    document.insert(ignore_permissions=True)
    return cstr(document.name)


def _ensure_uoms() -> None:
    measured = {"Gram", "Millilitre", "Litre", "Kilogram"}
    all_uoms = {"Nos"}
    all_uoms.update(spec["stock_uom"] for spec in MOONLEAF_INGREDIENTS)
    all_uoms.update(spec["purchase_uom"] for spec in MOONLEAF_INGREDIENTS)
    for name in sorted(all_uoms):
        whole = 0 if name in measured else 1
        if frappe.db.exists("UOM", name):
            continue
        document = frappe.new_doc("UOM")
        document.uom_name = name
        document.must_be_whole_number = whole
        document.insert(ignore_permissions=True)


def _ensure_item_groups() -> None:
    for name in ("Ingredient", "Packaging", "Products", "Coffee", "Matcha"):
        if frappe.db.exists("Item Group", name):
            if name in {"Coffee", "Matcha"} and cint(
                frappe.db.get_value("Item Group", name, "is_group")
            ):
                frappe.throw(
                    f"The sample café needs {name} to be a menu category, but this test site already uses it as a folder.",
                    frappe.ValidationError,
                )
            continue
        document = frappe.new_doc("Item Group")
        document.item_group_name = name
        document.parent_item_group = "All Item Groups"
        document.is_group = 0
        document.insert(ignore_permissions=True)


def _warehouse_name(label: str) -> str | None:
    return cstr(frappe.db.get_value("Warehouse", {"warehouse_name": label, "company": DEMO_COMPANY}, "name")).strip() or None


def _ensure_warehouse(label: str) -> str:
    existing = _warehouse_name(label)
    if existing:
        return existing
    document = frappe.new_doc("Warehouse")
    document.warehouse_name = label
    document.company = DEMO_COMPANY
    document.is_group = 0
    document.insert(ignore_permissions=True)
    return cstr(document.name)


def _supplier_id() -> str:
    return cstr(
        frappe.db.exists("Supplier", DEMO_SUPPLIER)
        or frappe.db.get_value(
            "Supplier",
            {"supplier_name": MOONLEAF_SUPPLIER_NAME},
            "name",
        )
    ).strip()


def _ensure_supplier() -> str:
    existing = _supplier_id()
    document = frappe.get_doc("Supplier", existing) if existing else frappe.new_doc("Supplier")
    if not existing:
        document.name = DEMO_SUPPLIER
    document.supplier_name = MOONLEAF_SUPPLIER_NAME
    document.supplier_group = "All Supplier Groups"
    document.supplier_type = "Company"
    document.country = "Malaysia"
    document.save(ignore_permissions=True) if existing else document.insert(ignore_permissions=True)
    return cstr(document.name)


def _set_if_supported(document: Any, fieldname: str, value: Any) -> None:
    meta = getattr(document, "meta", None)
    if meta and meta.has_field(fieldname):
        setattr(document, fieldname, value)


def _ensure_item(
    code: str,
    name: str,
    uom: str,
    group: str,
    *,
    stock_item: bool = True,
    business_role: str,
) -> None:
    existing = frappe.db.exists("Item", code)
    document = frappe.get_doc("Item", existing) if existing else frappe.new_doc("Item")
    if existing and (
        cstr(getattr(document, "stock_uom", "")) != uom
        or cint(getattr(document, "is_stock_item", 0)) != int(stock_item)
    ):
        frappe.throw(
            f"{name} no longer matches the sample café. Use a new test site to rebuild the sample.",
            frappe.ValidationError,
        )
    if not existing:
        document.item_code = code
    document.item_name = name
    document.item_group = group
    document.stock_uom = uom
    document.is_stock_item = 1 if stock_item else 0
    document.include_item_in_manufacturing = 1 if stock_item else 0
    document.is_sales_item = 0 if stock_item else 1
    document.is_purchase_item = 1 if code in PURCHASE_UOMS else 0
    _set_if_supported(document, "custom_fb_item_role", business_role)
    _set_if_supported(document, "custom_fb_recipe_required", int(not stock_item))
    _set_if_supported(document, "custom_fb_inventory_excluded", 0)
    _set_if_supported(document, "custom_fb_track_theoretical_stock", 1)
    _set_if_supported(document, "custom_kopos_track_stock", int(stock_item))
    _set_if_supported(document, "custom_kopos_availability_mode", "auto")
    if code in PURCHASE_UOMS:
        purchase_uom, factor = PURCHASE_UOMS[code]
        document.purchase_uom = purchase_uom
        matching = [row for row in (getattr(document, "uoms", None) or []) if cstr(row.uom) == purchase_uom]
        if matching:
            if Decimal(cstr(matching[0].conversion_factor)) != Decimal(factor):
                frappe.throw(
                    f"The supplier pack for {name} no longer matches the sample café.",
                    frappe.ValidationError,
                )
        else:
            document.append("uoms", {"uom": purchase_uom, "conversion_factor": factor})
    if existing:
        document.save(ignore_permissions=True)
    else:
        document.insert(ignore_permissions=True)


def _ensure_preparations(warehouse: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for spec in MOONLEAF_PREPARATIONS:
        _ensure_item(
            spec["code"],
            spec["name"],
            "Millilitre",
            "Ingredient",
            business_role="Prep Item",
        )
        item = frappe.get_doc("Item", spec["code"])
        _set_if_supported(item, "has_batch_no", 0)
        _set_if_supported(item, "shelf_life_in_days", spec["shelf_life_days"])
        defaults = [
            row
            for row in (getattr(item, "item_defaults", None) or [])
            if cstr(getattr(row, "company", "")) == DEMO_COMPANY
        ]
        if defaults:
            defaults[0].default_warehouse = warehouse
        else:
            item.append(
                "item_defaults",
                {"company": DEMO_COMPANY, "default_warehouse": warehouse},
            )
        item.save(ignore_permissions=True)

        existing = frappe.db.get_value(
            "BOM",
            {"item": spec["code"], "docstatus": 1, "is_active": 1},
            "name",
        )
        if existing:
            result[spec["code"]] = cstr(existing)
            continue
        document = frappe.new_doc("BOM")
        document.item = spec["code"]
        document.company = DEMO_COMPANY
        document.quantity = spec["batch_qty"]
        _set_if_supported(document, "uom", "Millilitre")
        _set_if_supported(document, "is_active", 1)
        _set_if_supported(document, "is_default", 1)
        _set_if_supported(document, "with_operations", 0)
        _set_if_supported(document, "custom_kopos_autoprep_enabled", 1)
        _set_if_supported(document, "custom_kopos_batch_qty", spec["batch_qty"])
        _set_if_supported(document, "custom_kopos_min_ready_qty", spec["min_ready_qty"])
        _set_if_supported(document, "custom_kopos_preparation_lead_minutes", spec["lead_minutes"])
        _set_if_supported(document, "custom_kopos_preparation_variance_percent", Decimal("10"))
        _set_if_supported(document, "custom_kopos_preparation_instructions", spec["instructions"])
        for input_item, input_qty in spec["inputs"]:
            input_uom = cstr(frappe.db.get_value("Item", input_item, "stock_uom"))
            document.append(
                "items",
                {
                    "item_code": input_item,
                    "qty": Decimal(input_qty),
                    "uom": input_uom,
                    "stock_uom": input_uom,
                    "conversion_factor": 1,
                },
            )
        document.insert(ignore_permissions=True)
        document.submit()
        result[spec["code"]] = cstr(document.name)
    return result


def _ensure_customer_choices() -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for group_order, spec in enumerate(MOONLEAF_CUSTOMER_CHOICES, start=1):
        group_name = frappe.db.get_value(
            "FB Modifier Group", {"group_code": spec["code"]}, "name"
        )
        group = frappe.get_doc("FB Modifier Group", group_name) if group_name else frappe.new_doc("FB Modifier Group")
        if not group_name:
            group.group_code = spec["code"]
        group.group_name = spec["name"]
        group.selection_type = spec["selection_type"]
        group.is_required = spec["required"]
        group.min_selection = spec["min"]
        group.max_selection = spec["max"]
        group.display_order = group_order
        group.active = 1
        group.default_resolution_policy = "Auto Apply Default"
        group.save(ignore_permissions=True) if group_name else group.insert(ignore_permissions=True)

        default_choice = ""
        for option_order, option in enumerate(spec["options"], start=1):
            option_name = frappe.db.get_value(
                "FB Modifier", {"modifier_code": option["code"]}, "name"
            )
            if option_name:
                choice = frappe.get_doc("FB Modifier", option_name)
            else:
                choice = frappe.new_doc("FB Modifier")
                choice.modifier_code = option["code"]
                choice.modifier_name = option["name"]
                choice.modifier_group = group.name
                choice.kind = option["kind"]
                choice.price_adjustment = option["price"]
                choice.is_default = option["default"]
                choice.display_order = option_order
                choice.active = 1
                choice.target_substitution_key = option.get("target_substitution_key")
                choice.target_item = option.get("target_item")
                choice.new_item = option.get("new_item")
                choice.qty_delta = option.get("qty_delta")
                choice.qty_uom = option.get("qty_uom")
                choice.instruction_text = option.get("instruction")
                choice.affects_stock = int(option["kind"] in {"Add", "Replace", "Remove", "Scale"})
                choice.affects_recipe = choice.affects_stock
                choice.insert(ignore_permissions=True)
            if option["default"]:
                default_choice = cstr(choice.name)
        result[spec["key"]] = {
            "group": cstr(group.name),
            "default_choice": default_choice,
        }
    return result


def _ensure_selling_price_list() -> str:
    existing = frappe.db.exists("Price List", MOONLEAF_SELLING_PRICE_LIST)
    document = frappe.get_doc("Price List", existing) if existing else frappe.new_doc("Price List")
    if not existing:
        document.price_list_name = MOONLEAF_SELLING_PRICE_LIST
    document.enabled = 1
    document.selling = 1
    document.buying = 0
    document.currency = "MYR"
    document.save(ignore_permissions=True) if existing else document.insert(ignore_permissions=True)
    return cstr(document.name)


def _ensure_pos_profile(warehouse: str, price_list: str) -> str:
    """Make the fictional counter usable by the guided pages and a tablet."""

    defaults = frappe.db.get_value(
        "Company",
        DEMO_COMPANY,
        ["default_currency", "write_off_account", "cost_center"],
        as_dict=True,
    )
    if not defaults or not cstr(defaults.get("write_off_account")).strip() or not cstr(defaults.get("cost_center")).strip():
        frappe.throw(
            "The sample café is missing its basic money setup.",
            frappe.ValidationError,
        )
    customer = _ensure_walk_in_customer()
    cash_account = _cash_account()
    cash_mode = _ensure_cash_mode(cash_account)
    existing = frappe.db.exists("POS Profile", DEMO_POS_PROFILE)
    document = frappe.get_doc("POS Profile", existing) if existing else frappe.new_doc("POS Profile")
    if not existing:
        document.name = DEMO_POS_PROFILE
    document.company = DEMO_COMPANY
    document.warehouse = warehouse
    document.currency = cstr(defaults.get("default_currency")).strip() or "MYR"
    document.selling_price_list = price_list
    document.customer = customer
    document.write_off_account = cstr(defaults.get("write_off_account")).strip()
    document.write_off_cost_center = cstr(defaults.get("cost_center")).strip()
    document.write_off_limit = 0
    document.disabled = 0
    if getattr(document, "meta", None) and document.meta.has_field("item_groups"):
        document.set("item_groups", [{"item_group": "Coffee"}, {"item_group": "Matcha"}])
    document.set("payments", [{"mode_of_payment": cash_mode, "default": 1}])
    document.save(ignore_permissions=True) if existing else document.insert(ignore_permissions=True)
    return cstr(document.name)


def _ensure_walk_in_customer() -> str:
    existing = frappe.db.exists("Customer", DEMO_CUSTOMER)
    if existing:
        return cstr(existing)
    customer_groups = frappe.get_all(
        "Customer Group",
        filters={"is_group": 0},
        pluck="name",
        order_by="name asc",
        limit_page_length=1,
    )
    territories = frappe.get_all(
        "Territory",
        filters={"is_group": 0},
        pluck="name",
        order_by="name asc",
        limit_page_length=1,
    )
    if not customer_groups or not territories:
        frappe.throw(
            "The sample café is missing its basic customer setup.",
            frappe.ValidationError,
        )
    document = frappe.new_doc("Customer")
    document.customer_name = DEMO_CUSTOMER
    document.customer_group = customer_groups[0]
    document.territory = territories[0]
    document.insert(ignore_permissions=True)
    return cstr(document.name)


def _cash_account() -> str:
    accounts = frappe.get_all(
        "Account",
        filters={
            "company": DEMO_COMPANY,
            "account_type": "Cash",
            "is_group": 0,
            "disabled": 0,
        },
        pluck="name",
        order_by="name asc",
        limit_page_length=1,
    )
    if not accounts:
        frappe.throw(
            "The sample café is missing its cash account.",
            frappe.ValidationError,
        )
    return cstr(accounts[0])


def _ensure_cash_mode(account: str) -> str:
    existing = frappe.db.exists("Mode of Payment", "Cash")
    document = frappe.get_doc("Mode of Payment", existing) if existing else frappe.new_doc("Mode of Payment")
    if not existing:
        document.mode_of_payment = "Cash"
    document.enabled = 1
    document.type = "Cash"
    company_rows = [
        row
        for row in (getattr(document, "accounts", None) or [])
        if cstr(getattr(row, "company", None)).strip() == DEMO_COMPANY
    ]
    if company_rows:
        company_rows[0].default_account = account
        for duplicate in company_rows[1:]:
            document.remove(duplicate)
    else:
        document.append(
            "accounts",
            {"company": DEMO_COMPANY, "default_account": account},
        )
    document.save(ignore_permissions=True) if existing else document.insert(ignore_permissions=True)
    return cstr(document.name)


def _ensure_item_price(item_code: str, price_list: str, price: str) -> None:
    existing = frappe.db.get_value(
        "Item Price", {"item_code": item_code, "price_list": price_list}, "name"
    )
    document = frappe.get_doc("Item Price", existing) if existing else frappe.new_doc("Item Price")
    if not existing:
        document.item_code = item_code
        document.price_list = price_list
    document.price_list_rate = Decimal(price)
    document.currency = "MYR"
    document.selling = 1
    document.save(ignore_permissions=True) if existing else document.insert(ignore_permissions=True)


def _ensure_recipe(
    code: str,
    name: str,
    components: tuple[tuple[str, str, str, str, str, str], ...],
    choice_groups: tuple[str, ...],
    customer_choices: dict[str, dict[str, str]],
) -> None:
    recipe_base = f"{DEMO_PREFIX}-RECIPE-{code.removeprefix(f'{DEMO_PREFIX}-')}"
    legacy_name = frappe.db.exists("FB Recipe", recipe_base)
    if legacy_name:
        legacy = frappe.get_doc("FB Recipe", legacy_name)
        if cstr(getattr(legacy, "status", "")) == "Active":
            legacy.status = "Retired"
            legacy.save(ignore_permissions=True)
    recipe_code = f"{recipe_base}-V2"
    if frappe.db.exists("FB Recipe", recipe_code):
        recipe = frappe.get_doc("FB Recipe", recipe_code)
        item = frappe.get_doc("Item", code)
        _set_if_supported(item, "custom_fb_default_recipe", recipe.name)
        item.save(ignore_permissions=True)
        return
    document = frappe.new_doc("FB Recipe")
    document.recipe_code = recipe_code
    document.recipe_name = name
    document.sellable_item = code
    document.recipe_type = "Finished Drink"
    document.status = "Active"
    document.version_no = 2
    document.yield_qty = "1"
    document.yield_uom = "Nos"
    document.default_serving_qty = "1"
    document.default_serving_uom = "Nos"
    document.station = "Coffee & Matcha Bar"
    document.company = DEMO_COMPANY
    for index, (item, quantity, uom, component_type, loss, substitution_key) in enumerate(components, start=1):
        document.append("components", {
            "item": item,
            "component_type": component_type,
            "qty": quantity,
            "uom": uom,
            "affects_stock": 1,
            "affects_cogs": 1,
            "loss_factor_pct": loss,
            "is_substitutable": int(bool(substitution_key)),
            "substitution_key": substitution_key or None,
            "sort_order": index,
        })
    for display_order, group_key in enumerate(choice_groups, start=1):
        choice = customer_choices[group_key]
        document.append(
            "allowed_modifier_groups",
            {
                "modifier_group": choice["group"],
                "required": int(bool(choice["default_choice"])),
                "override_min_selection": 1 if choice["default_choice"] else 0,
                "override_max_selection": 1 if choice["default_choice"] else 2,
                "default_modifier": choice["default_choice"] or None,
                "display_order": display_order,
                "always_prompt": int(bool(choice["default_choice"])),
            },
        )
    document.insert(ignore_permissions=True)
    item = frappe.get_doc("Item", code)
    _set_if_supported(item, "custom_fb_default_recipe", document.name)
    item.save(ignore_permissions=True)


def _ensure_opening_stock(main_warehouse: str) -> str:
    existing = frappe.get_all(
        "Stock Reconciliation",
        filters={"company": DEMO_COMPANY, "purpose": "Opening Stock", "docstatus": 1},
        fields=["name"],
        order_by="creation asc, name asc",
        limit_page_length=2,
    )
    if len(existing) > 1:
        frappe.throw("The sample café has more than one opening stock record.", frappe.ValidationError)
    if existing:
        return cstr(existing[0].get("name"))

    accounts = frappe.get_all(
        "Account",
        filters={"company": DEMO_COMPANY, "account_type": "Temporary", "is_group": 0, "disabled": 0},
        fields=["name"],
        limit_page_length=2,
    )
    if len(accounts) != 1:
        frappe.throw("The sample café needs one Temporary opening account before stock can be loaded.", frappe.ValidationError)

    document = frappe.new_doc("Stock Reconciliation")
    document.company = DEMO_COMPANY
    document.purpose = "Opening Stock"
    document.posting_date = now_datetime().date()
    meta = frappe.get_meta("Stock Reconciliation")
    if meta.has_field("expense_account"):
        document.expense_account = cstr(accounts[0].get("name"))
    for item_code, (main_qty, _source_qty, valuation_rate) in OPENING_STOCK.items():
        stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
        document.append("items", {
            "item_code": item_code,
            "warehouse": main_warehouse,
            "qty": main_qty,
            "stock_uom": stock_uom,
            "valuation_rate": valuation_rate,
        })
    document.insert(ignore_permissions=True)
    document.submit()
    if cint(getattr(document, "docstatus", 0)) != 1:
        frappe.throw("The sample café opening stock did not finish.", frappe.ValidationError)
    return cstr(document.name)


def _ensure_storeroom_stock(source_warehouse: str) -> str:
    """Load transfer practice stock without polluting the opening-count gate."""

    marker = f"{DEMO_POLICY_MARKER}:storeroom-stock"
    existing = frappe.get_all(
        "Stock Entry",
        filters={"company": DEMO_COMPANY, "stock_entry_type": "Material Receipt", "remarks": marker},
        fields=["name", "docstatus"],
        order_by="creation asc, name asc",
        limit_page_length=2,
    )
    if len(existing) > 1:
        frappe.throw("The sample café has more than one storeroom stock receipt.", frappe.ValidationError)
    if existing:
        if cint(existing[0].get("docstatus")) != 1:
            frappe.throw("The sample café storeroom stock receipt is not finished.", frappe.ValidationError)
        return cstr(existing[0].get("name"))

    document = frappe.new_doc("Stock Entry")
    document.company = DEMO_COMPANY
    document.stock_entry_type = "Material Receipt"
    document.posting_date = now_datetime().date()
    document.remarks = marker
    for item_code, (_main_qty, source_qty, valuation_rate) in OPENING_STOCK.items():
        stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
        document.append("items", {
            "item_code": item_code,
            "t_warehouse": source_warehouse,
            "qty": source_qty,
            "uom": stock_uom,
            "stock_uom": stock_uom,
            "conversion_factor": "1",
            "basic_rate": valuation_rate,
        })
    document.insert(ignore_permissions=True)
    document.submit()
    if cint(getattr(document, "docstatus", 0)) != 1:
        frappe.throw("The sample café storeroom stock receipt did not finish.", frappe.ValidationError)
    return cstr(document.name)


def _ensure_prepared_stock(
    warehouse: str,
    preparations: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Make the opening prepared batches from the stocked raw ingredients."""

    from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry

    result: dict[str, dict[str, str]] = {}
    for spec in MOONLEAF_PREPARATIONS:
        fingerprint = hashlib.sha256(
            f"moonleaf-demo-first-batch|{DEMO_COMPANY}|{warehouse}|{preparations[spec['code']]}".encode()
        ).hexdigest()
        work_order_name = frappe.db.get_value(
            "Work Order",
            {"custom_kopos_preparation_fingerprint": fingerprint},
            "name",
        )
        work_order = frappe.get_doc("Work Order", work_order_name) if work_order_name else frappe.new_doc("Work Order")
        if not work_order_name:
            work_order.company = DEMO_COMPANY
            work_order.production_item = spec["code"]
            work_order.bom_no = preparations[spec["code"]]
            work_order.qty = spec["batch_qty"]
            work_order.fg_warehouse = warehouse
            _set_if_supported(work_order, "skip_transfer", 1)
            _set_if_supported(work_order, "custom_kopos_preparation_fingerprint", fingerprint)
            work_order.insert(ignore_permissions=True)

        stock_entry_name = frappe.db.get_value(
            "Stock Entry",
            {"work_order": work_order.name, "docstatus": 1, "purpose": "Manufacture"},
            "name",
        )
        if stock_entry_name:
            result[spec["code"]] = {
                "work_order": cstr(work_order.name),
                "stock_update": cstr(stock_entry_name),
            }
            continue
        for row in (getattr(work_order, "required_items", None) or []):
            row.source_warehouse = warehouse
        _set_if_supported(work_order, "skip_transfer", 1)
        if cint(getattr(work_order, "docstatus", 0)) == 0:
            work_order.save(ignore_permissions=True)
            work_order.submit()
        if cint(getattr(work_order, "docstatus", 0)) != 1:
            frappe.throw(
                f"The sample preparation for {spec['name']} did not start.",
                frappe.ValidationError,
            )

        values = make_stock_entry(
            work_order.name,
            "Manufacture",
            qty=spec["batch_qty"],
            target_warehouse=warehouse,
        )
        stock_update = frappe.get_doc(values)
        stock_update.company = DEMO_COMPANY
        stock_update.work_order = work_order.name
        stock_update.fg_completed_qty = spec["batch_qty"]
        _set_if_supported(stock_update, "to_warehouse", warehouse)
        for row in (getattr(stock_update, "items", None) or []):
            if cint(getattr(row, "is_finished_item", 0)):
                row.t_warehouse = warehouse
            else:
                row.s_warehouse = warehouse
        stock_update.insert(ignore_permissions=True)
        stock_update.submit()
        if cint(getattr(stock_update, "docstatus", 0)) != 1:
            frappe.throw(
                f"The sample preparation for {spec['name']} did not finish.",
                frappe.ValidationError,
            )
        result[spec["code"]] = {
            "work_order": cstr(work_order.name),
            "stock_update": cstr(stock_update.name),
        }
    return result


def _ensure_made_item_waste(warehouse: str) -> dict[str, str]:
    """Spill one customised drink through the real recipe-derived waste path."""

    waste_id = f"{DEMO_PREFIX}-MADE-ITEM-WASTE-V1"
    existing = frappe.db.get_value(
        "FB Waste Event",
        {"waste_id": waste_id},
        ["name", "docstatus", "stock_entry"],
        as_dict=True,
    )
    if existing:
        if cint(existing.get("docstatus")) != 1 or not cstr(existing.get("stock_entry")):
            frappe.throw(
                "The sample café spill is saved but its stock update is unfinished.",
                frappe.ValidationError,
            )
        return {
            "waste": cstr(existing.get("name")),
            "stock_update": cstr(existing.get("stock_entry")),
        }

    from kopos_connector.api.inventory import _record_waste_event

    product = MOONLEAF_MATCHA_LATTE_ITEM
    extra_matcha = next(
        option["code"]
        for group in MOONLEAF_CUSTOMER_CHOICES
        for option in group["options"]
        if option["name"] == "Extra matcha"
    )
    payload_hash = hashlib.sha256(waste_id.encode()).hexdigest()
    waste_name = _record_waste_event(
        {
            "company": DEMO_COMPANY,
            "warehouse": warehouse,
            "waste_id": waste_id,
            "reason_code": "Prep Error",
            "reason_text": "Fictional training spill",
            "source_type": "made_item",
            "source_item": product,
            "source_quantity": "1",
            "selected_modifiers": [extra_matcha],
            "payload_hash": payload_hash,
            "staff_user": DEMO_DIRECTOR,
        },
        f"moonleaf-demo-waste:{payload_hash}",
    )
    document = frappe.get_doc("FB Waste Event", waste_name)
    return {
        "waste": cstr(document.name),
        "stock_update": cstr(getattr(document, "stock_entry", "")),
    }


def _ensure_policy(
    main_warehouse: str,
    source_warehouse: str,
    *,
    opening_stock: str,
    director: str,
    reset: bool,
) -> str:
    existing = frappe.db.get_value("FB Inventory Policy", {"company": DEMO_COMPANY, "warehouse": main_warehouse}, "name")
    if existing and not reset:
        return cstr(existing)
    document = frappe.get_doc("FB Inventory Policy", existing) if existing else frappe.new_doc("FB Inventory Policy")
    document.company = DEMO_COMPANY
    document.warehouse = main_warehouse
    document.transit_warehouse = source_warehouse
    document.automation_state = "Review First"
    document.inventory_contract_version = "inventory-autopilot-v1"
    document.opening_stock_reconciliation = opening_stock
    document.inventory_exception_owner = director
    document.purchase_review_owner = director
    document.max_source_age_minutes = 30
    document.permitted_actions = "Material Request\nDraft Purchase Order\nTransfer\nManufacture\nCount"
    document.quantity_ceiling = "50000"
    document.value_ceiling = "5000"
    document.count_variance_percent_ceiling = "10"
    document.flags.ignore_permissions = True
    document.save() if existing else document.insert(ignore_permissions=True)
    return cstr(document.name)


def _ensure_draft_purchase_order(warehouse: str, *, reset: bool) -> str:
    supplier = _supplier_id()
    if not supplier:
        frappe.throw(
            "The sample café supplier is missing.",
            frappe.ValidationError,
        )
    existing = frappe.db.get_value(
        "Purchase Order",
        {"company": DEMO_COMPANY, "supplier": supplier, "docstatus": 0},
        "name",
    )
    if existing and not reset:
        return cstr(existing)
    document = frappe.get_doc("Purchase Order", existing) if existing else frappe.new_doc("Purchase Order")
    if cint(getattr(document, "docstatus", 0)) != 0:
        frappe.throw("The demo supplier order must remain a draft; use a new test site to restore it.", frappe.ValidationError)
    document.supplier = supplier
    document.company = DEMO_COMPANY
    document.currency = "MYR"
    document.schedule_date = (now_datetime() + timedelta(days=7)).date()
    document.set("items", [])
    matcha = next(spec for spec in MOONLEAF_INGREDIENTS if "MATCHA-POWDER" in spec["code"])
    document.append("items", {
        "item_code": matcha["code"],
        "qty": "2",
        "uom": matcha["purchase_uom"],
        "stock_uom": matcha["stock_uom"],
        "conversion_factor": matcha["conversion"],
        "rate": matcha["purchase_rate"],
        "warehouse": warehouse,
        "schedule_date": document.schedule_date,
    })
    document.flags.ignore_permissions = True
    document.save() if existing else document.insert(ignore_permissions=True)
    return cstr(document.name)


def _plan_hash() -> str:
    return hashlib.sha256(DEMO_POLICY_MARKER.encode()).hexdigest()


def _ensure_plan(warehouse: str, purchase_order: str, *, reset: bool) -> str:
    existing = frappe.db.get_value("FB Inventory Plan", {"input_hash": _plan_hash()}, "name")
    if existing and not reset:
        return cstr(existing)
    document = frappe.get_doc("FB Inventory Plan", existing) if existing else frappe.new_doc("FB Inventory Plan")
    document.company = DEMO_COMPANY
    document.warehouse = warehouse
    document.planning_date = now_datetime().date()
    document.status = "Review First"
    document.input_hash = _plan_hash()
    document.policy_hash = _plan_hash()
    document.forecast_state = "Please check"
    document.forecast_evidence = "Fictional café demand: matcha and oat milk are below the seven-day target."
    document.gate_results = json.dumps({"demo_only": True, "supplier_send": False}, sort_keys=True)
    document.created_documents = json.dumps([{"doctype": "Purchase Order", "name": purchase_order}], sort_keys=True)
    document.set("lines", [])
    for item, quantity, uom, reason in (
        (f"{DEMO_PREFIX}-MATCHA-POWDER", "2", "Tin", "Seven days of matcha plus safety stock."),
        (f"{DEMO_PREFIX}-OAT-MILK", "12", "Carton", "Expected iced latte demand plus safety stock."),
        (f"{DEMO_PREFIX}-16OZ-CUP", "4", "Sleeve", "Packaging is below the next delivery target."),
    ):
        document.append("lines", {"item": item, "action": "Purchase", "warehouse": warehouse, "quantity": quantity, "quantity_decimal": quantity, "uom": uom, "reason": reason})
    document.flags.ignore_permissions = True
    document.save() if existing else document.insert(ignore_permissions=True)
    return cstr(document.name)


def _ensure_projection_examples(policy: str) -> None:
    for suffix, state, error in (("SUCCESS", "Succeeded", None), ("FAILURE", "Failed", "JiJi could not finish this stock update. The saved information is safe.")):
        projection_id = f"{DEMO_PREFIX}-PROJECTION-{suffix}"
        if frappe.db.exists("FB Projection Log", {"projection_id": projection_id}):
            continue
        document = frappe.new_doc("FB Projection Log")
        document.projection_id = projection_id
        document.source_doctype = "FB Inventory Policy"
        document.source_name = policy
        document.source_event_type = "demo_inventory_health"
        document.projection_type = "Stock Issue"
        document.idempotency_key = projection_id.lower()
        document.payload_hash = hashlib.sha256(projection_id.encode()).hexdigest()
        document.state = state
        document.retry_count = 1 if state == "Failed" else 0
        document.last_error = error
        document.created_at = now_datetime() - timedelta(minutes=10 if state == "Failed" else 20)
        document.last_attempt_at = now_datetime() - timedelta(minutes=5 if state == "Failed" else 19)
        document.insert(ignore_permissions=True)


def _ensure_count_task(warehouse: str, *, reset: bool) -> str:
    schedule_key = f"{DEMO_PREFIX}:weekly-count"
    existing = frappe.db.get_value("FB Inventory Count Task", {"schedule_key": schedule_key}, "name")
    if existing and not reset:
        return cstr(existing)
    document = frappe.get_doc("FB Inventory Count Task", existing) if existing else frappe.new_doc("FB Inventory Count Task")
    document.company = DEMO_COMPANY
    document.warehouse = warehouse
    document.stock_group = "Ingredient"
    document.schedule_frequency = "Weekly"
    document.schedule_period = "demo-week"
    document.schedule_key = schedule_key
    document.revision = max(1, cint(getattr(document, "revision", 0)))
    document.stock_watermark = hashlib.sha256(f"{schedule_key}:{document.revision}".encode()).hexdigest()
    document.status = "Assigned"
    document.set("lines", [])
    for item, _name, uom, _group in STOCK_ITEMS[:7]:
        document.append("lines", {"item_id": item, "uom": uom, "stock_uom": uom, "purchase_uom": uom, "conversion_factor": "1"})
    document.flags.ignore_permissions = True
    document.save() if existing else document.insert(ignore_permissions=True)
    return cstr(document.name)
