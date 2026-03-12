from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt, now_datetime, nowdate


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
        "item_code": "STOCK-MATCHA",
        "actual_qty": get_bin_qty("STOCK-MATCHA", warehouse),
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
    currency = frappe.db.get_value("Company", company, "default_currency") or "USD"
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
            "payments": [{"mode_of_payment": "Cash", "default": 1}],
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
    item_code = "ICED-MATCHA"
    if frappe.db.exists("Item", item_code):
        return item_code

    item_group = _ensure_item_group()
    doc = frappe.get_doc(
        {
            "doctype": "Item",
            "item_code": item_code,
            "item_name": "Iced Matcha Latte",
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
    return item_code


def _ensure_stock_item() -> str:
    item_code = "STOCK-MATCHA"
    if frappe.db.exists("Item", item_code):
        return item_code

    item_group = _ensure_item_group()
    doc = frappe.get_doc(
        {
            "doctype": "Item",
            "item_code": item_code,
            "item_name": "Stock Matcha Latte",
            "item_group": item_group,
            "stock_uom": "Nos",
            "is_sales_item": 1,
            "is_stock_item": 1,
            "standard_rate": 10,
            "custom_kopos_availability_mode": "auto",
            "custom_kopos_track_stock": 1,
            "custom_kopos_min_qty": 1,
        }
    )
    doc.insert(ignore_permissions=True)
    return item_code


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


def _first_name(doctype: str, filters: dict[str, Any]) -> str:
    names = frappe.get_all(doctype, filters=filters, pluck="name", limit=1)
    if not names:
        frappe.throw(f"No {doctype} found for filters: {filters}")
    return names[0]
