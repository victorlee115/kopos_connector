# pyright: reportMissingImports=false
"""Resolve provider-paid evidence that arrives after a static QR sale won.

The cashier-facing sale is already final and must never be reopened.  A late
Maybank payment is therefore held as a possible duplicate until the exact
static reconciliation is resolved.  Finance truth then has only two valid
outcomes:

* a reconciled static payment makes the Maybank payment a refund liability;
* a failed static payment lets the earliest Maybank payment settle the already
  submitted invoice, without creating another sale, invoice, or stock issue.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import cstr, now_datetime

from kopos_connector.kopos.services.accounting._duplicate_qr_contract import (
    POSSIBLE_DUPLICATE_STATUS,
    SETTLED_EXISTING_SALE_STATUS,
    _text,
    _validate_duplicate_identity,
    _value,
)
from kopos_connector.kopos.services.accounting._duplicate_qr_journal import (
    _set_source_values,
)
from kopos_connector.kopos.services.accounting.duplicate_qr_payment_service import (
    register_duplicate_paid_incident,
)
from kopos_connector.kopos.services.accounting.qr_reconciliation_service import (
    assert_qr_suspense_failure_reclassification,
    assert_qr_suspense_reclassification,
    ensure_qr_suspense_reclassification,
)


MANUAL_RECONCILIATION_DOCTYPE = "Manual QR Reconciliation"
MAYBANK_TRANSACTION_DOCTYPE = "Maybank QR Transaction"
FB_ORDER_PAYMENT_DOCTYPE = "FB Order Payment"
STATIC_QR_WINNER = "static_qr"
MAYBANK_PROVIDER = "maybank_qr"


def resolve_late_paid_after_static_winner(
    transaction: Any,
    *,
    order_doc: Any,
    paid_attempts: list[Any],
) -> dict[str, Any]:
    """Classify or consume one late paid attempt under exact locked evidence."""

    reconciliation = _lock_static_reconciliation(order_doc)
    existing_status = _text(_value(transaction, "duplicate_payment_status"))
    if existing_status == SETTLED_EXISTING_SALE_STATUS:
        return assert_late_payment_settled_existing_sale(
            transaction,
            order_doc=order_doc,
            reconciliation=reconciliation,
        )
    identity = _validate_duplicate_identity(
        transaction,
        order_doc=order_doc,
        winning_transaction_name="",
        require_submitted_sale=True,
    )
    _validate_locked_reconciliation(reconciliation, identity=identity)

    reconciliation_status = _text(_value(reconciliation, "status"))
    if reconciliation_status == "pending_reconciliation":
        return _mark_possible_duplicate(
            transaction,
            identity=identity,
            reason=(
                "The static QR payment is still being checked. No refund "
                "liability has been posted."
            ),
        )
    if reconciliation_status == "reconciled":
        return register_duplicate_paid_incident(
            transaction,
            order_doc=order_doc,
            winning_transaction_name="",
        )
    if reconciliation_status != "reconciliation_failed":
        frappe.throw(
            "Winning static QR reconciliation state is invalid",
            frappe.ValidationError,
        )

    earliest_unconsumed = _earliest_unconsumed_paid_attempt(paid_attempts)
    source_name = identity["source_name"]
    if not earliest_unconsumed or _text(
        _value(earliest_unconsumed, "name")
    ) != source_name:
        return _mark_possible_duplicate(
            transaction,
            identity=identity,
            reason=(
                "The failed static QR payment has an earlier authenticated "
                "Maybank payment awaiting settlement. No refund liability has "
                "been posted for this later attempt."
            ),
        )

    return _settle_existing_sale_after_failed_static(
        transaction,
        order_doc=order_doc,
        reconciliation=reconciliation,
        identity=identity,
    )


def assert_late_payment_settled_existing_sale(
    transaction: Any,
    *,
    order_doc: Any,
    reconciliation: Any | None = None,
) -> dict[str, Any]:
    """Re-prove the terminal no-second-sale settlement without mutating it."""

    source_name = _text(_value(transaction, "name"))
    order_name = _text(_value(order_doc, "name"))
    invoice_name = _text(_value(order_doc, "sales_invoice"))
    payment_row_name = _text(_value(transaction, "fb_order_payment"))
    static_reconciliation = reconciliation or _lock_static_reconciliation(order_doc)
    if (
        not source_name
        or not order_name
        or not invoice_name
        or not payment_row_name
        or _text(_value(transaction, "duplicate_payment_status"))
        != SETTLED_EXISTING_SALE_STATUS
        or _text(_value(transaction, "consumption_key")) != order_name
        or _text(_value(transaction, "sales_invoice")) != invoice_name
        or _text(_value(transaction, "invoice_consumption_key")) != invoice_name
        or not _text(_value(transaction, "consumed_at"))
        or _text(_value(transaction, "duplicate_winning_channel"))
        != STATIC_QR_WINNER
        or _text(_value(transaction, "duplicate_winning_static_reconciliation"))
        != _text(_value(static_reconciliation, "name"))
        or _text(_value(transaction, "duplicate_winning_transaction"))
    ):
        frappe.throw(
            "Late Maybank settlement does not match the existing submitted sale",
            frappe.ValidationError,
        )
    if _text(_value(static_reconciliation, "status")) != "reconciled":
        frappe.throw(
            "Late Maybank settlement static reconciliation is not reconciled",
            frappe.ValidationError,
        )
    failure_reason = _text(
        _value(static_reconciliation, "failure_accounting_reason")
    )
    failure_journal = _text(_value(static_reconciliation, "failure_journal_entry"))
    recovery_journal = _text(
        _value(static_reconciliation, "reclassification_journal_entry")
    )
    if not failure_reason or not failure_journal or not recovery_journal:
        frappe.throw(
            "Late Maybank settlement lacks exact failed-static recovery evidence",
            frappe.ValidationError,
        )
    evidence = assert_qr_suspense_reclassification(static_reconciliation)
    if _text(evidence.get("journal_entry")) != recovery_journal:
        frappe.throw(
            "Late Maybank settlement Journal Entry does not match its evidence",
            frappe.ValidationError,
        )
    return {
        "status": "already_submitted",
        "transaction": source_name,
        "fb_order": order_name,
        "sales_invoice": invoice_name,
        "winning_channel": STATIC_QR_WINNER,
        "winning_static_reconciliation": _text(
            _value(static_reconciliation, "name")
        ),
        "settlement_status": "reconciled",
        "duplicate_payment_status": SETTLED_EXISTING_SALE_STATUS,
        "reclassification_journal_entry": recovery_journal,
        "sales_invoice_created": False,
    }


def _lock_static_reconciliation(order_doc: Any) -> Any:
    reconciliation_name = _text(
        _value(order_doc, "automatic_qr_static_reconciliation")
    )
    if not reconciliation_name:
        frappe.throw(
            "Static QR winning reconciliation is missing",
            frappe.ValidationError,
        )
    rows = frappe.db.sql(
        "SELECT name FROM `tabManual QR Reconciliation` "
        "WHERE name = %s LIMIT 1 FOR UPDATE",
        (reconciliation_name,),
    )
    if len(rows or []) != 1:
        frappe.throw(
            "Static QR winning reconciliation was not found",
            frappe.ValidationError,
        )
    return frappe.get_doc(MANUAL_RECONCILIATION_DOCTYPE, reconciliation_name)


def _validate_locked_reconciliation(
    reconciliation: Any,
    *,
    identity: Mapping[str, Any],
) -> None:
    expected = {
        "name": identity["winning_static_reconciliation"],
        "claim_role": "winning_settlement",
        "fb_order": identity["order_name"],
        "fb_order_payment": identity["payment_row_name"],
        "sales_invoice": identity["invoice_name"],
        "device_id": identity["device_id"],
        "company": identity["company"],
        "currency": identity["currency"],
    }
    for fieldname, expected_value in expected.items():
        actual = _text(_value(reconciliation, fieldname))
        if fieldname == "claim_role" and not actual:
            actual = "winning_settlement"
        if fieldname == "currency":
            actual = actual.upper()
        if actual != _text(expected_value):
            frappe.throw(
                f"Static QR winning reconciliation {fieldname} does not match",
                frappe.ValidationError,
            )
    if _text(_value(reconciliation, "winning_maybank_qr_transaction")):
        frappe.throw(
            "Static QR winning reconciliation is a secondary claim",
            frappe.ValidationError,
        )


def _mark_possible_duplicate(
    transaction: Any,
    *,
    identity: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    existing = _text(_value(transaction, "duplicate_payment_status"))
    if existing and existing != POSSIBLE_DUPLICATE_STATUS:
        frappe.throw(
            "Late Maybank payment already has a different terminal disposition",
            frappe.ValidationError,
        )
    expected = {
        "duplicate_payment_status": POSSIBLE_DUPLICATE_STATUS,
        "duplicate_winning_channel": STATIC_QR_WINNER,
        "duplicate_winning_transaction": None,
        "duplicate_winning_static_reconciliation": identity[
            "winning_static_reconciliation"
        ],
    }
    for fieldname, expected_value in expected.items():
        current = _text(_value(transaction, fieldname))
        if current and current != _text(expected_value):
            frappe.throw(
                f"Late Maybank payment {fieldname} does not match its static sale",
                frappe.ValidationError,
            )
    _set_source_values(
        transaction,
        {
            **expected,
            "reconciliation_note": reason,
        },
    )
    return {
        "status": "payment_incident",
        "transaction": identity["source_name"],
        "fb_order": identity["order_name"],
        "sales_invoice": identity["invoice_name"],
        "winning_channel": STATIC_QR_WINNER,
        "winning_static_reconciliation": identity[
            "winning_static_reconciliation"
        ],
        "settlement_status": POSSIBLE_DUPLICATE_STATUS,
        "duplicate_payment_status": POSSIBLE_DUPLICATE_STATUS,
        "liability_journal_entry": None,
        "sales_invoice_created": False,
    }


def _earliest_unconsumed_paid_attempt(paid_attempts: list[Any]) -> Any | None:
    for attempt in paid_attempts:
        if not _text(_value(attempt, "consumption_key")):
            return attempt
    return None


def _settle_existing_sale_after_failed_static(
    transaction: Any,
    *,
    order_doc: Any,
    reconciliation: Any,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    if any(
        _text(_value(transaction, fieldname))
        for fieldname in (
            "duplicate_accounting_key",
            "duplicate_liability_journal_entry",
            "duplicate_refund_key",
            "duplicate_refund_journal_entry",
        )
    ):
        frappe.throw(
            "Late Maybank payment already has duplicate-liability accounting and cannot settle the sale",
            frappe.ValidationError,
        )
    failure_reason = _text(_value(reconciliation, "reconciliation_failed_reason"))
    if not failure_reason:
        frappe.throw(
            "Failed static QR reconciliation reason is missing",
            frappe.ValidationError,
        )
    failure_evidence = assert_qr_suspense_failure_reclassification(
        reconciliation,
        failure_reason,
    )
    failure_journal = _text(failure_evidence.get("journal_entry"))
    if not failure_journal or failure_journal != _text(
        _value(reconciliation, "failure_journal_entry")
    ):
        frappe.throw(
            "Failed static QR reconciliation has no exact accounting evidence",
            frappe.ValidationError,
        )

    reconciliation_name = identity["winning_static_reconciliation"]
    payment_row_name = identity["payment_row_name"]
    provider_note = (
        "Authenticated Maybank payment settled the existing sale after the "
        "static QR payment was proven unsuccessful. No second sale, invoice, "
        "or stock movement was created."
    )
    frappe.db.set_value(
        MANUAL_RECONCILIATION_DOCTYPE,
        reconciliation_name,
        {
            "status": "pending_reconciliation",
            "reconciliation_failed_reason": None,
            "reconciliation_note": provider_note,
        },
        update_modified=False,
    )
    frappe.db.set_value(
        FB_ORDER_PAYMENT_DOCTYPE,
        payment_row_name,
        "settlement_status",
        "pending_reconciliation",
        update_modified=False,
    )
    _assign(reconciliation, "status", "pending_reconciliation")
    _assign(reconciliation, "reconciliation_failed_reason", None)

    accounting_evidence = ensure_qr_suspense_reclassification(reconciliation)
    recovery_journal = _text(accounting_evidence.get("journal_entry"))
    if not recovery_journal:
        frappe.throw(
            "Late Maybank settlement did not produce submitted accounting evidence",
            frappe.ValidationError,
        )

    reconciled_at = now_datetime()
    frappe.db.set_value(
        MANUAL_RECONCILIATION_DOCTYPE,
        reconciliation_name,
        {
            "status": "reconciled",
            "reconciled_by": "Maybank provider-paid evidence",
            "reconciled_at": reconciled_at,
            "reconciliation_note": provider_note,
            "reconciliation_failed_reason": None,
            "reclassification_journal_entry": recovery_journal,
        },
        update_modified=False,
    )
    frappe.db.set_value(
        FB_ORDER_PAYMENT_DOCTYPE,
        payment_row_name,
        "settlement_status",
        "reconciled",
        update_modified=False,
    )
    _assign(reconciliation, "status", "reconciled")
    _assign(reconciliation, "reclassification_journal_entry", recovery_journal)

    _set_source_values(
        transaction,
        {
            "sales_invoice": identity["invoice_name"],
            "consumption_key": identity["order_name"],
            "invoice_consumption_key": identity["invoice_name"],
            "consumed_at": reconciled_at,
            "duplicate_payment_status": SETTLED_EXISTING_SALE_STATUS,
            "duplicate_winning_channel": STATIC_QR_WINNER,
            "duplicate_winning_transaction": None,
            "duplicate_winning_static_reconciliation": reconciliation_name,
            "reconciliation_note": provider_note,
        },
    )
    return {
        "status": "already_submitted",
        "transaction": identity["source_name"],
        "fb_order": identity["order_name"],
        "sales_invoice": identity["invoice_name"],
        "winning_channel": STATIC_QR_WINNER,
        "winning_static_reconciliation": reconciliation_name,
        "settlement_status": "reconciled",
        "duplicate_payment_status": SETTLED_EXISTING_SALE_STATUS,
        "reclassification_journal_entry": recovery_journal,
        "sales_invoice_created": False,
    }


def _assign(document: Any, fieldname: str, value: Any) -> None:
    if isinstance(document, dict):
        document[fieldname] = value
    else:
        setattr(document, fieldname, value)
