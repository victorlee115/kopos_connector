# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import html
import json
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import (
    add_to_date,
    cint,
    cstr,
    get_datetime,
    get_system_timezone,
    now_datetime,
)

from kopos_connector.services.maybank.client import MaybankClient
from kopos_connector.utils.diagnostics import log_sanitized_error, redacted_json

STATUS_MAP = {
    "0": "failed",
    "1": "paid",
    "2": "pending",
    "3": "scanned",
    "4": "failed",
    "6": "timeout",
}
MAX_AMOUNT_SEN = 10_000_000
MAX_QR_PER_MINUTE = 10
DEFAULT_QR_PER_OUTLET_PER_MINUTE = 120
QR_RATE_LIMIT_WINDOW_SECONDS = 60
QR_RATE_LIMIT_SCRIPT = """
local window_seconds = tonumber(ARGV[1])
local device_limit = tonumber(ARGV[2])
local outlet_limit = tonumber(ARGV[3])
local device_count = tonumber(redis.call('GET', KEYS[1]) or '0')
local outlet_count = tonumber(redis.call('GET', KEYS[2]) or '0')
if device_count >= device_limit then
    return {device_limit + 1, outlet_count}
end
if outlet_count >= outlet_limit then
    return {device_count, outlet_limit + 1}
end
device_count = redis.call('INCR', KEYS[1])
if device_count == 1 then
    redis.call('EXPIRE', KEYS[1], window_seconds)
end
outlet_count = redis.call('INCR', KEYS[2])
if outlet_count == 1 then
    redis.call('EXPIRE', KEYS[2], window_seconds)
end
return {device_count, outlet_count}
"""
DEFAULT_QR_TTL_SECONDS = 60
GRACE_SECONDS = 30
CREATION_LEASE_SECONDS = 2 * 60
CREATION_ABANDON_AFTER_SECONDS = 15 * 60
CREATION_ABANDON_CONFIRMATION = "ABANDON AMBIGUOUS MAYBANK QR"
PENDING_RECONCILIATION_RESOLUTION_AFTER_SECONDS = 24 * 60 * 60
PENDING_RECONCILIATION_CONFIRMATION = "CLOSE MAYBANK QR RECONCILIATION"
PAID_TRANSACTION_MESSAGE = "payment already completed for this order"
REUSABLE_STATUSES = ("pending", "scanned")
POLLABLE_STATUSES = frozenset(REUSABLE_STATUSES)
UNKNOWN_STATUS = "unknown"
MAYBANK_PROVIDER = "maybank_qr"
MAYBANK_CURRENCY = "MYR"
PROVIDER_STATUS_TRANSITIONS = {
    "creating": frozenset({"pending", "scanned", "paid", "failed", "timeout"}),
    "pending": frozenset({"scanned", "paid", "failed", "timeout"}),
    "scanned": frozenset({"paid", "failed", "timeout"}),
    "paid": frozenset(),
    "failed": frozenset(),
    "timeout": frozenset(),
    # An audited ambiguous-generation abandonment can still be superseded by
    # a late provider response from the original in-flight call.
    "unknown": frozenset({"pending", "scanned", "paid", "failed", "timeout"}),
}
USED_IDEMPOTENCY_MESSAGE = (
    "idempotency_key has already been used; use a new idempotency_key"
)
AMBIGUOUS_IDEMPOTENCY_MESSAGE = (
    "Maybank QR idempotency evidence is ambiguous; provider access is blocked "
    "until the duplicate records are reconciled"
)


def _serialize_site_datetime(value: Any) -> str:
    dt = get_datetime(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(get_system_timezone()))
    return dt.isoformat()


def _coerce_site_datetime(value: Any) -> Any:
    dt = get_datetime(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(get_system_timezone()))
    return dt


