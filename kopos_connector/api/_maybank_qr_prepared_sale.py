# pyright: reportMissingImports=false

"""Locked prepared-sale loading for Maybank provider generation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import frappe
from frappe.utils import cint, cstr, now_datetime

from kopos_connector.kopos.api.money_contract import (
    MoneyContractValidationError,
    persisted_money_to_sen,
)
from kopos_connector.kopos.services.accounting.maybank_payment_service import (
    normalize_qr_token,
)

from ._maybank_qr_contract import (
    MAYBANK_CURRENCY,
    MaybankQrPreflightRejection,
    _request_fingerprint,
)
from ._maybank_qr_persistence import _load_linked_generation_attempts_for_update
from ._maybank_qr_replacement import (
    MaybankQrReplacementRequest,
    MaybankQrReplacementRejection,
)


def load_prepared_automatic_qr_sale(
    *,
    fb_order: str,
    fb_order_payment: str,
    accepted_sale_fingerprint: str,
    device_id: str,
    amount_sen: int,
    idempotency_key: str,
    currency: str,
    replacement_request: MaybankQrReplacementRequest | None,
    validate_generation_attempt: Callable[..., int],
) -> dict[str, Any]:
    """Lock and prove the immutable prepared sale before provider preflight."""

    if not fb_order:
        frappe.throw("fb_order is required", frappe.ValidationError)
    if not fb_order_payment:
        frappe.throw("fb_order_payment is required", frappe.ValidationError)
    if not accepted_sale_fingerprint:
        frappe.throw(
            "accepted_sale_fingerprint is required",
            frappe.ValidationError,
        )

    frappe.db.sql(
        "SELECT name FROM `tabFB Order` WHERE name = %s LIMIT 1 FOR UPDATE",
        (fb_order,),
    )
    order_doc = frappe.get_doc("FB Order", fb_order)
    if cstr(getattr(order_doc, "device_id", None)).strip() != device_id:
        frappe.throw(
            "Prepared Automatic QR sale belongs to another device",
            frappe.ValidationError,
        )
    if cstr(getattr(order_doc, "currency", None)).strip().upper() != MAYBANK_CURRENCY:
        frappe.throw(
            "Prepared Automatic QR sale currency must be MYR",
            frappe.ValidationError,
        )
    if cstr(
        getattr(order_doc, "accepted_sale_fingerprint", None)
    ).strip() != accepted_sale_fingerprint:
        frappe.throw(
            "accepted_sale_fingerprint does not match the prepared sale",
            frappe.ValidationError,
        )
    if cstr(getattr(order_doc, "automatic_qr_payment", None)).strip() != (
        fb_order_payment
    ):
        frappe.throw(
            "fb_order_payment does not match the prepared sale",
            frappe.ValidationError,
        )
    matching_payments = [
        payment
        for payment in list(order_doc.get("payments") or [])
        if cstr(getattr(payment, "name", None)).strip() == fb_order_payment
    ]
    if len(matching_payments) != 1:
        frappe.throw(
            "Prepared Automatic QR payment row was not found",
            frappe.ValidationError,
        )
    payment = matching_payments[0]
    payment_channel = normalize_qr_token(
        getattr(payment, "payment_channel_code", None)
    )
    static_reconciliation = cstr(
        getattr(payment, "manual_qr_reconciliation", None)
    ).strip()
    is_submitted_static_winner = bool(
        payment_channel == "static qr"
        and cint(getattr(order_doc, "docstatus", 0)) == 1
        and cstr(getattr(order_doc, "automatic_qr_state", None)).strip()
        == "finalized"
        and cstr(
            getattr(order_doc, "automatic_qr_winner_channel", None)
        ).strip()
        == "static_qr"
        and static_reconciliation
        and static_reconciliation
        == cstr(
            getattr(
                order_doc,
                "automatic_qr_static_reconciliation",
                None,
            )
        ).strip()
        and cint(getattr(payment, "is_manual_confirmation", 0))
        and not cstr(
            getattr(payment, "maybank_qr_transaction", None)
        ).strip()
    )
    if payment_channel not in {"maybank", "maybank qr"} and not (
        is_submitted_static_winner
    ):
        frappe.throw(
            "Prepared payment is not a Maybank QR payment",
            frappe.ValidationError,
        )
    try:
        persisted_amount_sen = persisted_money_to_sen(
            getattr(payment, "amount", None),
            "Prepared Automatic QR payment amount",
        )
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if persisted_amount_sen != amount_sen:
        frappe.throw(
            "Prepared Automatic QR payment amount does not match",
            frappe.ValidationError,
        )
    request_fingerprint = _request_fingerprint(
        device_id,
        idempotency_key,
        fb_order=fb_order,
        fb_order_payment=fb_order_payment,
        accepted_sale_fingerprint=accepted_sale_fingerprint,
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
    attempts = _load_linked_generation_attempts_for_update(
        fb_order,
        fb_order_payment,
    )
    replacement_rejection: MaybankQrReplacementRejection | None = None
    preflight_rejection: MaybankQrPreflightRejection | None = None
    try:
        provider_round_number = validate_generation_attempt(
            order_doc=order_doc,
            attempts=attempts,
            device_id=device_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            amount_sen=amount_sen,
            currency=currency,
            accepted_sale_fingerprint=accepted_sale_fingerprint,
            replacement_request=replacement_request,
            now=now_datetime(),
        )
    except MaybankQrReplacementRejection as rejection:
        # The order and every linked attempt are locked and identity-checked.
        # The outer workflow will register a durable no-provider fence.
        replacement_rejection = rejection
        provider_round_number = 1
    except MaybankQrPreflightRejection as rejection:
        # A submitted static winner cannot start a new provider call. The
        # outer workflow persists an exact no-provider fence for this request
        # identity so a late background job can release its local candidate.
        preflight_rejection = rejection
        provider_round_number = 1
    prepared_sale: dict[str, Any] = {
        "fb_order": fb_order,
        "fb_order_payment": fb_order_payment,
        "accepted_sale_fingerprint": accepted_sale_fingerprint,
        "payment_method": cstr(
            getattr(payment, "payment_method", None)
        ).strip(),
        "company": cstr(getattr(order_doc, "company", None)).strip(),
        "currency": cstr(getattr(order_doc, "currency", None)).strip().upper(),
        "provider_round_number": provider_round_number,
    }
    if replacement_rejection is not None:
        prepared_sale["replacement_rejection"] = replacement_rejection
    if preflight_rejection is not None:
        prepared_sale["preflight_rejection"] = preflight_rejection
    return prepared_sale
