# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe
from frappe.utils import flt, now_datetime, nowdate

from kopos_connector.kopos.api.money_contract import (
    persisted_money_to_sen,
    sen_to_decimal,
)


DEMO_DRINK_ITEM = "SMOKE-STRAWBERRY-001"
LEGACY_DEMO_DRINK_ITEM = "STRAWBERRY-MATCHA-LATTE"
DEMO_DRINK_NAME = "Strawberry Matcha Latte"
DEMO_DRINK_BARCODE = "SMOKE-STRAWBERRY-001"
DEMO_RECIPE_CODE = "SMOKE-STRAWBERRY-MATCHA"
DEMO_MATCHA_ITEM = "SMOKE-MATCHA-POWDER"
DEMO_STRAWBERRY_ITEM = "SMOKE-STRAWBERRY-PUREE"
DEMO_MILK_ITEM = "SMOKE-MILK"
DEMO_CUP_ITEM = "SMOKE-CUP"
DEMO_MATCHA_QTY_PER_ORDER = 18
DEMO_STRAWBERRY_QTY_PER_ORDER = 40
DEMO_MILK_QTY_PER_ORDER = 180
DEMO_CUP_QTY_PER_ORDER = 1
SMOKE_ACCEPTANCE_MINIMUM_ORDERS = 500
SMOKE_ACCEPTANCE_STOCK_HEADROOM_MULTIPLIER = 2
SMOKE_ACCEPTANCE_MATCHA_TARGET_QTY = (
    DEMO_MATCHA_QTY_PER_ORDER
    * SMOKE_ACCEPTANCE_MINIMUM_ORDERS
    * SMOKE_ACCEPTANCE_STOCK_HEADROOM_MULTIPLIER
)
SMOKE_ACCEPTANCE_STRAWBERRY_TARGET_QTY = (
    DEMO_STRAWBERRY_QTY_PER_ORDER
    * SMOKE_ACCEPTANCE_MINIMUM_ORDERS
    * SMOKE_ACCEPTANCE_STOCK_HEADROOM_MULTIPLIER
)
SMOKE_ACCEPTANCE_MILK_TARGET_QTY = (
    DEMO_MILK_QTY_PER_ORDER
    * SMOKE_ACCEPTANCE_MINIMUM_ORDERS
    * SMOKE_ACCEPTANCE_STOCK_HEADROOM_MULTIPLIER
)
SMOKE_ACCEPTANCE_CUP_TARGET_QTY = (
    DEMO_CUP_QTY_PER_ORDER
    * SMOKE_ACCEPTANCE_MINIMUM_ORDERS
    * SMOKE_ACCEPTANCE_STOCK_HEADROOM_MULTIPLIER
)
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
    matcha_qty: float = SMOKE_ACCEPTANCE_MATCHA_TARGET_QTY,
    strawberry_qty: float = SMOKE_ACCEPTANCE_STRAWBERRY_TARGET_QTY,
    milk_qty: float = SMOKE_ACCEPTANCE_MILK_TARGET_QTY,
    cup_qty: float = SMOKE_ACCEPTANCE_CUP_TARGET_QTY,
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
        "money_contract_version": "sen_v1",
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
            "subtotal_sen": 1200,
            "tax_amount_sen": 0,
            "rounding_adjustment_sen": 0,
            "total_sen": 1200,
            "items": [
                {
                    "line_id": f"LINE-{frappe.generate_hash(length=8)}",
                    "item_code": DEMO_DRINK_ITEM,
                    "item_name": DEMO_DRINK_NAME,
                    "recipe": DEMO_RECIPE_CODE,
                    "recipe_version": 1,
                    "qty": 1,
                    "unit_price_sen": 1200,
                    "discount_amount_sen": 0,
                    "modifier_total_sen": 0,
                    "line_total_sen": 1200,
                    "modifiers": [],
                }
            ],
            "payments": [
                {
                    "payment_id": "SMOKE-DEMO-PAYMENT-1",
                    "payment_method": "Cash",
                    "amount_sen": 1200,
                    "tendered_amount_sen": 1200,
                    "change_amount_sen": 0,
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
        "money_contract_version": "sen_v1",
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
            "subtotal_sen": 1200,
            "tax_amount_sen": 0,
            "rounding_adjustment_sen": 0,
            "total_sen": 1200,
            "items": [
                {
                    "line_id": f"LINE-{frappe.generate_hash(length=8)}",
                    "item_code": DEMO_DRINK_ITEM,
                    "item_name": DEMO_DRINK_NAME,
                    "recipe": DEMO_RECIPE_CODE,
                    "recipe_version": 1,
                    "qty": 1,
                    "unit_price_sen": 1200,
                    "discount_amount_sen": 0,
                    "modifier_total_sen": 0,
                    "line_total_sen": 1200,
                    "modifiers": [],
                }
            ],
            "payments": [
                {
                    "payment_id": "SMOKE-ADVISORY-PAYMENT-1",
                    "payment_method": "Cash",
                    "amount_sen": 1200,
                    "tendered_amount_sen": 1200,
                    "change_amount_sen": 0,
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
            "qty": DEMO_MATCHA_QTY_PER_ORDER,
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
            "qty": DEMO_STRAWBERRY_QTY_PER_ORDER,
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
            "qty": DEMO_MILK_QTY_PER_ORDER,
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
            "qty": DEMO_CUP_QTY_PER_ORDER,
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
                "ingredient_stock_entries": len(
                    _list(data.get("ingredient_stock_entries"))
                ),
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

    credentials = ensure_device_api_credentials(device_doc)

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
        if not _doctype_has_field(doctype, "custom_kopos_device_id"):
            continue
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

    _delete_orphan_smoke_ledger_artifacts(device_id)
    fb_orders = _get_smoke_fb_order_names(device_id)
    fb_shifts = _get_smoke_fb_shift_names(device_id)
    return_events = _get_smoke_return_event_names(fb_orders)
    sales_invoices = _get_smoke_sales_invoice_names(device_id, fb_orders, return_events)
    resolved_sales = _get_smoke_resolved_sale_names(fb_orders)
    settlement_journal_entries = _get_smoke_settlement_journal_entry_names(
        return_events
    )
    stock_entries = _get_smoke_stock_entry_names(
        fb_orders,
        return_events=return_events,
        resolved_sales=resolved_sales,
    )
    serial_batch_bundles = _get_smoke_serial_batch_bundle_names(stock_entries)

    _delete_task15_injected_projection_logs()
    _delete_projection_logs_for_sources("FB Order", fb_orders)
    _delete_projection_logs_for_sources("FB Shift", fb_shifts)
    _delete_projection_logs_for_sources("FB Return Event", return_events)
    _delete_smoke_projection_logs_by_fixture_fields()
    _cancel_submitted_smoke_stock_entries(stock_entries)

    for doctype, names in (
        ("Serial and Batch Bundle", serial_batch_bundles),
        ("FB Resolved Sale", resolved_sales),
        ("Journal Entry", settlement_journal_entries),
        ("FB Return Event", return_events),
        ("FB Order", fb_orders),
        ("Sales Invoice", sales_invoices),
        ("Stock Entry", stock_entries),
        ("FB Shift", fb_shifts),
    ):
        for name in names:
            _delete_smoke_doc(doctype, name)

    _delete_smoke_ledger_artifacts(
        sales_invoices=sales_invoices,
        settlement_journal_entries=settlement_journal_entries,
        stock_entries=stock_entries,
    )
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


def _get_smoke_settlement_journal_entry_names(
    return_events: list[str],
) -> list[str]:
    if not return_events:
        return []

    names: list[str] = []
    rows = frappe.get_all(
        "FB Return Event",
        filters={"name": ["in", return_events]},
        fields=["settlement_doctype", "settlement_document"],
    )
    names.extend(
        str(row.get("settlement_document"))
        for row in rows or []
        if row.get("settlement_doctype") == "Journal Entry"
        and row.get("settlement_document")
    )

    if _doctype_has_field("Journal Entry", "custom_fb_return_event"):
        linked_rows = frappe.get_all(
            "Journal Entry",
            filters={"custom_fb_return_event": ["in", return_events]},
            fields=["name"],
        )
        names.extend(
            str(row.get("name"))
            for row in linked_rows or []
            if row.get("name")
        )
    return _unique_names(names)


def _get_smoke_stock_entry_names(
    fb_orders: list[str],
    *,
    return_events: list[str] | None = None,
    resolved_sales: list[str] | None = None,
) -> list[str]:
    names: list[str] = []
    if fb_orders:
        rows = frappe.get_all(
            "FB Order",
            filters={"name": ["in", fb_orders]},
            fields=["ingredient_stock_entry"],
        )
        names.extend(
            str(row.get("ingredient_stock_entry"))
            for row in rows or []
            if row.get("ingredient_stock_entry")
        )
    if return_events:
        rows = frappe.get_all(
            "FB Return Event Line",
            filters={"parent": ["in", return_events]},
            fields=["reversal_stock_entry"],
        )
        names.extend(
            str(row.get("reversal_stock_entry"))
            for row in rows or []
            if row.get("reversal_stock_entry")
        )
    if resolved_sales:
        rows = frappe.get_all(
            "FB Resolved Sale",
            filters={"name": ["in", resolved_sales]},
            fields=["stock_entry_issue", "stock_entry_reversal"],
        )
        for row in rows or []:
            names.extend(
                str(value)
                for value in (
                    row.get("stock_entry_issue"),
                    row.get("stock_entry_reversal"),
                )
                if value
            )
    return _unique_names(names)


def _get_smoke_serial_batch_bundle_names(stock_entries: list[str]) -> list[str]:
    if not stock_entries:
        return []
    rows = frappe.get_all(
        "Serial and Batch Bundle",
        filters={
            "voucher_type": "Stock Entry",
            "voucher_no": ["in", stock_entries],
        },
        fields=["name"],
    )
    return _unique_names(
        str(row.get("name")) for row in rows or [] if row.get("name")
    )


def _delete_smoke_ledger_artifacts(
    *,
    sales_invoices: list[str],
    settlement_journal_entries: list[str],
    stock_entries: list[str],
) -> None:
    """Remove only ledger rows whose exact vouchers were proven smoke-owned."""

    for voucher_type, voucher_names in (
        ("Sales Invoice", sales_invoices),
        ("Journal Entry", settlement_journal_entries),
        ("Stock Entry", stock_entries),
    ):
        _delete_and_verify_ledger_rows(
            "GL Entry",
            {
                "voucher_type": voucher_type,
                "voucher_no": ["in", voucher_names],
            },
            voucher_names,
        )

    for voucher_type, voucher_names in (
        ("Sales Invoice", sales_invoices),
        ("Journal Entry", settlement_journal_entries),
    ):
        _delete_and_verify_ledger_rows(
            "Payment Ledger Entry",
            {
                "voucher_type": voucher_type,
                "voucher_no": ["in", voucher_names],
            },
            voucher_names,
        )

    _delete_and_verify_ledger_rows(
        "Payment Ledger Entry",
        {
            "against_voucher_type": "Sales Invoice",
            "against_voucher_no": ["in", sales_invoices],
        },
        sales_invoices,
    )
    _delete_and_verify_ledger_rows(
        "Stock Ledger Entry",
        {
            "voucher_type": "Stock Entry",
            "voucher_no": ["in", stock_entries],
        },
        stock_entries,
    )


def _delete_orphan_smoke_ledger_artifacts(device_id: str) -> None:
    """Purge ledger-only smoke vouchers without trusting a reusable voucher name.

    GL remarks carry the device identifier written by the projection service.  They
    are therefore the only durable ownership evidence after a smoke voucher and
    its FB source documents have already been deleted.  Opaque payment and stock
    ledger rows are removed only when the parent is absent and every GL row for
    that exact voucher carries the same smoke-device marker.
    """

    candidate_rows = frappe.get_all(
        "GL Entry",
        filters={"remarks": ["like", f"%Device ID: {device_id}%"]},
        fields=["name", "voucher_type", "voucher_no", "remarks"],
    )
    candidates_by_voucher: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in candidate_rows or []:
        voucher_type = str(row.get("voucher_type") or "").strip()
        voucher_no = str(row.get("voucher_no") or "").strip()
        if voucher_type not in {"Sales Invoice", "Journal Entry", "Stock Entry"}:
            continue
        if not voucher_no or not _remarks_prove_smoke_device(
            row.get("remarks"), device_id
        ):
            continue
        candidates_by_voucher.setdefault((voucher_type, voucher_no), []).append(row)

    orphan_sales_invoices: list[str] = []
    orphan_journal_entries: list[str] = []
    orphan_stock_entries: list[str] = []
    exact_gl_rows: list[str] = []

    for voucher_key, candidate_voucher_rows in candidates_by_voucher.items():
        voucher_type, voucher_no = voucher_key
        if frappe.db.exists(voucher_type, voucher_no):
            if not _existing_voucher_is_smoke_owned(
                voucher_type, voucher_no, device_id
            ):
                exact_gl_rows.extend(
                    str(row.get("name") or "") for row in candidate_voucher_rows
                )
            continue

        voucher_gl_rows = frappe.get_all(
            "GL Entry",
            filters={"voucher_type": voucher_type, "voucher_no": voucher_no},
            fields=["name", "remarks"],
        )
        if not voucher_gl_rows or not all(
            _remarks_prove_smoke_device(row.get("remarks"), device_id)
            for row in voucher_gl_rows
        ):
            exact_gl_rows.extend(
                str(row.get("name") or "") for row in candidate_voucher_rows
            )
            continue

        if voucher_type == "Sales Invoice":
            orphan_sales_invoices.append(voucher_no)
        elif voucher_type == "Journal Entry":
            orphan_journal_entries.append(voucher_no)
        else:
            orphan_stock_entries.append(voucher_no)

    _delete_and_verify_named_ledger_rows("GL Entry", exact_gl_rows)
    _delete_smoke_ledger_artifacts(
        sales_invoices=_unique_names(orphan_sales_invoices),
        settlement_journal_entries=_unique_names(orphan_journal_entries),
        stock_entries=_unique_names(orphan_stock_entries),
    )


def _remarks_prove_smoke_device(remarks: Any, device_id: str) -> bool:
    device_lines = [
        line.strip()
        for line in str(remarks or "").splitlines()
        if line.strip().startswith("Device ID:")
    ]
    return device_lines == [f"Device ID: {device_id}"]


def _existing_voucher_is_smoke_owned(
    voucher_type: str,
    voucher_no: str,
    device_id: str,
) -> bool:
    if voucher_type == "Sales Invoice":
        current_device_id = frappe.db.get_value(
            voucher_type, voucher_no, "custom_fb_device_id"
        )
        return str(current_device_id or "").strip() == device_id
    if voucher_type in {"Journal Entry", "Stock Entry"}:
        remarks = frappe.db.get_value(voucher_type, voucher_no, "remarks")
        return _remarks_prove_smoke_device(remarks, device_id)
    return False


def _delete_and_verify_named_ledger_rows(
    doctype: str,
    row_names: list[str],
) -> None:
    names = _unique_names(row_names)
    if not names:
        return
    filters = {"name": ["in", names]}
    frappe.db.delete(doctype, filters)
    remaining = frappe.get_all(doctype, filters=filters, fields=["name"], limit=1)
    if remaining:
        raise RuntimeError(f"Smoke reset left proven smoke-owned {doctype} rows")


def _cancel_submitted_smoke_stock_entries(stock_entries: list[str]) -> None:
    """Reverse stock quantities before test-only document and ledger deletion."""

    for stock_entry_name in stock_entries:
        if not frappe.db.exists("Stock Entry", stock_entry_name):
            continue
        docstatus = int(
            frappe.db.get_value("Stock Entry", stock_entry_name, "docstatus") or 0
        )
        if docstatus != 1:
            continue
        stock_entry = frappe.get_doc("Stock Entry", stock_entry_name)
        flags = getattr(stock_entry, "flags", None)
        if flags is not None:
            flags.ignore_links = True
        try:
            stock_entry.cancel()
        except Exception as error:
            raise RuntimeError(
                f"Failed to cancel smoke-owned Stock Entry {stock_entry_name} during reset"
            ) from error
        if int(getattr(stock_entry, "docstatus", 0) or 0) != 2:
            raise RuntimeError(
                f"Smoke-owned Stock Entry {stock_entry_name} did not cancel during reset"
            )


def _delete_and_verify_ledger_rows(
    doctype: str,
    filters: dict[str, Any],
    voucher_names: list[str],
) -> None:
    if not voucher_names:
        return
    frappe.db.delete(doctype, filters)
    remaining = frappe.get_all(doctype, filters=filters, fields=["name"], limit=1)
    if remaining:
        raise RuntimeError(
            f"Smoke reset left {doctype} rows for proven smoke vouchers"
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

    device_profile = _collect_device_profile_evidence(device)
    business_state = _collect_smoke_business_state(
        device_id,
        ingredient_warehouse=device_profile.get("pos_profile_warehouse"),
    )

    return {
        "status": "ready",
        "site": frappe.local.site,
        "site_timezone": frappe.utils.get_system_timezone(),
        "device": {
            "device_id": device_id,
            "enabled": bool(device.enabled),
            "pos_profile": device.pos_profile,
            **device_profile,
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


def _collect_device_profile_evidence(device: Any) -> dict[str, Any]:
    profile_name = str(_value(device, "pos_profile") or "").strip()
    if not profile_name:
        return {
            "pos_profile_resolved": False,
            "pos_profile_company": None,
            "pos_profile_customer": None,
            "pos_profile_warehouse": None,
            "pos_profile_currency": None,
        }
    try:
        profile = frappe.get_cached_doc("POS Profile", profile_name)
    except Exception:
        profile = None
    return {
        "pos_profile_resolved": profile is not None,
        "pos_profile_company": _value(profile, "company") if profile else None,
        "pos_profile_customer": _value(profile, "customer") if profile else None,
        "pos_profile_warehouse": _value(profile, "warehouse") if profile else None,
        "pos_profile_currency": _value(profile, "currency") if profile else None,
    }


def _collect_smoke_business_state(
    device_id: str,
    *,
    ingredient_warehouse: Any = None,
) -> dict[str, Any]:
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
    for shift in fb_shifts:
        for fieldname in (
            "opening_float",
            "expected_cash",
            "counted_cash",
            "cash_variance",
        ):
            shift[fieldname] = _exact_money(shift.get(fieldname))
    fb_orders = _get_rows(
        "FB Order",
        filters={"device_id": device_id},
        fields=[
            "name",
            "order_id",
            "display_number",
            "order_type",
            "catalog_version",
            "external_idempotency_key",
            "device_id",
            "shift",
            "staff_id",
            "booth_warehouse",
            "company",
            "status",
            "invoice_status",
            "stock_status",
            "sales_invoice",
            "ingredient_stock_entry",
            "grand_total",
            "net_total",
            "tax_total",
            "tax_rate",
            "rounding_adjustment",
            "currency",
            "docstatus",
            "sale_datetime",
            "creation",
        ],
        order_by="creation asc, name asc",
    )
    for order in fb_orders:
        for fieldname in (
            "grand_total",
            "net_total",
            "tax_total",
            "rounding_adjustment",
        ):
            order[fieldname] = _exact_money(order.get(fieldname))
        order["tax_rate"] = _decimal_text(order.get("tax_rate"))
        order_name = str(order.get("name") or "")
        order_doc = frappe.get_doc("FB Order", order_name) if order_name else None
        order["items"] = _collect_fb_order_items(order_doc)
        order["payments"] = _collect_fb_order_payments(order_doc)
    sales_invoices = _collect_sales_invoices(device_id)
    ingredient_stock_entries = _collect_ingredient_stock_entries(fb_orders)
    ingredient_bin_balances = _collect_ingredient_bin_balances(
        ingredient_warehouse
    )
    manual_qr_reconciliations = _collect_manual_qr_reconciliations(device_id)
    maybank_qr_transactions = _collect_maybank_qr_transactions(device_id)
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
        "ingredient_stock_entries": ingredient_stock_entries,
        "ingredient_bin_balances": ingredient_bin_balances,
        "manual_qr_reconciliations": manual_qr_reconciliations,
        "maybank_qr_transactions": maybank_qr_transactions,
        "maybank_qr_policy": {
            "payment_expiry_grace_seconds": _maybank_payment_expiry_grace_seconds()
        },
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
    ingredient_stock_entries = _list(data.get("ingredient_stock_entries"))
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
    invoices_by_name = {row.get("name"): row for row in invoices if row.get("name")}
    stock_entries_by_name = {
        row.get("name"): row
        for row in ingredient_stock_entries
        if row.get("name")
    }
    dated_orders = [row for row in fb_orders if row.get("sale_datetime")]

    expect("fb_shift_open_close_proven", bool(closed_shifts), fb_shifts)
    expect(
        "fb_shift_timestamps_ordered",
        bool(closed_shifts)
        and all(_shift_timestamps_in_order(shift) for shift in closed_shifts),
        closed_shifts,
    )
    expect("no_open_smoke_fb_shift_after_cleanup", not open_shifts, open_shifts)
    expect("fb_order_submit_proven", bool(submitted_orders), fb_orders)
    expect(
        "fb_order_sale_datetime_persisted",
        bool(fb_orders) and len(dated_orders) == len(fb_orders),
        fb_orders,
    )
    expect("posted_sales_invoice_proven", bool(posted_sale_invoices), invoices)
    expect(
        "sales_invoice_sale_datetime_preserved",
        bool(dated_orders)
        and all(
            not order.get("sales_invoice")
            or _projection_posts_at_sale_datetime(
                order,
                invoices_by_name.get(order.get("sales_invoice")),
            )
            for order in dated_orders
        ),
        {"orders": dated_orders, "sales_invoices": invoices},
    )
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
    expect(
        "ingredient_stock_entry_sale_datetime_preserved",
        bool(ingredient_stock_entries)
        and all(
            not order.get("ingredient_stock_entry")
            or _projection_posts_at_sale_datetime(
                order,
                stock_entries_by_name.get(order.get("ingredient_stock_entry")),
            )
            for order in dated_orders
        ),
        {"orders": dated_orders, "stock_entries": ingredient_stock_entries},
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
            "ingredient_stock_entries": len(ingredient_stock_entries),
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
            "is_pos",
            "is_return",
            "return_against",
            "update_stock",
            "grand_total",
            "net_total",
            "total_taxes_and_charges",
            "rounding_adjustment",
            "rounded_total",
            "disable_rounded_total",
            "write_off_amount",
            "paid_amount",
            "change_amount",
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
            "custom_fb_void_idempotency_key",
            "custom_fb_void_request_fingerprint",
            "custom_fb_void_manager",
            "custom_fb_void_approval_token_id",
        ],
        order_by="posting_date asc, posting_time asc, name asc",
    )
    invoices: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name") or "")
        invoice_doc = frappe.get_doc("Sales Invoice", name) if name else None
        invoice = dict(row)
        invoice["is_pos"] = bool(invoice.get("is_pos"))
        invoice["is_return"] = bool(invoice.get("is_return"))
        invoice["update_stock"] = bool(invoice.get("update_stock"))
        invoice["disable_rounded_total"] = bool(
            invoice.get("disable_rounded_total")
        )
        invoice["grand_total"] = _exact_money(invoice.get("grand_total"))
        invoice["net_total"] = _exact_money(invoice.get("net_total"))
        invoice["total_taxes_and_charges"] = _exact_money(
            invoice.get("total_taxes_and_charges")
        )
        invoice["rounding_adjustment"] = _exact_money(
            invoice.get("rounding_adjustment")
        )
        invoice["rounded_total"] = _exact_money(invoice.get("rounded_total"))
        invoice["write_off_amount"] = _exact_money(
            invoice.get("write_off_amount")
        )
        invoice["paid_amount"] = _exact_money(invoice.get("paid_amount"))
        invoice["change_amount"] = _exact_money(invoice.get("change_amount"))
        invoice["outstanding_amount"] = _exact_money(
            invoice.get("outstanding_amount")
        )
        invoice["items"] = _collect_invoice_items(invoice_doc)
        invoice["payments"] = _collect_invoice_payments(invoice_doc)
        invoice["taxes"] = _collect_invoice_taxes(invoice_doc)
        invoice["gl_entries"] = _collect_invoice_gl_entries(name)
        invoices.append(invoice)
    return invoices


def _collect_manual_qr_reconciliations(device_id: str) -> list[dict[str, Any]]:
    """Collect only release-safe Manual QR accounting evidence for one device."""

    rows = _get_rows(
        "Manual QR Reconciliation",
        filters={"device_id": device_id},
        fields=[
            "name",
            "status",
            "fb_order",
            "sales_invoice",
            "fb_order_payment",
            "device_id",
            "staff_id",
            "company",
            "currency",
            "business_date",
            "amount_sen",
            "payment_reference",
            "provider_session_id",
            "reconciliation_idempotency_key",
            "suspense_account",
            "evidence_kind",
            "evidence_captured_at",
            "evidence_json",
            "receipt_file",
            "receipt_idempotency_key",
            "receipt_idempotency_fingerprint",
            "receipt_payment_id",
            "receipt_order_id",
            "receipt_amount_sen",
            "receipt_file_name",
            "receipt_file_hash",
            "receipt_captured_at",
            "receipt_uploaded_at",
            "reconciled_by",
            "reconciled_at",
            "reconciliation_note",
            "reconciliation_failed_reason",
        ],
        order_by="creation asc, name asc",
    )
    payment_names = [
        str(row.get("fb_order_payment"))
        for row in rows
        if row.get("fb_order_payment")
    ]
    payment_rows = (
        _get_rows(
            "FB Order Payment",
            filters={"name": ["in", payment_names]},
            fields=[
                "name",
                "parent",
                "source_payment_id",
                "amount",
                "payment_channel_code",
                "reference_no",
                "external_transaction_id",
                "is_manual_confirmation",
                "settlement_status",
                "manual_qr_reconciliation",
                "reconciliation_idempotency_key",
            ],
            order_by="name asc",
        )
        if payment_names
        else []
    )
    payment_by_name = {
        str(payment.get("name")): payment for payment in payment_rows
    }
    for row in rows:
        evidence = _parse_json_record(row.pop("evidence_json", None))
        row["evidence"] = {
            "evidence_kind": evidence.get("evidence_kind"),
            "captured_at": evidence.get("captured_at"),
            "upload_status": evidence.get("upload_status"),
            "reconciliation_status": evidence.get("reconciliation_status"),
            "no_receipt_acknowledged": bool(
                evidence.get("no_receipt_acknowledged")
            ),
            "no_receipt_reason_code": evidence.get("no_receipt_reason_code"),
            "local_confirmation_reference": evidence.get(
                "local_confirmation_reference"
            ),
            "evidence_upload_idempotency_key": evidence.get(
                "evidence_upload_idempotency_key"
            ),
            "reconciliation_idempotency_key": evidence.get(
                "reconciliation_idempotency_key"
            ),
            "evidence_captured_device_id": evidence.get(
                "evidence_captured_device_id"
            ),
        }
        payment = payment_by_name.get(str(row.get("fb_order_payment") or ""))
        row["payment"] = payment
        if payment:
            payment["amount"] = _exact_money(payment.get("amount"))
            payment["payment_id"] = payment.get("source_payment_id")
        row["receipt_file_evidence"] = _collect_receipt_file_evidence(
            row.get("receipt_file")
        )
    return rows


def _collect_receipt_file_evidence(file_document: Any) -> dict[str, Any] | None:
    file_name = str(file_document or "").strip()
    if not file_name:
        return None
    evidence: dict[str, Any] = {
        "name": file_name,
        "exists": False,
        "content_readable": False,
        "file_name": None,
        "is_private": False,
        "attached_to_doctype": None,
        "attached_to_name": None,
        "content_sha256": None,
        "byte_length": None,
    }
    if not frappe.db.exists("File", file_name):
        return evidence
    evidence["exists"] = True
    try:
        file_doc = frappe.get_doc("File", file_name)
        evidence.update(
            {
                "name": _value(file_doc, "name") or file_name,
                "file_name": _value(file_doc, "file_name"),
                "is_private": bool(_value(file_doc, "is_private")),
                "attached_to_doctype": _value(
                    file_doc, "attached_to_doctype"
                ),
                "attached_to_name": _value(file_doc, "attached_to_name"),
            }
        )
        content = file_doc.get_content()
        if isinstance(content, str):
            content = content.encode("utf-8")
        if not isinstance(content, (bytes, bytearray)):
            return evidence
        content_bytes = bytes(content)
        evidence["content_readable"] = True
        evidence["content_sha256"] = hashlib.sha256(content_bytes).hexdigest()
        evidence["byte_length"] = len(content_bytes)
    except Exception:
        # Keep the release-safe failure shape; the acceptance validator rejects
        # unreadable or mismatched File evidence without leaking file content.
        return evidence
    return evidence


def _maybank_payment_expiry_grace_seconds() -> int:
    from kopos_connector.kopos.services.accounting.maybank_payment_service import (
        PAYMENT_EXPIRY_GRACE_SECONDS,
    )

    return int(PAYMENT_EXPIRY_GRACE_SECONDS)


def _collect_maybank_qr_transactions(device_id: str) -> list[dict[str, Any]]:
    """Collect provider-authenticity and exact-consumption evidence without QR data."""

    PAYMENT_EXPIRY_GRACE_SECONDS = _maybank_payment_expiry_grace_seconds()

    rows = _get_rows(
        "Maybank QR Transaction",
        filters={"device_id": device_id},
        fields=[
            "name",
            "transaction_refno",
            "status",
            "maybank_status",
            "sale_amount",
            "sale_amount_sen",
            "fb_order",
            "sales_invoice",
            "outlet_id",
            "device_id",
            "provider",
            "currency",
            "idempotency_key",
            "request_fingerprint",
            "consumption_key",
            "invoice_consumption_key",
            "created_at",
            "business_date",
            "scanned_at",
            "paid_at",
            "consumed_at",
            "expires_at",
            "last_polled_at",
            "poll_count",
        ],
        order_by="creation asc, transaction_refno asc",
    )
    references = [
        str(row.get("transaction_refno"))
        for row in rows
        if row.get("transaction_refno")
    ]
    payment_rows = (
        _get_rows(
            "FB Order Payment",
            filters={"external_transaction_id": ["in", references]},
            fields=[
                "name",
                "parent",
                "source_payment_id",
                "amount",
                "payment_channel_code",
                "reference_no",
                "external_transaction_id",
                "is_manual_confirmation",
                "maybank_qr_transaction",
                "settlement_status",
            ],
            order_by="name asc",
        )
        if references
        else []
    )
    payment_by_reference = {
        str(payment.get("external_transaction_id")): payment
        for payment in payment_rows
    }
    for row in rows:
        payment = payment_by_reference.get(str(row.get("transaction_refno") or ""))
        row["payment"] = payment
        if payment:
            payment["amount"] = _exact_money(payment.get("amount"))
            payment["payment_id"] = payment.get("source_payment_id")
        paid_at = frappe.utils.get_datetime(row.get("paid_at")) if row.get("paid_at") else None
        expires_at = (
            frappe.utils.get_datetime(row.get("expires_at"))
            if row.get("expires_at")
            else None
        )
        seconds_after_expiry = (
            (paid_at - expires_at).total_seconds()
            if paid_at is not None and expires_at is not None and paid_at > expires_at
            else 0
        )
        row["payment_expiry_grace_seconds"] = PAYMENT_EXPIRY_GRACE_SECONDS
        row["paid_seconds_after_expiry"] = seconds_after_expiry
        row["paid_after_expiry"] = bool(seconds_after_expiry > 0)
        row["paid_after_expiry_within_grace"] = bool(
            0 < seconds_after_expiry <= PAYMENT_EXPIRY_GRACE_SECONDS
        )
    return rows


def _collect_ingredient_stock_entries(
    fb_orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    names = [
        str(order.get("ingredient_stock_entry"))
        for order in fb_orders
        if order.get("ingredient_stock_entry")
    ]
    return _collect_stock_entries(names)


def _collect_stock_entries(entry_names: list[str]) -> list[dict[str, Any]]:
    """Collect exact Stock Entry, item-detail, and SLE lifecycle evidence."""

    names = _unique_names(entry_names)
    if not names:
        return []
    rows = _get_rows(
        "Stock Entry",
        filters={"name": ["in", names]},
        fields=[
            "name",
            "docstatus",
            "purpose",
            "stock_entry_type",
            "posting_date",
            "posting_time",
            "custom_fb_order",
            "custom_fb_shift",
        ],
        order_by="posting_date asc, posting_time asc, name asc",
    )
    for row in rows:
        entry_name = str(row.get("name") or "")
        entry_doc = frappe.get_doc("Stock Entry", entry_name) if entry_name else None
        row["items"] = [
            {
                "name": _value(item, "name"),
                "item_code": _value(item, "item_code"),
                "qty": _decimal_text(_value(item, "qty")),
                "transfer_qty": _decimal_text(_value(item, "transfer_qty")),
                "stock_uom": _value(item, "stock_uom"),
                "s_warehouse": _value(item, "s_warehouse"),
                "t_warehouse": _value(item, "t_warehouse"),
            }
            for item in (getattr(entry_doc, "items", None) or [])
        ]
        row["stock_ledger_entries"] = _collect_stock_ledger_entries(
            entry_name,
            include_cancelled=int(row.get("docstatus") or 0) == 2,
        )
    return rows


def _collect_stock_ledger_entries(
    entry_name: str, *, include_cancelled: bool = False
) -> list[dict[str, Any]]:
    if not entry_name:
        return []
    filters: dict[str, Any] = {
        "voucher_type": "Stock Entry",
        "voucher_no": entry_name,
    }
    if not include_cancelled:
        filters["is_cancelled"] = 0
    rows = _get_rows(
        "Stock Ledger Entry",
        filters=filters,
        fields=[
            "name",
            "voucher_type",
            "voucher_no",
            "voucher_detail_no",
            "item_code",
            "warehouse",
            "actual_qty",
            "qty_after_transaction",
            "is_cancelled",
            "posting_datetime",
            "creation",
        ],
        order_by="posting_datetime asc, creation asc, name asc",
    )
    for row in rows:
        row["actual_qty"] = _decimal_text(row.get("actual_qty"))
        row["qty_after_transaction"] = _decimal_text(
            row.get("qty_after_transaction")
        )
        row["is_cancelled"] = bool(row.get("is_cancelled"))
        for fieldname in ("posting_datetime", "creation"):
            if row.get(fieldname) is not None:
                row[fieldname] = str(row[fieldname])
    return rows


def _collect_ingredient_bin_balances(warehouse: Any) -> list[dict[str, Any]]:
    warehouse_name = str(warehouse or "").strip()
    if not warehouse_name:
        return []
    tracked_items = _get_rows(
        "Item",
        filters={"is_stock_item": 1, "custom_kopos_track_stock": 1},
        fields=["item_code"],
        order_by="item_code asc",
    )
    item_codes = _unique_names(row.get("item_code") for row in tracked_items)
    if not item_codes:
        return []
    bin_rows = _get_rows(
        "Bin",
        filters={
            "warehouse": warehouse_name,
            "item_code": ["in", item_codes],
        },
        fields=["name", "item_code", "warehouse", "actual_qty"],
        order_by="item_code asc, warehouse asc, name asc",
    )
    bin_by_item = {
        str(row.get("item_code") or "").strip(): row
        for row in bin_rows
        if str(row.get("item_code") or "").strip()
    }
    balances: list[dict[str, Any]] = []
    for item_code in item_codes:
        bin_row = bin_by_item.get(item_code, {})
        balances.append(
            {
                "name": bin_row.get("name"),
                "item_code": item_code,
                "warehouse": warehouse_name,
                "actual_qty": _decimal_text(bin_row.get("actual_qty") or 0),
            }
        )
    return balances


def _collect_fb_order_items(order_doc: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in getattr(order_doc, "items", None) or []:
        rows.append(
            {
                "line_id": _value(line, "line_id"),
                "item": _value(line, "item"),
                "item_name_snapshot": _value(line, "item_name_snapshot"),
                "qty": _decimal_text(_value(line, "qty")),
                "uom": _value(line, "uom"),
                "unit_price": _exact_money(_value(line, "unit_price")),
                "modifier_total": _exact_money(_value(line, "modifier_total")),
                "discount_amount": _exact_money(
                    _value(line, "discount_amount")
                ),
                "line_total": _exact_money(_value(line, "line_total")),
                "recipe": _value(line, "recipe"),
                "recipe_version": _value(line, "recipe_version"),
                "is_recipe_managed": bool(_value(line, "is_recipe_managed")),
                "resolved_sale": _value(line, "resolved_sale"),
                "resolved_components": _parse_json_list(
                    _value(line, "resolved_components_snapshot")
                ),
                "selected_modifiers": [
                    {
                        "modifier_group": _value(modifier, "modifier_group"),
                        "modifier": _value(modifier, "modifier"),
                        "price_adjustment": _exact_money(
                            _value(modifier, "price_adjustment")
                        ),
                        "affects_stock": bool(
                            _value(modifier, "affects_stock")
                        ),
                        "affects_recipe": bool(
                            _value(modifier, "affects_recipe")
                        ),
                    }
                    for modifier in (
                        getattr(line, "selected_modifiers", None) or []
                    )
                ],
            }
        )
    return rows


def _collect_fb_order_payments(order_doc: Any) -> list[dict[str, Any]]:
    return [
        {
            "name": _value(payment, "name"),
            "payment_id": _value(payment, "source_payment_id"),
            "payment_method": _value(payment, "payment_method"),
            "payment_channel_code": _value(payment, "payment_channel_code"),
            "amount": _exact_money(_value(payment, "amount")),
            "tendered_amount": _exact_money(
                _value(payment, "tendered_amount"), optional=True
            ),
            "change_amount": _exact_money(
                _value(payment, "change_amount"), optional=True
            ),
            "reference_no": _value(payment, "reference_no"),
            "external_transaction_id": _value(
                payment, "external_transaction_id"
            ),
            "settlement_status": _value(payment, "settlement_status"),
        }
        for payment in (getattr(order_doc, "payments", None) or [])
    ]


def _parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _parse_json_record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _projection_posts_at_sale_datetime(
    order: dict[str, Any],
    projection: dict[str, Any] | None,
) -> bool:
    if not projection or not order.get("sale_datetime"):
        return False

    try:
        sale_datetime = frappe.utils.get_datetime(order["sale_datetime"])
    except (TypeError, ValueError, OverflowError):
        return False

    if not sale_datetime:
        return False

    posting_date = str(projection.get("posting_date") or "")[:10]
    posting_time = _posting_time_text(projection.get("posting_time"))
    return (
        sale_datetime.date().isoformat() == posting_date
        and sale_datetime.strftime("%H:%M:%S") == posting_time
    )


def _shift_timestamps_in_order(shift: dict[str, Any]) -> bool:
    if not shift.get("opened_at") or not shift.get("closed_at"):
        return False
    try:
        opened_at = frappe.utils.get_datetime(shift["opened_at"])
        closed_at = frappe.utils.get_datetime(shift["closed_at"])
    except (TypeError, ValueError, OverflowError):
        return False
    if not opened_at or not closed_at:
        return False
    try:
        return closed_at >= opened_at
    except TypeError:
        return False


def _posting_time_text(value: Any) -> str:
    if value is None:
        return ""
    total_seconds = getattr(value, "total_seconds", None)
    if callable(total_seconds):
        seconds = int(total_seconds()) % (24 * 60 * 60)
        return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    strftime = getattr(value, "strftime", None)
    if callable(strftime):
        return strftime("%H:%M:%S")
    text = str(value).strip().split(".", 1)[0]
    parts = text.split(":")
    if len(parts) == 2:
        parts.append("00")
    if len(parts) != 3:
        return text
    try:
        return ":".join(f"{int(part):02d}" for part in parts)
    except ValueError:
        return text


def _collect_invoice_items(invoice_doc: Any) -> list[dict[str, Any]]:
    rows = []
    for item in getattr(invoice_doc, "items", []) or []:
        rows.append(
            {
                "item_code": _value(item, "item_code"),
                "qty": _decimal_text(_value(item, "qty")),
                "rate": _decimal_text(_value(item, "rate")),
                "amount": _exact_money(_value(item, "amount")),
                "net_rate": _decimal_text(_value(item, "net_rate")),
                "net_amount": _exact_money(_value(item, "net_amount")),
                "warehouse": _value(item, "warehouse"),
                "income_account": _value(item, "income_account"),
                "cost_center": _value(item, "cost_center"),
                "project": _value(item, "project"),
                "order_line_ref": _value(item, "custom_fb_order_line_ref"),
                "resolved_sale": _value(item, "custom_fb_resolved_sale"),
                "modifier_total": _exact_money(
                    _value(item, "custom_kopos_modifier_total")
                ),
                "has_modifiers": bool(
                    _value(item, "custom_kopos_has_modifiers")
                ),
                "modifiers_json": _value(item, "custom_kopos_modifiers"),
            }
        )
    return rows


def _collect_invoice_payments(invoice_doc: Any) -> list[dict[str, Any]]:
    rows = []
    for payment in getattr(invoice_doc, "payments", []) or []:
        rows.append(
            {
                "mode_of_payment": _value(payment, "mode_of_payment"),
                "amount": _exact_money(_value(payment, "amount")),
                "account": _value(payment, "account"),
                "payment_id": _value(payment, "custom_fb_source_payment_id"),
            }
        )
    return rows


def _collect_invoice_taxes(invoice_doc: Any) -> list[dict[str, Any]]:
    rows = []
    for tax in getattr(invoice_doc, "taxes", []) or []:
        rows.append(
            {
                "charge_type": _value(tax, "charge_type"),
                "account_head": _value(tax, "account_head"),
                "tax_amount": _exact_money(_value(tax, "tax_amount")),
                "tax_amount_after_discount_amount": _exact_money(
                    _value(tax, "tax_amount_after_discount_amount")
                ),
                "included_in_print_rate": bool(
                    _value(tax, "included_in_print_rate")
                ),
            }
        )
    return rows


def _collect_invoice_gl_entries(invoice_name: str) -> list[dict[str, Any]]:
    """Collect exact submitted-ledger evidence for one Sales Invoice."""

    if not invoice_name:
        return []
    rows = _get_rows(
        "GL Entry",
        filters={
            "voucher_type": "Sales Invoice",
            "voucher_no": invoice_name,
        },
        fields=[
            "name",
            "posting_date",
            "account",
            "account_currency",
            "debit",
            "credit",
            "debit_in_account_currency",
            "credit_in_account_currency",
            "party_type",
            "party",
            "against",
            "voucher_type",
            "voucher_no",
            "against_voucher_type",
            "against_voucher",
            "cost_center",
            "project",
            "remarks",
            "is_cancelled",
        ],
        order_by="posting_date asc, creation asc, name asc",
    )
    for row in rows:
        row["debit"] = _exact_money(row.get("debit"))
        row["credit"] = _exact_money(row.get("credit"))
        row["debit_in_account_currency"] = _exact_money(
            row.get("debit_in_account_currency")
        )
        row["credit_in_account_currency"] = _exact_money(
            row.get("credit_in_account_currency")
        )
        row["is_cancelled"] = bool(row.get("is_cancelled"))
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
            "request_fingerprint",
            "approval_token_id",
            "approved_by_manager",
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
    return_names = _unique_names(row.get("name") for row in rows)
    line_rows = (
        _get_rows(
            "FB Return Event Line",
            filters={"parent": ["in", return_names]},
            fields=[
                "name",
                "parent",
                "idx",
                "original_resolved_sale",
                "qty_returned",
                "reversal_stock_entry",
            ],
            order_by="parent asc, idx asc, name asc",
        )
        if return_names
        else []
    )
    lines_by_parent: dict[str, list[dict[str, Any]]] = {}
    for line in line_rows:
        parent = str(line.get("parent") or "")
        if not parent:
            continue
        lines_by_parent.setdefault(parent, []).append(
            {
                "name": line.get("name"),
                "original_resolved_sale": line.get("original_resolved_sale"),
                "qty_returned": _decimal_text(line.get("qty_returned")),
                "reversal_stock_entry": line.get("reversal_stock_entry"),
            }
        )

    reversal_entries = _collect_stock_entries(
        _unique_names(
            line.get("reversal_stock_entry")
            for line in line_rows
            if line.get("reversal_stock_entry")
        )
    )
    reversal_by_name = {
        str(entry.get("name") or ""): entry
        for entry in reversal_entries
        if entry.get("name")
    }
    for row in rows:
        row["return_to_stock"] = bool(row.get("return_to_stock"))
        row["settlement_amount"] = _money(row.get("settlement_amount"))
        lines = lines_by_parent.get(str(row.get("name") or ""), [])
        row["lines"] = lines
        row["reversal_stock_entries"] = [
            reversal_by_name[name]
            for name in _unique_names(
                line.get("reversal_stock_entry") for line in lines
            )
            if name in reversal_by_name
        ]
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
    cash_sales_sen = 0
    for invoice in sale_invoices:
        invoice_cash_tender_sen = sum(
            _money_sen(payment.get("amount")) or 0
            for payment in _list(invoice.get("payments"))
            if payment.get("mode_of_payment") == "Cash"
        )
        if invoice_cash_tender_sen:
            cash_sales_sen += invoice_cash_tender_sen - (
                _money_sen(invoice.get("change_amount")) or 0
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
        if not _doctype_has_field(doctype, "custom_kopos_device_id"):
            result[key] = {
                "doctype": doctype,
                "device_field_present": False,
                "count": 0,
                "records": [],
            }
            continue
        rows = _get_rows(doctype, filters=filters, fields=["name", "docstatus", "status"])
        result[key] = {
            "doctype": doctype,
            "device_field_present": True,
            "count": len(rows),
            "records": rows,
        }
    return result


def _doctype_has_field(doctype: str, fieldname: str) -> bool:
    """Verify metadata and storage before filtering on quarantined fields."""

    return bool(
        frappe.get_meta(doctype).has_field(fieldname)
        and frappe.db.has_column(doctype, fieldname)
    )


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
                "sale_idempotency_key": invoice.get(
                    "custom_fb_idempotency_key"
                ),
                "idempotency_key": invoice.get("custom_fb_idempotency_key"),
                "void_idempotency_key": invoice.get(
                    "custom_fb_void_idempotency_key"
                ),
                "void_request_fingerprint": invoice.get(
                    "custom_fb_void_request_fingerprint"
                ),
                "void_manager": invoice.get("custom_fb_void_manager"),
                "void_approval_token_id": invoice.get(
                    "custom_fb_void_approval_token_id"
                ),
                "invoice_docstatus": invoice.get("docstatus"),
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
                "sale_datetime": row.get("sale_datetime"),
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
                "posting_date": row.get("posting_date"),
                "posting_time": row.get("posting_time"),
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


def _exact_money(value: Any, *, optional: bool = False) -> str | None:
    if value is None or value == "":
        if optional:
            return None
        value = 0
    amount_sen = persisted_money_to_sen(value, "smoke evidence money")
    return format(sen_to_decimal(amount_sen), ".2f")


def _decimal_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        decimal_value = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise ValueError("smoke evidence quantity must be an exact decimal") from error
    if not decimal_value.is_finite():
        raise ValueError("smoke evidence quantity must be finite")
    return format(decimal_value, "f")


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
