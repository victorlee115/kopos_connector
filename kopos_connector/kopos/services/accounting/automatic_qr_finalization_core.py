# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import cint, cstr, now_datetime

from kopos_connector.kopos.api.money_contract import (
    MoneyContractValidationError,
    persisted_money_to_sen,
)
from kopos_connector.kopos.services.accounting.duplicate_qr_payment_service import (
    register_duplicate_paid_incident,
)
from kopos_connector.kopos.services.accounting.maybank_payment_service import (
    normalize_qr_token,
)
from kopos_connector.kopos.services.accounting.qr_reconciliation_service import (
    ensure_qr_suspense_reclassification,
)
from kopos_connector.kopos.services.accounting.static_winner_late_payment import (
    resolve_late_paid_after_static_winner,
)
from kopos_connector.utils.diagnostics import log_sanitized_error


MAYBANK_PROVIDER = "maybank_qr"
MAYBANK_CURRENCY = "MYR"
MAYBANK_PAYMENT_CHANNELS = {"maybank", "maybank qr"}
MAYBANK_MODE_OF_PAYMENT = "duitnow qr"
FINALIZER_JOB_TIMEOUT_SECONDS = 5 * 60


def enqueue_automatic_qr_finalization(transaction_name: str) -> Any:
    """Queue finalization only after the provider-paid transition commits.

    The deterministic job id suppresses concurrent duplicate jobs. The scheduled
    recovery sweep is the durable fallback after a worker or queue outage.
    """

    resolved_name = cstr(transaction_name).strip()
    if not resolved_name:
        raise ValueError("Maybank QR transaction name is required")
    digest = hashlib.sha256(resolved_name.encode("utf-8")).hexdigest()
    return frappe.enqueue(
        "kopos_connector.kopos.services.accounting."
        "automatic_qr_finalization_service.finalize_paid_automatic_qr_sale",
        queue="short",
        enqueue_after_commit=True,
        job_id=f"kopos-auto-qr-finalize-{digest}",
        timeout=FINALIZER_JOB_TIMEOUT_SECONDS,
        transaction_name=resolved_name,
    )


