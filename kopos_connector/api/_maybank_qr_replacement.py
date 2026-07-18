# pyright: reportMissingImports=false

"""Safe display replacement for provider-issued Maybank QR attempts.

A display replacement never cancels, releases, or financially supersedes the
old provider reference. It only authorizes one more provider generation for the
same immutable prepared payment while every earlier reference remains eligible
for status polling and late-payment reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import frappe
from frappe.utils import cstr, now_datetime

from ._maybank_qr_contract import (
    MAYBANK_PROVIDER,
    PREFLIGHT_REASON_REPLACEMENT_LIMIT,
    PREFLIGHT_REASON_REPLACEMENT_NOT_EXPIRED,
    PREFLIGHT_REASON_REPLACEMENT_STATE,
    PREFLIGHT_REASON_REPLACEMENT_TARGET,
    REPLACEMENT_REJECTION_CODES,
    _coerce_site_datetime,
    _existing_value,
    _parse_integer_sen,
    _request_fingerprint,
    _require_provider_transaction_reference,
)


DISPLAY_REPLACEMENT_REASONS = frozenset(
    {"expired_display", "unrenderable_display"}
)
MAX_PROVIDER_ISSUED_ATTEMPTS_PER_SALE = 3
REPLACEABLE_PROVIDER_STATUSES = frozenset({"pending", "failed", "timeout"})
REPLACEMENT_BLOCKING_PROVIDER_STATUSES = frozenset(
    {"creating", "scanned", "paid", "unknown"}
)


@dataclass(frozen=True)
class MaybankQrReplacementRequest:
    replacement_reason: str
    replaces_transaction_refno: str


class MaybankQrReplacementRejection(frappe.ValidationError):
    """A replacement-only rejection proven to precede provider I/O."""

    def __init__(self, message: str, reason_code: str) -> None:
        super().__init__(message)
        if reason_code not in REPLACEMENT_REJECTION_CODES:
            raise ValueError("unsupported Maybank QR replacement rejection reason")
        self.reason_code = reason_code


def _reject_replacement(message: str, reason_code: str) -> None:
    raise MaybankQrReplacementRejection(message, reason_code)


def parse_maybank_qr_replacement_request(
    payload: dict[str, Any],
) -> MaybankQrReplacementRequest | None:
    """Parse the additive request pair without changing first-attempt calls."""

    raw_reason = payload.get("replacement_reason")
    raw_reference = payload.get("replaces_transaction_refno")
    reason_supplied = raw_reason is not None and cstr(raw_reason) != ""
    reference_supplied = raw_reference is not None and cstr(raw_reference) != ""
    if not reason_supplied and not reference_supplied:
        return None
    if not reason_supplied or not reference_supplied:
        frappe.throw(
            "replacement_reason and replaces_transaction_refno must be provided together",
            frappe.ValidationError,
        )

    reason = cstr(raw_reason)
    if reason != reason.strip() or reason not in DISPLAY_REPLACEMENT_REASONS:
        frappe.throw(
            "replacement_reason must be expired_display or unrenderable_display",
            frappe.ValidationError,
        )
    reference = _require_provider_transaction_reference(
        raw_reference,
        "replaces_transaction_refno",
    )
    return MaybankQrReplacementRequest(
        replacement_reason=reason,
        replaces_transaction_refno=reference,
    )


def _provider_issued_attempt(attempt: Any) -> bool:
    reference = cstr(_existing_value(attempt, "transaction_refno"))
    if not reference or reference.startswith("REQUEST-"):
        return False
    try:
        _require_provider_transaction_reference(
            reference,
            "linked Maybank QR transaction_refno",
        )
    except frappe.ValidationError:
        return False
    return bool(cstr(_existing_value(attempt, "qr_data")).strip())


def _validate_target_fingerprint(
    target: Any,
    *,
    accepted_sale_fingerprint: str,
    amount_sen: int,
    currency: str,
) -> None:
    target_device_id = cstr(_existing_value(target, "device_id")).strip()
    target_idempotency_key = cstr(
        _existing_value(target, "idempotency_key")
    ).strip()
    target_order = cstr(_existing_value(target, "fb_order")).strip()
    target_payment = cstr(
        _existing_value(target, "fb_order_payment")
    ).strip()
    target_replacement_reason = cstr(
        _existing_value(target, "replacement_reason")
    ).strip()
    target_replaces_reference = cstr(
        _existing_value(target, "replaces_transaction_refno")
    ).strip()
    if bool(target_replacement_reason) != bool(target_replaces_reference):
        _reject_replacement(
            "Linked Automatic QR replacement evidence is incomplete",
            PREFLIGHT_REASON_REPLACEMENT_TARGET,
        )
    if target_replacement_reason and (
        target_replacement_reason not in DISPLAY_REPLACEMENT_REASONS
    ):
        _reject_replacement(
            "Linked Automatic QR replacement evidence is invalid",
            PREFLIGHT_REASON_REPLACEMENT_TARGET,
        )
    expected = _request_fingerprint(
        target_device_id,
        target_idempotency_key,
        fb_order=target_order,
        fb_order_payment=target_payment,
        accepted_sale_fingerprint=accepted_sale_fingerprint,
        amount_sen=amount_sen,
        currency=currency,
        replacement_reason=target_replacement_reason,
        replaces_transaction_refno=target_replaces_reference,
    )
    if cstr(_existing_value(target, "request_fingerprint")).strip() != expected:
        _reject_replacement(
            "Replacement target does not match the prepared sale fingerprint",
            PREFLIGHT_REASON_REPLACEMENT_TARGET,
        )


def validate_maybank_qr_display_replacement(
    *,
    attempts: list[Any],
    request: MaybankQrReplacementRequest,
    accepted_sale_fingerprint: str,
    device_id: str,
    company: str,
    currency: str,
    amount_sen: int,
    fb_order: str,
    fb_order_payment: str,
    now: Any | None = None,
) -> int:
    """Validate and return the next provider-issued attempt number.

    The caller holds the FB Order lock and all linked transaction row locks.
    This function is deliberately side-effect free, so every rejection occurs
    before provider-client construction or network I/O.
    """

    issued_attempts: list[Any] = []
    requested_target: Any | None = None
    for attempt in attempts:
        status = cstr(_existing_value(attempt, "status")).strip()
        if status in REPLACEMENT_BLOCKING_PROVIDER_STATUSES:
            _reject_replacement(
                "Automatic QR cannot be replaced while an attempt is creating, "
                "scanned, paid, or ambiguous",
                PREFLIGHT_REASON_REPLACEMENT_STATE,
            )
        if not _provider_issued_attempt(attempt):
            continue

        try:
            linked_amount_sen = _parse_integer_sen(
                _existing_value(attempt, "sale_amount_sen"),
                "linked Maybank QR attempt sale_amount_sen",
            )
        except frappe.ValidationError:
            _reject_replacement(
                "Replacement target does not match the prepared sale",
                PREFLIGHT_REASON_REPLACEMENT_TARGET,
            )
            raise AssertionError("frappe.throw must raise")
        if (
            cstr(_existing_value(attempt, "provider")).strip().lower()
            != MAYBANK_PROVIDER
            or cstr(_existing_value(attempt, "device_id")).strip() != device_id
            or cstr(_existing_value(attempt, "company")).strip() != company
            or cstr(_existing_value(attempt, "currency")).strip().upper()
            != currency
            or linked_amount_sen != amount_sen
            or cstr(_existing_value(attempt, "fb_order")).strip() != fb_order
            or cstr(
                _existing_value(attempt, "fb_order_payment")
            ).strip()
            != fb_order_payment
        ):
            _reject_replacement(
                "Replacement target does not match the prepared sale",
                PREFLIGHT_REASON_REPLACEMENT_TARGET,
            )

        issued_attempts.append(attempt)
        if (
            cstr(_existing_value(attempt, "transaction_refno")).strip()
            == request.replaces_transaction_refno
        ):
            requested_target = attempt

    if requested_target is None:
        _reject_replacement(
            "The QR being replaced was not found for this prepared sale",
            PREFLIGHT_REASON_REPLACEMENT_TARGET,
        )
    if issued_attempts[-1] is not requested_target:
        _reject_replacement(
            "Only the latest QR for this prepared sale can be replaced",
            PREFLIGHT_REASON_REPLACEMENT_TARGET,
        )
    if len(issued_attempts) >= MAX_PROVIDER_ISSUED_ATTEMPTS_PER_SALE:
        _reject_replacement(
            "This sale has reached the Automatic QR replacement limit",
            PREFLIGHT_REASON_REPLACEMENT_LIMIT,
        )

    status = cstr(_existing_value(requested_target, "status")).strip()
    if status not in REPLACEABLE_PROVIDER_STATUSES:
        _reject_replacement(
            "The latest Automatic QR is not safe to replace",
            PREFLIGHT_REASON_REPLACEMENT_STATE,
        )
    expires_at_value = _existing_value(requested_target, "expires_at")
    if not expires_at_value:
        _reject_replacement(
            "The QR being replaced has no trusted display expiry",
            PREFLIGHT_REASON_REPLACEMENT_TARGET,
        )
    try:
        expires_at = _coerce_site_datetime(expires_at_value)
        current_now = _coerce_site_datetime(now or now_datetime())
    except Exception:
        _reject_replacement(
            "The QR being replaced has an invalid display expiry",
            PREFLIGHT_REASON_REPLACEMENT_TARGET,
        )
        raise AssertionError("frappe.throw must raise")
    if (
        request.replacement_reason == "expired_display"
        and current_now < expires_at
    ):
        _reject_replacement(
            "The latest QR has not expired yet",
            PREFLIGHT_REASON_REPLACEMENT_NOT_EXPIRED,
        )

    _validate_target_fingerprint(
        requested_target,
        accepted_sale_fingerprint=accepted_sale_fingerprint,
        amount_sen=amount_sen,
        currency=currency,
    )
    return len(issued_attempts) + 1
