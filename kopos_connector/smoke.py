from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt, now_datetime, nowdate


DEMO_DRINK_ITEM = "STRAWBERRY-MATCHA-LATTE"
DEMO_DRINK_NAME = "Strawberry Matcha Latte"
DEMO_RECIPE_CODE = "SMOKE-STRAWBERRY-MATCHA"
DEMO_MATCHA_ITEM = "SMOKE-MATCHA-POWDER"
DEMO_STRAWBERRY_ITEM = "SMOKE-STRAWBERRY-PUREE"
DEMO_MILK_ITEM = "SMOKE-MILK"
DEMO_CUP_ITEM = "SMOKE-CUP"
DEMO_CURRENCY = "MYR"


def setup_refund_smoke_data() -> dict[str, Any]:
    from erpnext.setup.utils import before_tests

    before_tests()

    company = frappe.get_all("Company", pluck="name", limit=1)[0]
    customer = _ensure_customer(company)
    warehouse = _ensure_warehouse(company)
    cost_center = _ensure_cost_center(company)
    cash_account = _ensure_cash_account(company)
    expense_account = _ensure_expense_account(company)

    _ensure_mode_of_payment("Cash", company, cash_account, "Cash")
    _ensure_mode_of_payment("DuitNow QR", company, cash_account, "Bank")

    pos_profile = _ensure_pos_profile(
        company=company,
        warehouse=warehouse,
        customer=customer,
        write_off_account=expense_account,
        write_off_cost_center=cost_center,
    )
    _ensure_pos_settings()
    opening_entry = _ensure_pos_opening_entry(company, pos_profile)
    modifier_group = _ensure_modifier_group()
    item = _ensure_item(company, warehouse, modifier_group)

    frappe.db.commit()
    return {
        "company": company,
        "customer": customer,
        "warehouse": warehouse,
        "cost_center": cost_center,
        "cash_account": cash_account,
        "expense_account": expense_account,
        "pos_profile": pos_profile,
        "pos_opening_entry": opening_entry,
        "item_code": item,
    }


def inspect_refund_draft(invoice_name: str = "ACC-PSINV-2026-00001") -> dict[str, Any]:
    from kopos_connector.api.orders import (
        build_credit_note,
        get_default_pos_profile,
        validate_refund_payload,
    )

    original = frappe.get_doc("POS Invoice", invoice_name)
    payload = validate_refund_payload(
        {
            "idempotency_key": "inspect-refund-draft",
            "device_id": "TEST-DEVICE-001",
            "original_invoice": invoice_name,
            "refund_type": "full",
            "refund_reason": "Customer changed mind",
            "return_to_stock": False,
            "payment_mode": "cash",
        }
    )
    profile = get_default_pos_profile()
    draft = build_credit_note(payload, original, profile)
    return {
        "doctype": draft.doctype,
        "is_return": draft.is_return,
        "return_against": draft.return_against,
        "grand_total": draft.grand_total,
        "paid_amount": draft.paid_amount,
        "write_off_amount": getattr(draft, "write_off_amount", None),
        "payments": [
            {
                "mode_of_payment": row.mode_of_payment,
                "amount": row.amount,
            }
            for row in (draft.get("payments") or [])
        ],
        "items": [
            {
                "item_code": row.item_code,
                "qty": row.qty,
                "rate": row.rate,
                "amount": row.amount,
            }
            for row in draft.items
        ],
    }


def setup_stock_item_smoke_data(target_qty: float = 5) -> dict[str, Any]:
    from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

    company = frappe.get_all("Company", pluck="name", limit=1)[0]
    warehouse = _ensure_warehouse(company)
    item_code = _ensure_stock_item()
    current_qty = get_bin_qty(item_code, warehouse)
    delta = flt(target_qty) - current_qty
    if delta > 0:
        entry = make_stock_entry(
            item_code=item_code,
            qty=delta,
            company=company,
            to_warehouse=warehouse,
            do_not_save=True,
        )
        entry.insert(ignore_permissions=True)
        entry.submit()
    elif delta < 0:
        entry = make_stock_entry(
            item_code=item_code,
            qty=abs(delta),
            company=company,
            from_warehouse=warehouse,
            do_not_save=True,
        )
        entry.insert(ignore_permissions=True)
        entry.submit()

    frappe.db.commit()
    return {
        "company": company,
        "warehouse": warehouse,
        "item_code": item_code,
        "actual_qty": get_bin_qty(item_code, warehouse),
    }