def finalize_paid_automatic_qr_sale(transaction_name: str) -> dict[str, Any]:
    """Submit one exact prepared FB Order after authoritative provider payment.

    Lock order is always FB Order first, followed by every paid provider attempt
    for the logical payment in deterministic order. The provider status path
    never takes the order lock and only enqueues this function after commit, so
    it cannot invert this lock order.
    """

    resolved_name = cstr(transaction_name).strip()
    if not resolved_name:
        frappe.throw(
            "Maybank QR transaction name is required",
            frappe.ValidationError,
        )

    transaction_link = frappe.db.get_value(
        "Maybank QR Transaction",
        resolved_name,
        ["name", "fb_order", "fb_order_payment"],
        as_dict=True,
    )
    if not transaction_link:
        frappe.throw(
            "Maybank QR transaction was not found for Automatic QR finalization",
            frappe.ValidationError,
        )
    order_name = cstr(_value(transaction_link, "fb_order")).strip()
    payment_row_name = cstr(
        _value(transaction_link, "fb_order_payment")
    ).strip()
    if not order_name or not payment_row_name:
        frappe.throw(
            "Paid Maybank QR transaction is not bound to a prepared sale",
            frappe.ValidationError,
        )

    locked_order = frappe.db.sql(
        "SELECT name FROM `tabFB Order` WHERE name = %s LIMIT 1 FOR UPDATE",
        (order_name,),
    )
    if len(locked_order or []) != 1:
        frappe.throw(
            "Prepared Automatic QR FB Order was not found",
            frappe.ValidationError,
        )
    order_doc = frappe.get_doc("FB Order", order_name)
    if not cstr(_value(order_doc, "accepted_sale_fingerprint")).strip():
        frappe.throw(
            "Paid Maybank QR transaction is not bound to an accepted Automatic QR sale",
            frappe.ValidationError,
        )
    if cstr(_value(order_doc, "automatic_qr_payment")).strip() != payment_row_name:
        frappe.throw(
            "Paid Maybank QR payment row does not match the prepared sale",
            frappe.ValidationError,
        )

    payment = _get_exact_payment(order_doc, payment_row_name)
    paid_attempts = _load_paid_attempts_for_update(order_name, payment_row_name)
    requested_attempt = next(
        (
            attempt
            for attempt in paid_attempts
            if cstr(_value(attempt, "name")).strip() == resolved_name
        ),
        None,
    )
    if requested_attempt is None:
        frappe.throw(
            "Maybank QR transaction does not contain authoritative provider-paid evidence",
            frappe.ValidationError,
        )
    _validate_paid_attempt(
        requested_attempt,
        order_doc=order_doc,
        payment=payment,
        payment_row_name=payment_row_name,
    )

    docstatus = cint(_value(order_doc, "docstatus"))
    if docstatus == 1:
        if _is_submitted_static_qr_winner(order_doc, payment):
            return resolve_late_paid_after_static_winner(
                requested_attempt,
                order_doc=order_doc,
                paid_attempts=paid_attempts,
            )
        if _submitted_payment_matches_attempt(
            order_doc,
            payment,
            requested_attempt,
        ):
            _mark_order_finalized(order_doc)
            result = _result(
                "already_submitted",
                order_doc=order_doc,
                transaction=requested_attempt,
            )
            reconciliation = _reconcile_provider_paid_manual_settlement(
                order_doc=order_doc,
                payment=payment,
                transaction=requested_attempt,
            )
            if reconciliation:
                result.update(reconciliation)
            return result
        return _mark_late_paid_incident(
            requested_attempt,
            order_doc=order_doc,
            winning_transaction_name=cstr(
                _value(payment, "maybank_qr_transaction")
            ).strip(),
        )
    if docstatus != 0:
        return _mark_late_paid_incident(
            requested_attempt,
            order_doc=order_doc,
            winning_transaction_name=cstr(
                _value(payment, "maybank_qr_transaction")
            ).strip(),
        )

    winner = paid_attempts[0]
    _validate_paid_attempt(
        winner,
        order_doc=order_doc,
        payment=payment,
        payment_row_name=payment_row_name,
    )
    winner_name = cstr(_value(winner, "name")).strip()
    if resolved_name != winner_name:
        return _mark_late_paid_incident(
            requested_attempt,
            order_doc=order_doc,
            winning_transaction_name=winner_name,
        )

    _apply_provider_paid_payment(payment, winner)
    order_doc.automatic_qr_state = "provider_paid"
    order_doc.automatic_qr_winner_channel = MAYBANK_PROVIDER
    order_doc.save(ignore_permissions=True)
    order_doc.submit()
    _mark_order_finalized(order_doc)

    incident_registration_pending = _register_late_paid_incidents_after_sale_commit(
        paid_attempts[1:],
        order_doc=order_doc,
        winning_transaction_name=winner_name,
    )
    result = _result(
        "submitted",
        order_doc=order_doc,
        transaction=winner,
    )
    if incident_registration_pending:
        result["incident_registration_pending"] = incident_registration_pending
    return result


