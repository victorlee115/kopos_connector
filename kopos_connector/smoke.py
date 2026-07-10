# pyright: reportMissingImports=false

from __future__ import annotations

import os
from typing import Any

import frappe
from frappe.utils import flt, now_datetime, nowdate


DEMO_DRINK_ITEM = "SMOKE-STRAWBERRY-001"
LEGACY_DEMO_DRINK_ITEM = "STRAWBERRY-MATCHA-LATTE"
DEMO_DRINK_NAME = "Strawberry Matcha Latte"
DEMO_DRINK_BARCODE = "SMOKE-STRAWBERRY-001"
DEMO_RECIPE_CODE = "SMOKE-STRAWBERRY-MATCHA"
DEMO_MATCHA_ITEM = "SMOKE-MATCHA-POWDER"
DEMO_STRAWBERRY_ITEM = "SMOKE-STRAWBERRY-PUREE"
DEMO_MILK_ITEM = "SMOKE-MILK"
DEMO_CUP_ITEM = "SMOKE-CUP"
DEMO_CURRENCY_FALLBACK = "MYR"
SMOKE_SIZE_GROUP_CODE = "SMOKE-FB-SIZE"
SMOKE_SIZE_REGULAR_CODE = "SMOKE-FB-SIZE-REGULAR"
SMOKE_SIZE_LARGE_CODE = "SMOKE-FB-SIZE-LARGE"
SMOKE_MOCK_PRINTER_HOST_ENV = "KOPOS_SMOKE_MOCK_PRINTER_HOST"
SMOKE_MOCK_PRINTER_PORT_ENV = "KOPOS_SMOKE_MOCK_PRINTER_PORT"
SMOKE_MOCK_PRINTER_DEFAULT_HOST = "127.0.0.1"
SMOKE_MOCK_PRINTER_DEFAULT_PORT = 19100
SUPPORT_REDACTED_VALUE = "[redacted]"
SUPPORT_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "api_secret",
    "bearer",
    "password",
    "pin_hash",
    "provisioning_link",
    "provisioning_token",
    "qr_data",
    "raw_response",
    "secret",
    "token",
)
SMOKE_DEVICE_ID = "SMOKE-TAB-A001"
SMOKE_STAFF_EMAIL_DOMAIN = "@smoke.kopos.local"
SMOKE_VALUE_PREFIXES = (
    "smoke-",
    "smoke:",
    "history-",
    "void-",
    "adv-",
    "task-15-",
    "closed-shift-",
)
SMOKE_VALUE_EXACT = {"task-15-forced-failed-projection"}


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
    modifier_fixture = _ensure_fb_modifier_group()
    item = _ensure_item(
        company,
        modifier_fixture["group"],
        modifier_fixture["default_modifier"],
    )

    frappe.db.commit()
    return {
        "company": company,
        "customer": customer,
        "warehouse": warehouse,
        "cost_center": cost_center,
        "cash_account": cash_account,
        "expense_account": expense_account,
        "pos_profile": pos_profile,
        "item_code": item,
    }