def set_stock_item_smoke_qty_zero() -> dict[str, Any]:
    return setup_stock_item_smoke_data(target_qty=0)


def set_stock_item_smoke_qty_five() -> dict[str, Any]:
    return setup_stock_item_smoke_data(target_qty=5)


def get_stock_item_smoke_state() -> dict[str, Any]:
    company = frappe.get_all("Company", pluck="name", limit=1)[0]
    warehouse = _ensure_warehouse(company)
    return {
        "company": company,
        "warehouse": warehouse,
        "item_code": DEMO_MATCHA_ITEM,
        "actual_qty": get_bin_qty(DEMO_MATCHA_ITEM, warehouse),
    }


def get_demo_ingredient_state() -> dict[str, Any]:
    company = frappe.get_all("Company", pluck="name", limit=1)[0]
    warehouse = _ensure_warehouse(company)
    return {
        "company": company,
        "warehouse": warehouse,
        "matcha_qty": get_bin_qty(DEMO_MATCHA_ITEM, warehouse),
        "strawberry_qty": get_bin_qty(DEMO_STRAWBERRY_ITEM, warehouse),
        "milk_qty": get_bin_qty(DEMO_MILK_ITEM, warehouse),
        "cup_qty": get_bin_qty(DEMO_CUP_ITEM, warehouse),
    }


def set_demo_ingredient_quantities(
    matcha_qty: float = 500,
    strawberry_qty: float = 1000,
    milk_qty: float = 2000,
    cup_qty: float = 20,
) -> dict[str, Any]:
    from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

    company = frappe.get_all("Company", pluck="name", limit=1)[0]
    warehouse = _ensure_warehouse(company)
    _ensure_stock_item()

    def _set_qty(item_code: str, target_qty: float) -> None:
        current_qty = get_bin_qty(item_code, warehouse)
        delta = flt(target_qty) - current_qty
        if delta > 0:
            entry = make_stock_entry(
                item_code=item_code,
                qty=delta,
                company=company,
                to_warehouse=warehouse,
                do_not_save=True,
            )
            entry.insert(ignore_permissions=True)
            entry.submit()
        elif delta < 0:
            entry = make_stock_entry(
                item_code=item_code,
                qty=abs(delta),
                company=company,
                from_warehouse=warehouse,
                do_not_save=True,
            )
            entry.insert(ignore_permissions=True)
            entry.submit()

    _set_qty(DEMO_MATCHA_ITEM, matcha_qty)
    _set_qty(DEMO_STRAWBERRY_ITEM, strawberry_qty)
    _set_qty(DEMO_MILK_ITEM, milk_qty)
    _set_qty(DEMO_CUP_ITEM, cup_qty)
    frappe.db.commit()
    return get_demo_ingredient_state()


def ensure_demo_fb_shift(shift_code: str = "smoke-shift-001") -> dict[str, Any]:
    existing_name = frappe.db.get_value("FB Shift", {"shift_code": shift_code}, "name")
    if existing_name:
        shift = frappe.get_doc("FB Shift", existing_name)
        return {
            "name": shift.name,
            "shift_code": shift.shift_code,
            "device_id": shift.device_id,
            "staff_id": shift.staff_id,
            "warehouse": shift.warehouse,
            "company": shift.company,
            "status": shift.status,
        }

    company = frappe.get_all("Company", pluck="name", limit=1)[0]
    warehouse = _ensure_warehouse(company)
    shift = frappe.new_doc("FB Shift")
    shift.shift_code = shift_code
    shift.device_id = "SMOKE-TAB-A001"
    shift.staff_id = "staff@smoke.kopos.local"
    shift.warehouse = warehouse
    shift.company = company
    shift.status = "Open"
    shift.opening_float = 0
    shift.insert(ignore_permissions=True)
    frappe.db.commit()
    return {
        "name": shift.name,
        "shift_code": shift.shift_code,
        "device_id": shift.device_id,
        "staff_id": shift.staff_id,
        "warehouse": shift.warehouse,
        "company": shift.company,
        "status": shift.status,
    }