def _load_paid_attempts_for_update(
    order_name: str,
    payment_row_name: str,
) -> list[Any]:
    rows = frappe.db.sql(
        """
        SELECT
            name, transaction_refno, status, maybank_status,
            sale_amount_sen, currency, provider, device_id, outlet_id,
            qr_data, expires_at, paid_at, creation,
            fb_order, fb_order_payment, sales_invoice,
            consumption_key, invoice_consumption_key, consumed_at,
            manual_reconciliation_status, reconciliation_note,
            reconciliation_idempotency_key,
            company, suspense_account,
            reclassification_journal_entry,
            reconciliation_failed_reason, failure_journal_entry,
            duplicate_payment_status, duplicate_winning_channel,
            duplicate_winning_transaction,
            duplicate_winning_static_reconciliation,
            duplicate_accounting_key, duplicate_clearing_account,
            duplicate_liability_account, duplicate_liability_journal_entry,
            duplicate_refund_key, duplicate_refund_journal_entry,
            duplicate_refund_reference, duplicate_refund_evidence_reference,
            duplicate_refund_evidence_file,
            duplicate_refund_evidence_sha256,
            duplicate_refund_amount_sen, duplicate_refund_currency,
            duplicate_refund_date,
            duplicate_refund_note, duplicate_refunded_by, duplicate_refunded_at,
            business_date
        FROM `tabMaybank QR Transaction`
        WHERE fb_order = %s
          AND fb_order_payment = %s
          AND status = 'paid'
          AND maybank_status = 1
        ORDER BY COALESCE(paid_at, creation), creation, name
        FOR UPDATE
        """,
        (order_name, payment_row_name),
        as_dict=True,
    )
    if not rows:
        frappe.throw(
            "Prepared Automatic QR sale has no provider-paid transaction",
            frappe.ValidationError,
        )
    return list(rows)


def _get_exact_payment(order_doc: Any, payment_row_name: str) -> Any:
    payments = [
        payment
        for payment in list(_value(order_doc, "payments") or [])
        if cstr(_value(payment, "name")).strip() == payment_row_name
    ]
    if len(payments) != 1:
        frappe.throw(
            "Prepared Automatic QR payment row was not found exactly once",
            frappe.ValidationError,
        )
    payment = payments[0]
    if _normalized(_value(payment, "payment_method")) != MAYBANK_MODE_OF_PAYMENT:
        frappe.throw(
            "Prepared Automatic QR payment method is invalid",
            frappe.ValidationError,
        )
    channel = _normalized(_value(payment, "payment_channel_code"))
    is_static_winner = (
        channel == "static qr"
        and cstr(_value(order_doc, "automatic_qr_winner_channel")).strip()
        == "static_qr"
        and cstr(_value(payment, "manual_qr_reconciliation")).strip()
        and cint(_value(payment, "is_manual_confirmation"))
    )
    if channel not in MAYBANK_PAYMENT_CHANNELS and not is_static_winner:
        frappe.throw(
            "Prepared Automatic QR payment channel is invalid",
            frappe.ValidationError,
        )
    return payment


def _is_submitted_static_qr_winner(order_doc: Any, payment: Any) -> bool:
    reconciliation = cstr(_value(payment, "manual_qr_reconciliation")).strip()
    return bool(
        cint(_value(order_doc, "docstatus")) == 1
        and cstr(_value(order_doc, "automatic_qr_winner_channel")).strip()
        == "static_qr"
        and _normalized(_value(payment, "payment_channel_code")) == "static qr"
        and cint(_value(payment, "is_manual_confirmation"))
        and reconciliation
        and reconciliation
        == cstr(
            _value(order_doc, "automatic_qr_static_reconciliation")
        ).strip()
    )


