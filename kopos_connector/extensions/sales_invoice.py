from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, cstr


class KoPOSSalesInvoiceIntegrityMixin:
    """Allow original KoPOS sale cancellation only through its exact void proof."""

    def before_cancel(self) -> None:
        if _is_original_kopos_sale(self):
            _validate_kopos_void_proof(self)
        parent_before_cancel: Any = getattr(super(), "before_cancel", None)
        if callable(parent_before_cancel):
            parent_before_cancel()


def _is_original_kopos_sale(invoice: Any) -> bool:
    return not cint(getattr(invoice, "is_return", None)) and bool(
        cstr(getattr(invoice, "custom_fb_order", None)).strip()
        or cstr(getattr(invoice, "custom_fb_idempotency_key", None)).strip()
    )


def _validate_kopos_void_proof(invoice: Any) -> None:
    invoice_name = cstr(getattr(invoice, "name", None)).strip()
    order_name = cstr(getattr(invoice, "custom_fb_order", None)).strip()
    sale_idempotency_key = cstr(
        getattr(invoice, "custom_fb_idempotency_key", None)
    ).strip()
    void_idempotency_key = cstr(
        getattr(invoice, "custom_fb_void_idempotency_key", None)
    ).strip()
    void_fingerprint = cstr(
        getattr(invoice, "custom_fb_void_request_fingerprint", None)
    ).strip().lower()
    manager_id = cstr(getattr(invoice, "custom_fb_void_manager", None)).strip()
    approval_token_id = cstr(
        getattr(invoice, "custom_fb_void_approval_token_id", None)
    ).strip()
    if (
        not invoice_name
        or not order_name
        or not sale_idempotency_key
        or not void_idempotency_key
        or len(void_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in void_fingerprint)
        or not manager_id
        or not approval_token_id
    ):
        frappe.throw(
            _(
                "KoPOS Sales Invoices can only be cancelled through the exact "
                "manager-approved void_order workflow"
            ),
            frappe.ValidationError,
        )

    order = frappe.get_doc("FB Order", order_name)
    if (
        cint(getattr(order, "docstatus", None)) != 1
        or cstr(getattr(order, "sales_invoice", None)).strip() != invoice_name
        or cstr(getattr(order, "external_idempotency_key", None)).strip()
        != sale_idempotency_key
        or cstr(getattr(order, "company", None)).strip()
        != cstr(getattr(invoice, "company", None)).strip()
        or cstr(getattr(order, "currency", None)).strip().upper()
        != cstr(getattr(invoice, "currency", None)).strip().upper()
    ):
        frappe.throw(
            _("KoPOS Sales Invoice void proof does not match its exact FB Order"),
            frappe.ValidationError,
        )

    _load_consumed_void_approval_proof(
        approval_token_id=approval_token_id,
        approval_manager_id=manager_id,
        idempotency_key=void_idempotency_key,
        resource_id=invoice_name,
    )


def _load_consumed_void_approval_proof(
    *,
    approval_token_id: str,
    approval_manager_id: str,
    idempotency_key: str,
    resource_id: str,
) -> dict[str, str]:
    from kopos_connector.utils.manager_approval import (
        load_consumed_manager_approval_proof,
    )

    return load_consumed_manager_approval_proof(
        approval_token_id=approval_token_id,
        approval_manager_id=approval_manager_id,
        action="void_order",
        idempotency_key=idempotency_key,
        resource_id=resource_id,
    )
