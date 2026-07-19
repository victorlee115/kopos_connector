# pyright: reportMissingImports=false

"""Locked Maybank QR persistence, replay, and durable fence evidence."""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cstr, now_datetime

from kopos_connector.utils.diagnostics import redacted_json

from ._maybank_qr_contract import (
    AMBIGUOUS_IDEMPOTENCY_MESSAGE,
    CREATION_LEASE_SECONDS,
    MAYBANK_CURRENCY,
    MAYBANK_QR_REQUEST_REJECTED_NO_PROVIDER_ATTEMPT,
    PREFLIGHT_REASON_CODES,
    PREFLIGHT_REASON_REPLACEMENT_REQUEST,
    REPLACEMENT_REJECTION_CODES,
    UNKNOWN_STATUS,
    _coerce_site_datetime,
    _existing_value,
    _format_sale_amount,
    _has_explicit_timezone,
    _parse_integer_sen,
    _persisted_sale_amount_sen,
    _require_exact_persisted_text,
    _require_provider_transaction_reference,
    _reservation_reference,
    _serialize_site_datetime,
)


MAYBANK_QR_DISPLAY_UNAVAILABLE = "maybank_qr_display_unavailable"

def _load_existing_txn(device_id: str, idempotency_key: str) -> Any:
    rows = frappe.db.sql(
        """
        SELECT
            name, transaction_refno, status, qr_data, sale_amount,
            sale_amount_sen, expires_at, device_id, provider, company, currency,
            business_date, idempotency_key, request_fingerprint, outlet_id, created_at,
            replacement_reason, replaces_transaction_refno,
            fb_order, fb_order_payment, sales_invoice,
            raw_response,
            last_polled_at, poll_count, maybank_status, paid_at, scanned_at
        FROM `tabMaybank QR Transaction`
        WHERE device_id = %s AND idempotency_key = %s
        ORDER BY creation, name
        LIMIT 2
        FOR UPDATE
        """,
        (device_id, idempotency_key),
        as_dict=True,
    )
    if len(rows or []) > 1:
        frappe.throw(AMBIGUOUS_IDEMPOTENCY_MESSAGE, frappe.ValidationError)
    return rows[0] if rows else None