def _validate_paid_attempt(
    transaction: Any,
    *,
    order_doc: Any,
    payment: Any,
    payment_row_name: str,
) -> None:
    order_name = cstr(_value(order_doc, "name")).strip()
    if (
        cstr(_value(transaction, "status")).strip().lower() != "paid"
        or cstr(_value(transaction, "maybank_status")).strip() != "1"
    ):
        frappe.throw(
            "Maybank QR transaction lacks authoritative provider-paid evidence",
            frappe.ValidationError,
        )
    if cstr(_value(transaction, "provider")).strip().lower() != MAYBANK_PROVIDER:
        frappe.throw(
            "Maybank QR provider identity is invalid",
            frappe.ValidationError,
        )
    if (
        cstr(_value(transaction, "fb_order")).strip() != order_name
        or cstr(_value(transaction, "fb_order_payment")).strip()
        != payment_row_name
    ):
        frappe.throw(
            "Maybank QR transaction binding does not match the prepared sale",
            frappe.ValidationError,
        )
    reference = cstr(_value(transaction, "transaction_refno")).strip()
    if not reference or reference.lower().startswith("static-"):
        frappe.throw(
            "Maybank QR transaction reference is invalid",
            frappe.ValidationError,
        )
    if not cstr(_value(transaction, "qr_data")).strip():
        frappe.throw(
            "Maybank QR transaction was not issued to the POS",
            frappe.ValidationError,
        )
    if cstr(_value(transaction, "device_id")).strip() != cstr(
        _value(order_doc, "device_id")
    ).strip():
        frappe.throw(
            "Maybank QR transaction device does not match the prepared sale",
            frappe.ValidationError,
        )
    transaction_company = cstr(_value(transaction, "company")).strip()
    prepared_company = cstr(_value(order_doc, "company")).strip()
    if (
        not prepared_company
        or (transaction_company and transaction_company != prepared_company)
        or (not transaction_company and cint(_value(order_doc, "docstatus")) == 0)
    ):
        frappe.throw(
            "Maybank QR transaction company does not match the prepared sale",
            frappe.ValidationError,
        )
    if (
        cstr(_value(transaction, "currency")).strip().upper()
        != MAYBANK_CURRENCY
        or cstr(_value(order_doc, "currency")).strip().upper()
        != MAYBANK_CURRENCY
    ):
        frappe.throw(
            "Automatic QR finalization requires MYR",
            frappe.ValidationError,
        )
    transaction_amount_sen = _strict_integer_sen(
        _value(transaction, "sale_amount_sen"),
        "Maybank QR transaction sale_amount_sen",
    )
    try:
        payment_amount_sen = persisted_money_to_sen(
            _value(payment, "amount"),
            "Prepared Automatic QR payment amount",
        )
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if transaction_amount_sen != payment_amount_sen:
        frappe.throw(
            "Maybank QR transaction amount does not match the prepared payment",
            frappe.ValidationError,
        )
    consumption_key = cstr(_value(transaction, "consumption_key")).strip()
    if consumption_key and consumption_key != order_name:
        frappe.throw(
            "Maybank QR transaction is consumed by another FB Order",
            frappe.ValidationError,
        )


def _apply_provider_paid_payment(payment: Any, transaction: Any) -> None:
    reference = cstr(_value(transaction, "transaction_refno")).strip()
    payment.reference_no = reference
    payment.external_transaction_id = reference
    payment.maybank_qr_transaction = cstr(_value(transaction, "name")).strip()
    payment.is_manual_confirmation = 0
    payment.settlement_status = "verified"
    payment.suspense_account = None
    payment.manual_qr_reconciliation = None


def _payment_matches_attempt(payment: Any, transaction: Any) -> bool:
    transaction_name = cstr(_value(transaction, "name")).strip()
    transaction_reference = cstr(
        _value(transaction, "transaction_refno")
    ).strip()
    linked_transaction = cstr(
        _value(payment, "maybank_qr_transaction")
    ).strip()
    external_reference = cstr(
        _value(payment, "external_transaction_id")
    ).strip()
    receipt_reference = cstr(_value(payment, "reference_no")).strip()
    return bool(
        transaction_reference
        and external_reference == transaction_reference
        and receipt_reference in {"", transaction_reference}
        and linked_transaction == transaction_name
    )


def _submitted_payment_matches_attempt(
    order_doc: Any,
    payment: Any,
    transaction: Any,
) -> bool:
    if not _payment_matches_attempt(payment, transaction):
        return False
    order_name = cstr(_value(order_doc, "name")).strip()
    if cstr(_value(transaction, "consumption_key")).strip() != order_name:
        return False
    order_invoice = cstr(_value(order_doc, "sales_invoice")).strip()
    transaction_invoice = cstr(_value(transaction, "sales_invoice")).strip()
    invoice_key = cstr(_value(transaction, "invoice_consumption_key")).strip()
    if transaction_invoice and transaction_invoice != order_invoice:
        return False
    if invoice_key and invoice_key != order_invoice:
        return False
    return True