def run_demo_fb_sale_audit(return_to_stock: bool = False) -> dict[str, Any]:
    from kopos_connector.api.fb_returns import process_return as process_fb_return
    from kopos_connector.kopos.api.fb_orders import submit_order

    shift = ensure_demo_fb_shift()
    before = set_demo_ingredient_quantities()
    order_id = f"SMOKE-DEMO-{frappe.generate_hash(length=8)}"
    idempotency_key = f"SMOKE-DEMO-{frappe.generate_hash(length=16)}"
    frappe.local.form_dict = {
        "order_id": order_id,
        "idempotency_key": idempotency_key,
        "device_id": "SMOKE-TAB-A001",
        "shift_id": shift["shift_code"],
        "staff_id": shift["staff_id"],
        "warehouse": shift["warehouse"],
        "company": shift["company"],
        "currency": DEMO_CURRENCY,
        "order": {
            "display_number": "SMK-DEMO-1",
            "order_type": "takeaway",
            "created_at": now_datetime().isoformat(),
            "items": [
                {
                    "line_id": f"LINE-{frappe.generate_hash(length=8)}",
                    "item_code": DEMO_DRINK_ITEM,
                    "item_name": DEMO_DRINK_NAME,
                    "qty": 1,
                    "rate": 12.0,
                    "discount_amount": 0,
                    "modifier_total": 0,
                    "amount": 12.0,
                    "modifiers": [],
                }
            ],
            "payments": [
                {
                    "payment_method": "Cash",
                    "amount": 12.0,
                    "tendered_amount": 12.0,
                    "change_amount": 0,
                }
            ],
        },
    }
    result = submit_order()
    frappe.db.commit()
    order_doc = frappe.get_doc("FB Order", result["fb_order"])
    after_submit = get_demo_ingredient_state()

    refund_result = None
    after_return = None
    if return_to_stock:
        resolved_sales = frappe.get_all(
            "FB Resolved Sale",
            filters={"fb_order": order_doc.name},
            pluck="name",
        )
        if resolved_sales:
            frappe.local.form_dict = {
                "return_id": f"RETURN-{frappe.generate_hash(length=8)}",
                "fb_order": order_doc.name,
                "original_sales_invoice": order_doc.sales_invoice,
                "reason_code": "Other",
                "reason_text": "Smoke audit return",
                "return_to_stock": 1,
                "lines": [
                    {
                        "original_resolved_sale": resolved_sales[0],
                        "qty_returned": 1,
                    }
                ],
            }
            refund_result = process_fb_return()
            frappe.db.commit()
            after_return = get_demo_ingredient_state()

    return {
        "before": before,
        "submit_result": result,
        "fb_order": {
            "name": order_doc.name,
            "sales_invoice": order_doc.sales_invoice,
            "ingredient_stock_entry": order_doc.ingredient_stock_entry,
            "invoice_status": order_doc.invoice_status,
            "stock_status": order_doc.stock_status,
        },
        "after_submit": after_submit,
        "refund_result": refund_result,
        "after_return": after_return,
    }


def run_demo_out_of_stock_audit() -> dict[str, Any]:
    ensure_demo_fb_shift()
    set_demo_ingredient_quantities(matcha_qty=0)
    from kopos_connector.kopos.api.fb_orders import submit_order

    frappe.local.form_dict = {
        "order_id": f"OOS-{frappe.generate_hash(length=8)}",
        "idempotency_key": f"OOS-{frappe.generate_hash(length=16)}",
        "device_id": "SMOKE-TAB-A001",
        "shift_id": "smoke-shift-001",
        "staff_id": "staff@smoke.kopos.local",
        "warehouse": _ensure_warehouse(
            frappe.get_all("Company", pluck="name", limit=1)[0]
        ),
        "company": frappe.get_all("Company", pluck="name", limit=1)[0],
        "currency": DEMO_CURRENCY,
        "order": {
            "display_number": "SMK-OOS-1",
            "order_type": "takeaway",
            "created_at": now_datetime().isoformat(),
            "items": [
                {
                    "line_id": f"LINE-{frappe.generate_hash(length=8)}",
                    "item_code": DEMO_DRINK_ITEM,
                    "item_name": DEMO_DRINK_NAME,
                    "qty": 1,
                    "rate": 12.0,
                    "discount_amount": 0,
                    "modifier_total": 0,
                    "amount": 12.0,
                    "modifiers": [],
                }
            ],
            "payments": [
                {
                    "payment_method": "Cash",
                    "amount": 12.0,
                    "tendered_amount": 12.0,
                    "change_amount": 0,
                }
            ],
        },
    }
    try:
        submit_order()
        return {"status": "unexpected_success", "stock": get_demo_ingredient_state()}
    except Exception as exc:
        frappe.db.rollback()
        return {
            "status": "blocked",
            "message": str(exc),
            "stock": get_demo_ingredient_state(),
        }


