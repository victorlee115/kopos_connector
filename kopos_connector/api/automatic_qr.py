# pyright: reportMissingImports=false

from __future__ import annotations

import html
import json
from typing import Any

import frappe
from frappe.utils import cstr, now_datetime

from kopos_connector.api._maybank_qr_contract import _request_fingerprint
from kopos_connector.api._maybank_qr_persistence import (
    _build_persisted_preflight_rejection_response,
    _load_generation_attempt_candidates_for_release_for_update,
)
from kopos_connector.kopos.api.money_contract import (
    MoneyContractValidationError,
    persisted_money_to_sen,
)


CANCELLABLE_ORDER_STATES = {"prepared", "provider_rejected"}
CANCELLATION_RECOVERY_ACTION = "discard_prepared_sale_and_choose_payment"


def has_durable_no_provider_release_fence(fb_order: str) -> bool:
    """Prove from locked provider evidence that a Draft cannot have been paid."""

    order_doc = _lock_prepared_order(cstr(fb_order).strip())
    if int(getattr(order_doc, "docstatus", 0) or 0) != 0:
        return False
    if cstr(getattr(order_doc, "automatic_qr_state", None)).strip() != (
        "provider_rejected"
    ):
        return False
    payment_name = cstr(
        getattr(order_doc, "automatic_qr_payment", None)
    ).strip()
    if not payment_name:
        return False
    payment = _find_prepared_payment(order_doc, payment_name)
    try:
        attempts = _lock_and_validate_provider_attempts(
            order_doc=order_doc,
            payment=payment,
            amount_sen=_payment_amount_sen(payment),
        )
    except frappe.ValidationError:
        return False

    order_status = cstr(getattr(order_doc, "status", None)).strip()
    if order_status == "Draft":
        return bool(attempts)
    if order_status != "Cancelled":
        return False
    # The immutable FB Order identity plus exact provider-attempt evidence is
    # the release fence. Recipe-era resolved-sale rows are optional display /
    # inventory metadata and must never block shift close.
    return True