def _reconcile_provider_paid_manual_settlement(
    *,
    order_doc: Any,
    payment: Any,
    transaction: Any,
) -> dict[str, Any]:
    """Let provider truth monotonically supersede manual reconciliation failure."""

    transaction_name = cstr(_value(transaction, "name")).strip()
    payment_row_name = cstr(_value(payment, "name")).strip()
    source_status = cstr(
        _value(transaction, "manual_reconciliation_status")
    ).strip()
    payment_status = cstr(_value(payment, "settlement_status")).strip()
    is_manual = bool(cint(_value(payment, "is_manual_confirmation"))) or (
        source_status
        in {
            "pending_reconciliation",
            "reconciled",
            "reconciliation_failed",
        }
        or payment_status
        in {
            "pending_reconciliation",
            "reconciled",
            "reconciliation_failed",
        }
    )
    if not is_manual:
        return {}

    if source_status == "reconciled" and payment_status == "reconciled":
        return {
            "settlement_status": "reconciled",
            "reclassification_journal_entry": cstr(
                _value(transaction, "reclassification_journal_entry")
            ).strip()
            or None,
        }

    provider_note = (
        "Authenticated Maybank provider-paid evidence superseded the earlier "
        "manual reconciliation result. The sale remains submitted while "
        "suspense reclassification completes."
    )
    if source_status != "pending_reconciliation":
        frappe.db.set_value(
            "Maybank QR Transaction",
            transaction_name,
            {
                "manual_reconciliation_status": "pending_reconciliation",
                "reconciliation_failed_reason": None,
                "reconciled_by": None,
                "reconciled_at": None,
                "reconciliation_note": provider_note,
            },
            update_modified=False,
        )
    if payment_status != "pending_reconciliation":
        frappe.db.set_value(
            "FB Order Payment",
            payment_row_name,
            "settlement_status",
            "pending_reconciliation",
            update_modified=False,
        )
        payment.settlement_status = "pending_reconciliation"

    try:
        _ensure_provider_paid_reconciliation_context(
            order_doc=order_doc,
            payment=payment,
            transaction=transaction,
        )
        accounting_evidence = ensure_qr_suspense_reclassification(
            {
                "doctype": "Maybank QR Transaction",
                "name": transaction_name,
            }
        )
    except Exception as error:
        # Provider-paid truth and the submitted sale remain durable. Projection
        # or accounting readiness is retried by the recovery sweep and cannot
        # push the monotonic settlement state back to reconciliation_failed.
        log_sanitized_error(
            "Automatic QR provider-paid suspense reclassification pending",
            error,
        )
        return {
            "settlement_status": "pending_reconciliation",
            "reclassification_journal_entry": None,
        }

    journal_entry = cstr(accounting_evidence.get("journal_entry")).strip()
    if not journal_entry:
        log_sanitized_error(
            "Automatic QR provider-paid suspense reclassification pending",
            RuntimeError("submitted Journal Entry evidence was not returned"),
        )
        return {
            "settlement_status": "pending_reconciliation",
            "reclassification_journal_entry": None,
        }

    reconciled_at = now_datetime()
    frappe.db.set_value(
        "Maybank QR Transaction",
        transaction_name,
        {
            "manual_reconciliation_status": "reconciled",
            "reconciliation_failed_reason": None,
            "reconciled_by": "Maybank provider-paid evidence",
            "reconciled_at": reconciled_at,
            "reconciliation_note": (
                "Authenticated Maybank provider-paid evidence reconciled the "
                "manual confirmation settlement."
            ),
            "reclassification_journal_entry": journal_entry,
        },
        update_modified=False,
    )
    frappe.db.set_value(
        "FB Order Payment",
        payment_row_name,
        "settlement_status",
        "reconciled",
        update_modified=False,
    )
    payment.settlement_status = "reconciled"
    return {
        "settlement_status": "reconciled",
        "reclassification_journal_entry": journal_entry,
    }