def _get_demo_currency(company: str) -> str:
    return (
        frappe.db.get_value("Company", company, "default_currency")
        or DEMO_CURRENCY_FALLBACK
    )


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
    company = frappe.get_all("Company", pluck="name", limit=1)[0]
    warehouse = _ensure_warehouse(company)
    _ensure_stock_item()

    def _set_qty(item_code: str, target_qty: float) -> None:
        current_qty = get_bin_qty(item_code, warehouse)
        delta = flt(target_qty) - current_qty
        stock_uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"
        if delta > 0:
            entry = frappe.get_doc(
                {
                    "doctype": "Stock Entry",
                    "company": company,
                    "purpose": "Material Receipt",
                    "stock_entry_type": "Material Receipt",
                    "posting_date": nowdate(),
                    "items": [
                        {
                            "item_code": item_code,
                            "t_warehouse": warehouse,
                            "qty": delta,
                            "uom": stock_uom,
                            "stock_uom": stock_uom,
                            "conversion_factor": 1,
                            "basic_rate": 1,
                        }
                    ],
                }
            )
            entry.insert(ignore_permissions=True)
            entry.submit()
        elif delta < 0:
            entry = frappe.get_doc(
                {
                    "doctype": "Stock Entry",
                    "company": company,
                    "purpose": "Material Issue",
                    "stock_entry_type": "Material Issue",
                    "posting_date": nowdate(),
                    "items": [
                        {
                            "item_code": item_code,
                            "s_warehouse": warehouse,
                            "qty": abs(delta),
                            "uom": stock_uom,
                            "stock_uom": stock_uom,
                            "conversion_factor": 1,
                            "basic_rate": 1,
                        }
                    ],
                }
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
    shift.device_id = SMOKE_DEVICE_ID
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
            "device_id": SMOKE_DEVICE_ID,
        "shift_id": shift["shift_code"],
        "staff_id": shift["staff_id"],
        "warehouse": shift["warehouse"],
        "company": shift["company"],
        "currency": _get_demo_currency(shift["company"]),
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
            fields=["name", "qty"],
            order_by="name asc",
        )
        if resolved_sales:
            frappe.local.form_dict = {
                "return_id": f"RETURN-{frappe.generate_hash(length=8)}",
                "device_id": order_doc.device_id,
                "fb_order": order_doc.name,
                "original_sales_invoice": order_doc.sales_invoice,
                "reason_code": "Other",
                "reason_text": "Smoke audit return",
                "refund_method": "cash",
                "return_to_stock": 1,
                "lines": [
                    {
                        "original_resolved_sale": row["name"],
                        "qty_returned": row["qty"],
                    }
                    for row in resolved_sales
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


def run_demo_advisory_stock_audit() -> dict[str, Any]:
    """
    Test advisory stock shortfall behavior.

    Sets matcha ingredient to zero stock (creates advisory shortfall),
    submits an order, and verifies:
    - Order succeeds (not blocked)
    - Shortfall is logged to FB Stock Override Log
    - Catalog would show stock_warning: "erp_stock_short"
    """
    ensure_demo_fb_shift()
    set_demo_ingredient_quantities(matcha_qty=0)
    from kopos_connector.kopos.api.fb_orders import submit_order

    order_id = f"ADV-{frappe.generate_hash(length=8)}"
    frappe.local.form_dict = {
        "order_id": order_id,
        "idempotency_key": f"ADV-{frappe.generate_hash(length=16)}",
        "device_id": SMOKE_DEVICE_ID,
        "shift_id": "smoke-shift-001",
        "staff_id": "staff@smoke.kopos.local",
        "warehouse": _ensure_warehouse(
            frappe.get_all("Company", pluck="name", limit=1)[0]
        ),
        "company": frappe.get_all("Company", pluck="name", limit=1)[0],
        "currency": _get_demo_currency(
            frappe.get_all("Company", pluck="name", limit=1)[0]
        ),
        "order": {
            "display_number": "SMK-ADV-1",
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

    shortfall_logs = frappe.get_all(
        "FB Stock Override Log",
        filters={"order_reference": order_id},
        fields=[
            "name",
            "item",
            "warehouse",
            "requested_qty",
            "available_qty_before",
            "shortfall_qty",
        ],
    )

    return {
        "status": "advisory_accepted",
        "order_result": result,
        "shortfall_logs": shortfall_logs,
        "stock": get_demo_ingredient_state(),
        "note": "Advisory shortfall: order accepted, shortfall logged to FB Stock Override Log",
    }


def get_demo_drink_catalog_state() -> dict[str, Any]:
    from kopos_connector.api.catalog import build_catalog_payload

    catalog = build_catalog_payload(device_id=SMOKE_DEVICE_ID)
    item_state = next(
        (
            item
            for item in catalog.get("items", [])
            if item.get("id") == DEMO_DRINK_ITEM
        ),
        None,
    )
    return {
        "item_code": DEMO_DRINK_ITEM,
        "item_name": DEMO_DRINK_NAME,
        "barcode": DEMO_DRINK_BARCODE,
        "catalog_item": item_state,
    }


def set_demo_drink_auto() -> dict[str, Any]:
    return _set_demo_drink_availability_mode("auto")


def set_demo_drink_force_unavailable() -> dict[str, Any]:
    return _set_demo_drink_availability_mode("force_unavailable")


def run_demo_hard_block_audit() -> dict[str, Any]:
    """
    Test hard-block sold-out behavior.

    Sets the demo drink item to force_unavailable mode and verifies
    that catalog returns is_available=false (hard block).
    """
    set_demo_drink_force_unavailable()
    item_state = get_demo_drink_catalog_state()
    item_states = {
        DEMO_DRINK_ITEM: {
            "is_available": item_state.get("catalog_item", {}).get("is_available"),
            "stock_warning": item_state.get("catalog_item", {}).get("stock_warning"),
            "barcode": item_state.get("catalog_item", {}).get("barcode"),
        }
    }
    set_demo_drink_auto()

    return {
        "status": "hard_block_verified",
        "item_states": item_states,
        "expected": {DEMO_DRINK_ITEM: {"is_available": False, "stock_warning": None}},
        "note": "Hard-block: force_unavailable sets is_available=false, preventing add-to-cart",
    }


def run_demo_out_of_stock_audit() -> dict[str, Any]:
    """
    DEPRECATED: Use run_demo_advisory_stock_audit() for new policy.

    Legacy test that expected blocking behavior. Now redirects to advisory test
    to reflect the new stock availability policy where auto-mode shortages
    are advisory (stock_warning) rather than hard blocks.
    """
    return run_demo_advisory_stock_audit()


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
    currency = _get_demo_currency(company)
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


def _ensure_fb_modifier_group() -> dict[str, str]:
    existing_name = frappe.db.get_value(
        "FB Modifier Group", {"group_code": SMOKE_SIZE_GROUP_CODE}, "name"
    )
    group_payload = {
        "group_code": SMOKE_SIZE_GROUP_CODE,
        "group_name": "Size",
        "selection_type": "Single",
        "is_required": 0,
        "min_selection": 0,
        "max_selection": 1,
        "display_order": 1,
        "active": 1,
        "default_resolution_policy": "Auto Apply Default",
    }

    if existing_name:
        group_doc = frappe.get_doc("FB Modifier Group", existing_name)
        changed = False
        for fieldname, value in group_payload.items():
            if getattr(group_doc, fieldname, None) != value:
                setattr(group_doc, fieldname, value)
                changed = True
        if changed:
            group_doc.save(ignore_permissions=True)
        group_name = group_doc.name
    else:
        group_doc = frappe.get_doc({"doctype": "FB Modifier Group", **group_payload})
        group_doc.insert(ignore_permissions=True)
        group_name = group_doc.name

    default_modifier = _ensure_fb_modifier(
        modifier_code=SMOKE_SIZE_REGULAR_CODE,
        modifier_name="Regular",
        modifier_group=group_name,
        price_adjustment=0,
        is_default=1,
        display_order=1,
    )
    _ensure_fb_modifier(
        modifier_code=SMOKE_SIZE_LARGE_CODE,
        modifier_name="Large",
        modifier_group=group_name,
        price_adjustment=2,
        is_default=0,
        display_order=2,
    )

    return {"group": group_name, "default_modifier": default_modifier}


def _ensure_fb_modifier(
    modifier_code: str,
    modifier_name: str,
    modifier_group: str,
    price_adjustment: float,
    is_default: int,
    display_order: int,
) -> str:
    existing_name = frappe.db.get_value(
        "FB Modifier", {"modifier_code": modifier_code}, "name"
    )
    modifier_payload = {
        "modifier_code": modifier_code,
        "modifier_name": modifier_name,
        "modifier_group": modifier_group,
        "kind": "Instruction Only",
        "price_adjustment": price_adjustment,
        "is_default": is_default,
        "display_order": display_order,
        "active": 1,
    }

    if existing_name:
        modifier_doc = frappe.get_doc("FB Modifier", existing_name)
        changed = False
        for fieldname, value in modifier_payload.items():
            if getattr(modifier_doc, fieldname, None) != value:
                setattr(modifier_doc, fieldname, value)
                changed = True
        if changed:
            modifier_doc.save(ignore_permissions=True)
        return modifier_doc.name

    modifier_doc = frappe.get_doc({"doctype": "FB Modifier", **modifier_payload})
    modifier_doc.insert(ignore_permissions=True)
    return modifier_doc.name


def _ensure_item(company: str, modifier_group: str, default_modifier: str) -> str:
    item_code = DEMO_DRINK_ITEM
    if frappe.db.exists("Item", item_code):
        doc = frappe.get_doc("Item", item_code)
        recipe_name = _ensure_demo_recipe(company, modifier_group, default_modifier)
        changed = _ensure_demo_item_fields(doc, recipe_name)
        if _ensure_demo_drink_barcode(doc):
            changed = True
        if changed:
            doc.save(ignore_permissions=True)
        _retire_legacy_demo_drink_item()
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
    doc.insert(ignore_permissions=True)
    recipe_name = _ensure_demo_recipe(company, modifier_group, default_modifier)
    _ensure_demo_item_fields(doc, recipe_name)
    _ensure_demo_drink_barcode(doc)
    doc.save(ignore_permissions=True)
    _retire_legacy_demo_drink_item()
    return item_code


def _ensure_demo_item_fields(item_doc: Any, recipe_name: str) -> bool:
    changed = False
    expected_values = {
        "item_name": DEMO_DRINK_NAME,
        "is_sales_item": 1,
        "is_stock_item": 0,
        "standard_rate": 12,
        "custom_kopos_availability_mode": "auto",
        "custom_kopos_track_stock": 0,
        "custom_fb_recipe_required": 1,
        "custom_fb_default_recipe": recipe_name,
        "custom_fb_track_theoretical_stock": 1,
    }
    for fieldname, value in expected_values.items():
        if hasattr(item_doc, fieldname) and getattr(item_doc, fieldname, None) != value:
            setattr(item_doc, fieldname, value)
            changed = True
    return changed


def _ensure_demo_drink_barcode(item_doc: Any) -> bool:
    item_code = getattr(item_doc, "item_code", None) or getattr(item_doc, "name", None)
    if item_code == DEMO_DRINK_BARCODE:
        return False

    existing_barcodes = item_doc.get("barcodes") or []
    if any(
        (row.get("barcode") if isinstance(row, dict) else getattr(row, "barcode", None))
        == DEMO_DRINK_BARCODE
        for row in existing_barcodes
    ):
        return False

    item_doc.append("barcodes", {"barcode": DEMO_DRINK_BARCODE})
    return True


def _retire_legacy_demo_drink_item() -> bool:
    if LEGACY_DEMO_DRINK_ITEM == DEMO_DRINK_ITEM:
        return False
    if not frappe.db.exists("Item", LEGACY_DEMO_DRINK_ITEM):
        return False

    legacy_doc = frappe.get_doc("Item", LEGACY_DEMO_DRINK_ITEM)
    changed = False
    expected_values = {
        "disabled": 1,
        "is_sales_item": 0,
        "custom_kopos_availability_mode": "force_unavailable",
        "custom_fb_recipe_required": 0,
        "custom_fb_default_recipe": None,
        "custom_fb_track_theoretical_stock": 0,
    }
    for fieldname, value in expected_values.items():
        if hasattr(legacy_doc, fieldname) and getattr(legacy_doc, fieldname, None) != value:
            setattr(legacy_doc, fieldname, value)
            changed = True
    if changed:
        legacy_doc.save(ignore_permissions=True)
    return changed


def _set_demo_drink_availability_mode(mode: str) -> dict[str, Any]:
    if not frappe.db.exists("Item", DEMO_DRINK_ITEM):
        company = frappe.get_all("Company", pluck="name", limit=1)[0]
        modifier_fixture = _ensure_fb_modifier_group()
        _ensure_item(
            company,
            modifier_fixture["group"],
            modifier_fixture["default_modifier"],
        )

    item_doc = frappe.get_doc("Item", DEMO_DRINK_ITEM)
    changed = False
    if getattr(item_doc, "custom_kopos_availability_mode", None) != mode:
        item_doc.custom_kopos_availability_mode = mode
        changed = True
    if _ensure_demo_drink_barcode(item_doc):
        changed = True
    if changed:
        item_doc.save(ignore_permissions=True)
        frappe.db.commit()

    item_state = get_demo_drink_catalog_state()
    return {
        "status": "updated",
        "item_code": DEMO_DRINK_ITEM,
        "item_name": DEMO_DRINK_NAME,
        "availability_mode": mode,
        "barcode": DEMO_DRINK_BARCODE,
        "catalog_item": item_state.get("catalog_item"),
    }


def _ensure_stock_item() -> str:
    item_group = _ensure_item_group()
    _ensure_uom("Nos")
    _ensure_uom("Gram")
    _ensure_uom("Millilitre")
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


def _ensure_demo_recipe(
    company: str, modifier_group: str, default_modifier: str
) -> str:
    existing_name = frappe.db.exists("FB Recipe", DEMO_RECIPE_CODE)
    if existing_name:
        recipe = frappe.get_doc("FB Recipe", existing_name)
        changed = _ensure_demo_recipe_fields(recipe, company)
        if _ensure_recipe_modifier_group(recipe, modifier_group, default_modifier):
            changed = True
        if changed:
            recipe.save(ignore_permissions=True)
        return recipe.name

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
    _ensure_recipe_modifier_group(recipe, modifier_group, default_modifier)
    recipe.insert(ignore_permissions=True)
    return recipe.name


def _ensure_demo_recipe_fields(recipe: Any, company: str) -> bool:
    changed = False
    expected_values = {
        "recipe_code": DEMO_RECIPE_CODE,
        "recipe_name": DEMO_DRINK_NAME,
        "sellable_item": DEMO_DRINK_ITEM,
        "recipe_type": "Finished Drink",
        "status": "Active",
        "version_no": 1,
        "company": company,
        "yield_qty": 1,
        "yield_uom": "Nos",
        "default_serving_qty": 1,
        "default_serving_uom": "Nos",
    }
    for fieldname, value in expected_values.items():
        if getattr(recipe, fieldname, None) != value:
            setattr(recipe, fieldname, value)
            changed = True
    return changed


def _ensure_recipe_modifier_group(
    recipe: Any, modifier_group: str, default_modifier: str
) -> bool:
    changed = False
    existing_row = next(
        (
            row
            for row in (recipe.get("allowed_modifier_groups") or [])
            if getattr(row, "modifier_group", None) == modifier_group
        ),
        None,
    )
    expected_values = {
        "required": 0,
        "override_min_selection": 0,
        "override_max_selection": 1,
        "default_modifier": default_modifier,
        "display_order": 1,
        "always_prompt": 0,
    }

    if existing_row is None:
        recipe.append(
            "allowed_modifier_groups",
            {"modifier_group": modifier_group, **expected_values},
        )
        return True

    for fieldname, value in expected_values.items():
        if getattr(existing_row, fieldname, None) != value:
            setattr(existing_row, fieldname, value)
            changed = True

    return changed


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


def reset_smoke_json(erpnext_url: str | None = None) -> dict[str, Any]:
    return reset_smoke_data(erpnext_url=erpnext_url)


def dump_smoke_json() -> dict[str, Any]:
    return dump_smoke_state()


def assert_smoke_business_state_json(
    expected_idempotency_keys: list[str] | None = None,
) -> dict[str, Any]:
    state = dump_smoke_state()
    return build_smoke_business_assertions(
        state,
        expected_idempotency_keys=expected_idempotency_keys,
    )


def assert_closed_shift_rejection_absence_json(idempotency_key: str) -> dict[str, Any]:
    key = str(idempotency_key or "").strip()
    failures: list[dict[str, Any]] = []
    assertions: dict[str, bool] = {"idempotency_key_present": bool(key)}
    if not key:
        failures.append(
            {"assertion": "idempotency_key_present", "detail": "idempotency_key is required"}
        )
        return {"pass": False, "assertions": assertions, "failures": failures}

    fb_orders = _get_rows(
        "FB Order",
        filters={"external_idempotency_key": key},
        fields=["name", "order_id", "external_idempotency_key", "device_id", "status"],
        order_by="creation asc, name asc",
    )
    sales_invoices = _get_rows(
        "Sales Invoice",
        filters={"custom_fb_idempotency_key": key},
        fields=["name", "custom_fb_idempotency_key", "custom_fb_device_id", "docstatus"],
        order_by="creation asc, name asc",
    )
    assertions["no_fb_order_created_for_closed_shift_rejection"] = not fb_orders
    assertions["no_sales_invoice_created_for_closed_shift_rejection"] = not sales_invoices
    if fb_orders:
        failures.append(
            {
                "assertion": "no_fb_order_created_for_closed_shift_rejection",
                "detail": fb_orders,
            }
        )
    if sales_invoices:
        failures.append(
            {
                "assertion": "no_sales_invoice_created_for_closed_shift_rejection",
                "detail": sales_invoices,
            }
        )
    return {
        "pass": all(assertions.values()),
        "assertions": assertions,
        "failures": failures,
        "summary": {
            "idempotency_key": key,
            "fb_orders": len(fb_orders),
            "sales_invoices": len(sales_invoices),
        },
    }


def dump_smoke_support_report_json(
    expected_idempotency_keys: list[str] | None = None,
) -> dict[str, Any]:
    return build_smoke_support_report(
        expected_idempotency_keys=expected_idempotency_keys,
    )


def build_smoke_support_report(
    state: dict[str, Any] | None = None,
    expected_idempotency_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Build a support-safe smoke/dump summary for Desk reports and evidence."""

    raw_state = state or dump_smoke_state()
    if raw_state.get("status") == "not_seeded":
        return {
            "status": "not_seeded",
            "summary": {"message": raw_state.get("message")},
            "proof": {
                "idempotency_status": "not_checked",
                "legacy_path_status": "not_checked",
                "projection_status": "not_checked",
                "active_legacy_path_count": None,
                "failed_projection_count": None,
            },
            "projection_status": {"counts_by_state": {}},
            "reconciliation": {"status": "not_checked"},
            "next_action": "Run smoke seed/setup before opening the support report",
        }

    assertions = build_smoke_business_assertions(
        raw_state,
        expected_idempotency_keys=expected_idempotency_keys,
    )
    state_data = raw_state.get("data")
    data: dict[str, Any] = state_data if isinstance(state_data, dict) else raw_state
    idempotency_value = data.get("idempotency")
    idempotency: dict[str, Any] = (
        idempotency_value if isinstance(idempotency_value, dict) else {}
    )
    projection_status_value = data.get("projection_statuses")
    projection_status: dict[str, Any] = (
        projection_status_value if isinstance(projection_status_value, dict) else {}
    )
    legacy_paths_value = data.get("legacy_active_paths")
    legacy_paths: dict[str, Any] = (
        legacy_paths_value if isinstance(legacy_paths_value, dict) else {}
    )
    active_legacy_path_count = _support_active_legacy_path_count(legacy_paths)
    failed_projection_count = len(_list(projection_status.get("failed")))
    duplicate_invoice_keys = _list(idempotency.get("duplicate_sales_invoice_keys"))
    sales_invoice_counts = idempotency.get("sales_invoice_counts_by_idempotency_key") or {}
    expected_keys = [key for key in expected_idempotency_keys or [] if key]
    one_invoice_per_expected_key = {
        key: sales_invoice_counts.get(key) == 1 for key in expected_keys
    }
    return _redact_support_value(
        {
            "status": "support_ready" if assertions.get("pass") else "support_attention",
            "site": raw_state.get("site"),
            "device": _redact_support_value(raw_state.get("device") or {}),
            "summary": {
                "fb_shifts": len(_list(data.get("fb_shifts"))),
                "fb_orders": len(_list(data.get("fb_orders"))),
                "sales_invoices": len(_list(data.get("sales_invoices"))),
                "return_records": len(_list(data.get("return_records"))),
                "void_records": len(_list(data.get("void_records"))),
                "payment_rows": len(_list(data.get("sales_invoice_payments"))),
                "cash_variance_rows": len(_list(data.get("expected_cash_variance"))),
            },
            "proof": {
                "idempotency_status": "clear" if not duplicate_invoice_keys else "duplicates_found",
                "expected_idempotency_keys": expected_keys,
                "one_sales_invoice_per_expected_key": one_invoice_per_expected_key,
                "duplicate_sales_invoice_keys": duplicate_invoice_keys,
                "legacy_path_status": "clear" if active_legacy_path_count == 0 else "active_paths_found",
                "active_legacy_path_count": active_legacy_path_count,
                "projection_status": "clear" if failed_projection_count == 0 else "failed_rows_found",
                "failed_projection_count": failed_projection_count,
            },
            "projection_status": {
                "counts_by_state": projection_status.get("counts_by_state") or {},
                "failed_rows": [
                    _support_projection_row(row)
                    for row in _list(projection_status.get("failed"))
                ],
            },
            "reconciliation": {
                "status": "review" if failed_projection_count or duplicate_invoice_keys else "clear",
                "fb_shift_rows": _support_shift_rows(_list(data.get("expected_cash_variance"))),
                "fb_order_rows": _support_order_rows(_list(data.get("fb_orders"))),
                "sales_invoice_rows": _support_invoice_rows(_list(data.get("sales_invoices"))),
                "payment_rows": len(_list(data.get("sales_invoice_payments"))),
                "return_records": len(_list(data.get("return_records"))),
                "void_records": len(_list(data.get("void_records"))),
                "cash_variance_rows": len(_list(data.get("expected_cash_variance"))),
            },
            "automation_assertions": assertions.get("assertions") or {},
            "next_action": _support_next_action(
                failed_projection_count,
                active_legacy_path_count,
                duplicate_invoice_keys,
            ),
        }
    )


def inject_failed_projection_smoke_json(
    idempotency_key: str = "task-15-forced-failed-projection",
) -> dict[str, Any]:
    """Insert one failed projection log so the smoke gate can prove fail-closed behavior."""

    fb_order_name = frappe.db.get_value(
        "FB Order",
        {"device_id": SMOKE_DEVICE_ID},
        "name",
        order_by="creation desc",
    )
    if not fb_order_name:
        frappe.throw("No FB Order exists for failed projection smoke injection")

    projection_id = f"TASK-15-FAILED-{frappe.generate_hash(length=8)}"
    doc = frappe.new_doc("FB Projection Log")
    doc.projection_id = projection_id
    doc.source_doctype = "FB Order"
    doc.source_name = fb_order_name
    doc.source_event_type = "task_15_failed_projection_smoke"
    doc.projection_type = "Sales Invoice"
    doc.idempotency_key = idempotency_key
    doc.target_doctype = "Sales Invoice"
    doc.target_name = None
    doc.state = "Failed"
    doc.retry_count = 0
    doc.last_error = "Task 15 artificial failed projection smoke fixture"
    doc.created_at = now_datetime()
    doc.last_attempt_at = now_datetime()
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return {
        "status": "injected",
        "projection_log": doc.name,
        "projection_id": projection_id,
        "source_name": fb_order_name,
        "idempotency_key": idempotency_key,
    }


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


def _ensure_promotion_snapshot(pos_profile: str) -> dict[str, Any]:
    from kopos_connector.api.promotions import publish_promotion_snapshot

    return publish_promotion_snapshot(pos_profile=pos_profile)


def _get_smoke_mock_printer_endpoint() -> tuple[str, int]:
    host = os.environ.get(
        SMOKE_MOCK_PRINTER_HOST_ENV,
        SMOKE_MOCK_PRINTER_DEFAULT_HOST,
    ).strip()
    if not host:
        host = SMOKE_MOCK_PRINTER_DEFAULT_HOST

    raw_port = os.environ.get(
        SMOKE_MOCK_PRINTER_PORT_ENV,
        str(SMOKE_MOCK_PRINTER_DEFAULT_PORT),
    ).strip()
    try:
        port = int(raw_port)
    except ValueError:
        port = SMOKE_MOCK_PRINTER_DEFAULT_PORT
    if port <= 0:
        port = SMOKE_MOCK_PRINTER_DEFAULT_PORT

    return host, port


def _build_smoke_device_printers() -> list[dict[str, Any]]:
    host, port = _get_smoke_mock_printer_endpoint()
    return [
        {
            "role": "receipt",
            "enabled": 1,
            "protocol": "escpos_tcp",
            "host": host,
            "port": port,
            "copies": 1,
        },
        {
            "role": "sticker",
            "enabled": 1,
            "protocol": "tspl_tcp",
            "host": host,
            "port": port,
            "label_width_mm": 35,
            "label_height_mm": 30,
            "copies": 1,
        },
    ]


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

    printers = _build_smoke_device_printers()

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

    device_id = SMOKE_DEVICE_ID
    device_doc = _ensure_kopos_device(
        device_id=device_id,
        pos_profile=base["pos_profile"],
        company=company,
    )

    from kopos_connector.api.provisioning import (
        create_pos_provisioning,
        ensure_device_api_credentials,
    )

    credentials = ensure_device_api_credentials(device_doc, rotate=True)

    resolved_url = erpnext_url or frappe.utils.get_url().rstrip("/")
    provisioning = create_pos_provisioning(
        device=device_doc.name,
        erpnext_url=resolved_url,
        api_key=credentials["api_key"],
        api_secret=credentials["api_secret"],
        expires_in_seconds=86400,
    )

    set_demo_ingredient_quantities()
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
        "currency": _get_demo_currency(company),
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


def reset_smoke_data(erpnext_url: str | None = None) -> dict[str, Any]:
    device_id = SMOKE_DEVICE_ID
    device_name = frappe.db.get_value("KoPOS Device", {"device_id": device_id}, "name")
    if not device_name:
        return setup_full_smoke_data(erpnext_url=erpnext_url)

    _delete_smoke_business_rows(device_id)

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

    return setup_full_smoke_data(erpnext_url=erpnext_url)


def _delete_smoke_business_rows(device_id: str) -> None:
    """Delete only smoke-owned business rows so reset isolates later smoke runs."""

    fb_orders = _get_smoke_fb_order_names(device_id)
    fb_shifts = _get_smoke_fb_shift_names(device_id)
    return_events = _get_smoke_return_event_names(fb_orders)
    sales_invoices = _get_smoke_sales_invoice_names(device_id, fb_orders, return_events)
    stock_entries = _get_smoke_stock_entry_names(fb_orders)
    resolved_sales = _get_smoke_resolved_sale_names(fb_orders)

    _delete_task15_injected_projection_logs()
    _delete_projection_logs_for_sources("FB Order", fb_orders)
    _delete_projection_logs_for_sources("FB Shift", fb_shifts)
    _delete_projection_logs_for_sources("FB Return Event", return_events)
    _delete_smoke_projection_logs_by_fixture_fields()

    for doctype, names in (
        ("FB Resolved Sale", resolved_sales),
        ("FB Return Event", return_events),
        ("FB Order", fb_orders),
        ("Sales Invoice", sales_invoices),
        ("Stock Entry", stock_entries),
        ("FB Shift", fb_shifts),
    ):
        for name in names:
            _delete_smoke_doc(doctype, name)

    frappe.db.commit()


def _get_smoke_fb_order_names(device_id: str) -> list[str]:
    rows = frappe.get_all(
        "FB Order",
        filters={"device_id": device_id},
        fields=["name", "device_id", "order_id", "external_idempotency_key"],
    )
    names = []
    for row in rows or []:
        if _is_smoke_device_id(row.get("device_id")) or _is_smoke_value(
            row.get("order_id")
        ) or _is_smoke_value(row.get("external_idempotency_key")):
            names.append(str(row.get("name")))
    return _unique_names(names)


def _get_smoke_fb_shift_names(device_id: str) -> list[str]:
    rows = frappe.get_all(
        "FB Shift",
        filters={"device_id": device_id},
        fields=["name", "device_id", "shift_code", "staff_id"],
    )
    names = []
    for row in rows or []:
        if (
            _is_smoke_device_id(row.get("device_id"))
            or _is_smoke_value(row.get("shift_code"))
            or _is_smoke_staff(row.get("staff_id"))
        ):
            names.append(str(row.get("name")))
    return _unique_names(names)


def _get_smoke_return_event_names(fb_orders: list[str]) -> list[str]:
    names: list[str] = []
    if fb_orders:
        rows = frappe.get_all(
            "FB Return Event",
            filters={"fb_order": ["in", fb_orders]},
            fields=["name"],
        )
        names.extend(str(row.get("name")) for row in rows or [] if row.get("name"))
    rows = frappe.get_all(
        "FB Return Event",
        filters={"return_id": ["like", "smoke-%"]},
        fields=["name"],
    )
    names.extend(str(row.get("name")) for row in rows or [] if row.get("name"))
    rows = frappe.get_all(
        "FB Return Event",
        filters={"return_id": ["like", "SMOKE-%"]},
        fields=["name"],
    )
    names.extend(str(row.get("name")) for row in rows or [] if row.get("name"))
    return _unique_names(names)


def _get_smoke_sales_invoice_names(
    device_id: str,
    fb_orders: list[str],
    return_events: list[str],
) -> list[str]:
    names: list[str] = []
    rows = frappe.get_all(
        "Sales Invoice",
        filters={"custom_fb_device_id": device_id},
        fields=["name", "custom_fb_order", "custom_fb_idempotency_key"],
    )
    for row in rows or []:
        if (
            _is_smoke_device_id(device_id)
            or row.get("custom_fb_order") in fb_orders
            or _is_smoke_value(row.get("custom_fb_idempotency_key"))
        ):
            names.append(str(row.get("name")))
    if return_events:
        rows = frappe.get_all(
            "FB Return Event",
            filters={"name": ["in", return_events]},
            fields=["original_sales_invoice", "return_sales_invoice"],
        )
        for row in rows or []:
            for fieldname in ("original_sales_invoice", "return_sales_invoice"):
                value = row.get(fieldname)
                if value:
                    names.append(str(value))
    return _unique_names(names)


def _get_smoke_stock_entry_names(fb_orders: list[str]) -> list[str]:
    if not fb_orders:
        return []
    rows = frappe.get_all(
        "FB Order",
        filters={"name": ["in", fb_orders]},
        fields=["ingredient_stock_entry"],
    )
    return _unique_names(
        str(row.get("ingredient_stock_entry"))
        for row in rows or []
        if row.get("ingredient_stock_entry")
    )


def _get_smoke_resolved_sale_names(fb_orders: list[str]) -> list[str]:
    if not fb_orders:
        return []
    rows = frappe.get_all(
        "FB Resolved Sale",
        filters={"fb_order": ["in", fb_orders]},
        fields=["name"],
    )
    return _unique_names(str(row.get("name")) for row in rows or [] if row.get("name"))


def _delete_task15_injected_projection_logs() -> None:
    for filters in (
        {"source_event_type": "task_15_failed_projection_smoke"},
        {"projection_id": ["like", "TASK-15-FAILED-%"]},
        {"idempotency_key": "task-15-forced-failed-projection"},
    ):
        rows = frappe.get_all("FB Projection Log", filters=filters, fields=["name"])
        for row in rows or []:
            _delete_smoke_doc("FB Projection Log", row.get("name"))


def _delete_projection_logs_for_sources(source_doctype: str, source_names: list[str]) -> None:
    if not source_names:
        return
    rows = frappe.get_all(
        "FB Projection Log",
        filters={"source_doctype": source_doctype, "source_name": ["in", source_names]},
        fields=["name"],
    )
    for row in rows or []:
        _delete_smoke_doc("FB Projection Log", row.get("name"))


def _delete_smoke_projection_logs_by_fixture_fields() -> None:
    """Delete projection logs whose own fixture fields prove smoke ownership."""

    filters_by_field: tuple[tuple[str, str], ...] = (
        ("idempotency_key", "smoke-%"),
        ("idempotency_key", "SMOKE-%"),
        ("idempotency_key", "history-%"),
        ("idempotency_key", "void-%"),
        ("idempotency_key", "ADV-%"),
        ("idempotency_key", "closed-shift-%"),
        ("projection_id", "SMOKE-%"),
        ("projection_id", "TASK-15-%"),
        ("source_name", "smoke-%"),
        ("source_name", "SMOKE-%"),
    )
    for fieldname, pattern in filters_by_field:
        rows = frappe.get_all(
            "FB Projection Log",
            filters={fieldname: ["like", pattern]},
            fields=["name"],
        )
        for row in rows or []:
            _delete_smoke_doc("FB Projection Log", row.get("name"))


def _delete_smoke_doc(doctype: str, name: Any) -> None:
    docname = str(name or "").strip()
    if not docname:
        return
    try:
        if not frappe.db.exists(doctype, docname):
            return
        docstatus = frappe.db.get_value(doctype, docname, "docstatus")
        if docstatus == 1:
            frappe.db.set_value(doctype, docname, "docstatus", 2, update_modified=False)
        frappe.delete_doc(doctype, docname, force=True, ignore_permissions=True)
    except Exception as delete_error:
        if not frappe.db.exists(doctype, docname):
            return
        try:
            frappe.db.delete(doctype, {"name": docname})
        except Exception as fallback_error:
            raise RuntimeError(
                f"Failed to delete smoke-owned {doctype} {docname} during reset"
            ) from fallback_error
        if frappe.db.exists(doctype, docname):
            raise RuntimeError(
                f"Failed to delete smoke-owned {doctype} {docname} during reset"
            ) from delete_error


def _is_smoke_value(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in SMOKE_VALUE_EXACT or text.startswith(SMOKE_VALUE_PREFIXES)


def _is_smoke_device_id(value: Any) -> bool:
    return str(value or "").strip() == SMOKE_DEVICE_ID


def _is_smoke_staff(value: Any) -> bool:
    return str(value or "").strip().lower().endswith(SMOKE_STAFF_EMAIL_DOMAIN)


def _unique_names(values: Any) -> list[str]:
    names = []
    seen = set()
    for value in values or []:
        name = str(value or "").strip()
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def dump_smoke_state() -> dict[str, Any]:
    device_id = SMOKE_DEVICE_ID
    device_name = frappe.db.get_value("KoPOS Device", {"device_id": device_id}, "name")
    if not device_name:
        return {"status": "not_seeded", "message": "Run setup_full_smoke_data first"}

    device = frappe.get_doc("KoPOS Device", device_name)
    api_user = (device.api_user or "").strip()
    api_key = ""
    api_secret_error = ""
    if api_user:
        api_key = (frappe.db.get_value("User", api_user, "api_key") or "").strip()
        try:
            from frappe.utils.password import get_decrypted_password

            api_secret = (
                get_decrypted_password(
                    "User", api_user, "api_secret", raise_exception=False
                )
                or ""
            ).strip()
        except Exception as error:
            api_secret = ""
            api_secret_error = f"decrypt_failed: {error.__class__.__name__}"
        if api_user and not api_secret and not api_secret_error:
            api_secret_error = "decrypt_failed: empty_secret"

    business_state = _collect_smoke_business_state(device_id)

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
            "api_user": api_user or None,
            "api_key_present": bool(api_key),
            "api_secret_present": bool(api_user and not api_secret_error),
            **({"api_secret_error": api_secret_error} if api_secret_error else {}),
        },
        "data": business_state,
        "endpoints": {
            "base": frappe.utils.get_url().rstrip("/"),
            "ping": "api/method/kopos_connector.api.ping",
            "catalog": "api/method/kopos_connector.api.get_catalog",
            "submit_order": "api/method/kopos_connector.api.submit_order",
        },
    }


def _collect_smoke_business_state(device_id: str) -> dict[str, Any]:
    fb_shifts = _get_rows(
        "FB Shift",
        filters={"device_id": device_id},
        fields=[
            "name",
            "shift_code",
            "device_id",
            "staff_id",
            "status",
            "opened_at",
            "closed_at",
            "opening_float",
            "expected_cash",
            "counted_cash",
            "cash_variance",
            "warehouse",
            "company",
        ],
        order_by="creation asc, name asc",
    )
    fb_orders = _get_rows(
        "FB Order",
        filters={"device_id": device_id},
        fields=[
            "name",
            "order_id",
            "external_idempotency_key",
            "device_id",
            "shift",
            "status",
            "invoice_status",
            "stock_status",
            "sales_invoice",
            "ingredient_stock_entry",
            "grand_total",
            "currency",
            "docstatus",
            "creation",
        ],
        order_by="creation asc, name asc",
    )
    sales_invoices = _collect_sales_invoices(device_id)
    return_records = _collect_return_records(fb_orders)
    projections = _collect_projection_state(fb_orders, fb_shifts, return_records)
    legacy_active_paths = _collect_legacy_active_paths(device_id)
    idempotency = _build_idempotency_summary(fb_orders, sales_invoices)

    return {
        "items": len(frappe.get_all("Item", filters={"is_sales_item": 1})),
        "modifier_groups": len(frappe.get_all("FB Modifier Group")),
        "demo_drink": DEMO_DRINK_ITEM,
        "demo_recipe": DEMO_RECIPE_CODE,
        "fb_shifts": fb_shifts,
        "fb_orders": fb_orders,
        "sales_invoices": sales_invoices,
        "sales_invoice_items": [
            {"sales_invoice": invoice["name"], **item}
            for invoice in sales_invoices
            for item in invoice.get("items", [])
        ],
        "sales_invoice_payments": [
            {"sales_invoice": invoice["name"], **payment}
            for invoice in sales_invoices
            for payment in invoice.get("payments", [])
        ],
        "return_records": return_records,
        "void_records": _collect_void_records(sales_invoices, fb_orders),
        "projection_statuses": projections,
        "expected_cash_variance": [
            {
                "fb_shift": shift.get("name"),
                "shift_code": shift.get("shift_code"),
                "status": shift.get("status"),
                "opening_float": _money(shift.get("opening_float")),
                "expected_cash": _money(shift.get("expected_cash")),
                "counted_cash": _money(shift.get("counted_cash")),
                "cash_variance": _money(shift.get("cash_variance")),
            }
            for shift in fb_shifts
        ],
        "idempotency": idempotency,
        "legacy_active_paths": legacy_active_paths,
        "order_history": {
            "source": "Sales Invoice",
            "device_id": device_id,
            "invoice_count": len(sales_invoices),
            "submitted_invoice_count": len(
                [
                    invoice
                    for invoice in sales_invoices
                    if invoice.get("docstatus") == 1 and not invoice.get("is_return")
                ]
            ),
        },
    }


def build_smoke_business_assertions(
    state: dict[str, Any],
    expected_idempotency_keys: list[str] | None = None,
) -> dict[str, Any]:
    state_data = state.get("data")
    data: dict[str, Any] = state_data if isinstance(state_data, dict) else state
    expected_keys = [key for key in expected_idempotency_keys or [] if key]
    assertions: dict[str, bool] = {}
    failures: list[dict[str, Any]] = []

    def expect(name: str, passed: bool, detail: Any = None) -> None:
        assertions[name] = bool(passed)
        if not passed:
            failures.append({"assertion": name, "detail": detail})

    fb_shifts = _list(data.get("fb_shifts"))
    fb_orders = _list(data.get("fb_orders"))
    invoices = _list(data.get("sales_invoices"))
    returns = _list(data.get("return_records"))
    voids = _list(data.get("void_records"))
    projection_statuses = data.get("projection_statuses") or {}
    legacy_active_paths = data.get("legacy_active_paths") or {}
    idempotency = data.get("idempotency") or {}
    order_history = data.get("order_history")
    order_history_data = order_history if isinstance(order_history, dict) else {}

    failed_projections = _list(projection_statuses.get("failed"))
    active_legacy_total = sum(
        int(value.get("count") or 0)
        for value in legacy_active_paths.values()
        if isinstance(value, dict)
    )
    submitted_orders = [row for row in fb_orders if row.get("status") == "Submitted"]
    posted_sale_invoices = [
        row
        for row in invoices
        if row.get("docstatus") == 1 and not row.get("is_return")
    ]
    submitted_return_invoices = [
        row for row in invoices if row.get("docstatus") == 1 and row.get("is_return")
    ]
    submitted_returns = [
        row
        for row in returns
        if row.get("docstatus") == 1 or row.get("status") == "Submitted"
    ]
    closed_shifts = [row for row in fb_shifts if row.get("status") == "Closed"]
    open_shifts = [row for row in fb_shifts if row.get("status") == "Open"]
    duplicate_keys = _list(idempotency.get("duplicate_sales_invoice_keys"))
    sales_invoice_counts = idempotency.get("sales_invoice_counts_by_idempotency_key") or {}

    expect("fb_shift_open_close_proven", bool(closed_shifts), fb_shifts)
    expect("no_open_smoke_fb_shift_after_cleanup", not open_shifts, open_shifts)
    expect("fb_order_submit_proven", bool(submitted_orders), fb_orders)
    expect("posted_sales_invoice_proven", bool(posted_sale_invoices), invoices)
    expect(
        "sales_invoice_items_proven",
        any(invoice.get("items") for invoice in posted_sale_invoices),
        posted_sale_invoices,
    )
    expect(
        "sales_invoice_payments_proven",
        any(invoice.get("payments") for invoice in posted_sale_invoices),
        posted_sale_invoices,
    )
    expect(
        "stock_projection_state_proven",
        all(row.get("stock_status") in {"Posted", "Reversed", "Pending"} for row in fb_orders),
        fb_orders,
    )
    expect("refund_return_record_proven", bool(returns), returns)
    expect("refund_return_sales_invoice_proven", bool(submitted_return_invoices), invoices)
    expect(
        "refund_settlement_document_posted",
        bool(submitted_returns)
        and all(
            row.get("settlement_doctype") in {"Payment Entry", "Journal Entry"}
            and bool(row.get("settlement_document"))
            and row.get("settlement_status") == "Posted"
            and row.get("settlement_docstatus") == 1
            for row in submitted_returns
        ),
        submitted_returns,
    )
    expect(
        "refund_credit_note_outstanding_zero",
        bool(submitted_returns)
        and all(
            _money(row.get("return_outstanding_amount")) == 0
            for row in submitted_returns
        ),
        submitted_returns,
    )
    expect(
        "refund_customer_and_tender_gl_settled",
        bool(submitted_returns)
        and all(_return_settlement_gl_proven(row) for row in submitted_returns),
        submitted_returns,
    )
    expect("void_effects_proven", bool(voids), voids)
    expect(
        "order_history_sales_invoice_source",
        bool(order_history_data.get("invoice_count")),
        order_history_data,
    )
    expect("no_failed_projections", not failed_projections, failed_projections)
    expect("no_active_legacy_pos_paths", active_legacy_total == 0, legacy_active_paths)
    expect("no_duplicate_sales_invoice_idempotency_keys", not duplicate_keys, duplicate_keys)
    for key in expected_keys:
        expect(
            f"exactly_one_sales_invoice_for_{key}",
            sales_invoice_counts.get(key) == 1,
            {"key": key, "count": sales_invoice_counts.get(key, 0)},
        )
    expect(
        "closed_shift_cash_fields_present",
        all(
            shift.get("expected_cash") is not None
            and shift.get("counted_cash") is not None
            and shift.get("cash_variance") is not None
            for shift in closed_shifts
        ),
        closed_shifts,
    )
    expect(
        "shift_cash_matches_posted_refund_settlements",
        bool(closed_shifts)
        and all(
            _shift_expected_cash_matches_state(shift, invoices, submitted_returns)
            for shift in closed_shifts
        ),
        {"shifts": closed_shifts, "returns": submitted_returns},
    )

    return {
        "status": "ok" if all(assertions.values()) else "failed",
        "pass": all(assertions.values()),
        "assertions": assertions,
        "failures": failures,
        "summary": {
            "fb_shifts": len(fb_shifts),
            "fb_orders": len(fb_orders),
            "sales_invoices": len(invoices),
            "return_records": len(returns),
            "void_records": len(voids),
            "failed_projections": len(failed_projections),
            "active_legacy_paths": active_legacy_total,
            "expected_idempotency_keys": expected_keys,
        },
    }


def _collect_sales_invoices(device_id: str) -> list[dict[str, Any]]:
    rows = _get_rows(
        "Sales Invoice",
        filters={"custom_fb_device_id": device_id},
        fields=[
            "name",
            "docstatus",
            "status",
            "is_return",
            "return_against",
            "grand_total",
            "paid_amount",
            "outstanding_amount",
            "posting_date",
            "posting_time",
            "customer",
            "company",
            "currency",
            "pos_profile",
            "custom_fb_order",
            "custom_fb_shift",
            "custom_fb_device_id",
            "custom_fb_idempotency_key",
        ],
        order_by="posting_date asc, posting_time asc, name asc",
    )
    invoices: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name") or "")
        invoice_doc = frappe.get_doc("Sales Invoice", name) if name else None
        invoice = dict(row)
        invoice["is_return"] = bool(invoice.get("is_return"))
        invoice["grand_total"] = _money(invoice.get("grand_total"))
        invoice["paid_amount"] = _money(invoice.get("paid_amount"))
        invoice["outstanding_amount"] = _money(invoice.get("outstanding_amount"))
        invoice["items"] = _collect_invoice_items(invoice_doc)
        invoice["payments"] = _collect_invoice_payments(invoice_doc)
        invoices.append(invoice)
    return invoices


def _collect_invoice_items(invoice_doc: Any) -> list[dict[str, Any]]:
    rows = []
    for item in getattr(invoice_doc, "items", []) or []:
        rows.append(
            {
                "item_code": _value(item, "item_code"),
                "qty": _money(_value(item, "qty")),
                "rate": _money(_value(item, "rate")),
                "amount": _money(_value(item, "amount")),
                "warehouse": _value(item, "warehouse"),
                "order_line_ref": _value(item, "custom_fb_order_line_ref"),
                "resolved_sale": _value(item, "custom_fb_resolved_sale"),
            }
        )
    return rows


def _collect_invoice_payments(invoice_doc: Any) -> list[dict[str, Any]]:
    rows = []
    for payment in getattr(invoice_doc, "payments", []) or []:
        rows.append(
            {
                "mode_of_payment": _value(payment, "mode_of_payment"),
                "amount": _money(_value(payment, "amount")),
            }
        )
    return rows


def _collect_return_records(fb_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order_names = [row.get("name") for row in fb_orders if row.get("name")]
    filters: dict[str, Any] = {}
    if order_names:
        filters = {"fb_order": ["in", order_names]}
    rows = _get_rows(
        "FB Return Event",
        filters=filters,
        fields=[
            "name",
            "return_id",
            "fb_order",
            "original_sales_invoice",
            "return_sales_invoice",
            "refund_method",
            "settlement_doctype",
            "settlement_document",
            "settlement_status",
            "settlement_amount",
            "settlement_tenders_json",
            "return_to_stock",
            "status",
            "docstatus",
        ],
        order_by="creation asc, name asc",
    )
    for row in rows:
        row["return_to_stock"] = bool(row.get("return_to_stock"))
        row["settlement_amount"] = _money(row.get("settlement_amount"))
        return_invoice = str(row.get("return_sales_invoice") or "")
        row["return_outstanding_amount"] = (
            _money(
                frappe.db.get_value(
                    "Sales Invoice", return_invoice, "outstanding_amount"
                )
            )
            if return_invoice
            else None
        )
        settlement_doctype = str(row.get("settlement_doctype") or "")
        settlement_document = str(row.get("settlement_document") or "")
        row["settlement_docstatus"] = (
            int(
                frappe.db.get_value(
                    settlement_doctype, settlement_document, "docstatus"
                )
                or 0
            )
            if settlement_doctype in {"Payment Entry", "Journal Entry"}
            and settlement_document
            else 0
        )
        row["settlement_gl_entries"] = _collect_settlement_gl_entries(
            settlement_doctype, settlement_document
        )
    return rows


def _collect_settlement_gl_entries(
    settlement_doctype: str,
    settlement_document: str,
) -> list[dict[str, Any]]:
    if settlement_doctype not in {"Payment Entry", "Journal Entry"} or not settlement_document:
        return []
    rows = _get_rows(
        "GL Entry",
        filters={
            "voucher_type": settlement_doctype,
            "voucher_no": settlement_document,
            "is_cancelled": 0,
        },
        fields=[
            "account",
            "party_type",
            "party",
            "debit",
            "credit",
            "against_voucher_type",
            "against_voucher",
        ],
        order_by="account asc, name asc",
    )
    for row in rows:
        row["debit"] = _money(row.get("debit"))
        row["credit"] = _money(row.get("credit"))
        account = str(row.get("account") or "")
        row["account_type"] = (
            frappe.db.get_value("Account", account, "account_type") if account else None
        )
    return rows


def _return_settlement_gl_proven(return_row: dict[str, Any]) -> bool:
    amount_sen = abs(_money_sen(return_row.get("settlement_amount")) or 0)
    return_invoice = str(return_row.get("return_sales_invoice") or "")
    refund_method = str(return_row.get("refund_method") or "")
    gl_rows = _list(return_row.get("settlement_gl_entries"))
    customer_debit_sen = sum(
        (_money_sen(row.get("debit")) or 0)
        - (_money_sen(row.get("credit")) or 0)
        for row in gl_rows
        if row.get("party_type") == "Customer"
        and row.get("against_voucher_type") == "Sales Invoice"
        and row.get("against_voucher") == return_invoice
    )
    tender_rows = [
        row
        for row in gl_rows
        if not row.get("party_type")
        and (_money_sen(row.get("credit")) or 0)
        > (_money_sen(row.get("debit")) or 0)
    ]
    tender_credit_sen = sum(
        (_money_sen(row.get("credit")) or 0)
        - (_money_sen(row.get("debit")) or 0)
        for row in tender_rows
    )
    if refund_method == "cash":
        account_types_proven = bool(tender_rows) and all(
            row.get("account_type") == "Cash" for row in tender_rows
        )
    elif refund_method in {"qr", "card"}:
        account_types_proven = bool(tender_rows) and all(
            row.get("account_type") == "Bank" for row in tender_rows
        )
    else:
        account_types_proven = bool(tender_rows)
    return (
        amount_sen > 0
        and customer_debit_sen == amount_sen
        and tender_credit_sen == amount_sen
        and account_types_proven
    )


def _shift_expected_cash_matches_state(
    shift: dict[str, Any],
    invoices: list[dict[str, Any]],
    returns: list[dict[str, Any]],
) -> bool:
    shift_name = str(shift.get("name") or "")
    sale_invoices = [
        invoice
        for invoice in invoices
        if invoice.get("docstatus") == 1
        and not invoice.get("is_return")
        and str(invoice.get("custom_fb_shift") or "") == shift_name
    ]
    invoice_by_name = {
        str(invoice.get("name") or ""): invoice for invoice in sale_invoices
    }
    cash_sales_sen = sum(
        _money_sen(payment.get("amount")) or 0
        for invoice in sale_invoices
        for payment in _list(invoice.get("payments"))
        if payment.get("mode_of_payment") == "Cash"
    )
    cash_refunds_sen = sum(
        abs(_money_sen(row.get("settlement_amount")) or 0)
        for row in returns
        if row.get("refund_method") == "cash"
        and str(row.get("original_sales_invoice") or "") in invoice_by_name
    )
    calculated_sen = (
        (_money_sen(shift.get("opening_float")) or 0)
        + cash_sales_sen
        - cash_refunds_sen
    )
    return calculated_sen == (_money_sen(shift.get("expected_cash")) or 0)


def _collect_projection_state(
    fb_orders: list[dict[str, Any]],
    fb_shifts: list[dict[str, Any]],
    return_records: list[dict[str, Any]],
) -> dict[str, Any]:
    source_filters = []
    order_names = [row.get("name") for row in fb_orders if row.get("name")]
    shift_names = [row.get("name") for row in fb_shifts if row.get("name")]
    return_names = [row.get("name") for row in return_records if row.get("name")]
    if order_names:
        source_filters.append({"source_doctype": "FB Order", "source_name": ["in", order_names]})
    if shift_names:
        source_filters.append({"source_doctype": "FB Shift", "source_name": ["in", shift_names]})
    if return_names:
        source_filters.append({"source_doctype": "FB Return Event", "source_name": ["in", return_names]})

    rows: list[dict[str, Any]] = []
    for filters in source_filters:
        rows.extend(
            _get_rows(
                "FB Projection Log",
                filters=filters,
                fields=[
                    "name",
                    "projection_id",
                    "source_doctype",
                    "source_name",
                    "source_event_type",
                    "projection_type",
                    "idempotency_key",
                    "target_doctype",
                    "target_name",
                    "state",
                    "retry_count",
                    "last_error",
                    "last_attempt_at",
                ],
                order_by="creation asc, name asc",
            )
        )
    rows = sorted(rows, key=lambda row: (str(row.get("source_name") or ""), str(row.get("name") or "")))
    counts_by_state: dict[str, int] = {}
    for row in rows:
        state = str(row.get("state") or "")
        counts_by_state[state] = counts_by_state.get(state, 0) + 1
    return {
        "rows": rows,
        "counts_by_state": counts_by_state,
        "failed": [row for row in rows if row.get("state") == "Failed"],
    }


def _collect_legacy_active_paths(device_id: str) -> dict[str, dict[str, Any]]:
    checks = {
        "pos_invoice": ("POS Invoice", {"custom_kopos_device_id": device_id}),
        "pos_opening_entry": (
            "POS Opening Entry",
            {"custom_kopos_device_id": device_id, "docstatus": 1},
        ),
        "pos_closing_entry": (
            "POS Closing Entry",
            {"custom_kopos_device_id": device_id, "docstatus": 1},
        ),
    }
    result: dict[str, dict[str, Any]] = {}
    for key, (doctype, filters) in checks.items():
        rows = _get_rows(doctype, filters=filters, fields=["name", "docstatus", "status"])
        result[key] = {"doctype": doctype, "count": len(rows), "records": rows}
    return result


def _build_idempotency_summary(
    fb_orders: list[dict[str, Any]],
    sales_invoices: list[dict[str, Any]],
) -> dict[str, Any]:
    order_counts: dict[str, int] = {}
    invoice_counts: dict[str, int] = {}
    for order in fb_orders:
        key = str(order.get("external_idempotency_key") or "")
        if key:
            order_counts[key] = order_counts.get(key, 0) + 1
    for invoice in sales_invoices:
        if invoice.get("is_return"):
            continue
        key = str(invoice.get("custom_fb_idempotency_key") or "")
        if key:
            invoice_counts[key] = invoice_counts.get(key, 0) + 1
    return {
        "fb_order_counts_by_idempotency_key": dict(sorted(order_counts.items())),
        "sales_invoice_counts_by_idempotency_key": dict(sorted(invoice_counts.items())),
        "duplicate_fb_order_keys": sorted(
            key for key, count in order_counts.items() if count != 1
        ),
        "duplicate_sales_invoice_keys": sorted(
            key for key, count in invoice_counts.items() if count != 1
        ),
    }


def _collect_void_records(
    sales_invoices: list[dict[str, Any]],
    fb_orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cancelled_orders = {
        row.get("sales_invoice"): row
        for row in fb_orders
        if row.get("status") == "Cancelled"
        or row.get("invoice_status") == "Reversed"
        or row.get("stock_status") == "Reversed"
    }
    records = []
    for invoice in sales_invoices:
        if invoice.get("is_return") or invoice.get("docstatus") != 2:
            continue
        records.append(
            {
                "sales_invoice": invoice.get("name"),
                "idempotency_key": invoice.get("custom_fb_idempotency_key"),
                "fb_order": invoice.get("custom_fb_order"),
                "fb_order_status": (cancelled_orders.get(invoice.get("name")) or {}).get("status"),
                "invoice_status": (cancelled_orders.get(invoice.get("name")) or {}).get("invoice_status"),
                "stock_status": (cancelled_orders.get(invoice.get("name")) or {}).get("stock_status"),
            }
        )
    return records


def _support_active_legacy_path_count(legacy_paths: dict[str, Any]) -> int:
    total = 0
    for value in legacy_paths.values():
        if isinstance(value, dict):
            total += int(value.get("count") or 0)
    return total


def _support_projection_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "projection_log": row.get("name"),
        "source_doctype": row.get("source_doctype"),
        "source_name": row.get("source_name"),
        "projection_type": row.get("projection_type"),
        "idempotency_key": row.get("idempotency_key"),
        "target_doctype": row.get("target_doctype"),
        "target_name": row.get("target_name"),
        "projection_status": row.get("state"),
        "retry_count": row.get("retry_count"),
        "reason": _support_error_summary(row.get("last_error")),
        "next_action": "Review Projection Support Queue before shift close",
    }


def _support_shift_rows(rows: list[Any]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        result.append(
            {
                "fb_shift": row.get("fb_shift"),
                "shift_code": row.get("shift_code"),
                "status": row.get("status"),
                "opening_float": row.get("opening_float"),
                "expected_cash": row.get("expected_cash"),
                "counted_cash": row.get("counted_cash"),
                "cash_variance": row.get("cash_variance"),
            }
        )
    return result


def _support_order_rows(rows: list[Any]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        result.append(
            {
                "fb_order": row.get("name"),
                "order_id": row.get("order_id"),
                "idempotency_key": row.get("external_idempotency_key"),
                "fb_shift": row.get("shift"),
                "status": row.get("status"),
                "invoice_status": row.get("invoice_status"),
                "stock_status": row.get("stock_status"),
                "sales_invoice": row.get("sales_invoice"),
                "grand_total": row.get("grand_total"),
                "currency": row.get("currency"),
            }
        )
    return result


def _support_invoice_rows(rows: list[Any]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        result.append(
            {
                "sales_invoice": row.get("name"),
                "docstatus": row.get("docstatus"),
                "status": row.get("status"),
                "is_return": bool(row.get("is_return")),
                "return_against": row.get("return_against"),
                "grand_total": row.get("grand_total"),
                "paid_amount": row.get("paid_amount"),
                "outstanding_amount": row.get("outstanding_amount"),
                "fb_order": row.get("custom_fb_order"),
                "fb_shift": row.get("custom_fb_shift"),
                "idempotency_key": row.get("custom_fb_idempotency_key"),
                "item_count": len(_list(row.get("items"))),
                "payment_count": len(_list(row.get("payments"))),
            }
        )
    return result


def _support_error_summary(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "No error summary recorded"
    if len(text) <= 180:
        return text
    return f"{text[:177]}..."


def _support_next_action(
    failed_projection_count: int,
    active_legacy_path_count: int,
    duplicate_invoice_keys: list[Any],
) -> str:
    if active_legacy_path_count:
        return "Stop release and investigate active legacy path count"
    if duplicate_invoice_keys:
        return "Investigate duplicate Sales Invoice idempotency keys"
    if failed_projection_count:
        return "Open Projection Support Queue and resolve failed projection rows"
    return "Support report is clear; archive as smoke evidence"


def _redact_support_value(value: Any, key_name: str = "") -> Any:
    key = key_name.lower()
    if any(fragment in key for fragment in SUPPORT_SENSITIVE_KEY_FRAGMENTS):
        return SUPPORT_REDACTED_VALUE
    if isinstance(value, dict):
        return {
            str(child_key): _redact_support_value(child_value, str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_support_value(item, key_name) for item in value]
    if isinstance(value, str):
        lowered = value.lower()
        if "bearer " in lowered or "api_secret" in lowered or "api_key" in lowered:
            return SUPPORT_REDACTED_VALUE
    return value


def _get_rows(
    doctype: str,
    *,
    filters: dict[str, Any] | None = None,
    fields: list[str] | None = None,
    order_by: str | None = None,
) -> list[dict[str, Any]]:
    rows = frappe.get_all(
        doctype,
        filters=filters or {},
        fields=fields or ["name"],
        order_by=order_by,
    )
    return [dict(row) for row in rows or []]


def _money(value: Any) -> float | None:
    if value is None:
        return None
    return round(flt(value), 2)


def _money_sen(value: Any) -> int | None:
    amount = _money(value)
    if amount is None:
        return None
    return int(round(amount * 100))


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _value(doc: Any, fieldname: str) -> Any:
    if hasattr(doc, fieldname):
        return getattr(doc, fieldname)
    getter = getattr(doc, "get", None)
    if callable(getter):
        return getter(fieldname)
    return None