def get_bin_qty(item_code: str, warehouse: str) -> float:
    return flt(
        frappe.db.get_value(
            "Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
        )
        or 0
    )


def _ensure_customer(company: str) -> str:
    existing = frappe.db.exists("Customer", "Walk-in Customer")
    if existing:
        return existing

    doc = frappe.get_doc(
        {
            "doctype": "Customer",
            "customer_name": "Walk-in Customer",
            "customer_group": _first_name("Customer Group", {"is_group": 0}),
            "territory": _first_name("Territory", {"is_group": 0}),
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_warehouse(company: str) -> str:
    existing = frappe.get_all(
        "Warehouse",
        filters={"company": company, "is_group": 0, "disabled": 0},
        fields=["name", "warehouse_type"],
        limit=1,
    )
    for row in existing:
        if not row.get("warehouse_type") or row.get("warehouse_type") == "":
            return row["name"]

    root = frappe.get_all(
        "Warehouse",
        filters={"company": company, "is_group": 1},
        pluck="name",
        limit=1,
    )[0]
    abbr = frappe.db.get_value("Company", company, "abbr")
    doc = frappe.get_doc(
        {
            "doctype": "Warehouse",
            "warehouse_name": "KoPOS Store",
            "company": company,
            "parent_warehouse": root,
            "is_group": 0,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name if doc.name.endswith(f" - {abbr}") else doc.name


def _ensure_cost_center(company: str) -> str:
    existing = frappe.get_all(
        "Cost Center",
        filters={"company": company, "is_group": 0},
        pluck="name",
        limit=1,
    )
    if existing:
        return existing[0]

    root = frappe.get_all(
        "Cost Center",
        filters={"company": company, "is_group": 1},
        pluck="name",
        limit=1,
    )[0]
    doc = frappe.get_doc(
        {
            "doctype": "Cost Center",
            "cost_center_name": "Main",
            "company": company,
            "parent_cost_center": root,
            "is_group": 0,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_cash_account(company: str) -> str:
    existing = frappe.get_all(
        "Account",
        filters={
            "company": company,
            "account_type": ["in", ["Cash", "Bank"]],
            "is_group": 0,
        },
        pluck="name",
        limit=1,
    )
    if existing:
        return existing[0]
    return _first_name(
        "Account", {"company": company, "root_type": "Asset", "is_group": 0}
    )


def _ensure_expense_account(company: str) -> str:
    existing = frappe.get_all(
        "Account",
        filters={"company": company, "root_type": "Expense", "is_group": 0},
        pluck="name",
        limit=1,
    )
    return existing[0]


def _ensure_mode_of_payment(
    mode_name: str, company: str, account: str, mode_type: str
) -> str:
    existing = frappe.db.exists("Mode of Payment", mode_name)
    if existing:
        doc = frappe.get_doc("Mode of Payment", mode_name)
    else:
        doc = frappe.get_doc(
            {
                "doctype": "Mode of Payment",
                "mode_of_payment": mode_name,
                "enabled": 1,
                "type": mode_type,
            }
        )
        doc.insert(ignore_permissions=True)

    if not any(row.company == company for row in doc.accounts):
        doc.append(
            "accounts",
            {
                "company": company,
                "default_account": account,
            },
        )
        doc.save(ignore_permissions=True)

    return doc.name


def _ensure_pos_profile(
    company: str,
    warehouse: str,
    customer: str,
    write_off_account: str,
    write_off_cost_center: str,
) -> str:
    name = "KoPOS Main"
    existing = frappe.db.exists("POS Profile", name)
    currency = DEMO_CURRENCY
    if existing:
        doc = frappe.get_doc("POS Profile", existing)
        doc.company = company
        doc.currency = currency
        doc.warehouse = warehouse
        doc.customer = customer
        doc.write_off_account = write_off_account
        doc.write_off_cost_center = write_off_cost_center
        doc.write_off_limit = 0
        if not any(row.mode_of_payment == "Cash" for row in doc.payments):
            doc.append("payments", {"mode_of_payment": "Cash", "default": 1})
        if not any(row.mode_of_payment == "DuitNow QR" for row in doc.payments):
            doc.append("payments", {"mode_of_payment": "DuitNow QR", "default": 0})
        doc.save(ignore_permissions=True)
        return doc.name

    doc = frappe.get_doc(
        {
            "doctype": "POS Profile",
            "name": name,
            "company": company,
            "currency": currency,
            "warehouse": warehouse,
            "customer": customer,
            "write_off_account": write_off_account,
            "write_off_cost_center": write_off_cost_center,
            "write_off_limit": 0,
            "payments": [
                {"mode_of_payment": "Cash", "default": 1},
                {"mode_of_payment": "DuitNow QR", "default": 0},
            ],
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_pos_settings() -> None:
    settings = frappe.get_single("POS Settings")
    if settings.invoice_type != "POS Invoice":
        settings.invoice_type = "POS Invoice"
        settings.save(ignore_permissions=True)


def _ensure_pos_opening_entry(company: str, pos_profile: str) -> str:
    existing = frappe.get_all(
        "POS Opening Entry",
        filters={
            "pos_profile": pos_profile,
            "status": ["in", ["Open", "Submitted"]],
            "docstatus": 1,
        },
        pluck="name",
        limit=1,
    )
    if existing:
        return existing[0]

    doc = frappe.get_doc(
        {
            "doctype": "POS Opening Entry",
            "period_start_date": now_datetime(),
            "posting_date": nowdate(),
            "company": company,
            "pos_profile": pos_profile,
            "user": "Administrator",
            "balance_details": [{"mode_of_payment": "Cash", "opening_amount": 100}],
        }
    )
    doc.insert(ignore_permissions=True)
    doc.submit()
    return doc.name


def _ensure_modifier_group() -> str:
    name = "Size"
    existing = frappe.db.exists("KoPOS Modifier Group", {"group_name": name})
    if existing:
        return existing

    doc = frappe.get_doc(
        {
            "doctype": "KoPOS Modifier Group",
            "group_name": name,
            "selection_type": "single",
            "is_required": 1,
            "min_selections": 1,
            "max_selections": 1,
            "display_order": 1,
            "is_active": 1,
            "options": [
                {
                    "option_name": "Regular",
                    "price_adjustment": 0,
                    "is_default": 1,
                    "is_active": 1,
                    "display_order": 1,
                },
                {
                    "option_name": "Large",
                    "price_adjustment": 2,
                    "is_default": 0,
                    "is_active": 1,
                    "display_order": 2,
                },
            ],
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_item(company: str, warehouse: str, modifier_group: str) -> str:
    item_code = DEMO_DRINK_ITEM
    if frappe.db.exists("Item", item_code):
        _ensure_demo_recipe(company)
        return item_code

    item_group = _ensure_item_group()
    doc = frappe.get_doc(
        {
            "doctype": "Item",
            "item_code": item_code,
            "item_name": DEMO_DRINK_NAME,
            "item_group": item_group,
            "stock_uom": "Nos",
            "is_sales_item": 1,
            "is_stock_item": 0,
            "standard_rate": 12,
            "custom_kopos_availability_mode": "auto",
            "custom_kopos_track_stock": 0,
        }
    )
    if hasattr(doc, "modifier_groups"):
        doc.append(
            "modifier_groups", {"modifier_group": modifier_group, "display_order": 1}
        )
    doc.insert(ignore_permissions=True)
    recipe_name = _ensure_demo_recipe(company)
    if hasattr(doc, "custom_fb_recipe_required"):
        doc.custom_fb_recipe_required = 1
    if hasattr(doc, "custom_fb_default_recipe"):
        doc.custom_fb_default_recipe = recipe_name
    if hasattr(doc, "custom_fb_track_theoretical_stock"):
        doc.custom_fb_track_theoretical_stock = 1
    doc.save(ignore_permissions=True)
    return item_code


def _ensure_stock_item() -> str:
    item_group = _ensure_item_group()
    _ensure_uom("Nos")
    ingredient_specs = [
        (DEMO_MATCHA_ITEM, "Matcha Powder", "Gram", 1),
        (DEMO_STRAWBERRY_ITEM, "Strawberry Puree", "Millilitre", 1),
        (DEMO_MILK_ITEM, "Milk", "Millilitre", 1),
        (DEMO_CUP_ITEM, "Cup", "Nos", 1),
    ]
    for item_code, item_name, uom, is_stock in ingredient_specs:
        if frappe.db.exists("Item", item_code):
            continue
        doc = frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": item_code,
                "item_name": item_name,
                "item_group": item_group,
                "stock_uom": uom,
                "is_sales_item": 0,
                "is_stock_item": is_stock,
                "standard_rate": 1,
                "custom_kopos_availability_mode": "auto",
                "custom_kopos_track_stock": 1,
                "custom_kopos_min_qty": 1,
            }
        )
        doc.insert(ignore_permissions=True)
    return DEMO_MATCHA_ITEM


def _ensure_demo_recipe(company: str) -> str:
    if frappe.db.exists("FB Recipe", DEMO_RECIPE_CODE):
        return DEMO_RECIPE_CODE

    _ensure_stock_item()
    recipe = frappe.new_doc("FB Recipe")
    recipe.recipe_code = DEMO_RECIPE_CODE
    recipe.recipe_name = DEMO_DRINK_NAME
    recipe.sellable_item = DEMO_DRINK_ITEM
    recipe.recipe_type = "Finished Drink"
    recipe.status = "Active"
    recipe.version_no = 1
    recipe.company = company
    recipe.yield_qty = 1
    recipe.yield_uom = "Nos"
    recipe.default_serving_qty = 1
    recipe.default_serving_uom = "Nos"
    recipe.append(
        "components",
        {
            "item": DEMO_MATCHA_ITEM,
            "component_type": "Ingredient",
            "qty": 18.0,
            "uom": "Gram",
            "affects_stock": 1,
            "affects_cogs": 1,
        },
    )
    recipe.append(
        "components",
        {
            "item": DEMO_STRAWBERRY_ITEM,
            "component_type": "Ingredient",
            "qty": 40.0,
            "uom": "Millilitre",
            "affects_stock": 1,
            "affects_cogs": 1,
        },
    )
    recipe.append(
        "components",
        {
            "item": DEMO_MILK_ITEM,
            "component_type": "Ingredient",
            "qty": 180.0,
            "uom": "Millilitre",
            "affects_stock": 1,
            "affects_cogs": 1,
        },
    )
    recipe.append(
        "components",
        {
            "item": DEMO_CUP_ITEM,
            "component_type": "Packaging",
            "qty": 1.0,
            "uom": "Nos",
            "affects_stock": 1,
            "affects_cogs": 1,
        },
    )
    recipe.insert(ignore_permissions=True)
    return recipe.name


def _ensure_uom(uom_name: str) -> str:
    if frappe.db.exists("UOM", uom_name):
        return uom_name
    doc = frappe.get_doc({"doctype": "UOM", "uom_name": uom_name})
    doc.insert(ignore_permissions=True)
    return doc.name


def _ensure_item_group() -> str:
    name = "KoPOS Beverages"
    if frappe.db.exists("Item Group", name):
        return name

    parent = frappe.get_all(
        "Item Group", filters={"is_group": 1}, pluck="name", limit=1
    )[0]
    doc = frappe.get_doc(
        {
            "doctype": "Item Group",
            "item_group_name": name,
            "parent_item_group": parent,
            "is_group": 0,
            "show_in_website": 1,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def setup_full_smoke_json(erpnext_url: str | None = None) -> dict[str, Any]:
    return setup_full_smoke_data(erpnext_url=erpnext_url)


def reset_smoke_json() -> dict[str, Any]:
    return reset_smoke_data()


def dump_smoke_json() -> dict[str, Any]:
    return dump_smoke_state()


def _first_name(doctype: str, filters: dict[str, Any]) -> str:
    names = frappe.get_all(doctype, filters=filters, pluck="name", limit=1)
    if not names:
        frappe.throw(f"No {doctype} found for filters: {filters}")
    return names[0]


def _ensure_frappe_user(email: str, display_name: str) -> None:
    existing = frappe.db.exists("User", email)
    if existing:
        return
    doc = frappe.get_doc(
        {
            "doctype": "User",
            "email": email,
            "first_name": display_name,
            "enabled": 1,
            "user_type": "System User",
            "send_welcome_email": 0,
            "new_password": frappe.generate_hash(length=32),
        }
    )
    doc.insert(ignore_permissions=True)


def _ensure_item_modifier_link(item_code: str, modifier_group_name: str) -> None:
    group = frappe.db.exists(
        "KoPOS Modifier Group", {"group_name": modifier_group_name}
    )
    if not group:
        return
    item = frappe.get_doc("Item", item_code)
    if not hasattr(item, "modifier_groups"):
        return
    if any(
        getattr(row, "modifier_group", None) == group for row in item.modifier_groups
    ):
        return
    item.append("modifier_groups", {"modifier_group": group, "display_order": 1})
    item.save(ignore_permissions=True)


def _ensure_promotion_snapshot(pos_profile: str) -> dict[str, Any]:
    from kopos_connector.api.promotions import publish_promotion_snapshot

    return publish_promotion_snapshot(pos_profile=pos_profile)


def _ensure_kopos_device(device_id: str, pos_profile: str, company: str) -> Any:
    from kopos_connector.utils.pin import hash_pin

    _ensure_frappe_user("staff@smoke.kopos.local", "Staff Ahmad")
    _ensure_frappe_user("manager@smoke.kopos.local", "Manager Siti")

    users = [
        {
            "user": "staff@smoke.kopos.local",
            "display_name": "Staff Ahmad",
            "active": 1,
            "default_cashier": 1,
            "pin_hash": hash_pin("1234"),
            "can_manager_override": 0,
            "can_refund": 1,
            "can_void": 0,
            "can_open_shift": 1,
            "can_close_shift": 1,
        },
        {
            "user": "manager@smoke.kopos.local",
            "display_name": "Manager Siti",
            "active": 1,
            "default_cashier": 0,
            "pin_hash": hash_pin("2345"),
            "can_manager_override": 1,
            "can_refund": 1,
            "can_void": 1,
            "can_open_shift": 1,
            "can_close_shift": 1,
        },
    ]

    printers = [
        {
            "role": "receipt",
            "enabled": 1,
            "protocol": "escpos_tcp",
            "host": "receipt-printer",
            "port": 9100,
            "copies": 1,
        },
    ]

    existing = frappe.db.exists("KoPOS Device", {"device_id": device_id})
    if existing:
        doc = frappe.get_doc("KoPOS Device", existing)
        doc.pos_profile = pos_profile
        doc.device_users = []
        doc.printers = []
        for row in users:
            doc.append("device_users", row)
        for row in printers:
            doc.append("printers", row)
        doc.save(ignore_permissions=True)
        return doc

    doc = frappe.get_doc(
        {
            "doctype": "KoPOS Device",
            "device_id": device_id,
            "device_name": "Smoke Test Tablet",
            "device_prefix": "SMK",
            "pos_profile": pos_profile,
            "enabled": 1,
            "allow_training_mode": 1,
            "allow_manual_settings_override": 0,
            "app_min_version": "0.1.0",
            "device_users": users,
            "printers": printers,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc


def setup_full_smoke_data(erpnext_url: str | None = None) -> dict[str, Any]:
    base = setup_refund_smoke_data()
    company = base["company"]

    pos_opening = base.get("pos_opening_entry")
    if pos_opening:
        docstatus = frappe.db.get_value("POS Opening Entry", pos_opening, "docstatus")
        if docstatus == 1:
            frappe.db.set_value(
                "POS Opening Entry", pos_opening, "docstatus", 2, update_modified=False
            )
        frappe.delete_doc(
            "POS Opening Entry", pos_opening, force=True, ignore_permissions=True
        )
        frappe.db.commit()

    device_id = "SMOKE-TAB-A001"
    device_doc = _ensure_kopos_device(
        device_id=device_id,
        pos_profile=base["pos_profile"],
        company=company,
    )

    _ensure_item_modifier_link(base["item_code"], "Size")

    from kopos_connector.api.provisioning import (
        create_pos_provisioning,
        ensure_device_api_credentials,
    )

    credentials = ensure_device_api_credentials(device_doc)

    resolved_url = erpnext_url or frappe.utils.get_url().rstrip("/")
    provisioning = create_pos_provisioning(
        device=device_doc.name,
        erpnext_url=resolved_url,
        api_key=credentials["api_key"],
        api_secret=credentials["api_secret"],
        expires_in_seconds=86400,
    )

    promotion_snapshot = _ensure_promotion_snapshot(base["pos_profile"])

    frappe.db.commit()

    return {
        "erpnext_url": resolved_url,
        "site": frappe.local.site,
        "device_id": device_id,
        "device_name": device_doc.device_name,
        "device_prefix": device_doc.device_prefix,
        "api_key": credentials["api_key"],
        "api_secret": credentials["api_secret"],
        "provisioning_token": provisioning.get("token"),
        "promotion_snapshot": promotion_snapshot,
        "pos_profile": base["pos_profile"],
        "company": company,
        "warehouse": base["warehouse"],
        "currency": DEMO_CURRENCY,
        "item_code": base["item_code"],
        "stock_item_code": DEMO_MATCHA_ITEM,
        "users": [
            {
                "id": "staff@smoke.kopos.local",
                "display_name": "Staff Ahmad",
                "pin": "1234",
            },
            {
                "id": "manager@smoke.kopos.local",
                "display_name": "Manager Siti",
                "pin": "2345",
            },
        ],
    }


def reset_smoke_data() -> dict[str, Any]:
    device_id = "SMOKE-TAB-A001"
    device_name = frappe.db.get_value("KoPOS Device", {"device_id": device_id}, "name")
    if not device_name:
        return setup_full_smoke_data()

    for doctype in ("POS Invoice", "POS Closing Entry", "POS Opening Entry"):
        records = frappe.get_all(
            doctype,
            filters={"custom_kopos_device_id": device_id},
            pluck="name",
        )
        for name in records:
            docstatus = frappe.db.get_value(doctype, name, "docstatus")
            if docstatus == 1:
                frappe.db.set_value(
                    doctype, name, "docstatus", 2, update_modified=False
                )
            frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)

    frappe.db.commit()

    return setup_full_smoke_data()


def dump_smoke_state() -> dict[str, Any]:
    device_id = "SMOKE-TAB-A001"
    device_name = frappe.db.get_value("KoPOS Device", {"device_id": device_id}, "name")
    if not device_name:
        return {"status": "not_seeded", "message": "Run setup_full_smoke_data first"}

    device = frappe.get_doc("KoPOS Device", device_name)
    invoices = frappe.get_all(
        "POS Invoice",
        filters={"custom_kopos_device_id": device_id},
        fields=["name", "grand_total", "docstatus", "posting_date"],
    )
    openings = frappe.get_all(
        "POS Opening Entry",
        filters={"custom_kopos_device_id": device_id, "docstatus": 1},
        pluck="name",
    )

    from frappe.utils.password import get_decrypted_password

    api_user = (device.api_user or "").strip()
    api_key = ""
    api_secret = ""
    if api_user:
        api_key = (frappe.db.get_value("User", api_user, "api_key") or "").strip()
        api_secret = (
            get_decrypted_password(
                "User", api_user, "api_secret", raise_exception=False
            )
            or ""
        ).strip()

    return {
        "status": "ready",
        "site": frappe.local.site,
        "device": {
            "device_id": device_id,
            "enabled": bool(device.enabled),
            "pos_profile": device.pos_profile,
            "config_version": device.config_version,
        },
        "credentials": {
            "api_key": api_key,
            "api_secret": api_secret,
        },
        "data": {
            "items": len(frappe.get_all("Item", filters={"is_sales_item": 1})),
            "modifier_groups": len(frappe.get_all("KoPOS Modifier Group")),
            "open_shift": len(openings) > 0,
            "pos_invoices": len(invoices),
            "invoices": invoices,
            "demo_drink": DEMO_DRINK_ITEM,
            "demo_recipe": DEMO_RECIPE_CODE,
        },
        "endpoints": {
            "base": frappe.utils.get_url().rstrip("/"),
            "ping": "api/method/kopos_connector.api.ping",
            "catalog": "api/method/kopos_connector.api.get_catalog",
            "submit_order": "api/method/kopos_connector.api.submit_order",
        },
    }