def _ensure_provider_paid_reconciliation_context(
    *,
    order_doc: Any,
    payment: Any,
    transaction: Any,
) -> None:
    """Backfill only sale-proven accounting context before JE creation."""

    transaction_name = cstr(_value(transaction, "name")).strip()
    order_name = cstr(_value(order_doc, "name")).strip()
    expected_company = cstr(_value(order_doc, "company")).strip()
    expected_currency = cstr(_value(order_doc, "currency")).strip().upper()
    expected_suspense = cstr(_value(payment, "suspense_account")).strip()
    expected_invoice = cstr(_value(order_doc, "sales_invoice")).strip()
    reconciliation_key = cstr(
        _value(transaction, "reconciliation_idempotency_key")
        or _value(payment, "reconciliation_idempotency_key")
    ).strip()
    if (
        not transaction_name
        or not order_name
        or not expected_company
        or not expected_currency
        or not expected_suspense
        or not expected_invoice
        or not reconciliation_key
    ):
        raise RuntimeError(
            "provider-paid manual QR accounting context is not ready"
        )

    expected_values = {
        "company": expected_company,
        "currency": expected_currency,
        "suspense_account": expected_suspense,
        "sales_invoice": expected_invoice,
        "invoice_consumption_key": expected_invoice,
        "reconciliation_idempotency_key": reconciliation_key,
    }
    updates: dict[str, str] = {}
    for fieldname, expected in expected_values.items():
        current = cstr(_value(transaction, fieldname)).strip()
        if fieldname == "currency":
            current = current.upper()
        if current and current != expected:
            frappe.throw(
                f"Maybank QR reconciliation {fieldname} does not match its submitted sale",
                frappe.ValidationError,
            )
        if not current:
            updates[fieldname] = expected

    if cstr(_value(transaction, "consumption_key")).strip() != order_name:
        frappe.throw(
            "Maybank QR reconciliation is not consumed by its submitted sale",
            frappe.ValidationError,
        )
    if updates:
        frappe.db.set_value(
            "Maybank QR Transaction",
            transaction_name,
            updates,
            update_modified=False,
        )


def _mark_order_finalized(order_doc: Any) -> None:
    if cstr(_value(order_doc, "automatic_qr_state")).strip() == "finalized":
        return
    frappe.db.set_value(
        "FB Order",
        cstr(_value(order_doc, "name")).strip(),
        "automatic_qr_state",
        "finalized",
        update_modified=False,
    )
    order_doc.automatic_qr_state = "finalized"


def _register_late_paid_incidents_after_sale_commit(
    later_attempts: list[Any],
    *,
    order_doc: Any,
    winning_transaction_name: str,
) -> list[str]:
    """Keep a valid winning sale independent of duplicate-incident writes.

    An additional provider payment is operationally important, but failure to
    write its support metadata must never roll back the customer's paid sale.
    The recovery lane will retry any unregistered incident independently.
    """

    if not later_attempts:
        return []

    # Establish the winning sale, invoice, and provider-consumption claim as a
    # durable checkpoint before any secondary incident bookkeeping begins.
    frappe.db.commit()
    pending: list[str] = []
    for later_attempt in later_attempts:
        transaction_name = cstr(_value(later_attempt, "name")).strip()
        try:
            fresh_order, fresh_attempt = _reload_late_paid_incident_for_update(
                cstr(_value(order_doc, "name")).strip(),
                transaction_name,
            )
            fresh_payment = _get_exact_payment(
                fresh_order,
                cstr(_value(fresh_attempt, "fb_order_payment")).strip(),
            )
            if _is_submitted_static_qr_winner(fresh_order, fresh_payment):
                resolve_late_paid_after_static_winner(
                    fresh_attempt,
                    order_doc=fresh_order,
                    paid_attempts=later_attempts,
                )
            else:
                _mark_late_paid_incident(
                    fresh_attempt,
                    order_doc=fresh_order,
                    winning_transaction_name=winning_transaction_name,
                )
            frappe.db.commit()
        except Exception as error:
            frappe.db.rollback()
            if transaction_name:
                pending.append(transaction_name)
            log_sanitized_error(
                "Duplicate Automatic QR payment incident registration pending",
                error,
            )
    return pending


