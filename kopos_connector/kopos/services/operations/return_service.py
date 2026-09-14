# pyright: reportMissingImports=false

from __future__ import annotations

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
    if not settlement_document:
        frappe.throw(
            "Accounting settlement failed for FB Return Event {0}".format(
                getattr(doc, "name", "")
            ),
            frappe.ValidationError,
        )
    refresh_fb_shift_cash(shift_name)
    return return_invoice, None


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
