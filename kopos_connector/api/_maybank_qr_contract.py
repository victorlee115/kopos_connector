# pyright: reportMissingImports=false

"""Maybank QR wire contract, constants, and strict value parsers."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import cint, cstr, get_datetime, get_system_timezone

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


PAYMENT_STATUS_RESPONSE_STATUSES = frozenset(
    {"pending", "scanned", "paid", "failed", "timeout"}
)


UNKNOWN_STATUS = "unknown"


MAYBANK_PROVIDER = "maybank_qr"


MAYBANK_CURRENCY = "MYR"


MAYBANK_MOCK_REFERENCE_PATTERN = re.compile(r"^MOCK-TXN-[A-F0-9]{16}$")


PROVIDER_STATUS_TRANSITIONS = {
    "creating": frozenset({"pending", "scanned", "paid", "failed", "timeout"}),
    "pending": frozenset({"scanned", "paid", "failed", "timeout"}),
    "scanned": frozenset({"paid", "failed", "timeout"}),
    "paid": frozenset(),
    # Provider failure/timeout is terminal for display, but not settlement
    # authority. Maybank can report a late payment after the QR's display TTL
    # or an earlier terminal-looking response, so authenticated paid truth must
    # remain monotonic and admissible.
    "failed": frozenset({"paid"}),
    "timeout": frozenset({"paid"}),
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


MAYBANK_QR_REQUEST_REJECTED_NO_PROVIDER_ATTEMPT = (
    "MAYBANK_QR_REQUEST_REJECTED_NO_PROVIDER_ATTEMPT"
)


PREFLIGHT_REASON_PROVIDER_CONFIGURATION = "provider_configuration_rejected"


PREFLIGHT_REASON_RATE_LIMIT = "rate_limit_exceeded"


PREFLIGHT_REASON_RATE_LIMITER_UNAVAILABLE = "rate_limiter_unavailable"


PREFLIGHT_REASON_CODES = frozenset(
    {
        PREFLIGHT_REASON_PROVIDER_CONFIGURATION,
        PREFLIGHT_REASON_RATE_LIMIT,
        PREFLIGHT_REASON_RATE_LIMITER_UNAVAILABLE,
    }
)


class MaybankQrPreflightRejection(frappe.ValidationError):
    """A known rejection raised before any provider request can begin."""

    def __init__(self, message: str, reason_code: str) -> None:
        super().__init__(message)
        if reason_code not in PREFLIGHT_REASON_CODES:
            raise ValueError("unsupported Maybank QR preflight rejection reason")
        self.reason_code = reason_code


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


def _require_exact_persisted_text(value: Any, fieldname: str) -> str:
    if not isinstance(value, str):
        frappe.throw(
            f"{fieldname} must be a nonempty exact string",
            frappe.ValidationError,
        )
    text = value
    if not text or text.strip() != text:
        frappe.throw(
            f"{fieldname} must be a nonempty exact value",
            frappe.ValidationError,
        )
    return text


def _require_provider_transaction_reference(value: Any, fieldname: str) -> str:
    reference = _require_exact_persisted_text(value, fieldname)
    if reference.lower().startswith("static-"):
        frappe.throw(
            f"{fieldname} is in the static QR namespace",
            frappe.ValidationError,
        )
    if reference.startswith("REQUEST-"):
        frappe.throw(
            f"{fieldname} is not a valid Maybank provider transaction reference",
            frappe.ValidationError,
        )
    return reference


def _persisted_sale_amount_sen(existing: Any) -> int:
    amount_sen = _parse_integer_sen(
        _existing_value(existing, "sale_amount_sen"),
        "Maybank transaction sale_amount_sen",
    )
    if amount_sen <= 0 or amount_sen > MAX_AMOUNT_SEN:
        frappe.throw(
            "Maybank transaction sale_amount_sen is outside the supported range",
            frappe.ValidationError,
        )
    return amount_sen


def _format_sale_amount(amount_sen: int) -> str:
    return f"{Decimal(amount_sen) / Decimal('100'):.2f}"


def _has_explicit_timezone(value: Any) -> bool:
    text = cstr(value).strip()
    if text.endswith(("Z", "z")):
        return True
    suffix = text[-6:]
    return bool(
        len(suffix) == 6
        and suffix[0] in {"+", "-"}
        and suffix[3] == ":"
        and suffix[1:3].isdigit()
        and suffix[4:6].isdigit()
    )


def _existing_value(existing: Any, fieldname: str) -> Any:
    if isinstance(existing, dict):
        return existing.get(fieldname)
    return getattr(existing, fieldname, None)


def _extract_status_entry(result: dict[str, Any]) -> dict[str, Any] | None:
    data = result.get("data")
    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    return None


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
    return _parse_decimal_amount_sen(value, "Maybank status response amount")


def _parse_decimal_amount_sen(value: Any, fieldname: str) -> int:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        frappe.throw(
            f"{fieldname} is invalid",
            frappe.ValidationError,
        )
        return 0
    if not amount.is_finite():
        frappe.throw(
            f"{fieldname} is invalid",
            frappe.ValidationError,
        )
    amount_sen = amount * Decimal("100")
    if amount_sen != amount_sen.to_integral_value():
        frappe.throw(
            f"{fieldname} contains fractional sen",
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


def _request_fingerprint(
    device_id: str,
    idempotency_key: str,
    *,
    fb_order: str = "",
    fb_order_payment: str = "",
    accepted_sale_fingerprint: str = "",
    amount_sen: int | None = None,
    currency: str = "",
) -> str:
    value = json.dumps(
        {
            "accepted_sale_fingerprint": accepted_sale_fingerprint,
            "amount_sen": amount_sen,
            "currency": currency,
            "device_id": device_id,
            "fb_order": fb_order,
            "fb_order_payment": fb_order_payment,
            "idempotency_key": idempotency_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
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