def _load_existing_txn(device_id: str, idempotency_key: str) -> Any:
    rows = frappe.db.sql(
        """
        SELECT
            name, transaction_refno, status, qr_data, sale_amount,
            sale_amount_sen, expires_at, device_id, provider, currency,
            business_date, request_fingerprint, outlet_id, created_at,
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
            sale_amount_sen, expires_at, device_id, provider, currency,
            business_date, request_fingerprint, outlet_id, created_at,
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


def _build_existing_txn_response(existing: Any) -> dict[str, Any]:
    return {
        "status": "ok",
        "qr_data": cstr(_existing_value(existing, "qr_data")),
        "transaction_refno": cstr(_existing_value(existing, "transaction_refno")),
        "sale_amount": cstr(_existing_value(existing, "sale_amount")),
        "expires_at": _serialize_site_datetime(_existing_value(existing, "expires_at")),
    }


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


def _existing_value(existing: Any, fieldname: str) -> Any:
    if isinstance(existing, dict):
        return existing.get(fieldname)
    return getattr(existing, fieldname, None)


def _resolve_existing_txn(
    device_id: str,
    idempotency_key: str,
    amount_sen: int,
    now: Any,
    *,
    existing: Any | None = None,
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

    status = cstr(_existing_value(existing, "status"))
    if status == "creating":
        return _build_creation_recovery_response(existing, now)
    if status == UNKNOWN_STATUS:
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
    if status in REUSABLE_STATUSES:
        expires_at = _coerce_site_datetime(_existing_value(existing, "expires_at"))
        if (
            status in REUSABLE_STATUSES
            and add_to_date(expires_at, seconds=GRACE_SECONDS) > now
        ):
            return _build_existing_txn_response(existing)

    if status == "paid":
        frappe.throw(PAID_TRANSACTION_MESSAGE)

    frappe.throw(USED_IDEMPOTENCY_MESSAGE, frappe.ValidationError)
    return None


def _extract_status_entry(result: dict[str, Any]) -> dict[str, Any] | None:
    data = result.get("data")
    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    return None


def _poll_txn_status(txn: Any) -> str:
    client = MaybankClient.from_settings()
    result = client.check_status(txn.transaction_refno)
    return _apply_provider_poll_result(txn.name, result)


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


def _load_txn_for_update(txn_name: str) -> Any:
    rows = frappe.db.sql(
        """
        SELECT
            name, transaction_refno, status, qr_data, sale_amount,
            sale_amount_sen, expires_at, device_id, provider, currency,
            business_date, request_fingerprint, outlet_id, created_at,
            last_polled_at, poll_count, maybank_status, paid_at, scanned_at
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


def _validate_status_response(result: dict[str, Any]) -> None:
    if cstr(result.get("status")).strip() != "QR000":
        frappe.throw(
            "Maybank status response was not successful",
            frappe.ValidationError,
        )


def _validate_status_entry_identity(txn: Any, entry: dict[str, Any]) -> int:
    if cstr(_existing_value(txn, "provider")).strip().lower() != MAYBANK_PROVIDER:
        frappe.throw(
            "Maybank transaction provider metadata is invalid",
            frappe.ValidationError,
        )

    expected_device = cstr(_existing_value(txn, "device_id")).strip()
    if not expected_device:
        frappe.throw(
            "Maybank transaction device metadata is missing",
            frappe.ValidationError,
        )

    expected_reference = cstr(_existing_value(txn, "transaction_refno")).strip()
    response_reference = cstr(entry.get("transaction_refno")).strip()
    if not response_reference or response_reference != expected_reference:
        frappe.throw(
            "Maybank status response transaction reference does not match",
            frappe.ValidationError,
        )

    response_amount = entry.get("sale_amount")
    if response_amount is None:
        response_amount = entry.get("amount")
    response_amount_sen = _parse_provider_amount_sen(response_amount)
    expected_amount_sen = _parse_integer_sen(
        _existing_value(txn, "sale_amount_sen"),
        "Maybank transaction sale_amount_sen",
    )
    if response_amount_sen != expected_amount_sen:
        frappe.throw(
            "Maybank status response amount does not match transaction",
            frappe.ValidationError,
        )

    expected_outlet = cstr(_existing_value(txn, "outlet_id")).strip()
    if not expected_outlet:
        frappe.throw(
            "Maybank transaction outlet metadata is missing",
            frappe.ValidationError,
        )
    response_outlet = cstr(entry.get("outlet_id")).strip()
    if response_outlet and response_outlet != expected_outlet:
        frappe.throw(
            "Maybank status response outlet does not match transaction",
            frappe.ValidationError,
        )

    expected_currency = cstr(_existing_value(txn, "currency")).strip().upper()
    if expected_currency != MAYBANK_CURRENCY:
        frappe.throw(
            "Maybank transaction currency metadata must be MYR",
            frappe.ValidationError,
        )
    response_currency = cstr(entry.get("currency")).strip().upper()
    if response_currency and response_currency != expected_currency:
        frappe.throw(
            "Maybank status response currency does not match transaction",
            frappe.ValidationError,
        )

    raw_status = cstr(entry.get("status")).strip()
    if raw_status not in STATUS_MAP:
        frappe.throw(
            "Maybank status response contains an unsupported status",
            frappe.ValidationError,
        )
    return int(raw_status)


def _parse_provider_amount_sen(value: Any) -> int:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        frappe.throw(
            "Maybank status response amount is invalid",
            frappe.ValidationError,
        )
        return 0
    if not amount.is_finite():
        frappe.throw(
            "Maybank status response amount is invalid",
            frappe.ValidationError,
        )
    amount_sen = amount * Decimal("100")
    if amount_sen != amount_sen.to_integral_value():
        frappe.throw(
            "Maybank status response amount contains fractional sen",
            frappe.ValidationError,
        )
    return int(amount_sen)


def _parse_integer_sen(value: Any, fieldname: str) -> int:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        frappe.throw(f"{fieldname} must be an integer", frappe.ValidationError)
        return 0
    if not amount.is_finite() or amount != amount.to_integral_value():
        frappe.throw(f"{fieldname} must be an integer", frappe.ValidationError)
    return int(amount)


def _parse_positive_amount_sen(value: Any) -> int:
    amount_sen = _parse_integer_sen(value, "amount_sen")
    if amount_sen <= 0 or amount_sen > MAX_AMOUNT_SEN:
        frappe.throw(
            "amount_sen must be between 1 and 10000000",
            frappe.ValidationError,
        )
    return amount_sen


def _request_fingerprint(device_id: str, idempotency_key: str) -> str:
    value = f"{device_id}\0{idempotency_key}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _reservation_reference(request_fingerprint: str) -> str:
    return f"REQUEST-{request_fingerprint.upper()}"


def _extract_expiry_seconds(qr_entry: dict[str, Any]) -> int:
    raw_expiry = (
        qr_entry.get("expires_in_seconds")
        or qr_entry.get("expiresInSeconds")
        or qr_entry.get("ttl_seconds")
        or qr_entry.get("ttlSeconds")
    )
    expires_in_seconds = (
        cint(raw_expiry) if raw_expiry is not None else DEFAULT_QR_TTL_SECONDS
    )
    return expires_in_seconds if expires_in_seconds > 0 else DEFAULT_QR_TTL_SECONDS


def _generate_qr_payload(
    client: MaybankClient, amount_rm: str, now: Any
) -> tuple[dict[str, Any], str, str, Any]:
    result = client.generate_qr(amount_rm)

    if result.get("status") != "QR000":
        frappe.throw(
            f"Maybank QR generation failed: {result.get('text', 'Unknown error')}"
        )

    data = result.get("data")
    if not data or not isinstance(data, list) or len(data) == 0:
        frappe.throw("Maybank returned empty data for QR generation")

    qr_entry = data[0]
    if not isinstance(qr_entry, dict):
        frappe.throw("Maybank returned invalid QR data")

    refno = cstr(qr_entry.get("transaction_refno", ""))
    qr_data = cstr(
        qr_entry.get("qr_data", qr_entry.get("qr_code", qr_entry.get("qrString", "")))
    )

    if not refno:
        frappe.throw("Maybank returned empty transaction reference")
    if refno.lower().startswith("static-"):
        frappe.throw(
            "Maybank returned a transaction reference in the static QR namespace",
            frappe.ValidationError,
        )
    if not qr_data:
        frappe.throw("Maybank returned empty QR data")

    expires_at = add_to_date(now, seconds=_extract_expiry_seconds(qr_entry))
    return result, refno, qr_data, expires_at


def _mark_generation_ambiguous(
    request_fingerprint: str,
    reason: str,
) -> None:
    reserved = _load_reserved_txn_for_update(request_fingerprint)
    if not reserved or cstr(_existing_value(reserved, "status")) != "creating":
        return
    frappe.db.set_value(
        "Maybank QR Transaction",
        _existing_value(reserved, "name"),
        {
            "raw_response": redacted_json(
                {
                    "status": "generation_ambiguous",
                    "reason": reason,
                    "provider_replay_blocked": True,
                }
            )
        },
        update_modified=False,
    )


def _finalize_reserved_generation(
    request_fingerprint: str,
    *,
    result: dict[str, Any],
    transaction_refno: str,
    qr_data: str,
    expires_at: Any,
) -> dict[str, Any]:
    reserved = _load_reserved_txn_for_update(request_fingerprint)
    if not reserved:
        frappe.throw(
            "Maybank QR reservation disappeared after provider generation",
            frappe.ValidationError,
        )
    current_status = cstr(_existing_value(reserved, "status")).strip()
    current_reference = cstr(
        _existing_value(reserved, "transaction_refno")
    ).strip()
    if current_reference == transaction_refno and current_status in (
        *REUSABLE_STATUSES,
        "paid",
    ):
        return _build_existing_txn_response(reserved)
    if current_status not in {"creating", UNKNOWN_STATUS}:
        frappe.throw(
            "Maybank QR reservation was resolved while provider generation was in flight",
            frappe.ValidationError,
        )
    expected_placeholder = _reservation_reference(request_fingerprint)
    if current_reference != expected_placeholder:
        frappe.throw(
            "Maybank QR reservation reference changed before provider finalization",
            frappe.ValidationError,
        )

    frappe.db.set_value(
        "Maybank QR Transaction",
        _existing_value(reserved, "name"),
        {
            "transaction_refno": transaction_refno,
            "qr_data": qr_data,
            "status": "pending",
            "maybank_status": 2,
            "expires_at": expires_at,
            "raw_response": redacted_json(result),
        },
        update_modified=False,
    )
    return {
        "status": "ok",
        "qr_data": qr_data,
        "transaction_refno": transaction_refno,
        "sale_amount": cstr(_existing_value(reserved, "sale_amount")),
        "expires_at": _serialize_site_datetime(expires_at),
    }


def generate_maybank_qr_payload(payload: dict[str, Any]) -> dict[str, Any]:
    amount_sen = _parse_positive_amount_sen(payload.get("amount_sen", 0))
    device_id = cstr(payload.get("device_id")).strip()
    idempotency_key = cstr(payload.get("idempotency_key")).strip()
    fb_order = cstr(payload.get("fb_order"))
    sales_invoice = cstr(payload.get("sales_invoice"))
    currency = cstr(payload.get("currency") or MAYBANK_CURRENCY).strip().upper()

    if not idempotency_key:
        frappe.throw("idempotency_key is required")
    if not device_id:
        frappe.throw("device_id is required")
    if currency != MAYBANK_CURRENCY:
        frappe.throw("Maybank QR currency must be MYR", frappe.ValidationError)
    if fb_order or sales_invoice:
        frappe.throw(
            "Maybank QR links are assigned only during verified sale submission",
            frappe.ValidationError,
        )

    now = now_datetime()
    existing_response = _resolve_existing_txn(
        device_id, idempotency_key, amount_sen, _coerce_site_datetime(now)
    )
    if existing_response:
        return existing_response

    client = MaybankClient.from_settings()
    outlet_id = cstr(client.outlet_id).strip()
    if not outlet_id:
        frappe.throw(
            "Maybank Settings outlet_id is required",
            frappe.ValidationError,
        )
    _check_rate_limit(device_id, outlet_id)
    amount_rm = f"{Decimal(amount_sen) / Decimal('100'):.2f}"
    current_now = now_datetime()
    request_fingerprint = _request_fingerprint(device_id, idempotency_key)
    txn = frappe.get_doc(
        {
            "doctype": "Maybank QR Transaction",
            "transaction_refno": _reservation_reference(request_fingerprint),
            "outlet_id": outlet_id,
            "sale_amount": amount_rm,
            "sale_amount_sen": amount_sen,
            "currency": currency,
            "status": "creating",
            "device_id": device_id,
            "provider": MAYBANK_PROVIDER,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "round_number": 1,
            "created_at": current_now,
            "business_date": get_datetime(current_now).date().isoformat(),
            "raw_response": redacted_json({"status": "creating"}),
        }
    )
    try:
        txn.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        reserved_transaction = _load_reserved_txn_for_update(request_fingerprint)
        existing_response = _resolve_existing_txn(
            device_id,
            idempotency_key,
            amount_sen,
            _coerce_site_datetime(now_datetime()),
            existing=reserved_transaction,
        )
        if existing_response:
            return existing_response
        raise

    # The provider does not expose a request idempotency key. Durably reserve
    # this local key before the irreversible network call so an ambiguous
    # timeout cannot roll the reservation back and create a second provider QR.
    frappe.db.commit()

    provider_now = now_datetime()
    try:
        result, refno, qr_data, expires_at = _generate_qr_payload(
            client, amount_rm, provider_now
        )
    except Exception:
        _mark_generation_ambiguous(
            request_fingerprint,
            "provider_generation_outcome_unknown",
        )
        frappe.db.commit()
        raise

    try:
        response = _finalize_reserved_generation(
            request_fingerprint,
            result=result,
            transaction_refno=refno,
            qr_data=qr_data,
            expires_at=expires_at,
        )
        frappe.db.commit()
        return response
    except Exception:
        frappe.db.rollback()
        _mark_generation_ambiguous(
            request_fingerprint,
            "provider_result_persistence_failed",
        )
        frappe.db.commit()
        raise


def _load_generation_snapshot(transaction_name: str) -> Any:
    rows = frappe.db.sql(
        """
        SELECT
            name, transaction_refno, status, qr_data, sale_amount,
            sale_amount_sen, expires_at, device_id, provider, currency,
            business_date, request_fingerprint, outlet_id, created_at,
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


def _validate_support_text(value: Any, fieldname: str, minimum: int, maximum: int) -> str:
    text = " ".join(cstr(value).strip().split())
    if len(text) < minimum or len(text) > maximum:
        frappe.throw(
            f"{fieldname} must be {minimum}-{maximum} characters",
            frappe.ValidationError,
        )
    return text


def _audit_generation_resolution(
    transaction_name: str,
    *,
    resolution: str,
    reason: str,
    evidence_reference: str,
    provider_transaction_refno: str | None,
) -> None:
    audit_payload = {
        "event": "maybank_qr_generation_resolution",
        "resolution": resolution,
        "reason": reason,
        "evidence_reference": evidence_reference,
        "provider_transaction_refno": provider_transaction_refno,
        "resolved_by": cstr(getattr(frappe.session, "user", None)).strip(),
        "resolved_at": _serialize_site_datetime(now_datetime()),
    }
    comment = frappe.get_doc(
        {
            "doctype": "Comment",
            "comment_type": "Info",
            "reference_doctype": "Maybank QR Transaction",
            "reference_name": transaction_name,
            "content": "<pre>"
            + html.escape(
                json.dumps(
                    audit_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            + "</pre>",
        }
    )
    comment.insert(ignore_permissions=True)


def _resolve_generation_with_provider_reference(
    transaction_name: str,
    provider_transaction_refno: str,
    *,
    reason: str,
    evidence_reference: str,
) -> dict[str, Any]:
    snapshot = _load_generation_snapshot(transaction_name)
    snapshot_status = cstr(_existing_value(snapshot, "status")).strip()
    if snapshot_status not in {"creating", UNKNOWN_STATUS, *REUSABLE_STATUSES, "paid"}:
        frappe.throw(
            "Maybank QR generation is already terminal",
            frappe.ValidationError,
        )
    snapshot_reference = cstr(
        _existing_value(snapshot, "transaction_refno")
    ).strip()
    if snapshot_status == "paid":
        if snapshot_reference != provider_transaction_refno:
            frappe.throw(
                "Maybank transaction is already paid under another provider reference",
                frappe.ValidationError,
            )
        return {
            "status": "paid",
            "transaction_name": transaction_name,
            "transaction_refno": snapshot_reference,
            "resolution": "already_paid",
        }

    client = MaybankClient.from_settings()
    result = client.check_status(provider_transaction_refno)
    _validate_status_response(result)
    entry = _extract_status_entry(result)
    if entry is None:
        frappe.throw(
            "Maybank returned no transaction for the supplied provider reference",
            frappe.ValidationError,
        )
    identity = dict(snapshot) if isinstance(snapshot, dict) else {
        field: _existing_value(snapshot, field)
        for field in (
            "provider",
            "device_id",
            "sale_amount_sen",
            "outlet_id",
            "currency",
        )
    }
    identity["transaction_refno"] = provider_transaction_refno
    raw_status = _validate_status_entry_identity(identity, entry)
    provider_status = STATUS_MAP.get(str(raw_status), UNKNOWN_STATUS)

    locked = _load_txn_for_update(transaction_name)
    current_status = cstr(_existing_value(locked, "status")).strip()
    current_reference = cstr(_existing_value(locked, "transaction_refno")).strip()
    if current_status == "paid":
        if current_reference != provider_transaction_refno:
            frappe.throw(
                "Maybank transaction is already paid under another provider reference",
                frappe.ValidationError,
            )
        return {
            "status": "paid",
            "transaction_name": transaction_name,
            "transaction_refno": current_reference,
            "resolution": "already_paid",
        }
    placeholder = _reservation_reference(
        cstr(_existing_value(locked, "request_fingerprint"))
    )
    if current_reference not in {placeholder, provider_transaction_refno}:
        frappe.throw(
            "Maybank QR generation was finalized with another provider reference",
            frappe.ValidationError,
        )
    if provider_status not in PROVIDER_STATUS_TRANSITIONS.get(
        current_status, frozenset()
    ) and provider_status != current_status:
        frappe.throw(
            "Provider evidence would regress the current Maybank transaction state",
            frappe.ValidationError,
        )

    poll_now = now_datetime()
    updates: dict[str, Any] = {
        "transaction_refno": provider_transaction_refno,
        "status": provider_status,
        "maybank_status": raw_status,
        "expires_at": _existing_value(locked, "expires_at") or poll_now,
        "last_polled_at": poll_now,
        "poll_count": cint(_existing_value(locked, "poll_count")) + 1,
        "raw_response": redacted_json(result),
    }
    if provider_status == "scanned" and not _existing_value(locked, "scanned_at"):
        updates["scanned_at"] = poll_now
    if provider_status == "paid" and not _existing_value(locked, "paid_at"):
        updates["paid_at"] = poll_now
    frappe.db.set_value(
        "Maybank QR Transaction",
        transaction_name,
        updates,
        update_modified=False,
    )
    _audit_generation_resolution(
        transaction_name,
        resolution="provider_transaction_found",
        reason=reason,
        evidence_reference=evidence_reference,
        provider_transaction_refno=provider_transaction_refno,
    )
    return {
        "status": provider_status,
        "transaction_name": transaction_name,
        "transaction_refno": provider_transaction_refno,
        "resolution": "provider_transaction_found",
        "new_generation_authorized": False,
    }


def _abandon_ambiguous_generation(
    transaction_name: str,
    *,
    confirmation: str,
    reason: str,
    evidence_reference: str,
) -> dict[str, Any]:
    if cstr(confirmation).strip() != CREATION_ABANDON_CONFIRMATION:
        frappe.throw(
            f"confirmation must be exactly {CREATION_ABANDON_CONFIRMATION}",
            frappe.ValidationError,
        )
    locked = _load_txn_for_update(transaction_name)
    current_status = cstr(_existing_value(locked, "status")).strip()
    if current_status == UNKNOWN_STATUS:
        return {
            "status": "generation_abandoned",
            "transaction_name": transaction_name,
            "resolution": "provider_transaction_absent",
            "new_generation_authorized": True,
        }
    if current_status != "creating":
        frappe.throw(
            "Only an ambiguous creating reservation can be abandoned",
            frappe.ValidationError,
        )
    created_at = _coerce_site_datetime(_existing_value(locked, "created_at"))
    age_seconds = int(
        (_coerce_site_datetime(now_datetime()) - created_at).total_seconds()
    )
    if age_seconds < CREATION_ABANDON_AFTER_SECONDS:
        frappe.throw(
            "Maybank QR generation lease is still active; abandonment is not yet safe",
            frappe.ValidationError,
        )

    resolution_evidence = {
        "status": "generation_abandoned",
        "resolution": "provider_transaction_absent",
        "reason": reason,
        "evidence_reference": evidence_reference,
        "resolved_by": cstr(getattr(frappe.session, "user", None)).strip(),
        "resolved_at": _serialize_site_datetime(now_datetime()),
        "provider_replay_blocked": True,
    }
    frappe.db.set_value(
        "Maybank QR Transaction",
        transaction_name,
        {
            "status": UNKNOWN_STATUS,
            "raw_response": redacted_json(resolution_evidence),
        },
        update_modified=False,
    )
    _audit_generation_resolution(
        transaction_name,
        resolution="provider_transaction_absent",
        reason=reason,
        evidence_reference=evidence_reference,
        provider_transaction_refno=None,
    )
    return {
        "status": "generation_abandoned",
        "transaction_name": transaction_name,
        "resolution": "provider_transaction_absent",
        "new_generation_authorized": True,
        "old_idempotency_key_replay_blocked": True,
    }


def _close_expired_reconciliation(
    transaction_name: str,
    provider_transaction_refno: str,
    *,
    confirmation: str,
    reason: str,
    evidence_reference: str,
) -> dict[str, Any]:
    if cstr(confirmation).strip() != PENDING_RECONCILIATION_CONFIRMATION:
        frappe.throw(
            f"confirmation must be exactly {PENDING_RECONCILIATION_CONFIRMATION}",
            frappe.ValidationError,
        )
    locked = _load_txn_for_update(transaction_name)
    current_status = cstr(_existing_value(locked, "status")).strip()
    current_reference = cstr(
        _existing_value(locked, "transaction_refno")
    ).strip()
    if current_reference != provider_transaction_refno:
        frappe.throw(
            "provider_transaction_refno does not match the Maybank transaction",
            frappe.ValidationError,
        )
    if current_status == "paid":
        frappe.throw(
            PAID_TRANSACTION_MESSAGE,
            frappe.ValidationError,
        )
    if current_status == "timeout":
        return {
            "status": "timeout",
            "transaction_name": transaction_name,
            "transaction_refno": current_reference,
            "resolution": "provider_transaction_cancelled",
            "new_generation_authorized": True,
        }
    if current_status not in POLLABLE_STATUSES:
        frappe.throw(
            "Only a pending or scanned Maybank transaction can be closed by reconciliation",
            frappe.ValidationError,
        )
    expires_at = _existing_value(locked, "expires_at")
    if not expires_at:
        frappe.throw(
            "Maybank transaction expiry evidence is missing",
            frappe.ValidationError,
        )
    expired_age_seconds = int(
        (
            _coerce_site_datetime(now_datetime())
            - _coerce_site_datetime(expires_at)
        ).total_seconds()
    )
    if expired_age_seconds < PENDING_RECONCILIATION_RESOLUTION_AFTER_SECONDS:
        frappe.throw(
            "Maybank transaction is still within the mandatory late-settlement "
            "reconciliation window",
            frappe.ValidationError,
        )

    resolution_evidence = {
        "status": "timeout",
        "resolution": "provider_transaction_cancelled",
        "reason": reason,
        "evidence_reference": evidence_reference,
        "resolved_by": cstr(getattr(frappe.session, "user", None)).strip(),
        "resolved_at": _serialize_site_datetime(now_datetime()),
        "provider_reference": provider_transaction_refno,
    }
    frappe.db.set_value(
        "Maybank QR Transaction",
        transaction_name,
        {
            "status": "timeout",
            "maybank_status": None,
            "last_polled_at": now_datetime(),
            "raw_response": redacted_json(resolution_evidence),
        },
        update_modified=False,
    )
    _audit_generation_resolution(
        transaction_name,
        resolution="provider_transaction_cancelled",
        reason=reason,
        evidence_reference=evidence_reference,
        provider_transaction_refno=provider_transaction_refno,
    )
    return {
        "status": "timeout",
        "transaction_name": transaction_name,
        "transaction_refno": provider_transaction_refno,
        "resolution": "provider_transaction_cancelled",
        "new_generation_authorized": True,
        "old_idempotency_key_replay_blocked": True,
    }


def resolve_maybank_qr_generation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    transaction_name = cstr(payload.get("transaction_name")).strip()
    if not transaction_name:
        frappe.throw("transaction_name is required", frappe.ValidationError)
    resolution = cstr(payload.get("resolution")).strip()
    if resolution not in {
        "provider_transaction_found",
        "provider_transaction_absent",
        "provider_transaction_cancelled",
    }:
        frappe.throw(
            "resolution must be provider_transaction_found, "
            "provider_transaction_absent, or provider_transaction_cancelled",
            frappe.ValidationError,
        )
    reason = _validate_support_text(payload.get("reason"), "reason", 12, 500)
    evidence_reference = _validate_support_text(
        payload.get("evidence_reference"),
        "evidence_reference",
        4,
        200,
    )
    if resolution in {
        "provider_transaction_found",
        "provider_transaction_cancelled",
    }:
        provider_reference = _validate_support_text(
            payload.get("provider_transaction_refno"),
            "provider_transaction_refno",
            1,
            120,
        )
        if provider_reference.lower().startswith("static-") or provider_reference.startswith(
            "REQUEST-"
        ):
            frappe.throw(
                "provider_transaction_refno is not a provider dynamic QR reference",
                frappe.ValidationError,
            )
        if resolution == "provider_transaction_cancelled":
            return _close_expired_reconciliation(
                transaction_name,
                provider_reference,
                confirmation=cstr(payload.get("confirmation")),
                reason=reason,
                evidence_reference=evidence_reference,
            )
        return _resolve_generation_with_provider_reference(
            transaction_name,
            provider_reference,
            reason=reason,
            evidence_reference=evidence_reference,
        )
    return _abandon_ambiguous_generation(
        transaction_name,
        confirmation=cstr(payload.get("confirmation")),
        reason=reason,
        evidence_reference=evidence_reference,
    )


def check_maybank_payment_payload(
    transaction_refno: str, device_id: str | None = None
) -> dict[str, Any]:
    if not transaction_refno:
        frappe.throw("transaction_refno is required")

    filters: dict[str, Any] = {"transaction_refno": transaction_refno}
    if device_id:
        filters["device_id"] = device_id

    txn_name = frappe.db.get_value("Maybank QR Transaction", filters, "name")
    if not txn_name:
        frappe.throw("Transaction not found", frappe.ValidationError)

    txn = frappe.get_doc("Maybank QR Transaction", txn_name)

    resolved_status = cstr(txn.status)
    if resolved_status in POLLABLE_STATUSES:
        last_poll = txn.last_polled_at or txn.created_at
        if (now_datetime() - last_poll).total_seconds() > 2:
            try:
                resolved_status = _poll_txn_status(txn)
            except Exception as error:
                log_sanitized_error("Maybank on-demand poll failed", error)

    paid_at = txn.paid_at
    if resolved_status == "paid" and not paid_at:
        paid_at = frappe.db.get_value(
            "Maybank QR Transaction",
            txn_name,
            "paid_at",
        )
    return {
        "status": resolved_status,
        "transaction_refno": txn.transaction_refno,
        "sale_amount": cstr(txn.sale_amount),
        "paid_at": _serialize_site_datetime(paid_at) if paid_at else None,
    }


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
    return status


def _config_positive_int(name: str, default: int, maximum: int) -> int:
    config = getattr(frappe, "conf", None)
    getter = getattr(config, "get", None)
    value = getter(name, default) if callable(getter) else getattr(config, name, default)
    parsed = cint(value)
    if parsed < 1 or parsed > maximum:
        frappe.throw(
            f"{name} must be between 1 and {maximum}",
            frappe.ValidationError,
        )
    return parsed


def _qr_rate_limit_key(scope: str, value: str) -> str:
    site = cstr(getattr(getattr(frappe, "local", None), "site", None)).strip()
    digest = hashlib.sha256(
        f"{site}\0{MAYBANK_PROVIDER}\0{scope}\0{value}".encode("utf-8")
    ).hexdigest()
    return f"kopos:maybank-qr-rate:{scope}:{digest}"


def _check_rate_limit(device_id: str, outlet_id: str) -> None:
    device_limit = _config_positive_int(
        "maybank_qr_per_device_per_minute",
        MAX_QR_PER_MINUTE,
        1_000,
    )
    outlet_limit = _config_positive_int(
        "maybank_qr_per_outlet_per_minute",
        DEFAULT_QR_PER_OUTLET_PER_MINUTE,
        10_000,
    )
    try:
        cache = frappe.cache()
        make_key = getattr(cache, "make_key", None)
        eval_script = getattr(cache, "eval", None)
        if not callable(make_key) or not callable(eval_script):
            raise RuntimeError("Redis atomic scripting is unavailable")
        result = eval_script(
            QR_RATE_LIMIT_SCRIPT,
            2,
            make_key(_qr_rate_limit_key("device", device_id)),
            make_key(_qr_rate_limit_key("outlet", outlet_id)),
            QR_RATE_LIMIT_WINDOW_SECONDS,
            device_limit,
            outlet_limit,
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise RuntimeError("Redis returned invalid Maybank QR rate-limit state")
        device_count = int(result[0])
        outlet_count = int(result[1])
    except Exception as error:
        log_sanitized_error("Maybank QR rate limiter unavailable", error)
        frappe.throw(
            "Maybank QR generation is temporarily unavailable",
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise") from error

    if device_count > device_limit or outlet_count > outlet_limit:
        frappe.throw(
            "Maybank QR generation rate limit exceeded. Try again shortly.",
            frappe.ValidationError,
        )