def _load_reserved_txn_for_update(request_fingerprint: str) -> Any:
    rows = frappe.db.sql(
        """
        SELECT
            name, transaction_refno, status, qr_data, sale_amount,
            sale_amount_sen, expires_at, device_id, provider, company, currency,
            business_date, idempotency_key, request_fingerprint, outlet_id, created_at,
            replacement_reason, replaces_transaction_refno,
            fb_order, fb_order_payment, sales_invoice,
            raw_response,
            last_polled_at, poll_count, maybank_status, paid_at, scanned_at
        FROM `tabMaybank QR Transaction`
        WHERE request_fingerprint = %s
        LIMIT 1
        FOR UPDATE
        """,
        (request_fingerprint,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _load_reserved_txn_with_order_lock(request_fingerprint: str) -> Any:
    """Lock a prepared sale before its provider attempt.

    Generation starts without a database lock while the provider request is in
    flight.  Re-acquiring the parent FB Order lock first serializes the delayed
    result with replacement-attempt validation, whose lock order is also order
    then attempts.  This prevents an audited release from being consumed by a
    new request while the old result is simultaneously made reusable.
    """

    snapshot = frappe.db.get_value(
        "Maybank QR Transaction",
        {"request_fingerprint": request_fingerprint},
        ["name", "fb_order"],
        as_dict=True,
    )
    if not snapshot:
        return None

    snapshot_name = cstr(_existing_value(snapshot, "name")).strip()
    snapshot_order = cstr(_existing_value(snapshot, "fb_order")).strip()
    if snapshot_order:
        locked_order = frappe.db.sql(
            "SELECT name FROM `tabFB Order` WHERE name = %s LIMIT 1 FOR UPDATE",
            (snapshot_order,),
        )
        if len(locked_order or []) != 1:
            frappe.throw(
                "Prepared Automatic QR FB Order was not found",
                frappe.ValidationError,
            )

    reserved = _load_reserved_txn_for_update(request_fingerprint)
    if not reserved:
        return None
    if (
        cstr(_existing_value(reserved, "name")).strip() != snapshot_name
        or cstr(_existing_value(reserved, "fb_order")).strip() != snapshot_order
    ):
        frappe.throw(
            "Maybank QR reservation binding changed during provider generation",
            frappe.ValidationError,
        )
    return reserved


def _build_existing_txn_response(existing: Any) -> dict[str, Any]:
    transaction_refno = _require_provider_transaction_reference(
        _existing_value(existing, "transaction_refno"),
        "Maybank transaction_refno",
    )
    amount_sen = _persisted_sale_amount_sen(existing)
    fb_order = _require_exact_persisted_text(
        _existing_value(existing, "fb_order"),
        "Maybank transaction fb_order",
    )
    fb_order_payment = _require_exact_persisted_text(
        _existing_value(existing, "fb_order_payment"),
        "Maybank transaction fb_order_payment",
    )
    qr_data = cstr(_existing_value(existing, "qr_data"))
    if not qr_data.strip():
        return _build_display_unavailable_response(existing)
    return {
        "status": "ok",
        "qr_data": qr_data,
        "transaction_refno": transaction_refno,
        # sale_amount is retained for legacy display consumers, but is derived
        # from the exact persisted integer-sen authority rather than a second
        # independently mutable money field.
        "sale_amount": _format_sale_amount(amount_sen),
        "sale_amount_sen": amount_sen,
        "expires_at": _serialize_site_datetime(
            _existing_value(existing, "expires_at")
        ),
        "fb_order": fb_order,
        "fb_order_payment": fb_order_payment,
    }


def _build_display_unavailable_response(existing: Any) -> dict[str, Any]:
    """Return durable provider identity without inventing display data."""

    transaction_refno = _require_provider_transaction_reference(
        _existing_value(existing, "transaction_refno"),
        "Maybank transaction_refno",
    )
    if cstr(_existing_value(existing, "qr_data")).strip():
        frappe.throw(
            "Maybank display-unavailable response cannot hide persisted QR data",
            frappe.ValidationError,
        )
    amount_sen = _persisted_sale_amount_sen(existing)
    fb_order = _require_exact_persisted_text(
        _existing_value(existing, "fb_order"),
        "Maybank transaction fb_order",
    )
    fb_order_payment = _require_exact_persisted_text(
        _existing_value(existing, "fb_order_payment"),
        "Maybank transaction fb_order_payment",
    )
    provider_status = cstr(_existing_value(existing, "status")).strip()
    if provider_status not in {
        "pending",
        "scanned",
        "paid",
        "failed",
        "timeout",
        UNKNOWN_STATUS,
    }:
        frappe.throw(
            "Maybank display-unavailable transaction has an invalid status",
            frappe.ValidationError,
        )
    replacement_authorized = provider_status in {"pending", "failed", "timeout"}
    paid_at = _existing_value(existing, "paid_at")
    return {
        "status": "display_unavailable",
        "error_code": MAYBANK_QR_DISPLAY_UNAVAILABLE,
        "message": "Maybank did not return a usable QR display",
        "qr_data": "",
        "transaction_refno": transaction_refno,
        "sale_amount": _format_sale_amount(amount_sen),
        "sale_amount_sen": amount_sen,
        "expires_at": _serialize_site_datetime(
            _existing_value(existing, "expires_at")
        ),
        "fb_order": fb_order,
        "fb_order_payment": fb_order_payment,
        "provider_status": provider_status,
        "provider_request_attempted": True,
        "provider_reference_retained": True,
        "display_authorized": False,
        "replacement_authorized": replacement_authorized,
        "replacement_reason": (
            "unrenderable_display" if replacement_authorized else None
        ),
        "support_required": provider_status == UNKNOWN_STATUS,
        "paid_at": _serialize_site_datetime(paid_at) if paid_at else None,
        "sales_invoice": cstr(_existing_value(existing, "sales_invoice")) or None,
    }


def _build_paid_existing_txn_response(existing: Any) -> dict[str, Any]:
    response = _build_existing_txn_response(existing)
    if response.get("status") == "display_unavailable":
        return response
    paid_at = _existing_value(existing, "paid_at")
    response.update(
        {
            "status": "paid",
            "paid_at": _serialize_site_datetime(paid_at) if paid_at else None,
            "sales_invoice": cstr(_existing_value(existing, "sales_invoice"))
            or None,
        }
    )
    return response


def _parse_preflight_rejection_evidence(existing: Any) -> dict[str, Any] | None:
    raw_response = _existing_value(existing, "raw_response")
    if isinstance(raw_response, str):
        try:
            evidence = json.loads(raw_response)
        except (TypeError, ValueError):
            return None
    elif isinstance(raw_response, dict):
        evidence = raw_response
    else:
        return None
    if not isinstance(evidence, dict):
        return None

    reason_code = cstr(evidence.get("preflight_reason_code")).strip()
    if (
        cstr(evidence.get("status")).strip() != "rejected"
        or cstr(evidence.get("error_code")).strip()
        != MAYBANK_QR_REQUEST_REJECTED_NO_PROVIDER_ATTEMPT
        or reason_code not in PREFLIGHT_REASON_CODES
        or evidence.get("provider_request_attempted") is not False
        or evidence.get("local_release_authorized") is not True
        or evidence.get("rejection_fence_registered") is not True
        or cstr(evidence.get("recovery_action")).strip()
        != "release_local_provider_intent"
    ):
        return None

    replacement_intent_rejected = evidence.get("replacement_intent_rejected")
    if replacement_intent_rejected is True:
        replacement_reason = cstr(evidence.get("replacement_reason"))
        replaces_reference = cstr(
            evidence.get("replaces_transaction_refno")
        )
        try:
            _require_provider_transaction_reference(
                replaces_reference,
                "preflight replacement transaction_refno",
            )
        except frappe.ValidationError:
            return None
        if (
            replacement_reason
            not in {"expired_display", "unrenderable_display"}
            or evidence.get("prior_provider_reference_retained") is not True
            or cstr(evidence.get("release_scope")).strip()
            != "replacement_intent_only"
        ):
            return None
        replacement_rejection_code = cstr(
            evidence.get("replacement_rejection_code")
        ).strip()
        if reason_code == PREFLIGHT_REASON_REPLACEMENT_REQUEST:
            if replacement_rejection_code not in REPLACEMENT_REJECTION_CODES:
                return None
        elif replacement_rejection_code != reason_code:
            return None
    elif any(
        key in evidence
        for key in (
            "replacement_intent_rejected",
            "replacement_reason",
            "replaces_transaction_refno",
            "prior_provider_reference_retained",
            "release_scope",
            "replacement_rejection_code",
        )
    ):
        return None
    return evidence


def _build_preflight_rejection_response(
    *,
    device_id: str,
    idempotency_key: str,
    amount_sen: int,
    reason_code: str,
    message: str,
    checked_at: str,
    replacement_reason: str = "",
    replaces_transaction_refno: str = "",
    replacement_rejection_code: str = "",
) -> dict[str, Any]:
    if reason_code not in PREFLIGHT_REASON_CODES:
        raise ValueError("unsupported Maybank QR preflight rejection reason")
    response = {
        "status": "rejected",
        "error_code": MAYBANK_QR_REQUEST_REJECTED_NO_PROVIDER_ATTEMPT,
        "message": message,
        "preflight_reason_code": reason_code,
        "provider_request_attempted": False,
        "rejection_fence_registered": True,
        "local_release_authorized": True,
        "recovery_action": "release_local_provider_intent",
        "device_id": device_id,
        "idempotency_key": idempotency_key,
        "amount_sen": amount_sen,
        "currency": MAYBANK_CURRENCY,
        "checked_at": checked_at,
    }
    if replacement_reason or replaces_transaction_refno:
        if (
            replacement_reason
            not in {"expired_display", "unrenderable_display"}
            or not replaces_transaction_refno
        ):
            raise ValueError("invalid Automatic QR replacement rejection identity")
        _require_provider_transaction_reference(
            replaces_transaction_refno,
            "preflight replacement transaction_refno",
        )
        if reason_code == PREFLIGHT_REASON_REPLACEMENT_REQUEST:
            if replacement_rejection_code not in REPLACEMENT_REJECTION_CODES:
                raise ValueError("invalid Automatic QR replacement rejection code")
        elif replacement_rejection_code != reason_code:
            raise ValueError(
                "replacement preflight failure must retain its exact reason code"
            )
        response.update(
            {
                "replacement_intent_rejected": True,
                "replacement_rejection_code": replacement_rejection_code,
                "replacement_reason": replacement_reason,
                "replaces_transaction_refno": replaces_transaction_refno,
                "prior_provider_reference_retained": True,
                "release_scope": "replacement_intent_only",
            }
        )
    return response


def _build_persisted_preflight_rejection_response(
    existing: Any,
    *,
    device_id: str,
    idempotency_key: str,
    amount_sen: int,
) -> dict[str, Any] | None:
    if cstr(_existing_value(existing, "status")).strip() != "failed":
        return None
    request_fingerprint = cstr(
        _existing_value(existing, "request_fingerprint")
    ).strip()
    if (
        not request_fingerprint
        or cstr(_existing_value(existing, "transaction_refno")).strip()
        != _reservation_reference(request_fingerprint)
        or cstr(_existing_value(existing, "device_id")).strip() != device_id
        or cstr(_existing_value(existing, "idempotency_key")).strip()
        != idempotency_key
        or cstr(_existing_value(existing, "currency")).strip().upper()
        != MAYBANK_CURRENCY
        # Frappe persists an unset Int field as numeric zero. Zero is not a
        # provider response here: the reservation reference plus the exact
        # server-authored no-provider fence below remain mandatory. Any real
        # provider status (including pending=2 or paid=1) still fails closed.
        or _existing_value(existing, "maybank_status") not in (None, "", 0)
    ):
        return None

    evidence = _parse_preflight_rejection_evidence(existing)
    if evidence is None:
        return None
    evidence_replacement = evidence.get("replacement_intent_rejected") is True
    persisted_replacement_reason = cstr(
        _existing_value(existing, "replacement_reason")
    )
    persisted_replaces_reference = cstr(
        _existing_value(existing, "replaces_transaction_refno")
    )
    if evidence_replacement:
        if (
            persisted_replacement_reason
            != cstr(evidence.get("replacement_reason"))
            or persisted_replaces_reference
            != cstr(evidence.get("replaces_transaction_refno"))
        ):
            return None
    elif persisted_replacement_reason or persisted_replaces_reference:
        return None
    try:
        evidence_amount_sen = _parse_integer_sen(
            evidence.get("amount_sen"),
            "preflight rejection amount_sen",
        )
    except frappe.ValidationError:
        return None
    if (
        cstr(evidence.get("device_id")).strip() != device_id
        or cstr(evidence.get("idempotency_key")).strip() != idempotency_key
        or evidence_amount_sen != amount_sen
        or cstr(evidence.get("currency")).strip().upper() != MAYBANK_CURRENCY
        or not _has_explicit_timezone(evidence.get("checked_at"))
    ):
        return None
    return _build_preflight_rejection_response(
        device_id=device_id,
        idempotency_key=idempotency_key,
        amount_sen=amount_sen,
        reason_code=cstr(evidence.get("preflight_reason_code")).strip(),
        message=cstr(evidence.get("message")).strip()
        or "Automatic QR request was rejected before contacting the provider",
        checked_at=cstr(evidence.get("checked_at")).strip(),
        replacement_reason=cstr(evidence.get("replacement_reason")),
        replaces_transaction_refno=cstr(
            evidence.get("replaces_transaction_refno")
        ),
        replacement_rejection_code=cstr(
            evidence.get("replacement_rejection_code")
        ),
    )


def _build_creation_recovery_response(existing: Any, now: Any) -> dict[str, Any]:
    created_at = _coerce_site_datetime(_existing_value(existing, "created_at"))
    resolved_now = _coerce_site_datetime(now)
    age_seconds = max(0, int((resolved_now - created_at).total_seconds()))
    active = age_seconds < CREATION_LEASE_SECONDS
    return {
        "status": "creating" if active else "generation_ambiguous",
        "error_code": (
            "maybank_qr_generation_in_progress"
            if active
            else "maybank_qr_generation_ambiguous"
        ),
        "message": (
            "Maybank QR generation is still in progress"
            if active
            else "Maybank QR generation outcome is ambiguous and requires support reconciliation"
        ),
        "transaction_refno": cstr(
            _existing_value(existing, "transaction_refno")
        ),
        "request_fingerprint": cstr(
            _existing_value(existing, "request_fingerprint")
        ),
        "provider_replay_blocked": True,
        "support_required": not active,
        "recovery_action": (
            "wait_and_check_generation"
            if active
            else "resolve_maybank_qr_generation"
        ),
        "retry_after_seconds": max(1, CREATION_LEASE_SECONDS - age_seconds)
        if active
        else None,
    }


def _record_poll_attempt(txn_name: str, payload: object) -> None:
    frappe.db.sql(
        """
        UPDATE `tabMaybank QR Transaction`
        SET last_polled_at = %s,
            poll_count = poll_count + 1,
            raw_response = %s
        WHERE name = %s
        """,
        (now_datetime(), redacted_json(payload), txn_name),
    )


def _record_poll_observation(txn_name: str) -> None:
    """Count a stale/untrusted response without replacing durable evidence."""

    frappe.db.sql(
        """
        UPDATE `tabMaybank QR Transaction`
        SET last_polled_at = %s,
            poll_count = poll_count + 1
        WHERE name = %s
        """,
        (now_datetime(), txn_name),
    )


def _load_txn_for_update(txn_name: str) -> Any:
    rows = frappe.db.sql(
        """
        SELECT
            name, transaction_refno, status, qr_data, sale_amount,
            sale_amount_sen, expires_at, device_id, provider, company, currency,
            business_date, request_fingerprint, outlet_id, created_at,
            idempotency_key, fb_order, fb_order_payment, sales_invoice,
            replacement_reason, replaces_transaction_refno, raw_response,
            last_polled_at, poll_count, maybank_status, paid_at, scanned_at,
            is_test_simulation, test_simulation_key,
            test_simulation_identity_sha256, test_simulated_by,
            test_simulated_at
        FROM `tabMaybank QR Transaction`
        WHERE name = %s
        LIMIT 1
        FOR UPDATE
        """,
        (txn_name,),
        as_dict=True,
    )
    if len(rows or []) != 1:
        frappe.throw(
            "Maybank QR transaction no longer exists",
            frappe.ValidationError,
        )
    return rows[0]


def _load_linked_generation_attempts_for_update(
    fb_order: str,
    fb_order_payment: str,
) -> list[Any]:
    return list(
        frappe.db.sql(
            """
            SELECT
                name, transaction_refno, status, maybank_status,
                qr_data, expires_at, sale_amount_sen, currency, provider,
                company, device_id,
                idempotency_key, request_fingerprint,
                replacement_reason, replaces_transaction_refno, round_number,
                fb_order, fb_order_payment, sales_invoice,
                creation, created_at, paid_at, raw_response
            FROM `tabMaybank QR Transaction`
            WHERE fb_order = %s AND fb_order_payment = %s
            ORDER BY creation, name
            FOR UPDATE
            """,
            (fb_order, fb_order_payment),
            as_dict=True,
        )
        or []
    )


def _raw_response_object(attempt: Any) -> dict[str, Any] | None:
    raw_response = _existing_value(attempt, "raw_response")
    if isinstance(raw_response, dict):
        return raw_response
    if not isinstance(raw_response, str):
        return None
    try:
        parsed = json.loads(raw_response)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _durable_generation_release(attempt: Any) -> str | None:
    """Return the exact audited fence authorizing a different provider attempt."""

    device_id = cstr(_existing_value(attempt, "device_id")).strip()
    idempotency_key = cstr(
        _existing_value(attempt, "idempotency_key")
    ).strip()
    try:
        amount_sen = _parse_integer_sen(
            _existing_value(attempt, "sale_amount_sen"),
            "linked Maybank QR attempt sale_amount_sen",
        )
    except frappe.ValidationError:
        return None
    if (
        device_id
        and idempotency_key
        and _build_persisted_preflight_rejection_response(
            attempt,
            device_id=device_id,
            idempotency_key=idempotency_key,
            amount_sen=amount_sen,
        )
        is not None
    ):
        return "preflight_rejected"

    evidence = _raw_response_object(attempt)
    if evidence is None:
        return None
    resolution = cstr(evidence.get("resolution")).strip()
    resolved_by = cstr(evidence.get("resolved_by")).strip()
    resolved_at = cstr(evidence.get("resolved_at")).strip()
    reason = cstr(evidence.get("reason")).strip()
    evidence_reference = cstr(evidence.get("evidence_reference")).strip()
    if (
        not resolved_by
        or not _has_explicit_timezone(resolved_at)
        or not reason
        or not evidence_reference
    ):
        return None

    status = cstr(_existing_value(attempt, "status")).strip()
    if (
        status == UNKNOWN_STATUS
        and cstr(evidence.get("status")).strip() == "generation_abandoned"
        and resolution == "provider_transaction_absent"
        and evidence.get("provider_replay_blocked") is True
    ):
        return resolution
    if (
        status == "timeout"
        and cstr(evidence.get("status")).strip() == "timeout"
        and resolution == "provider_transaction_cancelled"
        and cstr(evidence.get("provider_reference")).strip()
        == cstr(_existing_value(attempt, "transaction_refno")).strip()
    ):
        return resolution
    return None


def _load_generation_snapshot(transaction_name: str) -> Any:
    rows = frappe.db.sql(
        """
        SELECT
            name, transaction_refno, status, qr_data, sale_amount,
            sale_amount_sen, expires_at, device_id, provider, currency,
            business_date, request_fingerprint, outlet_id, created_at,
            fb_order, fb_order_payment, sales_invoice,
            last_polled_at, poll_count, maybank_status, paid_at, scanned_at
        FROM `tabMaybank QR Transaction`
        WHERE name = %s
        LIMIT 1
        """,
        (transaction_name,),
        as_dict=True,
    )
    if len(rows or []) != 1:
        frappe.throw("Maybank QR transaction was not found", frappe.ValidationError)
    return rows[0]
