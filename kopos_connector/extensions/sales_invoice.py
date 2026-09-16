from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, cstr

from kopos_connector.kopos.api.money_contract import persisted_money_to_sen, sen_to_decimal


class KoPOSSalesInvoiceIntegrityMixin:
    """Keep native accounting with KoPOS tender, return and void invariants."""

    def calculate_taxes_and_totals(self) -> None:
        super().calculate_taxes_and_totals()
        _apply_rounded_cash_change(self)

    def make_write_off_gl_entry(self, gl_entries: list[Any]) -> None:
        start = len(gl_entries)
        super().make_write_off_gl_entry(gl_entries)
        # Native v16 respects update_outstanding_for_self for the credit note's
        # receivable, but not its write-off. Fix only the native entry's link;
        # native ERPNext still owns amounts, accounts and posting. Remove when
        # the pinned implementation passes the rounded full-refund regression.
        if (cint(self.get("is_return")) and self.get("custom_fb_order")
                and cint(self.get("update_outstanding_for_self"))):
            for entry in gl_entries[start:]:
                if (entry.get("account") == self.get("debit_to")
                        and entry.get("against_voucher") == self.get("return_against")):
                    entry["against_voucher"] = self.name

    def before_save(self) -> None:
        parent_before_save: Any = getattr(super(), "before_save", None)
        if callable(parent_before_save):
            parent_before_save()
        # ERPNext's native hook resets payment rows from Mode of Payment.  The
        # connector must restore the branch-scoped suspense account of every
        # manually confirmed QR row after that refresh, before posting.
        from kopos_connector.kopos.services.accounting.sales_invoice_service import (
            enforce_manual_qr_payment_accounts,
        )

        enforce_manual_qr_payment_accounts(self)

    def before_cancel(self) -> None:
        if _is_original_kopos_sale(self):
            _validate_kopos_void_proof(self)
        parent_before_cancel: Any = getattr(super(), "before_cancel", None)
        if callable(parent_before_cancel):
            parent_before_cancel()


def _apply_rounded_cash_change(invoice: Any) -> None:
    """Bridge ERPNext's cash-change calculation to its native write-off total.

    ERPNext 16 computes outstanding after write-off, but cash change before it.
    Keep native line/tax/GL owners and derive only these dependent tender values.
    The invoice service still proves them against the immutable FB Order. This
    adapter can retire when the pinned native calculator passes the same real
    rounded-tender regressions. Ordinary invoices and credit notes are untouched.
    """
    if (
        not _is_original_kopos_sale(invoice)
        or not cint(invoice.get("is_pos"))
        or not invoice.get("write_off_amount")
        or not any(payment.get("type") == "Cash" for payment in invoice.get("payments") or [])
    ):
        return

    def amount(field: str) -> int:
        return persisted_money_to_sen(invoice.get(field) or 0, f"Sales Invoice {field}")

    payable = amount("rounded_total") or amount("grand_total")
    base_payable = amount("base_rounded_total") or amount("base_grand_total")
    payable -= amount("write_off_amount")
    base_payable -= amount("base_write_off_amount")
    change = max(0, amount("paid_amount") - payable)
    base_change = max(0, amount("base_paid_amount") - base_payable)
    invoice.change_amount = sen_to_decimal(change)
    invoice.base_change_amount = sen_to_decimal(base_change)
    if invoice.get("party_account_currency") == invoice.get("currency"):
        outstanding = payable - amount("total_advance") - amount("paid_amount") + change
    else:
        outstanding = base_payable - amount("total_advance") - amount("base_paid_amount") + base_change
    invoice.outstanding_amount = sen_to_decimal(outstanding)


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