def cancel_prepared_automatic_qr_sale_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Durably release a prepared sale only when Maybank was never contacted."""

    identity = _parse_cancellation_identity(payload)
    order_doc = _lock_prepared_order(identity["fb_order"])
    _validate_order_identity(order_doc, identity)
    payment = _find_prepared_payment(order_doc, identity["fb_order_payment"])
    amount_sen = _payment_amount_sen(payment)
    attempts = _lock_and_validate_provider_attempts(
        order_doc=order_doc,
        payment=payment,
        amount_sen=amount_sen,
    )
    state = cstr(getattr(order_doc, "automatic_qr_state", None)).strip()
    if int(getattr(order_doc, "docstatus", 0) or 0) != 0:
        frappe.throw(
            "Submitted Automatic QR sales cannot be cancelled as a prepared sale",
            frappe.ValidationError,
        )
    order_status = cstr(getattr(order_doc, "status", None)).strip()
    if order_status not in {"Draft", "Cancelled"}:
        frappe.throw(
            "Prepared Automatic QR sale is not in Draft status",
            frappe.ValidationError,
        )
    if order_status == "Cancelled":
        if state != "provider_rejected":
            frappe.throw(
                "Prepared Automatic QR cancellation fence is incomplete",
                frappe.ValidationError,
            )
        return _cancellation_response(identity, amount_sen, len(attempts))

    if state not in CANCELLABLE_ORDER_STATES:
        frappe.throw(
            "Automatic QR sale cannot be released after a provider request may have started",
            frappe.ValidationError,
        )
    if attempts and state != "provider_rejected":
        frappe.throw(
            "Prepared Automatic QR sale has inconsistent provider-attempt state",
            frappe.ValidationError,
        )
    if not attempts and state != "prepared":
        frappe.throw(
            "Automatic QR provider rejection evidence is missing",
            frappe.ValidationError,
        )

    frappe.db.set_value(
        "FB Order",
        order_doc.name,
        {
            "status": "Cancelled",
            "automatic_qr_state": "provider_rejected",
        },
        update_modified=False,
    )
    _write_cancellation_audit(identity, amount_sen, len(attempts))

    # Never authorize the tablet to discard local state until ERP has durably
    # fenced the immutable commercial sale and exact provider-attempt truth.
    # Optional legacy recipe/inventory rows are neither read nor written in
    # this cashier transaction; a missing, invalid, or slow subsystem cannot
    # delay local release.
    frappe.db.commit()
    return _cancellation_response(identity, amount_sen, len(attempts))


def _parse_cancellation_identity(payload: dict[str, Any]) -> dict[str, str]:
    identity = {
        fieldname: cstr(payload.get(fieldname)).strip()
        for fieldname in (
            "device_id",
            "fb_order",
            "fb_order_payment",
            "order_id",
            "idempotency_key",
            "accepted_sale_fingerprint",
            "reason",
        )
    }
    for fieldname, value in identity.items():
        if not value:
            frappe.throw(f"{fieldname} is required", frappe.ValidationError)
    if len(identity["reason"]) > 500:
        frappe.throw(
            "reason must not exceed 500 characters",
            frappe.ValidationError,
        )
    return identity


def _lock_prepared_order(fb_order: str) -> Any:
    rows = frappe.db.sql(
        "SELECT name FROM `tabFB Order` WHERE name = %s LIMIT 1 FOR UPDATE",
        (fb_order,),
    )
    if not rows:
        frappe.throw("Prepared Automatic QR sale was not found", frappe.ValidationError)
    return frappe.get_doc("FB Order", fb_order)


def _validate_order_identity(order_doc: Any, identity: dict[str, str]) -> None:
    expected = {
        "device_id": identity["device_id"],
        "order_id": identity["order_id"],
        "external_idempotency_key": identity["idempotency_key"],
        "accepted_sale_fingerprint": identity["accepted_sale_fingerprint"],
        "automatic_qr_payment": identity["fb_order_payment"],
    }
    for fieldname, expected_value in expected.items():
        if cstr(getattr(order_doc, fieldname, None)).strip() != expected_value:
            frappe.throw(
                f"Prepared Automatic QR sale {fieldname} does not match",
                frappe.ValidationError,
            )


def _find_prepared_payment(order_doc: Any, payment_name: str) -> Any:
    payments = [
        payment
        for payment in list(order_doc.get("payments") or [])
        if cstr(getattr(payment, "name", None)).strip() == payment_name
    ]
    if len(payments) != 1:
        frappe.throw(
            "Prepared Automatic QR payment row was not found",
            frappe.ValidationError,
        )
    return payments[0]


def _payment_amount_sen(payment: Any) -> int:
    try:
        amount_sen = persisted_money_to_sen(
            getattr(payment, "amount", None),
            "Prepared Automatic QR payment amount",
        )
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if amount_sen <= 0:
        frappe.throw(
            "Prepared Automatic QR payment amount must be greater than 0",
            frappe.ValidationError,
        )
    return amount_sen


def _lock_and_validate_provider_attempts(
    *,
    order_doc: Any,
    payment: Any,
    amount_sen: int,
) -> list[Any]:
    order_name = cstr(getattr(order_doc, "name", None)).strip()
    payment_name = cstr(getattr(payment, "name", None)).strip()
    device_id = cstr(getattr(order_doc, "device_id", None)).strip()
    accepted_fingerprint = cstr(
        getattr(order_doc, "accepted_sale_fingerprint", None)
    ).strip()
    attempts = _load_generation_attempt_candidates_for_release_for_update(
        order_name,
        payment_name,
    )
    for attempt in attempts:
        attempt_key = cstr(_value(attempt, "idempotency_key")).strip()
        if not attempt_key:
            frappe.throw(
                "Automatic QR attempt has no idempotency key",
                frappe.ValidationError,
            )
        expected_request_fingerprint = _request_fingerprint(
            device_id,
            attempt_key,
            fb_order=order_name,
            fb_order_payment=payment_name,
            accepted_sale_fingerprint=accepted_fingerprint,
            amount_sen=amount_sen,
            currency="MYR",
        )
        if (
            cstr(_value(attempt, "fb_order")).strip() != order_name
            or cstr(_value(attempt, "fb_order_payment")).strip() != payment_name
            or cstr(_value(attempt, "device_id")).strip() != device_id
            or cstr(_value(attempt, "request_fingerprint")).strip()
            != expected_request_fingerprint
            or _build_persisted_preflight_rejection_response(
                attempt,
                device_id=device_id,
                idempotency_key=attempt_key,
                amount_sen=amount_sen,
            )
            is None
        ):
            frappe.throw(
                "Automatic QR sale cannot be released because a provider request may have started",
                frappe.ValidationError,
            )
    return list(attempts)


def _referenced_resolved_sale_names(order_doc: Any) -> list[str]:
    return sorted(
        {
            cstr(_value(line, "resolved_sale")).strip()
            for line in list(_value(order_doc, "items") or [])
            if cstr(_value(line, "resolved_sale")).strip()
        }
    )


def _lock_resolved_sales(order_doc: Any) -> list[Any]:
    referenced_names = _referenced_resolved_sale_names(order_doc)
    if not referenced_names:
        return []
    placeholders = ", ".join(["%s"] * len(referenced_names))
    return list(
        frappe.db.sql(
            f"""
            SELECT name, status
            FROM `tabFB Resolved Sale`
            WHERE fb_order = %s AND name IN ({placeholders})
            ORDER BY name
            FOR UPDATE
            """,
            (cstr(_value(order_doc, "name")).strip(), *referenced_names),
            as_dict=True,
        )
        or []
    )


def _resolution_fence_is_complete(
    order_doc: Any,
    resolved_sales: list[Any],
    *,
    allowed_statuses: set[str],
) -> bool:
    """Validate optional legacy resolved-sale rows without requiring them.

    The accepted sale fingerprint and FB Order lines are the commercial sale
    authority. Older prepared orders can also reference FB Resolved Sale rows;
    when those references exist they must still be present and fenced. New
    non-inventory sales contain only the immutable line snapshot, so an empty
    resolved-sale set is valid and must not block cancellation or safe reset.
    """

    referenced_names = set(_referenced_resolved_sale_names(order_doc))
    rows_by_name = {
        cstr(_value(row, "name")).strip(): row
        for row in resolved_sales
        if cstr(_value(row, "name")).strip()
    }
    if referenced_names != set(rows_by_name):
        return False
    return all(
        cstr(_value(row, "status")).strip() in allowed_statuses
        for row in resolved_sales
    )


def _write_cancellation_audit(
    identity: dict[str, str], amount_sen: int, attempt_count: int
) -> None:
    audit = {
        "event": "prepared_automatic_qr_sale_cancelled",
        "device_id": identity["device_id"],
        "fb_order": identity["fb_order"],
        "fb_order_payment": identity["fb_order_payment"],
        "order_id": identity["order_id"],
        "idempotency_key": identity["idempotency_key"],
        "accepted_sale_fingerprint": identity["accepted_sale_fingerprint"],
        "amount_sen": amount_sen,
        "provider_attempt_fence_count": attempt_count,
        "provider_request_attempted": False,
        "reason": identity["reason"],
        "cancelled_by": cstr(getattr(frappe.session, "user", None)).strip(),
        "cancelled_at": cstr(now_datetime()),
    }
    comment = frappe.get_doc(
        {
            "doctype": "Comment",
            "comment_type": "Info",
            "reference_doctype": "FB Order",
            "reference_name": identity["fb_order"],
            "content": "<pre>"
            + html.escape(json.dumps(audit, sort_keys=True, separators=(",", ":")))
            + "</pre>",
        }
    )
    comment.insert(ignore_permissions=True)


def _cancellation_response(
    identity: dict[str, str], amount_sen: int, attempt_count: int
) -> dict[str, Any]:
    return {
        "status": "cancelled",
        "device_id": identity["device_id"],
        "fb_order": identity["fb_order"],
        "fb_order_payment": identity["fb_order_payment"],
        "order_id": identity["order_id"],
        "idempotency_key": identity["idempotency_key"],
        "accepted_sale_fingerprint": identity["accepted_sale_fingerprint"],
        "automatic_qr_state": "provider_rejected",
        "amount_sen": amount_sen,
        "provider_attempt_fence_count": attempt_count,
        "provider_request_attempted": False,
        "cancellation_fence_registered": True,
        "local_cancellation_authorized": True,
        "recovery_action": CANCELLATION_RECOVERY_ACTION,
    }


def _value(row: Any, fieldname: str) -> Any:
    if isinstance(row, dict):
        return row.get(fieldname)
    return getattr(row, fieldname, None)
