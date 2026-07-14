# pyright: reportMissingImports=false

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import frappe

from kopos_connector.api.devices import elevate_device_api_user
from kopos_connector.kopos.services.accounting.return_invoice_service import (
    create_return_sales_invoice,
    lock_fb_shift_cash_scope,
    refresh_fb_shift_cash,
)
from kopos_connector.kopos.services.accounting.return_settlement_service import (
    ensure_return_settlement,
)
from kopos_connector.kopos.services.inventory.stock_reversal_service import (
    create_reversal_stock_entry,
)


def process_return_event(doc: Any) -> tuple[str | None, str | None]:
    original_invoice = frappe.get_doc(
        "Sales Invoice", getattr(doc, "original_sales_invoice", None)
    )
    shift_name = getattr(original_invoice, "custom_fb_shift", None)
    lock_fb_shift_cash_scope(shift_name)
    with elevate_device_api_user():
        return_invoice = create_return_sales_invoice(doc)
        if not return_invoice:
            frappe.throw(
                "Return Sales Invoice projection failed for FB Return Event {0}".format(
                    getattr(doc, "name", "")
                ),
                frappe.ValidationError,
            )
        settlement_document = ensure_return_settlement(doc, return_invoice)
        reversal_entry = create_reversal_stock_entry(doc)
    if not settlement_document:
        frappe.throw(
            "Accounting settlement failed for FB Return Event {0}".format(
                getattr(doc, "name", "")
            ),
            frappe.ValidationError,
        )
    if int(getattr(doc, "return_to_stock", 0) or 0) and not reversal_entry:
        frappe.throw(
            "Stock reversal projection failed for FB Return Event {0}".format(
                getattr(doc, "name", "")
            ),
            frappe.ValidationError,
        )
    refresh_fb_shift_cash(shift_name)
    _update_resolved_sale_statuses(doc)
    return return_invoice, reversal_entry


def ensure_existing_return_event_settlement(doc: Any) -> str:
    """Recover missing proof for an already-submitted idempotent return."""
    return_invoice = getattr(doc, "return_sales_invoice", None)
    if not return_invoice:
        frappe.throw(
            f"FB Return Event {getattr(doc, 'name', '')} has no return Sales Invoice",
            frappe.ValidationError,
        )
    original_invoice = frappe.get_doc(
        "Sales Invoice", getattr(doc, "original_sales_invoice", None)
    )
    shift_name = getattr(original_invoice, "custom_fb_shift", None)
    lock_fb_shift_cash_scope(shift_name)
    with elevate_device_api_user():
        settlement_document = ensure_return_settlement(doc, return_invoice)
    refresh_fb_shift_cash(shift_name)
    return settlement_document


def _update_resolved_sale_statuses(doc: Any) -> None:
    quantities_by_sale: dict[str, Decimal] = {}
    for line in doc.get("lines") or []:
        resolved_sale_name = getattr(line, "original_resolved_sale", None)
        qty_returned = _quantity_decimal(
            getattr(line, "qty_returned", 0), "returned quantity"
        )
        if not resolved_sale_name or qty_returned <= 0:
            continue
        quantities_by_sale[resolved_sale_name] = (
            quantities_by_sale.get(resolved_sale_name, Decimal("0")) + qty_returned
        )

    for resolved_sale_name, qty_returned in sorted(quantities_by_sale.items()):
        resolved_sale = frappe.get_doc("FB Resolved Sale", resolved_sale_name)
        total_qty = _quantity_decimal(
            getattr(resolved_sale, "qty", 0), "resolved sale quantity"
        )
        next_status = "Returned" if qty_returned >= total_qty else "Partially Returned"
        resolved_sale.db_set("status", next_status, update_modified=False)


def _quantity_decimal(value: Any, label: str) -> Decimal:
    try:
        return Decimal(str(value or 0).strip() or "0")
    except (InvalidOperation, ValueError) as error:
        frappe.throw(f"Invalid {label}: {value}", frappe.ValidationError)
        raise ValueError(f"Invalid {label}: {value}") from error
