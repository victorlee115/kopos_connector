# pyright: reportMissingImports=false

"""Successful QR reconciliation disposition and Journal Entry proof."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cstr

from ._qr_journal_entry import (
    JournalSide,
    create_or_recover_journal,
    prove_journal,
)
from ._qr_reconciliation_context import (
    RECONCILIATION_FAILED_STATUS,
    _build_context,
    _find_existing_journal,
    _lock_and_reload_source,
    _require_failure_source_fields,
    _require_journal_fields,
    _validate_invoice_suspense_receipt,
    _value,
)


def ensure_qr_suspense_reclassification(
    reconciliation_source: Any,
) -> dict[str, Any]:
    """Create or recover one submitted QR suspense-to-bank Journal Entry.

    The source reconciliation row is the lock and idempotency aggregate. This
    function deliberately does not update that row, its FB Order Payment, or
    its Sales Invoice. The caller may mark reconciliation complete only after
    this function returns submitted GL evidence.
    """
    source = _lock_and_reload_source(reconciliation_source)
    context = _build_context(source)
    _require_journal_fields()
    _validate_invoice_suspense_receipt(context)
    _apply_historical_failure_recovery(context)

    journal_name = _find_existing_journal(source, context["reconciliation_key"])
    if not journal_name:
        journal_name = _create_or_recover_journal(context)
    return _validate_journal(context, journal_name)


def assert_qr_suspense_reclassification(
    reconciliation_source: Any,
) -> dict[str, Any]:
    """Re-prove the exact submitted Journal Entry without creating one."""
    source = _lock_and_reload_source(reconciliation_source)
    context = _build_context(source)
    _require_journal_fields()
    _validate_invoice_suspense_receipt(context)
    _apply_historical_failure_recovery(context)
    journal_name = _find_existing_journal(source, context["reconciliation_key"])
    if not journal_name:
        frappe.throw(
            f"QR reconciliation {context['reconciliation_key']} has no Journal Entry",
            frappe.ValidationError,
        )
    return _validate_journal(context, journal_name)


def _apply_historical_failure_recovery(context: dict[str, Any]) -> None:
    source = context["source"]
    snapshot_fields = (
        "failure_accounting_key",
        "failure_variance_account",
        "failure_cost_center",
        "failure_accounting_reason",
        "failure_journal_entry",
    )
    snapshots = {
        fieldname: cstr(_value(source, fieldname)).strip()
        for fieldname in snapshot_fields
    }
    populated = {fieldname for fieldname, value in snapshots.items() if value}
    if not populated:
        return
    if len(populated) != len(snapshot_fields):
        frappe.throw(
            f"{context['source_doctype']} {context['source_name']} has incomplete "
            "historical QR failure accounting evidence",
            frappe.ValidationError,
        )

    failure_context = _build_context(
        source,
        disposition=RECONCILIATION_FAILED_STATUS,
        failure_reason=snapshots["failure_accounting_reason"],
        allow_provider_paid_history=True,
    )
    _require_failure_source_fields(context["source_doctype"])
    _require_journal_fields(
        require_failure_fields=True,
        require_recovery_fields=True,
    )
    _validate_invoice_suspense_receipt(failure_context)
    failure_journal = _find_existing_journal(
        source,
        failure_context["reconciliation_key"],
        link_field="failure_journal_entry",
        key_field="custom_kopos_qr_failure_key",
    )
    if not failure_journal or failure_journal != snapshots["failure_journal_entry"]:
        frappe.throw(
            f"{context['source_doctype']} {context['source_name']} has no exact "
            "historical QR failure Journal Entry",
            frappe.ValidationError,
        )
    _validate_journal(failure_context, failure_journal)

    context["credit_account"] = failure_context["target_account"]
    context["credit_cost_center"] = failure_context["failure_cost_center"]
    context["prior_failure_journal_entry"] = failure_journal
    if context["target_account"] == context["credit_account"]:
        frappe.throw(
            "Late QR settlement bank account must differ from the failure variance account",
            frappe.ValidationError,
        )


def _journal_sides(context: dict[str, Any]) -> tuple[JournalSide, JournalSide]:
    failed = context["disposition"] == RECONCILIATION_FAILED_STATUS
    return (
        JournalSide(context["target_account"], context["failure_cost_center"] if failed else ""),
        JournalSide(context["credit_account"], context["credit_cost_center"]),
    )


def _journal_fields(context: dict[str, Any]) -> dict[str, Any]:
    failed = context["disposition"] == RECONCILIATION_FAILED_STATUS
    fields: dict[str, Any] = {
        ("custom_kopos_qr_failure_key" if failed else "custom_kopos_qr_reconciliation_key"):
            context["reconciliation_key"],
        "custom_kopos_qr_source_doctype": context["source_doctype"],
        "custom_kopos_qr_source_name": context["source_name"],
        "custom_kopos_qr_fb_order": context["order_name"],
        "custom_kopos_qr_sales_invoice": context["invoice_name"],
        "custom_kopos_qr_amount_sen": context["amount_sen"],
        "custom_kopos_qr_currency": context["currency"],
    }
    if failed:
        fields.update({
            "custom_kopos_qr_disposition": context["disposition"],
            "custom_kopos_qr_fb_order_payment": context["payment_row_name"],
            "custom_kopos_qr_target_account": context["target_account"],
            "custom_kopos_qr_cost_center": context["failure_cost_center"],
            "custom_kopos_qr_failure_reason": context["failure_reason"],
        })
    elif context["prior_failure_journal_entry"]:
        fields.update({
            "custom_kopos_qr_source_account": context["credit_account"],
            "custom_kopos_qr_prior_failure_journal": context["prior_failure_journal_entry"],
            "custom_kopos_qr_cost_center": context["credit_cost_center"],
        })
    return fields


def _create_or_recover_journal(context: dict[str, Any]) -> str:
    failed = context["disposition"] == RECONCILIATION_FAILED_STATUS
    debit, credit = _journal_sides(context)
    return create_or_recover_journal(
        company=context["company"],
        posting_date=context["posting_date"],
        user_remark=(
            f"KoPOS QR suspense {context['disposition']} for {context['source_doctype']} "
            f"{context['source_name']}; Sales Invoice {context['invoice_name']}"
        ),
        fields=_journal_fields(context),
        debit=debit,
        credit=credit,
        amount_sen=context["amount_sen"],
        savepoint="kopos_qr_reclassification",
        recover=lambda: _find_existing_journal(
            context["source"],
            context["reconciliation_key"],
            link_field="failure_journal_entry" if failed else "reclassification_journal_entry",
            key_field="custom_kopos_qr_failure_key" if failed else "custom_kopos_qr_reconciliation_key",
        ),
    )


def _validate_journal(context: dict[str, Any], journal_name: str) -> dict[str, Any]:
    debit, credit = _journal_sides(context)
    fields = _journal_fields(context)
    fields.pop("custom_kopos_qr_amount_sen")
    prove_journal(
        journal_name,
        label="QR reconciliation",
        expected_fields={**fields, "company": context["company"]},
        debit=debit,
        credit=credit,
        amount_sen=context["amount_sen"],
    )
    expected_sen = int(context["amount_sen"])

    result = {
        "journal_entry": journal_name,
        "reconciliation_key": context["reconciliation_key"],
        "source_doctype": context["source_doctype"],
        "source_name": context["source_name"],
        "fb_order": context["order_name"],
        "sales_invoice": context["invoice_name"],
        "amount_sen": expected_sen,
        "company": context["company"],
        "currency": context["currency"],
        "suspense_account": context["suspense_account"],
        "target_account": context["target_account"],
    }
    if context["disposition"] == RECONCILIATION_FAILED_STATUS:
        result.update(
            {
                "disposition": context["disposition"],
                "failure_reason": context["failure_reason"],
                "cost_center": context["failure_cost_center"],
            }
        )
    elif context["prior_failure_journal_entry"]:
        result.update(
            {
                "source_account": context["credit_account"],
                "prior_failure_journal_entry": context[
                    "prior_failure_journal_entry"
                ],
                "cost_center": context["credit_cost_center"],
            }
        )
    return result
