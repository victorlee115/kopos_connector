# pyright: reportMissingImports=false

"""Idempotent Matcha & Bean Café fixture for test and training sites only."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Any

import frappe
from frappe.utils import cint, cstr, now_datetime


DEMO_PREFIX = "DEMO-CAFE"
DEMO_COMPANY = "Matcha & Bean Café Sdn. Bhd."
DEMO_ABBR = "MBC"
DEMO_SUPPLIER = "DEMO-CAFE Uji Tea & Coffee Supply"
DEMO_DIRECTOR = "demo.stock.director@example.test"
DEMO_POLICY_MARKER = "demo-cafe-policy-v3"

STOCK_ITEMS = (
    ("DEMO-CAFE-MATCHA", "Ceremonial Matcha Powder", "Gram", "Ingredient"),
    ("DEMO-CAFE-ESPRESSO", "House Espresso Beans", "Gram", "Ingredient"),
    ("DEMO-CAFE-WHOLE-MILK", "Fresh Whole Milk", "Millilitre", "Ingredient"),
    ("DEMO-CAFE-OAT-MILK", "Barista Oat Milk", "Millilitre", "Ingredient"),
    ("DEMO-CAFE-VANILLA", "Vanilla Syrup", "Millilitre", "Ingredient"),
    ("DEMO-CAFE-STRAWBERRY", "Strawberry Purée", "Millilitre", "Ingredient"),
    ("DEMO-CAFE-WATER", "Filtered Water", "Millilitre", "Ingredient"),
    ("DEMO-CAFE-COLD-CUP", "12 oz Cold Cup", "Nos", "Packaging"),
    ("DEMO-CAFE-HOT-CUP", "8 oz Hot Cup", "Nos", "Packaging"),
    ("DEMO-CAFE-LID", "Compostable Cup Lid", "Nos", "Packaging"),
)

MENU_ITEMS = (
    ("DEMO-CAFE-MATCHA-LATTE", "Classic Matcha Latte", "14.00", (("DEMO-CAFE-MATCHA", "3", "Gram", "Ingredient"), ("DEMO-CAFE-WHOLE-MILK", "180", "Millilitre", "Ingredient"), ("DEMO-CAFE-HOT-CUP", "1", "Nos", "Packaging"), ("DEMO-CAFE-LID", "1", "Nos", "Packaging"))),
    ("DEMO-CAFE-STRAWBERRY-MATCHA", "Strawberry Matcha Latte", "16.00", (("DEMO-CAFE-MATCHA", "3", "Gram", "Ingredient"), ("DEMO-CAFE-OAT-MILK", "160", "Millilitre", "Ingredient"), ("DEMO-CAFE-STRAWBERRY", "25", "Millilitre", "Ingredient"), ("DEMO-CAFE-COLD-CUP", "1", "Nos", "Packaging"), ("DEMO-CAFE-LID", "1", "Nos", "Packaging"))),
    ("DEMO-CAFE-FLAT-WHITE", "Double-Shot Flat White", "13.00", (("DEMO-CAFE-ESPRESSO", "36", "Gram", "Ingredient"), ("DEMO-CAFE-WHOLE-MILK", "150", "Millilitre", "Ingredient"), ("DEMO-CAFE-HOT-CUP", "1", "Nos", "Packaging"), ("DEMO-CAFE-LID", "1", "Nos", "Packaging"))),
    ("DEMO-CAFE-OAT-LATTE", "Iced Oat Latte", "15.00", (("DEMO-CAFE-ESPRESSO", "36", "Gram", "Ingredient"), ("DEMO-CAFE-OAT-MILK", "180", "Millilitre", "Ingredient"), ("DEMO-CAFE-COLD-CUP", "1", "Nos", "Packaging"), ("DEMO-CAFE-LID", "1", "Nos", "Packaging"))),
    ("DEMO-CAFE-VANILLA-COLD-BREW", "Vanilla Cold Brew", "16.00", (("DEMO-CAFE-ESPRESSO", "45", "Gram", "Ingredient"), ("DEMO-CAFE-WATER", "180", "Millilitre", "Ingredient"), ("DEMO-CAFE-VANILLA", "15", "Millilitre", "Ingredient"), ("DEMO-CAFE-COLD-CUP", "1", "Nos", "Packaging"), ("DEMO-CAFE-LID", "1", "Nos", "Packaging"))),
)

PURCHASE_UOMS = {
    "DEMO-CAFE-MATCHA": ("Bag", "500"),
    "DEMO-CAFE-OAT-MILK": ("Carton", "1000"),
    "DEMO-CAFE-COLD-CUP": ("Carton", "50"),
}

OPENING_STOCK = {
    "DEMO-CAFE-MATCHA": ("1400", "2500", "0.24"),
    "DEMO-CAFE-ESPRESSO": ("6000", "9000", "0.08"),
    "DEMO-CAFE-WHOLE-MILK": ("12000", "6000", "0.008"),
    "DEMO-CAFE-OAT-MILK": ("8000", "18000", "0.012"),
    "DEMO-CAFE-VANILLA": ("1500", "3000", "0.03"),
    "DEMO-CAFE-STRAWBERRY": ("1500", "3000", "0.04"),
    "DEMO-CAFE-WATER": ("20000", "10000", "0.001"),
    "DEMO-CAFE-COLD-CUP": ("80", "250", "0.35"),
    "DEMO-CAFE-HOT-CUP": ("100", "250", "0.30"),
    "DEMO-CAFE-LID": ("160", "500", "0.12"),
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
    main_warehouse = _warehouse_name("Matcha & Bean Café")
    source_warehouse = _warehouse_name("Café Storeroom")
    recipes = frappe.get_all("FB Recipe", filters={"recipe_code": ["like", f"{DEMO_PREFIX}-RECIPE-%"]}, pluck="name", limit_page_length=20)
    plan = frappe.get_all("FB Inventory Plan", filters={"input_hash": _plan_hash()}, pluck="name", limit_page_length=1)
    purchase_order = frappe.get_all(
        "Purchase Order",
        filters={"company": DEMO_COMPANY, "supplier": DEMO_SUPPLIER, "docstatus": 0},
        fields=["name", "docstatus"],
        limit_page_length=1,
    )
    projection_logs = frappe.get_all("FB Projection Log", filters={"projection_id": ["like", f"{DEMO_PREFIX}-PROJECTION-%"]}, pluck="name", limit_page_length=10)
    count_tasks = frappe.get_all("FB Inventory Count Task", filters={"schedule_key": f"{DEMO_PREFIX}:weekly-count"}, pluck="name", limit_page_length=1)
    stocked_bins = frappe.get_all(
        "Bin",
        filters={"warehouse": main_warehouse, "item_code": ["like", f"{DEMO_PREFIX}-%"], "actual_qty": [">", 0]},
        pluck="name",
        limit_page_length=20,
    ) if main_warehouse else []
    assertions = {
        "company_exists": company_exists,
        "two_stock_locations_exist": bool(main_warehouse and source_warehouse),
        "fictional_stock_items_exist": sum(bool(frappe.db.exists("Item", code)) for code, *_ in STOCK_ITEMS) == len(STOCK_ITEMS),
        "fictional_menu_items_exist": sum(bool(frappe.db.exists("Item", code)) for code, *_ in MENU_ITEMS) == len(MENU_ITEMS),
        "active_recipes_exist": len(recipes) == len(MENU_ITEMS),
        "opening_stock_balances_exist": len(stocked_bins) == len(STOCK_ITEMS),
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
    _ensure_fiscal_year()
    director = _ensure_director()
    _ensure_item_groups()
    main_warehouse = _ensure_warehouse("Matcha & Bean Café")
    source_warehouse = _ensure_warehouse("Café Storeroom")
    _ensure_supplier()
    for code, name, uom, group in STOCK_ITEMS:
        _ensure_item(code, name, uom, group)
    for code, name, _price, components in MENU_ITEMS:
        _ensure_item(code, name, "Nos", "Products", stock_item=False)
        _ensure_recipe(code, name, components)
    opening_stock = _ensure_opening_stock(main_warehouse, source_warehouse)
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
        "opening_stock_reconciliation": opening_stock,
        "stock_director": director,
        "policy": policy,
        "plan": plan,
        "draft_purchase_order": purchase_order,
        "count_task": count_task,
        "supplier_delivery": "Use the seeded draft supplier order for Director review; submit a copied test order before practising delivery receipt.",
        "transfer": "Use Café Storeroom as the source and Matcha & Bean Café as the destination.",
        "supplier_send": "outside_jiji_only",
    }


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
    return DEMO_DIRECTOR


def _ensure_uoms() -> None:
    for name, whole in (("Gram", 0), ("Millilitre", 0), ("Nos", 1), ("Bag", 1), ("Carton", 1), ("Bottle", 1)):
        if frappe.db.exists("UOM", name):
            continue
        document = frappe.new_doc("UOM")
        document.uom_name = name
        document.must_be_whole_number = whole
        document.insert(ignore_permissions=True)


def _ensure_item_groups() -> None:
    for name in ("Ingredient", "Packaging", "Products"):
        if frappe.db.exists("Item Group", name):
            continue
        document = frappe.new_doc("Item Group")
        document.item_group_name = name
        document.parent_item_group = "All Item Groups"
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


def _ensure_supplier() -> None:
    if frappe.db.exists("Supplier", DEMO_SUPPLIER):
        return
    document = frappe.new_doc("Supplier")
    document.supplier_name = DEMO_SUPPLIER
    document.supplier_group = "All Supplier Groups"
    document.supplier_type = "Company"
    document.country = "Malaysia"
    document.insert(ignore_permissions=True)


def _ensure_item(code: str, name: str, uom: str, group: str, *, stock_item: bool = True) -> None:
    if frappe.db.exists("Item", code):
        return
    document = frappe.new_doc("Item")
    document.item_code = code
    document.item_name = name
    document.item_group = group
    document.stock_uom = uom
    document.is_stock_item = 1 if stock_item else 0
    document.include_item_in_manufacturing = 1 if stock_item else 0
    if code in PURCHASE_UOMS:
        purchase_uom, factor = PURCHASE_UOMS[code]
        document.purchase_uom = purchase_uom
        document.append("uoms", {"uom": purchase_uom, "conversion_factor": factor})
    document.insert(ignore_permissions=True)


def _ensure_recipe(code: str, name: str, components: tuple[tuple[str, str, str, str], ...]) -> None:
    recipe_code = f"{DEMO_PREFIX}-RECIPE-{code.removeprefix(f'{DEMO_PREFIX}-')}"
    if frappe.db.exists("FB Recipe", recipe_code):
        return
    document = frappe.new_doc("FB Recipe")
    document.recipe_code = recipe_code
    document.recipe_name = name
    document.sellable_item = code
    document.recipe_type = "Finished Drink"
    document.status = "Active"
    document.version_no = 1
    document.yield_qty = "1"
    document.yield_uom = "Nos"
    document.default_serving_qty = "1"
    document.default_serving_uom = "Nos"
    document.station = "Coffee & Matcha Bar"
    document.company = DEMO_COMPANY
    for index, (item, quantity, uom, component_type) in enumerate(components, start=1):
        document.append("components", {
            "item": item,
            "component_type": component_type,
            "qty": quantity,
            "uom": uom,
            "affects_stock": 1,
            "affects_cogs": 1,
            "loss_factor_pct": "0",
            "sort_order": index,
        })
    document.insert(ignore_permissions=True)


def _ensure_opening_stock(main_warehouse: str, source_warehouse: str) -> str:
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
    for item_code, (main_qty, source_qty, valuation_rate) in OPENING_STOCK.items():
        stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
        for warehouse, quantity in ((main_warehouse, main_qty), (source_warehouse, source_qty)):
            document.append("items", {
                "item_code": item_code,
                "warehouse": warehouse,
                "qty": quantity,
                "stock_uom": stock_uom,
                "valuation_rate": valuation_rate,
            })
    document.insert(ignore_permissions=True)
    document.submit()
    if cint(getattr(document, "docstatus", 0)) != 1:
        frappe.throw("The sample café opening stock did not finish.", frappe.ValidationError)
    return cstr(document.name)


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
    existing = frappe.db.get_value(
        "Purchase Order",
        {"company": DEMO_COMPANY, "supplier": DEMO_SUPPLIER, "docstatus": 0},
        "name",
    )
    if existing and not reset:
        return cstr(existing)
    document = frappe.get_doc("Purchase Order", existing) if existing else frappe.new_doc("Purchase Order")
    if cint(getattr(document, "docstatus", 0)) != 0:
        frappe.throw("The demo supplier order must remain a draft; use a new test site to restore it.", frappe.ValidationError)
    document.supplier = DEMO_SUPPLIER
    document.company = DEMO_COMPANY
    document.currency = "MYR"
    document.schedule_date = (now_datetime() + timedelta(days=7)).date()
    document.set("items", [])
    document.append("items", {"item_code": "DEMO-CAFE-MATCHA", "qty": "2", "uom": "Bag", "stock_uom": "Gram", "conversion_factor": "500", "rate": "120", "warehouse": warehouse, "schedule_date": document.schedule_date})
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
        ("DEMO-CAFE-MATCHA", "2", "Bag", "Seven days of matcha plus safety stock."),
        ("DEMO-CAFE-OAT-MILK", "12", "Carton", "Expected iced latte demand plus safety stock."),
        ("DEMO-CAFE-COLD-CUP", "4", "Carton", "Packaging is below the next delivery target."),
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
