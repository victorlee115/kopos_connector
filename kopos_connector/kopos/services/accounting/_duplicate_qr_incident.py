# pyright: reportMissingImports=false

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint

from kopos_connector.kopos.services.accounting._duplicate_qr_contract import (
    ACCOUNTING_PENDING_STATUS,
    DUPLICATE_STATUSES,
    LIABILITY_RECOGNITION_STAGE,
    REFUNDED_STATUS,
    REFUND_REQUIRED_STATUS,
    SETTLED_EXISTING_SALE_STATUS,
    _build_accounting_context,
    _require_schema_fields,
    _text,
    _validate_duplicate_identity,
    _value,
)
from kopos_connector.kopos.services.accounting._duplicate_qr_journal import (
    _create_or_recover_journal,
    _find_existing_journal,
    _set_source_values,
    _snapshot_recognition_context,
    _validate_journal,
)
from kopos_connector.kopos.services.accounting._duplicate_qr_terminal_evidence import (
    assert_duplicate_refund_terminal_evidence,
)
from kopos_connector.utils.diagnostics import log_sanitized_error


def register_duplicate_paid_incident(
    transaction: Any,
    *,
    order_doc: Any,
    winning_transaction_name: str = "",
) -> dict[str, Any]:
    """Register and account for one later provider-paid QR attempt.

    The winning FB Order and Sales Invoice are immutable inputs. This function
    never submits, cancels, or edits either document. Missing accounting setup
    is deliberately converted into a visible ``accounting_pending`` incident;
    it is not allowed to roll back or delay the winning sale.
    """

    identity = _validate_duplicate_identity(
        transaction,
        order_doc=order_doc,
        winning_transaction_name=winning_transaction_name,
        require_submitted_sale=False,
    )
    source_name = identity["source_name"]
    existing_status = _text(_value(transaction, "duplicate_payment_status"))
    if existing_status and existing_status not in DUPLICATE_STATUSES:
        frappe.throw(
            "Duplicate Automatic QR payment status is invalid",
            frappe.ValidationError,
        )
    if existing_status == SETTLED_EXISTING_SALE_STATUS:
        frappe.throw(
            "Maybank payment already settled the existing sale and cannot become a refund liability",
            frappe.ValidationError,
        )

    existing_winner = _text(_value(transaction, "duplicate_winning_transaction"))
    if existing_winner and existing_winner != identity["winning_transaction"]:
        frappe.throw(
            "Duplicate Automatic QR payment is bound to another winning transaction",
            frappe.ValidationError,
        )
    existing_channel = _text(_value(transaction, "duplicate_winning_channel"))
    if existing_channel and existing_channel != identity["winning_channel"]:
        frappe.throw(
            "Duplicate Automatic QR payment is bound to another winning channel",
            frappe.ValidationError,
        )
    existing_static_winner = _text(
        _value(transaction, "duplicate_winning_static_reconciliation")
    )
    if (
        existing_static_winner
        and existing_static_winner != identity["winning_static_reconciliation"]
    ):
        frappe.throw(
            "Duplicate Automatic QR payment is bound to another static reconciliation",
            frappe.ValidationError,
        )

    if not existing_status:
        updates: dict[str, Any] = {
            "duplicate_payment_status": ACCOUNTING_PENDING_STATUS,
            "duplicate_winning_channel": identity["winning_channel"],
            "duplicate_winning_transaction": identity["winning_transaction"] or None,
            "duplicate_winning_static_reconciliation": identity[
                "winning_static_reconciliation"
            ]
            or None,
            "reconciliation_note": (
                "Authenticated provider evidence reported an additional paid "
                f"Automatic QR attempt for FB Order {identity['order_name']}. "
                "The duplicate-payment flow does not reopen or mutate the winning "
                "sale or Sales Invoice. Customer refund liability accounting is pending."
            ),
        }
        # Migrate the short-lived pre-contract representation without letting
        # the ordinary Manual QR action mutate the winning payment row.
        if (
            _text(_value(transaction, "manual_reconciliation_status"))
            == "reconciliation_failed"
            and _text(_value(transaction, "reconciliation_failed_reason"))
            == "duplicate"
            and not _text(_value(transaction, "failure_journal_entry"))
        ):
            updates.update(
                {
                    "manual_reconciliation_status": None,
                    "reconciliation_failed_reason": None,
                }
            )
        _set_source_values(transaction, updates)
        existing_status = ACCOUNTING_PENDING_STATUS
    elif (
        (
            identity["winning_channel"] == "static_qr"
            and (
                not existing_channel
                or not existing_static_winner
            )
        )
        or (identity["winning_transaction"] and not existing_winner)
    ):
        # Do not backfill the new channel onto a historical dynamic incident.
        # Its blank field is the narrow compatibility marker that permits the
        # already-submitted pre-upgrade Journal Entry to retain blank additive
        # winner metadata while every original identity/GL field is re-proved.
        winner_channel_update = (
            identity["winning_channel"]
            if identity["winning_channel"] == "static_qr"
            else existing_channel or None
        )
        _set_source_values(
            transaction,
            {
                "duplicate_winning_channel": winner_channel_update,
                "duplicate_winning_transaction": identity["winning_transaction"]
                or None,
                "duplicate_winning_static_reconciliation": identity[
                    "winning_static_reconciliation"
                ]
                or None,
            },
        )

    if cint(_value(order_doc, "docstatus")) != 1 or not identity["invoice_name"]:
        return {
            "status": "payment_incident",
            "transaction": source_name,
            "fb_order": identity["order_name"],
            "winning_transaction": identity["winning_transaction"],
            "winning_channel": identity["winning_channel"],
            "winning_static_reconciliation": identity[
                "winning_static_reconciliation"
            ]
            or None,
            "settlement_status": ACCOUNTING_PENDING_STATUS,
            "duplicate_payment_status": ACCOUNTING_PENDING_STATUS,
            "liability_journal_entry": None,
            "sales_invoice_created": False,
        }

    accounting_savepoint = "kopos_duplicate_qr_accounting_attempt"
    savepoint_fn = getattr(frappe.db, "savepoint", None)
    accounting_savepoint_created = callable(savepoint_fn)
    if accounting_savepoint_created:
        savepoint_fn(accounting_savepoint)
    try:
        evidence = ensure_duplicate_liability_accounting(
            transaction,
            order_doc=order_doc,
            identity=identity,
        )
    except Exception as error:
        # The incident itself is already durable. Configuration, migration, or
        # GL readiness is retried by the paid-sale recovery sweep and cannot
        # reopen or mutate the winning sale, or create another invoice.
        if not accounting_savepoint_created:
            raise
        frappe.db.rollback(save_point=accounting_savepoint)
        log_sanitized_error(
            "Duplicate Automatic QR liability accounting pending",
            error,
        )
        return {
            "status": "payment_incident",
            "transaction": source_name,
            "fb_order": identity["order_name"],
            "winning_transaction": identity["winning_transaction"],
            "winning_channel": identity["winning_channel"],
            "winning_static_reconciliation": identity[
                "winning_static_reconciliation"
            ]
            or None,
            "settlement_status": ACCOUNTING_PENDING_STATUS,
            "duplicate_payment_status": ACCOUNTING_PENDING_STATUS,
            "liability_journal_entry": None,
            "sales_invoice_created": False,
        }

    journal_entry = _text(evidence.get("journal_entry"))
    if not journal_entry:
        log_sanitized_error(
            "Duplicate Automatic QR liability accounting pending",
            RuntimeError("submitted liability Journal Entry evidence was not returned"),
        )
        return {
            "status": "payment_incident",
            "transaction": source_name,
            "fb_order": identity["order_name"],
            "winning_transaction": identity["winning_transaction"],
            "winning_channel": identity["winning_channel"],
            "winning_static_reconciliation": identity[
                "winning_static_reconciliation"
            ]
            or None,
            "settlement_status": ACCOUNTING_PENDING_STATUS,
            "duplicate_payment_status": ACCOUNTING_PENDING_STATUS,
            "liability_journal_entry": None,
            "sales_invoice_created": False,
        }

    if existing_status == REFUNDED_STATUS:
        terminal_evidence = assert_duplicate_refund_terminal_evidence(
            transaction,
            order_doc=order_doc,
            identity=identity,
        )
        return {
            "status": "payment_incident",
            "transaction": source_name,
            "fb_order": identity["order_name"],
            "winning_transaction": identity["winning_transaction"],
            "winning_channel": identity["winning_channel"],
            "winning_static_reconciliation": identity[
                "winning_static_reconciliation"
            ]
            or None,
            "settlement_status": REFUNDED_STATUS,
            "duplicate_payment_status": REFUNDED_STATUS,
            "liability_journal_entry": journal_entry,
            "refund_journal_entry": _text(
                terminal_evidence.get("refund_journal_entry")
            ),
            "sales_invoice_created": False,
        }

    if existing_status != REFUNDED_STATUS:
        _set_source_values(
            transaction,
            {
                "duplicate_payment_status": REFUND_REQUIRED_STATUS,
                "duplicate_liability_journal_entry": journal_entry,
                "reconciliation_note": (
                    "Additional provider-paid Automatic QR attempt is recognized "
                    "as a customer refund liability. The winning sale lifecycle is "
                    "unchanged; a System Manager must record an exact provider refund."
                ),
            },
        )
        existing_status = REFUND_REQUIRED_STATUS

    return {
        "status": "payment_incident",
        "transaction": source_name,
        "fb_order": identity["order_name"],
        "winning_transaction": identity["winning_transaction"],
        "winning_channel": identity["winning_channel"],
        "winning_static_reconciliation": identity[
            "winning_static_reconciliation"
        ]
        or None,
        "settlement_status": existing_status,
        "duplicate_payment_status": existing_status,
        "liability_journal_entry": journal_entry,
        "sales_invoice_created": False,
    }

def ensure_duplicate_liability_accounting(
    transaction: Any,
    *,
    order_doc: Any,
    identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or re-prove one exact clearing-to-liability Journal Entry."""

    resolved_identity = identity or _validate_duplicate_identity(
        transaction,
        order_doc=order_doc,
        winning_transaction_name=_text(
            _value(transaction, "duplicate_winning_transaction")
        ),
        require_submitted_sale=True,
    )
    _require_schema_fields()
    context = _build_accounting_context(
        transaction,
        order_doc=order_doc,
        identity=resolved_identity,
        stage=LIABILITY_RECOGNITION_STAGE,
    )
    _snapshot_recognition_context(transaction, context)
    journal_name = _find_existing_journal(
        transaction,
        context["journal_key"],
        link_field="duplicate_liability_journal_entry",
    )
    if not journal_name:
        journal_name = _create_or_recover_journal(context)
    evidence = _validate_journal(context, journal_name)
    _set_source_values(
        transaction,
        {"duplicate_liability_journal_entry": journal_name},
    )
    return evidence