def _reload_late_paid_incident_for_update(
    order_name: str,
    transaction_name: str,
) -> tuple[Any, Any]:
    """Reacquire order -> provider-row locks after the winning-sale commit.

    The commit deliberately releases the original locks, so the pre-commit row
    dictionaries are no longer settlement authority. Reloading prevents a
    concurrent System Manager refund from being regressed by stale incident data.
    """

    if not order_name or not transaction_name:
        frappe.throw(
            "Duplicate Automatic QR incident identity is incomplete",
            frappe.ValidationError,
        )
    locked_order = frappe.db.sql(
        "SELECT name FROM `tabFB Order` WHERE name = %s LIMIT 1 FOR UPDATE",
        (order_name,),
    )
    if len(locked_order or []) != 1:
        frappe.throw(
            "Duplicate Automatic QR winning FB Order was not found",
            frappe.ValidationError,
        )
    locked_transaction = frappe.db.sql(
        "SELECT name FROM `tabMaybank QR Transaction` WHERE name = %s LIMIT 1 FOR UPDATE",
        (transaction_name,),
    )
    if len(locked_transaction or []) != 1:
        frappe.throw(
            "Duplicate Automatic QR provider transaction was not found",
            frappe.ValidationError,
        )
    return (
        frappe.get_doc("FB Order", order_name),
        frappe.get_doc("Maybank QR Transaction", transaction_name),
    )


def _mark_late_paid_incident(
    transaction: Any,
    *,
    order_doc: Any,
    winning_transaction_name: str,
) -> dict[str, Any]:
    was_unregistered = not cstr(
        _value(transaction, "duplicate_payment_status")
    ).strip()
    result = register_duplicate_paid_incident(
        transaction,
        order_doc=order_doc,
        winning_transaction_name=winning_transaction_name,
    )
    if was_unregistered:
        log_sanitized_error(
            "KoPOS duplicate Automatic QR refund liability",
            RuntimeError(
                "Authenticated provider evidence reported an additional paid "
                f"Automatic QR attempt {cstr(_value(transaction, 'name')).strip()} "
                f"for FB Order {cstr(_value(order_doc, 'name')).strip()}. "
                f"Liability state: {cstr(result.get('duplicate_payment_status')).strip()}. "
                "No additional Sales Invoice was created and the winning sale lifecycle was not changed."
            ),
        )
    return result


def _result(
    status: str,
    *,
    order_doc: Any,
    transaction: Any,
) -> dict[str, Any]:
    return {
        "status": status,
        "transaction": cstr(_value(transaction, "name")).strip(),
        "fb_order": cstr(_value(order_doc, "name")).strip(),
        "sales_invoice": cstr(_value(order_doc, "sales_invoice")).strip() or None,
        "invoice_status": cstr(_value(order_doc, "invoice_status")).strip() or None,
    }


def _strict_integer_sen(value: Any, fieldname: str) -> int:
    if isinstance(value, bool):
        frappe.throw(f"{fieldname} must be integer sen", frappe.ValidationError)
    text = cstr(value).strip()
    if not text or not text.isdigit():
        frappe.throw(f"{fieldname} must be integer sen", frappe.ValidationError)
    resolved = int(text)
    if resolved <= 0:
        frappe.throw(f"{fieldname} must be positive", frappe.ValidationError)
    return resolved


def _normalized(value: Any) -> str:
    return normalize_qr_token(value)


def _value(document: Any, fieldname: str) -> Any:
    if isinstance(document, Mapping):
        return document.get(fieldname)
    getter = getattr(document, "get", None)
    if callable(getter):
        value = getter(fieldname)
        if value is not None:
            return value
    return getattr(document, fieldname, None)
