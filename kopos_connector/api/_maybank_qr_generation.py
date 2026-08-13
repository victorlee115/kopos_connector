# pyright: reportMissingImports=false

"""Prepared-sale-bound Maybank QR generation and provider-attempt fencing."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import add_to_date, cint, cstr, get_datetime, now_datetime

from kopos_connector.kopos.services.accounting.maybank_payment_service import (
    resolve_manual_qr_suspense_account,
    resolve_verified_qr_settlement_account,
)
from kopos_connector.services.maybank.client import MaybankClient
from kopos_connector.services.maybank.branch_config import (
    PROFILE_AUTOMATIC_ENABLED_FIELD,
    profile_for_device,
    require_provider_binding,
    validate_settlement_bank_account,
)
from kopos_connector.utils.diagnostics import log_sanitized_error, redacted_json

from ._maybank_qr_contract import (
    AMBIGUOUS_IDEMPOTENCY_MESSAGE,
    MAYBANK_CURRENCY,
    MAYBANK_PROVIDER,
    PREFLIGHT_REASON_PROVIDER_CONFIGURATION,
    PREFLIGHT_REASON_REPLACEMENT_REQUEST,
    PREFLIGHT_REASON_REPLACEMENT_SALE_TERMINAL,
    PREFLIGHT_REASON_STATIC_WINNER,
    REUSABLE_STATUSES,
    UNKNOWN_STATUS,
    MaybankQrPreflightRejection,
    _coerce_site_datetime,
    _existing_value,
    _extract_expiry_seconds,
    _parse_decimal_amount_sen,
    _parse_integer_sen,
    _parse_positive_amount_sen,
    _request_fingerprint,
    _require_provider_transaction_reference,
    _reservation_reference,
    _serialize_site_datetime,
)
from ._maybank_qr_persistence import (
    _build_display_unavailable_response,
    _build_existing_txn_response,
    _build_paid_existing_txn_response,
    _build_preflight_rejection_response,
    _durable_generation_release,
    _load_existing_txn,
    _load_reserved_txn_for_update,
    _load_reserved_txn_with_order_lock,
    _raw_response_object,
)
from ._maybank_qr_prepared_sale import load_prepared_automatic_qr_sale
from ._maybank_qr_rate_limit import _check_rate_limit
from ._maybank_qr_replacement import (
    MaybankQrReplacementRequest,
    MaybankQrReplacementRejection,
    parse_maybank_qr_replacement_request,
    validate_maybank_qr_display_replacement,
)
from ._maybank_qr_resolution import _audit_generation_resolution
from ._maybank_qr_status import _resolve_existing_txn


class MaybankQrGenerationIdentityMismatch(frappe.ValidationError):
    """A provider reference exists, but its returned sale identity is unsafe."""

    def __init__(
        self,
        message: str,
        *,
        result: dict[str, Any],
        transaction_refno: str,
        qr_data: str,
        expires_at: Any,
    ) -> None:
        super().__init__(message)
        self.result = result
        self.transaction_refno = transaction_refno
        self.qr_data = qr_data
        self.expires_at = expires_at


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

    refno = _require_provider_transaction_reference(
        qr_entry.get("transaction_refno", ""),
        "Maybank QR generation response transaction_refno",
    )
    qr_data = cstr(
        qr_entry.get("qr_data", qr_entry.get("qr_code", qr_entry.get("qrString", "")))
    )
    expires_at = add_to_date(now, seconds=_extract_expiry_seconds(qr_entry))

    response_amount = qr_entry.get("sale_amount")
    if response_amount is None:
        response_amount = qr_entry.get("amount")
    if response_amount is not None:
        try:
            expected_amount_sen = _parse_decimal_amount_sen(
                amount_rm,
                "Maybank QR requested amount",
            )
            response_amount_sen = _parse_decimal_amount_sen(
                response_amount,
                "Maybank QR generation response amount",
            )
        except frappe.ValidationError as error:
            # Once Maybank has returned a valid provider reference, malformed
            # amount evidence is an identity conflict rather than an unknown
            # generation outcome. Carry the reference into the durable support
            # fence so neither a retry nor a replacement can lose it.
            raise MaybankQrGenerationIdentityMismatch(
                str(error),
                result=result,
                transaction_refno=refno,
                qr_data=qr_data,
                expires_at=expires_at,
            ) from error
        if response_amount_sen != expected_amount_sen:
            raise MaybankQrGenerationIdentityMismatch(
                "Maybank QR generation response amount does not match the prepared sale",
                result=result,
                transaction_refno=refno,
                qr_data=qr_data,
                expires_at=expires_at,
            )

    return result, refno, qr_data, expires_at


def _mark_generation_ambiguous(
    request_fingerprint: str,
    reason: str,
    *,
    result: dict[str, Any] | None = None,
    transaction_refno: str = "",
    qr_data: str = "",
    expires_at: Any | None = None,
) -> None:
    reserved = _load_reserved_txn_with_order_lock(request_fingerprint)
    if not reserved or cstr(_existing_value(reserved, "status")) != "creating":
        return
    updates: dict[str, Any] = {
        "raw_response": redacted_json(
            {
                "status": "generation_ambiguous",
                "reason": reason,
                "provider_replay_blocked": True,
                **(
                    {
                        "provider_transaction_refno": transaction_refno,
                        "provider_result": result,
                        "support_required": True,
                    }
                    if transaction_refno
                    else {}
                ),
            }
        )
    }
    if transaction_refno:
        current_reference = cstr(
            _existing_value(reserved, "transaction_refno")
        ).strip()
        if current_reference not in {
            _reservation_reference(request_fingerprint),
            transaction_refno,
        }:
            frappe.throw(
                "Maybank QR reservation reference changed before ambiguity fencing",
                frappe.ValidationError,
            )
        updates.update(
            {
                "transaction_refno": transaction_refno,
                "qr_data": qr_data,
                "expires_at": expires_at,
                "status": UNKNOWN_STATUS,
            }
        )
    frappe.db.set_value(
        "Maybank QR Transaction",
        _existing_value(reserved, "name"),
        updates,
        update_modified=False,
    )
    fb_order = cstr(_existing_value(reserved, "fb_order")).strip()
    if fb_order:
        _set_provider_state_for_unsubmitted_order(
            fb_order,
            "provider_ambiguous",
        )


def _set_provider_state_for_unsubmitted_order(
    order_name: str,
    provider_state: str,
) -> None:
    """Do not let a delayed provider result regress a completed static sale."""

    order = frappe.db.get_value(
        "FB Order",
        order_name,
        ["name", "docstatus", "automatic_qr_winner_channel"],
        as_dict=True,
    )
    if not order:
        frappe.throw(
            "Prepared Automatic QR FB Order was not found",
            frappe.ValidationError,
        )
    if cint(_existing_value(order, "docstatus")) != 0:
        return
    if cstr(_existing_value(order, "automatic_qr_winner_channel")).strip() == (
        "static_qr"
    ):
        return
    frappe.db.set_value(
        "FB Order",
        order_name,
        "automatic_qr_state",
        provider_state,
        update_modified=False,
    )


def _recover_known_provider_result(
    request_fingerprint: str,
    *,
    result: dict[str, Any],
    transaction_refno: str,
    qr_data: str,
    expires_at: Any,
) -> None:
    """Persist a validated provider result after the first finalization failed."""

    reserved = _load_reserved_txn_with_order_lock(request_fingerprint)
    if not reserved:
        frappe.throw(
            "Maybank QR reservation disappeared during provider result recovery",
            frappe.ValidationError,
        )
    current_status = cstr(_existing_value(reserved, "status")).strip()
    current_reference = cstr(
        _existing_value(reserved, "transaction_refno")
    ).strip()
    expected_placeholder = _reservation_reference(request_fingerprint)

    # A commit-acknowledgement failure can enter this recovery after another
    # transaction has already made the exact provider reference durable. Do
    # not regress its monotonic status or rewrite stronger provider evidence.
    if current_reference == transaction_refno and current_status in {
        "pending",
        "scanned",
        "paid",
        "failed",
        "timeout",
        UNKNOWN_STATUS,
    }:
        return

    if current_status == UNKNOWN_STATUS:
        if (
            current_reference == expected_placeholder
            and _durable_generation_release(reserved)
            == "provider_transaction_absent"
        ):
            # Support may have released the still-in-flight reservation while
            # the first finalization was failing. Preserve the late reference
            # under the existing no-display/no-reuse incident fence.
            _record_late_provider_result_after_release(
                reserved,
                request_fingerprint=request_fingerprint,
                result=result,
                transaction_refno=transaction_refno,
                qr_data=qr_data,
                expires_at=expires_at,
            )
            return
        frappe.throw(
            "Maybank QR provider result recovery conflicts with unresolved support evidence",
            frappe.ValidationError,
        )

    if current_status != "creating" or current_reference not in {
        expected_placeholder,
        transaction_refno,
    }:
        frappe.throw(
            "Maybank QR reservation state changed during provider result recovery",
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
            "raw_response": redacted_json(
                {
                    "status": "provider_result_recovered",
                    "provider_result": result,
                    "provider_replay_blocked": True,
                }
            ),
        },
        update_modified=False,
    )


def _late_provider_result_response(reserved: Any) -> dict[str, Any]:
    return {
        "status": "late_provider_result_fenced",
        "error_code": "maybank_qr_late_provider_result_fenced",
        "transaction_name": cstr(_existing_value(reserved, "name")),
        "transaction_refno": cstr(
            _existing_value(reserved, "transaction_refno")
        ),
        "display_authorized": False,
        "new_generation_authorized": False,
        "support_required": True,
        "settlement_status": "pending_reconciliation",
        "recovery_action": "resolve_maybank_qr_generation",
        "fb_order": cstr(_existing_value(reserved, "fb_order")),
        "fb_order_payment": cstr(
            _existing_value(reserved, "fb_order_payment")
        ),
    }


def _record_late_provider_result_after_release(
    reserved: Any,
    *,
    request_fingerprint: str,
    result: dict[str, Any],
    transaction_refno: str,
    qr_data: str,
    expires_at: Any,
) -> dict[str, Any]:
    transaction_name = cstr(_existing_value(reserved, "name")).strip()
    current_reference = cstr(
        _existing_value(reserved, "transaction_refno")
    ).strip()
    expected_placeholder = _reservation_reference(request_fingerprint)
    if current_reference not in {expected_placeholder, transaction_refno}:
        frappe.throw(
            "Released Maybank QR reservation returned another provider reference",
            frappe.ValidationError,
        )

    existing_evidence = _raw_response_object(reserved)
    if (
        current_reference == transaction_refno
        and existing_evidence is not None
        and cstr(existing_evidence.get("resolution")).strip()
        == "late_provider_result_after_release"
    ):
        return _late_provider_result_response(reserved)

    observed_at = _serialize_site_datetime(now_datetime())
    incident_evidence = {
        "status": "late_provider_result_fenced",
        "resolution": "late_provider_result_after_release",
        "incident_type": "released_generation_returned_late",
        "reason": (
            "Maybank returned a provider QR after the old reservation had been "
            "released by audited support evidence"
        ),
        "provider_transaction_refno": transaction_refno,
        "provider_result": result,
        "provider_replay_blocked": True,
        "display_authorized": False,
        "new_generation_authorized": False,
        "support_required": True,
        "settlement_status": "pending_reconciliation",
        "required_action": "provider_settlement_or_refund_investigation",
        "observed_at": observed_at,
    }
    frappe.db.set_value(
        "Maybank QR Transaction",
        transaction_name,
        {
            "transaction_refno": transaction_refno,
            "qr_data": qr_data,
            "expires_at": expires_at,
            "raw_response": redacted_json(incident_evidence),
        },
        update_modified=False,
    )
    try:
        _audit_generation_resolution(
            transaction_name,
            resolution="late_provider_result_after_release",
            reason=(
                "Delayed provider generation result arrived after audited local release"
            ),
            evidence_reference=f"provider-callback:{request_fingerprint[:16]}",
            provider_transaction_refno=transaction_refno,
        )
    except Exception as error:
        # The transaction incident record above is the durable authority. A
        # secondary Comment failure must not make the old QR displayable by
        # rolling that record back.
        log_sanitized_error(
            "Maybank QR late-result audit comment pending",
            error,
        )
    try:
        frappe.log_error(
            (
                "A released Maybank QR generation returned a late provider result; "
                f"transaction={transaction_name}"
            ),
            "Maybank QR late provider result fenced",
        )
    except Exception as error:
        log_sanitized_error("Maybank QR late-result alert pending", error)
    response_source = dict(reserved) if isinstance(reserved, dict) else reserved
    if isinstance(response_source, dict):
        response_source.update(
            {
                "transaction_refno": transaction_refno,
                "expires_at": expires_at,
                "raw_response": redacted_json(incident_evidence),
            }
        )
    return _late_provider_result_response(response_source)


def _finalize_reserved_generation(
    request_fingerprint: str,
    *,
    result: dict[str, Any],
    transaction_refno: str,
    qr_data: str,
    expires_at: Any,
) -> dict[str, Any]:
    reserved = _load_reserved_txn_with_order_lock(request_fingerprint)
    if not reserved:
        frappe.throw(
            "Maybank QR reservation disappeared after provider generation",
            frappe.ValidationError,
        )
    current_status = cstr(_existing_value(reserved, "status")).strip()
    current_reference = cstr(
        _existing_value(reserved, "transaction_refno")
    ).strip()
    if current_reference == transaction_refno and current_status in REUSABLE_STATUSES:
        return _build_existing_txn_response(reserved)
    if current_reference == transaction_refno and current_status == "paid":
        return _build_paid_existing_txn_response(reserved)
    if current_status == UNKNOWN_STATUS:
        if (
            _durable_generation_release(reserved)
            == "provider_transaction_absent"
        ):
            return _record_late_provider_result_after_release(
                reserved,
                request_fingerprint=request_fingerprint,
                result=result,
                transaction_refno=transaction_refno,
                qr_data=qr_data,
                expires_at=expires_at,
            )
        frappe.throw(
            "Maybank QR reservation has unresolved provider identity evidence",
            frappe.ValidationError,
        )
    if current_status != "creating":
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
    response_source = {
        "transaction_refno": transaction_refno,
        "qr_data": qr_data,
        "status": "pending",
        "sale_amount_sen": _existing_value(reserved, "sale_amount_sen"),
        "expires_at": expires_at,
        "fb_order": _existing_value(reserved, "fb_order"),
        "fb_order_payment": _existing_value(reserved, "fb_order_payment"),
    }
    if not qr_data.strip():
        return _build_display_unavailable_response(response_source)
    return _build_existing_txn_response(response_source)


def _register_preflight_rejection_fence(
    *,
    device_id: str,
    idempotency_key: str,
    amount_sen: int,
    currency: str,
    outlet_id: str,
    rejection: MaybankQrPreflightRejection | MaybankQrReplacementRejection,
    prepared_sale: dict[str, Any],
    replacement_request: MaybankQrReplacementRequest | None = None,
) -> dict[str, Any]:
    """Durably fence a request that is proven not to have reached Maybank.

    The fence is committed before release is authorized. This prevents a
    delayed or concurrent replay of the same logical request from reaching the
    provider after the tablet has safely discarded its local provider intent.
    """
    request_fingerprint = _request_fingerprint(
        device_id,
        idempotency_key,
        fb_order=prepared_sale["fb_order"],
        fb_order_payment=prepared_sale["fb_order_payment"],
        accepted_sale_fingerprint=prepared_sale["accepted_sale_fingerprint"],
        amount_sen=amount_sen,
        currency=currency,
        replacement_reason=(
            replacement_request.replacement_reason
            if replacement_request is not None
            else ""
        ),
        replaces_transaction_refno=(
            replacement_request.replaces_transaction_refno
            if replacement_request is not None
            else ""
        ),
    )
    checked_now = now_datetime()
    checked_at = _serialize_site_datetime(checked_now)
    response_reason_code = (
        PREFLIGHT_REASON_REPLACEMENT_REQUEST
        if isinstance(rejection, MaybankQrReplacementRejection)
        else rejection.reason_code
    )
    response = _build_preflight_rejection_response(
        device_id=device_id,
        idempotency_key=idempotency_key,
        amount_sen=amount_sen,
        reason_code=response_reason_code,
        message=cstr(rejection).strip()
        or "Automatic QR request was rejected before contacting the provider",
        checked_at=checked_at,
        replacement_reason=(
            replacement_request.replacement_reason
            if replacement_request is not None
            else ""
        ),
        replaces_transaction_refno=(
            replacement_request.replaces_transaction_refno
            if replacement_request is not None
            else ""
        ),
        replacement_rejection_code=(
            rejection.reason_code
            if replacement_request is not None
            else ""
        ),
    )
    fence = frappe.get_doc(
        {
            "doctype": "Maybank QR Transaction",
            "transaction_refno": _reservation_reference(request_fingerprint),
            "outlet_id": outlet_id or None,
            "sale_amount": f"{Decimal(amount_sen) / Decimal('100'):.2f}",
            "sale_amount_sen": amount_sen,
            "currency": currency,
            "company": prepared_sale["company"],
            "status": "failed",
            "device_id": device_id,
            "provider": MAYBANK_PROVIDER,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "replacement_reason": (
                replacement_request.replacement_reason
                if replacement_request is not None
                else None
            ),
            "replaces_transaction_refno": (
                replacement_request.replaces_transaction_refno
                if replacement_request is not None
                else None
            ),
            "fb_order": prepared_sale["fb_order"],
            "fb_order_payment": prepared_sale["fb_order_payment"],
            "round_number": int(prepared_sale.get("provider_round_number") or 1),
            "created_at": checked_now,
            "business_date": get_datetime(checked_now).date().isoformat(),
            # This envelope contains only server-authored contract fields and
            # request identity (never credentials or QR payload). Preserve it
            # exactly so a lost 409 response can be replayed byte-for-field,
            # even when an idempotency key contains a word the generic provider
            # diagnostic redactor would conservatively mask.
            "raw_response": json.dumps(
                response,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
    )
    try:
        fence.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        existing = _load_reserved_txn_for_update(request_fingerprint)
        existing_response = _resolve_existing_txn(
            device_id,
            idempotency_key,
            amount_sen,
            _coerce_site_datetime(now_datetime()),
            existing=existing,
        )
        if existing_response:
            return existing_response
        raise

    if (
        replacement_request is None
        and response_reason_code != PREFLIGHT_REASON_STATIC_WINNER
    ):
        frappe.db.set_value(
            "FB Order",
            prepared_sale["fb_order"],
            "automatic_qr_state",
            "provider_rejected",
            update_modified=False,
        )
    frappe.db.commit()
    return response


def _configuration_preflight_rejection() -> MaybankQrPreflightRejection:
    return MaybankQrPreflightRejection(
        "Automatic QR is unavailable because its ERP provider configuration is not ready",
        PREFLIGHT_REASON_PROVIDER_CONFIGURATION,
    )


def _validate_new_generation_attempt(
    *,
    order_doc: Any,
    attempts: list[Any],
    device_id: str,
    idempotency_key: str,
    request_fingerprint: str,
    amount_sen: int,
    currency: str,
    accepted_sale_fingerprint: str = "",
    replacement_request: MaybankQrReplacementRequest | None = None,
    now: Any | None = None,
) -> int:
    order_name = cstr(getattr(order_doc, "name", None)).strip()
    payment_row_name = cstr(
        getattr(order_doc, "automatic_qr_payment", None)
    ).strip()
    order_company = cstr(getattr(order_doc, "company", None)).strip()
    for attempt in attempts:
        try:
            linked_amount_sen = _parse_integer_sen(
                _existing_value(attempt, "sale_amount_sen"),
                "linked Maybank QR attempt sale_amount_sen",
            )
        except frappe.ValidationError:
            frappe.throw(
                "Linked Automatic QR attempt evidence is invalid",
                frappe.ValidationError,
            )
            raise AssertionError("frappe.throw must raise")
        if (
            cstr(_existing_value(attempt, "provider")).strip().lower()
            != MAYBANK_PROVIDER
            or cstr(_existing_value(attempt, "device_id")).strip() != device_id
            or cstr(_existing_value(attempt, "currency")).strip().upper()
            != currency
            or (
                order_company
                and cstr(_existing_value(attempt, "company")).strip()
                != order_company
            )
            or linked_amount_sen != amount_sen
            or cstr(_existing_value(attempt, "fb_order")).strip()
            != order_name
            or cstr(_existing_value(attempt, "fb_order_payment")).strip()
            != payment_row_name
        ):
            frappe.throw(
                "Linked Automatic QR attempt evidence does not match the prepared sale",
                frappe.ValidationError,
            )
    same_key_attempts = [
        attempt
        for attempt in attempts
        if cstr(_existing_value(attempt, "device_id")).strip() == device_id
        and cstr(_existing_value(attempt, "idempotency_key")).strip()
        == idempotency_key
    ]
    if len(same_key_attempts) > 1:
        frappe.throw(AMBIGUOUS_IDEMPOTENCY_MESSAGE, frappe.ValidationError)
    if same_key_attempts:
        if cstr(
            _existing_value(same_key_attempts[0], "request_fingerprint")
        ).strip() != request_fingerprint:
            frappe.throw(
                "Maybank QR idempotency key belongs to another prepared sale request",
                frappe.ValidationError,
            )
        # Exact replay is always allowed to reach the persisted-attempt resolver,
        # even after the order was submitted or the provider request was fenced.
        return int(_existing_value(same_key_attempts[0], "round_number") or 1)

    if cstr(getattr(order_doc, "status", None)).strip().lower() in {
        "cancelled",
        "canceled",
    }:
        message = "Cancelled Automatic QR sales cannot create another provider attempt"
        if replacement_request is not None:
            raise MaybankQrReplacementRejection(
                message,
                PREFLIGHT_REASON_REPLACEMENT_SALE_TERMINAL,
            )
        frappe.throw(message, frappe.ValidationError)
    if cint(getattr(order_doc, "docstatus", 0)) != 0:
        if (
            replacement_request is None
            and cstr(
                getattr(order_doc, "automatic_qr_winner_channel", None)
            ).strip()
            == "static_qr"
            and cstr(
                getattr(order_doc, "automatic_qr_state", None)
            ).strip()
            == "finalized"
        ):
            raise MaybankQrPreflightRejection(
                "Static QR already completed this prepared sale",
                PREFLIGHT_REASON_STATIC_WINNER,
            )
        message = "Submitted Automatic QR sales cannot create another provider attempt"
        if replacement_request is not None:
            raise MaybankQrReplacementRejection(
                message,
                PREFLIGHT_REASON_REPLACEMENT_SALE_TERMINAL,
            )
        frappe.throw(message, frappe.ValidationError)

    if replacement_request is not None:
        return validate_maybank_qr_display_replacement(
            attempts=attempts,
            request=replacement_request,
            accepted_sale_fingerprint=accepted_sale_fingerprint,
            device_id=device_id,
            company=order_company,
            currency=currency,
            amount_sen=amount_sen,
            fb_order=order_name,
            fb_order_payment=payment_row_name,
            now=now,
        )

    automatic_state = cstr(
        getattr(order_doc, "automatic_qr_state", None)
    ).strip()
    if not attempts:
        if automatic_state != "prepared":
            frappe.throw(
                "Prepared Automatic QR sale has inconsistent provider-attempt state",
                frappe.ValidationError,
            )
        return 1

    unsafe_attempts = [
        cstr(_existing_value(attempt, "name")).strip()
        for attempt in attempts
        if _durable_generation_release(attempt) is None
    ]
    if unsafe_attempts:
        frappe.throw(
            "A previous Automatic QR attempt is unresolved; replay its exact "
            "idempotency key or complete audited provider reconciliation",
            frappe.ValidationError,
        )
    if automatic_state not in {
        "prepared",
        "provider_pending",
        "provider_ambiguous",
        "provider_rejected",
    }:
        frappe.throw(
            "Prepared Automatic QR sale is not eligible for another provider attempt",
            frappe.ValidationError,
        )
    return 1


def _load_prepared_automatic_qr_sale(
    *,
    fb_order: str,
    fb_order_payment: str,
    accepted_sale_fingerprint: str,
    device_id: str,
    amount_sen: int,
    idempotency_key: str,
    currency: str,
    replacement_request: MaybankQrReplacementRequest | None = None,
) -> dict[str, Any]:
    return load_prepared_automatic_qr_sale(
        fb_order=fb_order,
        fb_order_payment=fb_order_payment,
        accepted_sale_fingerprint=accepted_sale_fingerprint,
        device_id=device_id,
        amount_sen=amount_sen,
        idempotency_key=idempotency_key,
        currency=currency,
        replacement_request=replacement_request,
        validate_generation_attempt=_validate_new_generation_attempt,
    )


def generate_maybank_qr_payload(payload: dict[str, Any]) -> dict[str, Any]:
    amount_sen = _parse_positive_amount_sen(payload.get("amount_sen", 0))
    device_id = cstr(payload.get("device_id")).strip()
    idempotency_key = cstr(payload.get("idempotency_key")).strip()
    fb_order = cstr(payload.get("fb_order")).strip()
    fb_order_payment = cstr(payload.get("fb_order_payment")).strip()
    accepted_sale_fingerprint = cstr(
        payload.get("accepted_sale_fingerprint")
    ).strip()
    currency = cstr(payload.get("currency") or MAYBANK_CURRENCY).strip().upper()
    replacement_request = parse_maybank_qr_replacement_request(payload)

    if not idempotency_key:
        frappe.throw("idempotency_key is required")
    if not device_id:
        frappe.throw("device_id is required")
    if currency != MAYBANK_CURRENCY:
        frappe.throw("Maybank QR currency must be MYR", frappe.ValidationError)
    prepared_sale = _load_prepared_automatic_qr_sale(
        fb_order=fb_order,
        fb_order_payment=fb_order_payment,
        accepted_sale_fingerprint=accepted_sale_fingerprint,
        device_id=device_id,
        amount_sen=amount_sen,
        idempotency_key=idempotency_key,
        currency=currency,
        replacement_request=replacement_request,
    )
    expected_request_fingerprint = _request_fingerprint(
        device_id,
        idempotency_key,
        fb_order=prepared_sale["fb_order"],
        fb_order_payment=prepared_sale["fb_order_payment"],
        accepted_sale_fingerprint=prepared_sale["accepted_sale_fingerprint"],
        amount_sen=amount_sen,
        currency=currency,
        replacement_reason=(
            replacement_request.replacement_reason
            if replacement_request is not None
            else ""
        ),
        replaces_transaction_refno=(
            replacement_request.replaces_transaction_refno
            if replacement_request is not None
            else ""
        ),
    )

    now = now_datetime()
    existing_transaction = _load_existing_txn(device_id, idempotency_key)
    if existing_transaction:
        if (
            cstr(_existing_value(existing_transaction, "fb_order")).strip()
            != prepared_sale["fb_order"]
            or cstr(
                _existing_value(existing_transaction, "fb_order_payment")
            ).strip()
            != prepared_sale["fb_order_payment"]
            or cstr(
                _existing_value(existing_transaction, "request_fingerprint")
            ).strip()
            != expected_request_fingerprint
            or cstr(
                _existing_value(existing_transaction, "company")
            ).strip()
            != prepared_sale["company"]
        ):
            frappe.throw(
                "Maybank QR idempotency key belongs to another prepared sale",
                frappe.ValidationError,
            )
    existing_response = _resolve_existing_txn(
        device_id,
        idempotency_key,
        amount_sen,
        _coerce_site_datetime(now),
        existing=existing_transaction,
        allow_paid_replay=True,
    )
    if existing_response:
        return existing_response

    replacement_rejection = prepared_sale.get("replacement_rejection")
    if isinstance(replacement_rejection, MaybankQrReplacementRejection):
        return _register_preflight_rejection_fence(
            device_id=device_id,
            idempotency_key=idempotency_key,
            amount_sen=amount_sen,
            currency=currency,
            outlet_id="",
            rejection=replacement_rejection,
            prepared_sale=prepared_sale,
            replacement_request=replacement_request,
        )

    preflight_rejection = prepared_sale.get("preflight_rejection")
    if isinstance(preflight_rejection, MaybankQrPreflightRejection):
        return _register_preflight_rejection_fence(
            device_id=device_id,
            idempotency_key=idempotency_key,
            amount_sen=amount_sen,
            currency=currency,
            outlet_id="",
            rejection=preflight_rejection,
            prepared_sale=prepared_sale,
            replacement_request=None,
        )

    try:
        # Resolve the authenticated device's POS Profile once and use that
        # immutable snapshot for all accounting/provider preflight checks.  A
        # second lookup here could otherwise observe a branch configuration
        # change halfway through one QR request.
        profile = profile_for_device(device_id)
        resolve_manual_qr_suspense_account(prepared_sale)
        resolve_verified_qr_settlement_account(
            prepared_sale["payment_method"],
            prepared_sale["company"],
            prepared_sale["currency"],
            cstr(getattr(profile, "custom_kopos_qr_clearing_account", None)).strip(),
        )
        validate_settlement_bank_account(
            profile,
            currency=prepared_sale["currency"],
        )
    except frappe.ValidationError:
        return _register_preflight_rejection_fence(
            device_id=device_id,
            idempotency_key=idempotency_key,
            amount_sen=amount_sen,
            currency=currency,
            outlet_id="",
            rejection=_configuration_preflight_rejection(),
            prepared_sale=prepared_sale,
            replacement_request=replacement_request,
        )

    try:
        if not cint(getattr(profile, PROFILE_AUTOMATIC_ENABLED_FIELD, 0)):
            raise frappe.ValidationError("Automatic QR is disabled on the POS Profile")
        account, profile_outlet_id = require_provider_binding(profile, for_new_payment=True)
        client = MaybankClient.from_account_doc(
            account,
            profile_outlet_id,
            require_enabled=True,
        )
    except frappe.ValidationError:
        return _register_preflight_rejection_fence(
            device_id=device_id,
            idempotency_key=idempotency_key,
            amount_sen=amount_sen,
            currency=currency,
            outlet_id="",
            rejection=_configuration_preflight_rejection(),
            prepared_sale=prepared_sale,
            replacement_request=replacement_request,
        )
    outlet_id = cstr(client.outlet_id).strip()
    if not outlet_id:
        return _register_preflight_rejection_fence(
            device_id=device_id,
            idempotency_key=idempotency_key,
            amount_sen=amount_sen,
            currency=currency,
            outlet_id="",
            rejection=_configuration_preflight_rejection(),
            prepared_sale=prepared_sale,
            replacement_request=replacement_request,
        )
    try:
        _check_rate_limit(device_id, outlet_id)
    except MaybankQrPreflightRejection as rejection:
        return _register_preflight_rejection_fence(
            device_id=device_id,
            idempotency_key=idempotency_key,
            amount_sen=amount_sen,
            currency=currency,
            outlet_id=outlet_id,
            rejection=rejection,
            prepared_sale=prepared_sale,
            replacement_request=replacement_request,
        )
    except frappe.ValidationError:
        return _register_preflight_rejection_fence(
            device_id=device_id,
            idempotency_key=idempotency_key,
            amount_sen=amount_sen,
            currency=currency,
            outlet_id=outlet_id,
            rejection=_configuration_preflight_rejection(),
            prepared_sale=prepared_sale,
            replacement_request=replacement_request,
        )
    amount_rm = f"{Decimal(amount_sen) / Decimal('100'):.2f}"
    current_now = now_datetime()
    request_fingerprint = expected_request_fingerprint
    txn = frappe.get_doc(
        {
            "doctype": "Maybank QR Transaction",
            "transaction_refno": _reservation_reference(request_fingerprint),
            "outlet_id": outlet_id,
            "pos_profile": cstr(getattr(profile, "name", None)).strip(),
            "maybank_qrpaybiz_account": cstr(getattr(account, "name", None)).strip(),
            "suspense_account": cstr(
                getattr(profile, "custom_kopos_manual_qr_suspense_account", None)
            ).strip(),
            "clearing_account": cstr(
                getattr(profile, "custom_kopos_qr_clearing_account", None)
            ).strip(),
            "settlement_bank_account": cstr(
                getattr(profile, "custom_kopos_qr_settlement_bank_account", None)
            ).strip(),
            "sale_amount": amount_rm,
            "sale_amount_sen": amount_sen,
            "currency": currency,
            "company": prepared_sale["company"],
            "status": "creating",
            "device_id": device_id,
            "provider": MAYBANK_PROVIDER,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "replacement_reason": (
                replacement_request.replacement_reason
                if replacement_request is not None
                else None
            ),
            "replaces_transaction_refno": (
                replacement_request.replaces_transaction_refno
                if replacement_request is not None
                else None
            ),
            "fb_order": prepared_sale["fb_order"],
            "fb_order_payment": prepared_sale["fb_order_payment"],
            "round_number": int(prepared_sale.get("provider_round_number") or 1),
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
            allow_paid_replay=True,
        )
        if existing_response:
            return existing_response
        raise

    frappe.db.set_value(
        "FB Order",
        prepared_sale["fb_order"],
        "automatic_qr_state",
        "provider_pending",
        update_modified=False,
    )

    # The provider does not expose a request idempotency key. Durably reserve
    # this local key before the irreversible network call so an ambiguous
    # timeout cannot roll the reservation back and create a second provider QR.
    frappe.db.commit()

    provider_now = now_datetime()
    try:
        result, refno, qr_data, expires_at = _generate_qr_payload(
            client, amount_rm, provider_now
        )
    except MaybankQrGenerationIdentityMismatch as error:
        _mark_generation_ambiguous(
            request_fingerprint,
            "provider_generation_identity_mismatch",
            result=error.result,
            transaction_refno=error.transaction_refno,
            qr_data=error.qr_data,
            expires_at=error.expires_at,
        )
        frappe.db.commit()
        raise
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
        _recover_known_provider_result(
            request_fingerprint,
            result=result,
            transaction_refno=refno,
            qr_data=qr_data,
            expires_at=expires_at,
        )
        frappe.db.commit()
        raise
