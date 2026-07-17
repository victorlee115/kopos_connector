# pyright: reportMissingImports=false

"""Maybank QR provider status polling and monotonic transition handling."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import add_to_date, cint, cstr, now_datetime

from kopos_connector.services.maybank.client import MaybankClient
from kopos_connector.utils.diagnostics import log_sanitized_error, redacted_json

from ._maybank_qr_contract import (
    GRACE_SECONDS,
    MAYBANK_CURRENCY,
    MAYBANK_PROVIDER,
    PAID_TRANSACTION_MESSAGE,
    PAYMENT_STATUS_RESPONSE_STATUSES,
    POLLABLE_STATUSES,
    PROVIDER_STATUS_TRANSITIONS,
    REUSABLE_STATUSES,
    STATUS_MAP,
    UNKNOWN_STATUS,
    USED_IDEMPOTENCY_MESSAGE,
    _coerce_site_datetime,
    _existing_value,
    _extract_status_entry,
    _format_sale_amount,
    _parse_integer_sen,
    _persisted_sale_amount_sen,
    _require_exact_persisted_text,
    _require_provider_transaction_reference,
    _serialize_site_datetime,
    _validate_status_entry_identity,
    _validate_status_response,
)
from ._maybank_qr_persistence import (
    _build_creation_recovery_response,
    _build_existing_txn_response,
    _build_paid_existing_txn_response,
    _build_persisted_preflight_rejection_response,
    _durable_generation_release,
    _load_existing_txn,
    _load_txn_for_update,
    _record_poll_attempt,
)

def _enqueue_paid_automatic_qr_finalization(
    transaction_name: str,
    transaction: Any,
) -> None:
    if (
        not cstr(_existing_value(transaction, "fb_order")).strip()
        or not cstr(_existing_value(transaction, "fb_order_payment")).strip()
    ):
        return
    try:
        from kopos_connector.kopos.services.accounting.automatic_qr_finalization_service import (
            enqueue_automatic_qr_finalization,
        )

        enqueue_automatic_qr_finalization(transaction_name)
    except Exception as error:
        # Provider-paid truth is already durable. Do not roll it back because
        # the queue is unavailable; the scheduled recovery sweep will submit
        # the prepared sale from the same evidence.
        log_sanitized_error(
            "Maybank paid Automatic QR finalization enqueue failed",
            error,
        )


def _resolve_existing_txn(
    device_id: str,
    idempotency_key: str,
    amount_sen: int,
    now: Any,
    *,
    existing: Any | None = None,
    allow_paid_replay: bool = False,
) -> dict[str, Any] | None:
    existing = existing or _load_existing_txn(device_id, idempotency_key)
    if not existing:
        return None

    existing_device_id = cstr(_existing_value(existing, "device_id"))
    if existing_device_id and existing_device_id != device_id:
        frappe.throw("existing transaction belongs to another device")

    existing_amount_sen = _parse_integer_sen(
        _existing_value(existing, "sale_amount_sen"),
        "existing transaction sale_amount_sen",
    )
    if existing_amount_sen != amount_sen:
        frappe.throw("existing transaction amount does not match idempotency key")

    preflight_rejection = _build_persisted_preflight_rejection_response(
        existing,
        device_id=device_id,
        idempotency_key=idempotency_key,
        amount_sen=amount_sen,
    )
    if preflight_rejection is not None:
        return preflight_rejection

    status = cstr(_existing_value(existing, "status"))
    if status == "creating":
        return _build_creation_recovery_response(existing, now)
    if status == UNKNOWN_STATUS:
        if _durable_generation_release(existing) != "provider_transaction_absent":
            frappe.throw(USED_IDEMPOTENCY_MESSAGE, frappe.ValidationError)
        return {
            **_build_creation_recovery_response(existing, now),
            "status": "generation_abandoned",
            "error_code": "maybank_qr_generation_abandoned",
            "message": (
                "This Maybank QR generation was abandoned after audited support "
                "review; use a new idempotency key"
            ),
            "support_required": True,
            "recovery_action": "start_new_generation_with_new_idempotency_key",
            "retry_after_seconds": None,
        }
    if (
        status == "timeout"
        and _durable_generation_release(existing)
        == "provider_transaction_cancelled"
    ):
        return {
            "status": "timeout",
            "transaction_name": cstr(_existing_value(existing, "name")),
            "transaction_refno": cstr(
                _existing_value(existing, "transaction_refno")
            ),
            "resolution": "provider_transaction_cancelled",
            "new_generation_authorized": True,
            "old_idempotency_key_replay_blocked": True,
            "fb_order": cstr(_existing_value(existing, "fb_order")),
            "fb_order_payment": cstr(
                _existing_value(existing, "fb_order_payment")
            ),
        }
    if status in REUSABLE_STATUSES:
        expires_at = _coerce_site_datetime(_existing_value(existing, "expires_at"))
        if (
            status in REUSABLE_STATUSES
            and add_to_date(expires_at, seconds=GRACE_SECONDS) > now
        ):
            return _build_existing_txn_response(existing)

    if status == "paid":
        if allow_paid_replay:
            return _build_paid_existing_txn_response(existing)
        frappe.throw(PAID_TRANSACTION_MESSAGE)

    frappe.throw(USED_IDEMPOTENCY_MESSAGE, frappe.ValidationError)
    return None


def _poll_txn_status(txn: Any) -> str:
    client = MaybankClient.from_settings()
    result = client.check_status(txn.transaction_refno)
    return _apply_provider_poll_result(txn.name, result)


def _apply_provider_poll_result(txn_name: str, result: dict[str, Any]) -> str:
    """Apply one provider response against a freshly locked transaction row.

    Provider calls happen before this lock. A stale response can therefore arrive
    after a newer worker has recorded payment; the locked transition table makes
    that response observational only and prevents any backward state transition.
    """
    try:
        _validate_status_response(result)
        entry = _extract_status_entry(result)
    except Exception:
        _record_poll_attempt(txn_name, result)
        raise
    if entry is None:
        frappe.log_error(
            f"Maybank empty response for transaction {txn_name}",
            "Maybank poll: empty data",
        )
        _record_poll_attempt(txn_name, result)
        return cstr(frappe.db.get_value("Maybank QR Transaction", txn_name, "status"))

    locked_txn = _load_txn_for_update(txn_name)
    try:
        raw_status = _validate_status_entry_identity(locked_txn, entry)
    except Exception:
        _record_poll_attempt(txn_name, result)
        raise
    new_status = STATUS_MAP.get(str(raw_status), UNKNOWN_STATUS)
    return _transition_txn_status_locked(
        locked_txn,
        new_status,
        raw_status,
        result,
    )


def _build_payment_status_response(
    transaction: Any,
    *,
    expected_transaction_refno: str,
    expected_device_id: str | None,
) -> dict[str, Any]:
    transaction_refno = _require_provider_transaction_reference(
        _existing_value(transaction, "transaction_refno"),
        "Maybank transaction transaction_refno",
    )
    if transaction_refno != expected_transaction_refno:
        frappe.throw(
            "Maybank persisted transaction reference does not match the requested session",
            frappe.ValidationError,
        )

    if (
        cstr(_existing_value(transaction, "provider")).strip().lower()
        != MAYBANK_PROVIDER
    ):
        frappe.throw(
            "Maybank transaction provider metadata is invalid",
            frappe.ValidationError,
        )
    if (
        cstr(_existing_value(transaction, "currency")).strip().upper()
        != MAYBANK_CURRENCY
    ):
        frappe.throw(
            "Maybank transaction currency metadata must be MYR",
            frappe.ValidationError,
        )
    if expected_device_id is not None:
        persisted_device_id = _require_exact_persisted_text(
            _existing_value(transaction, "device_id"),
            "Maybank transaction device_id",
        )
        if persisted_device_id != expected_device_id:
            frappe.throw(
                "Maybank persisted transaction device does not match the authenticated device",
                frappe.ValidationError,
            )

    status = cstr(_existing_value(transaction, "status")).strip()
    if status not in PAYMENT_STATUS_RESPONSE_STATUSES:
        frappe.throw(
            "Maybank transaction has an invalid persisted payment status",
            frappe.ValidationError,
        )
    amount_sen = _persisted_sale_amount_sen(transaction)
    paid_at = _existing_value(transaction, "paid_at")
    return {
        "status": status,
        "transaction_refno": transaction_refno,
        "sale_amount": _format_sale_amount(amount_sen),
        "sale_amount_sen": amount_sen,
        "paid_at": _serialize_site_datetime(paid_at) if paid_at else None,
    }


def check_maybank_payment_payload(
    transaction_refno: str, device_id: str | None = None
) -> dict[str, Any]:
    expected_transaction_refno = _require_provider_transaction_reference(
        transaction_refno,
        "transaction_refno",
    )
    expected_device_id = (
        _require_exact_persisted_text(device_id, "device_id")
        if device_id is not None
        else None
    )

    filters: dict[str, Any] = {"transaction_refno": expected_transaction_refno}
    if expected_device_id is not None:
        filters["device_id"] = expected_device_id

    txn_name = frappe.db.get_value("Maybank QR Transaction", filters, "name")
    if not txn_name:
        frappe.throw("Transaction not found", frappe.ValidationError)

    txn = frappe.get_doc("Maybank QR Transaction", txn_name)

    persisted_status = cstr(txn.status)
    poll_attempted = False
    if persisted_status in POLLABLE_STATUSES:
        last_poll = txn.last_polled_at or txn.created_at
        if (now_datetime() - last_poll).total_seconds() > 2:
            poll_attempted = True
            try:
                _poll_txn_status(txn)
            except Exception as error:
                log_sanitized_error("Maybank on-demand poll failed", error)

    # Polling persists provider truth under a row lock. Re-read after an
    # attempted poll so the response cannot be assembled from the stale
    # pre-poll document or from untrusted provider/client payload fields.
    if poll_attempted:
        txn = frappe.get_doc("Maybank QR Transaction", txn_name)
    return _build_payment_status_response(
        txn,
        expected_transaction_refno=expected_transaction_refno,
        expected_device_id=expected_device_id,
    )


def _update_txn_status(
    name: str, status: str, raw_status: int, raw_response: dict[str, Any]
) -> str:
    locked_txn = _load_txn_for_update(name)
    return _transition_txn_status_locked(
        locked_txn,
        status,
        raw_status,
        raw_response,
    )


def _transition_txn_status_locked(
    locked_txn: Any,
    status: str,
    raw_status: int,
    raw_response: dict[str, Any],
) -> str:
    name = cstr(_existing_value(locked_txn, "name")).strip()
    current_status = cstr(_existing_value(locked_txn, "status")).strip()
    if not name or current_status not in PROVIDER_STATUS_TRANSITIONS:
        frappe.throw(
            "Maybank transaction has an invalid persisted status",
            frappe.ValidationError,
        )
    if status not in PROVIDER_STATUS_TRANSITIONS:
        frappe.throw(
            "Maybank provider status cannot be persisted",
            frappe.ValidationError,
        )

    if status != current_status and status not in PROVIDER_STATUS_TRANSITIONS[
        current_status
    ]:
        _record_poll_attempt(name, raw_response)
        return current_status

    poll_now = now_datetime()
    updates: dict[str, Any] = {
        "status": status,
        "maybank_status": raw_status,
        "last_polled_at": poll_now,
        "poll_count": cint(_existing_value(locked_txn, "poll_count")) + 1,
        "raw_response": redacted_json(raw_response),
    }
    if status == "scanned" and not _existing_value(locked_txn, "scanned_at"):
        updates["scanned_at"] = poll_now
    elif status == "paid" and not _existing_value(locked_txn, "paid_at"):
        updates["paid_at"] = poll_now

    if status == current_status:
        updates.pop("status")
    frappe.db.set_value(
        "Maybank QR Transaction",
        name,
        updates,
        update_modified=False,
    )

    if status != current_status:
        txn = frappe.get_doc("Maybank QR Transaction", name)
        frappe.publish_realtime(
            "maybank_payment_status",
            {
                "transaction_refno": txn.transaction_refno,
                "status": status,
            },
            user=txn.owner,
            after_commit=True,
        )
        if status == "paid":
            _enqueue_paid_automatic_qr_finalization(name, locked_txn)
    return status
