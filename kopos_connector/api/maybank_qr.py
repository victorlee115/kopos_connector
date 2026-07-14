# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
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
from kopos_connector.utils.diagnostics import redacted_json

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
DEFAULT_QR_TTL_SECONDS = 60
GRACE_SECONDS = 30
PAID_TRANSACTION_MESSAGE = "payment already completed for this order"
REUSABLE_STATUSES = ("pending", "scanned")
UNKNOWN_STATUS = "unknown"
MAYBANK_PROVIDER = "maybank_qr"
MAYBANK_CURRENCY = "MYR"
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
            business_date, request_fingerprint
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
            business_date, request_fingerprint
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
    if status in REUSABLE_STATUSES:
        expires_at = _coerce_site_datetime(_existing_value(existing, "expires_at"))
        if expires_at <= now:
            txn = frappe.get_doc(
                "Maybank QR Transaction", _existing_value(existing, "name")
            )
            try:
                _poll_txn_status(txn)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    "Maybank existing transaction refresh failed",
                )
            existing = _load_existing_txn(device_id, idempotency_key)
            if not existing:
                return None
            status = cstr(_existing_value(existing, "status"))
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


def _poll_txn_status(txn: Any) -> None:
    client = MaybankClient.from_settings()
    result = client.check_status(txn.transaction_refno)
    try:
        _validate_status_response(result)
        entry = _extract_status_entry(result)
        if entry is not None:
            raw_status = _validate_status_entry_identity(txn, entry)
    except Exception:
        _record_poll_attempt(txn.name, result)
        raise
    if entry is None:
        frappe.log_error(
            f"Maybank empty response for {txn.transaction_refno}",
            "Maybank on-demand poll: empty data",
        )
        _record_poll_attempt(txn.name, result)
        return

    new_status = STATUS_MAP.get(str(raw_status), UNKNOWN_STATUS)
    if new_status != txn.status:
        _update_txn_status(txn.name, new_status, raw_status, result)
        txn.reload()
        return

    _record_poll_attempt(txn.name, result)


def _record_poll_attempt(txn_name: str, payload: object) -> None:
    frappe.db.sql(
        "UPDATE `tabMaybank QR Transaction` SET last_polled_at = %s, poll_count = poll_count + 1, raw_response = %s WHERE name = %s",
        (now_datetime(), redacted_json(payload), txn_name),
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

    _check_rate_limit(device_id)

    client = MaybankClient.from_settings()
    outlet_id = cstr(client.outlet_id).strip()
    if not outlet_id:
        frappe.throw(
            "Maybank Settings outlet_id is required",
            frappe.ValidationError,
        )
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
    result, refno, qr_data, expires_at = _generate_qr_payload(
        client, amount_rm, provider_now
    )
    txn.transaction_refno = refno
    txn.qr_data = qr_data
    txn.status = "pending"
    txn.maybank_status = 2
    txn.expires_at = expires_at
    txn.raw_response = redacted_json(result)
    txn.save(ignore_permissions=True)

    return {
        "status": "ok",
        "qr_data": qr_data,
        "transaction_refno": refno,
        "sale_amount": amount_rm,
        "expires_at": _serialize_site_datetime(expires_at),
    }


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

    if txn.status in ("pending", "scanned"):
        last_poll = txn.last_polled_at or txn.created_at
        if (now_datetime() - last_poll).total_seconds() > 2:
            try:
                _poll_txn_status(txn)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(), "Maybank on-demand poll failed"
                )

    return {
        "status": txn.status,
        "transaction_refno": txn.transaction_refno,
        "sale_amount": cstr(txn.sale_amount),
        "paid_at": _serialize_site_datetime(txn.paid_at) if txn.paid_at else None,
    }


def _update_txn_status(
    name: str, status: str, raw_status: int, raw_response: dict[str, Any]
) -> None:
    updates: dict[str, Any] = {
        "status": status,
        "maybank_status": raw_status,
        "last_polled_at": now_datetime(),
        "poll_count": cint(
            frappe.db.get_value("Maybank QR Transaction", name, "poll_count")
        )
        + 1,
        "raw_response": redacted_json(raw_response),
    }
    if status == "scanned" and not frappe.db.get_value(
        "Maybank QR Transaction", name, "scanned_at"
    ):
        updates["scanned_at"] = now_datetime()
    elif status == "paid":
        updates["paid_at"] = now_datetime()

    frappe.db.set_value("Maybank QR Transaction", name, updates)

    txn = frappe.get_doc("Maybank QR Transaction", name)
    frappe.publish_realtime(
        "maybank_payment_status",
        {
            "transaction_refno": txn.transaction_refno,
            "status": status,
        },
        user=txn.owner,
    )


def _check_rate_limit(device_id: str) -> None:
    recent = frappe.db.count(
        "Maybank QR Transaction",
        filters={
            "device_id": device_id,
            "created_at": [">", add_to_date(now_datetime(), minutes=-1)],
        },
    )
    if recent >= MAX_QR_PER_MINUTE:
        frappe.throw("QR generation rate limit exceeded. Try again shortly.")
