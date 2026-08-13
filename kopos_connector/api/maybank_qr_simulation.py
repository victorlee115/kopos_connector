# pyright: reportMissingImports=false

"""Guarded ERP Desk simulation for isolated mock Maybank QR transactions."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
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
from kopos_connector.services.maybank.client import (
    MAYBANK_MOCK_PAYMENT_MODE_MANUAL,
    MaybankClient,
    _config_value,
    _explicit_mock_mode_enabled,
    _mock_payment_mode,
)

from ._maybank_qr_contract import (
    MAX_AMOUNT_SEN,
    MAYBANK_CURRENCY,
    MAYBANK_MOCK_REFERENCE_PATTERN,
    MAYBANK_PROVIDER,
    REUSABLE_STATUSES,
    _existing_value,
    _extract_status_entry,
    _format_sale_amount,
    _parse_integer_sen,
    _parse_provider_amount_sen,
    _request_fingerprint,
    _serialize_site_datetime,
    _validate_status_entry_identity,
    _validate_status_response,
)
from ._maybank_qr_generation import _validate_new_generation_attempt
from ._maybank_qr_persistence import (
    _load_linked_generation_attempts_for_update,
    _load_txn_for_update,
)
from ._maybank_qr_status import _transition_txn_status_locked


MAYBANK_SIMULATION_FINGERPRINT_PATTERN = re.compile(r"^[a-f0-9]{64}$")
MAYBANK_TEST_SIMULATION_VERSION = "maybank_mock_paid_v1"
MAYBANK_TEST_SIMULATION_CONFIG = "allow_maybank_desk_simulation"
MAYBANK_TEST_SIMULATION_CONFIRMATION = "SIMULATE MAYBANK PAYMENT"


def _maybank_desk_simulation_context_enabled() -> bool:
    if not _explicit_mock_mode_enabled() or not cint(
        _config_value(MAYBANK_TEST_SIMULATION_CONFIG, 0)
    ):
        return False
    try:
        if _mock_payment_mode() != MAYBANK_MOCK_PAYMENT_MODE_MANUAL:
            return False
        return bool(
            frappe.db.exists(
                "Maybank QRPayBiz Account", {"enabled": 1, "base_url": "mock://"}
            )
        )
    except Exception:
        return False


def _require_maybank_desk_simulation_context() -> None:
    if not _explicit_mock_mode_enabled():
        frappe.throw(
            "Maybank payment simulation requires an explicitly opted-in developer or test site",
            frappe.ValidationError,
        )
    if not cint(_config_value(MAYBANK_TEST_SIMULATION_CONFIG, 0)):
        frappe.throw(
            f"Maybank payment simulation requires {MAYBANK_TEST_SIMULATION_CONFIG}=1",
            frappe.ValidationError,
        )
    if _mock_payment_mode() != MAYBANK_MOCK_PAYMENT_MODE_MANUAL:
        frappe.throw(
            "Maybank payment simulation requires maybank_mock_payment_mode=manual",
            frappe.ValidationError,
        )
    if not frappe.db.exists(
        "Maybank QRPayBiz Account", {"enabled": 1, "base_url": "mock://"}
    ):
        frappe.throw(
            "Maybank payment simulation requires an enabled branch-scoped Maybank account with API Base URL mock://",
            frappe.ValidationError,
        )


def get_maybank_qr_simulation_capability(transaction: Any) -> dict[str, bool]:
    """Return only non-sensitive form capability flags for the current session."""
    from kopos_connector.api.devices import (
        KOPOS_DEVICE_API_ROLE,
        get_session_roles,
    )

    roles = get_session_roles()
    authorized = (
        "System Manager" in roles and KOPOS_DEVICE_API_ROLE not in roles
    )
    reference = cstr(_existing_value(transaction, "transaction_refno")).strip()
    status = cstr(_existing_value(transaction, "status")).strip()
    mock_record = bool(MAYBANK_MOCK_REFERENCE_PATTERN.fullmatch(reference))
    test_context = authorized and _maybank_desk_simulation_context_enabled()
    already_simulated = bool(
        authorized
        and mock_record
        and status == "paid"
        and cint(_existing_value(transaction, "is_test_simulation")) == 1
    )
    return {
        "enabled": bool(
            test_context
            and mock_record
            and status in REUSABLE_STATUSES
            and not cint(_existing_value(transaction, "is_test_simulation"))
        ),
        "test_context": bool(test_context and mock_record),
        "already_simulated": already_simulated,
    }


def _load_maybank_simulation_sale_for_update(
    transaction_name: str,
) -> tuple[Any, Any, list[Any]]:
    """Lock the prepared sale before its provider attempts in canonical order."""
    transaction_link = frappe.db.get_value(
        "Maybank QR Transaction",
        transaction_name,
        ["name", "fb_order", "fb_order_payment"],
        as_dict=True,
    )
    if not transaction_link:
        frappe.throw(
            "Maybank QR transaction no longer exists",
            frappe.ValidationError,
        )
    order_name = cstr(_existing_value(transaction_link, "fb_order")).strip()
    payment_name = cstr(
        _existing_value(transaction_link, "fb_order_payment")
    ).strip()
    if not order_name or not payment_name:
        frappe.throw(
            "Maybank test transaction is not bound to a prepared sale",
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

    attempts = _load_linked_generation_attempts_for_update(
        order_name,
        payment_name,
    )
    matching_attempts = [
        attempt
        for attempt in attempts
        if cstr(_existing_value(attempt, "name")).strip() == transaction_name
    ]
    if len(matching_attempts) != 1:
        frappe.throw(
            "Maybank test transaction is not an exact attempt for the prepared sale",
            frappe.ValidationError,
        )

    transaction = _load_txn_for_update(transaction_name)
    if (
        cstr(_existing_value(transaction, "fb_order")).strip() != order_name
        or cstr(_existing_value(transaction, "fb_order_payment")).strip()
        != payment_name
    ):
        frappe.throw(
            "Maybank test transaction prepared-sale binding changed during validation",
            frappe.ValidationError,
        )
    return transaction, frappe.get_doc("FB Order", order_name), attempts


def _validate_maybank_simulation_prepared_sale(
    transaction: Any,
    identity: dict[str, Any],
    order_doc: Any,
    attempts: list[Any],
) -> None:
    """Prove the mock provider attempt still belongs to one exact prepared sale."""
    accepted_sale_fingerprint = cstr(
        getattr(order_doc, "accepted_sale_fingerprint", None)
    ).strip()
    if not MAYBANK_SIMULATION_FINGERPRINT_PATTERN.fullmatch(
        accepted_sale_fingerprint
    ):
        frappe.throw(
            "Prepared Automatic QR sale fingerprint is missing or invalid",
            frappe.ValidationError,
        )
    if cstr(getattr(order_doc, "name", None)).strip() != identity["fb_order"]:
        frappe.throw(
            "Prepared Automatic QR sale identity does not match the Maybank transaction",
            frappe.ValidationError,
        )
    if cstr(getattr(order_doc, "device_id", None)).strip() != identity["device_id"]:
        frappe.throw(
            "Prepared Automatic QR sale belongs to another device",
            frappe.ValidationError,
        )
    if (
        cstr(getattr(order_doc, "currency", None)).strip().upper()
        != identity["currency"]
    ):
        frappe.throw(
            "Prepared Automatic QR sale currency does not match the Maybank transaction",
            frappe.ValidationError,
        )
    if cstr(_existing_value(transaction, "company")).strip() != cstr(
        getattr(order_doc, "company", None)
    ).strip():
        frappe.throw(
            "Prepared Automatic QR sale company does not match the Maybank transaction",
            frappe.ValidationError,
        )
    if (
        cstr(getattr(order_doc, "automatic_qr_payment", None)).strip()
        != identity["fb_order_payment"]
    ):
        frappe.throw(
            "Prepared Automatic QR payment binding does not match the Maybank transaction",
            frappe.ValidationError,
        )

    matching_payments = [
        payment
        for payment in list(order_doc.get("payments") or [])
        if cstr(getattr(payment, "name", None)).strip()
        == identity["fb_order_payment"]
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
    if payment_channel not in {"maybank", "maybank qr"}:
        frappe.throw(
            "Prepared payment is not a Maybank QR payment",
            frappe.ValidationError,
        )
    try:
        payment_amount_sen = persisted_money_to_sen(
            getattr(payment, "amount", None),
            "Prepared Automatic QR payment amount",
        )
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if payment_amount_sen != identity["amount_sen"]:
        frappe.throw(
            "Prepared Automatic QR payment amount does not match the Maybank transaction",
            frappe.ValidationError,
        )

    expected_request_fingerprint = _request_fingerprint(
        identity["device_id"],
        identity["idempotency_key"],
        fb_order=identity["fb_order"],
        fb_order_payment=identity["fb_order_payment"],
        accepted_sale_fingerprint=accepted_sale_fingerprint,
        amount_sen=identity["amount_sen"],
        currency=identity["currency"],
        replacement_reason=cstr(
            _existing_value(transaction, "replacement_reason")
        ).strip(),
        replaces_transaction_refno=cstr(
            _existing_value(transaction, "replaces_transaction_refno")
        ).strip(),
    )
    if not hmac.compare_digest(
        identity["request_fingerprint"],
        expected_request_fingerprint,
    ):
        frappe.throw(
            "Maybank test transaction fingerprint does not match the prepared sale",
            frappe.ValidationError,
        )
    _validate_new_generation_attempt(
        order_doc=order_doc,
        attempts=attempts,
        device_id=identity["device_id"],
        idempotency_key=identity["idempotency_key"],
        request_fingerprint=expected_request_fingerprint,
        amount_sen=identity["amount_sen"],
        currency=identity["currency"],
    )

    if cstr(_existing_value(transaction, "status")).strip() in REUSABLE_STATUSES:
        if cint(getattr(order_doc, "docstatus", 0)) != 0:
            frappe.throw(
                "Only a draft prepared Automatic QR sale can receive a simulated payment",
                frappe.ValidationError,
            )
        if cstr(getattr(order_doc, "automatic_qr_state", None)).strip() != (
            "provider_pending"
        ):
            frappe.throw(
                "Prepared Automatic QR sale is not awaiting provider payment",
                frappe.ValidationError,
            )
        if cstr(getattr(payment, "settlement_status", None)).strip() != (
            "awaiting_provider"
        ):
            frappe.throw(
                "Prepared Automatic QR payment is not awaiting provider settlement",
                frappe.ValidationError,
            )
        if (
            cstr(getattr(payment, "reference_no", None)).strip()
            or cstr(getattr(payment, "external_transaction_id", None)).strip()
            or cint(getattr(payment, "is_manual_confirmation", 0))
        ):
            frappe.throw(
                "Prepared Automatic QR payment already contains settlement evidence",
                frappe.ValidationError,
            )


def _build_maybank_test_simulation_identity(
    transaction: Any,
) -> tuple[dict[str, Any], str, str]:
    transaction_name = cstr(_existing_value(transaction, "name")).strip()
    transaction_refno = cstr(
        _existing_value(transaction, "transaction_refno")
    ).strip()
    if not transaction_name or len(transaction_name) > 140:
        frappe.throw(
            "Maybank test transaction identity is invalid",
            frappe.ValidationError,
        )
    if not MAYBANK_MOCK_REFERENCE_PATTERN.fullmatch(transaction_refno):
        frappe.throw(
            "Maybank payment simulation accepts only generated MOCK-TXN references",
            frappe.ValidationError,
        )

    amount_sen = _parse_integer_sen(
        _existing_value(transaction, "sale_amount_sen"),
        "Maybank transaction sale_amount_sen",
    )
    if amount_sen <= 0 or amount_sen > MAX_AMOUNT_SEN:
        frappe.throw(
            "Maybank test transaction amount is outside the supported range",
            frappe.ValidationError,
        )
    if _parse_provider_amount_sen(
        _existing_value(transaction, "sale_amount")
    ) != amount_sen:
        frappe.throw(
            "Maybank test transaction decimal and integer-sen amounts do not match",
            frappe.ValidationError,
        )

    required_text = {
        "device_id": cstr(_existing_value(transaction, "device_id")).strip(),
        "outlet_id": cstr(_existing_value(transaction, "outlet_id")).strip(),
        "idempotency_key": cstr(
            _existing_value(transaction, "idempotency_key")
        ).strip(),
        "request_fingerprint": cstr(
            _existing_value(transaction, "request_fingerprint")
        ).strip(),
        "fb_order": cstr(_existing_value(transaction, "fb_order")).strip(),
        "fb_order_payment": cstr(
            _existing_value(transaction, "fb_order_payment")
        ).strip(),
        "qr_data": cstr(_existing_value(transaction, "qr_data")).strip(),
    }
    missing = sorted(key for key, value in required_text.items() if not value)
    if missing:
        frappe.throw(
            "Maybank test transaction identity is incomplete: " + ", ".join(missing),
            frappe.ValidationError,
        )
    if not MAYBANK_SIMULATION_FINGERPRINT_PATTERN.fullmatch(
        required_text["request_fingerprint"]
    ):
        frappe.throw(
            "Maybank test transaction request fingerprint is invalid",
            frappe.ValidationError,
        )
    if cstr(_existing_value(transaction, "provider")).strip() != MAYBANK_PROVIDER:
        frappe.throw(
            "Maybank test transaction provider is invalid",
            frappe.ValidationError,
        )
    if cstr(_existing_value(transaction, "currency")).strip().upper() != MAYBANK_CURRENCY:
        frappe.throw(
            "Maybank test transaction currency must be MYR",
            frappe.ValidationError,
        )

    identity: dict[str, Any] = {
        "version": MAYBANK_TEST_SIMULATION_VERSION,
        "transaction_name": transaction_name,
        "transaction_refno": transaction_refno,
        "amount_sen": amount_sen,
        "currency": MAYBANK_CURRENCY,
        "device_id": required_text["device_id"],
        "outlet_id": required_text["outlet_id"],
        "idempotency_key": required_text["idempotency_key"],
        "request_fingerprint": required_text["request_fingerprint"],
        "fb_order": required_text["fb_order"],
        "fb_order_payment": required_text["fb_order_payment"],
        "qr_data_sha256": hashlib.sha256(
            required_text["qr_data"].encode("utf-8")
        ).hexdigest(),
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    identity_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    simulation_key = hashlib.sha256(
        (
            MAYBANK_TEST_SIMULATION_VERSION
            + "\0"
            + transaction_name
            + "\0"
            + required_text["request_fingerprint"]
        ).encode("utf-8")
    ).hexdigest()
    return identity, identity_sha256, simulation_key


def _audit_maybank_test_payment_simulation(
    *,
    transaction_name: str,
    transaction_refno: str,
    amount_sen: int,
    prior_status: str,
    simulated_by: str,
    simulated_at: Any,
    identity_sha256: str,
) -> None:
    audit_payload = {
        "event": "maybank_qr_mock_payment_simulated",
        "version": MAYBANK_TEST_SIMULATION_VERSION,
        "transaction": transaction_name,
        "transaction_refno": transaction_refno,
        "amount_sen": amount_sen,
        "prior_status": prior_status,
        "new_status": "paid",
        "simulated_by": simulated_by,
        "simulated_at": _serialize_site_datetime(simulated_at),
        "identity_sha256": identity_sha256,
    }
    comment = frappe.get_doc(
        {
            "doctype": "Comment",
            "comment_type": "Info",
            "reference_doctype": "Maybank QR Transaction",
            "reference_name": transaction_name,
            "content": "<pre>"
            + html.escape(
                json.dumps(audit_payload, sort_keys=True, separators=(",", ":"))
            )
            + "</pre>",
        }
    )
    comment.insert(ignore_permissions=True)


def simulate_maybank_qr_payment_payload(
    transaction_name: str,
    confirmation: str,
) -> dict[str, Any]:
    """Apply one controlled mock paid response through the production transition."""
    _require_maybank_desk_simulation_context()
    if cstr(confirmation) != MAYBANK_TEST_SIMULATION_CONFIRMATION:
        frappe.throw(
            f"confirmation must be exactly {MAYBANK_TEST_SIMULATION_CONFIRMATION}",
            frappe.ValidationError,
        )
    resolved_name = cstr(transaction_name).strip()
    if not resolved_name or len(resolved_name) > 140:
        frappe.throw("transaction_name is invalid", frappe.ValidationError)

    transaction, order_doc, attempts = _load_maybank_simulation_sale_for_update(
        resolved_name
    )
    identity, identity_sha256, simulation_key = (
        _build_maybank_test_simulation_identity(transaction)
    )
    _validate_maybank_simulation_prepared_sale(
        transaction,
        identity,
        order_doc,
        attempts,
    )
    status = cstr(_existing_value(transaction, "status")).strip()
    if status == "paid":
        if not cint(_existing_value(transaction, "is_test_simulation")):
            frappe.throw(
                "A real provider-paid transaction cannot be relabelled as a test simulation",
                frappe.ValidationError,
            )
        if (
            cstr(_existing_value(transaction, "test_simulation_key")).strip()
            != simulation_key
            or cstr(
                _existing_value(transaction, "test_simulation_identity_sha256")
            ).strip()
            != identity_sha256
        ):
            frappe.throw(
                "Maybank test simulation evidence does not match the transaction identity",
                frappe.ValidationError,
            )
        stored_simulated_by = cstr(
            _existing_value(transaction, "test_simulated_by")
        ).strip()
        stored_simulated_at = _existing_value(transaction, "test_simulated_at")
        if not stored_simulated_by or not stored_simulated_at:
            frappe.throw(
                "Maybank test simulation audit evidence is incomplete",
                frappe.ValidationError,
            )
        return {
            "status": "paid",
            "result": "already_simulated",
            "transaction_name": identity["transaction_name"],
            "transaction_refno": identity["transaction_refno"],
            "sale_amount_sen": identity["amount_sen"],
            "test_only": True,
            "simulated_by": stored_simulated_by,
            "simulated_at": _serialize_site_datetime(stored_simulated_at),
        }
    if status not in REUSABLE_STATUSES:
        frappe.throw(
            "Only a pending or scanned mock Maybank transaction can be simulated as paid",
            frappe.ValidationError,
        )
    if cint(_existing_value(transaction, "is_test_simulation")):
        frappe.throw(
            "Maybank test simulation marker is inconsistent with transaction status",
            frappe.ValidationError,
        )

    simulated_by = cstr(getattr(frappe.session, "user", None)).strip()
    if not simulated_by or simulated_by == "Guest":
        frappe.throw("Authentication required", frappe.ValidationError)
    simulated_at = now_datetime()
    result = {
        "status": "QR000",
        "data": [
            {
                "status": 1,
                "transaction_refno": identity["transaction_refno"],
                "sale_amount": _format_sale_amount(identity["amount_sen"]),
                "outlet_id": identity["outlet_id"],
                "currency": MAYBANK_CURRENCY,
            }
        ],
        "kopos_test_simulation": {
            "version": MAYBANK_TEST_SIMULATION_VERSION,
            "identity_sha256": identity_sha256,
        },
    }
    _validate_status_response(result)
    entry = _extract_status_entry(result)
    if entry is None:
        frappe.throw(
            "Maybank test simulation response is empty",
            frappe.ValidationError,
        )
    raw_status = _validate_status_entry_identity(transaction, entry)
    if raw_status != 1:
        frappe.throw(
            "Maybank test simulation did not produce provider-paid status",
            frappe.ValidationError,
        )
    transitioned_status = _transition_txn_status_locked(
        transaction,
        "paid",
        raw_status,
        result,
    )
    if transitioned_status != "paid":
        frappe.throw(
            "Maybank test transaction could not transition to paid",
            frappe.ValidationError,
        )
    frappe.db.set_value(
        "Maybank QR Transaction",
        identity["transaction_name"],
        {
            "is_test_simulation": 1,
            "test_simulation_key": simulation_key,
            "test_simulation_identity_sha256": identity_sha256,
            "test_simulated_by": simulated_by,
            "test_simulated_at": simulated_at,
        },
        update_modified=False,
    )
    _audit_maybank_test_payment_simulation(
        transaction_name=identity["transaction_name"],
        transaction_refno=identity["transaction_refno"],
        amount_sen=identity["amount_sen"],
        prior_status=status,
        simulated_by=simulated_by,
        simulated_at=simulated_at,
        identity_sha256=identity_sha256,
    )
    return {
        "status": "paid",
        "result": "simulated",
        "transaction_name": identity["transaction_name"],
        "transaction_refno": identity["transaction_refno"],
        "sale_amount_sen": identity["amount_sen"],
        "test_only": True,
        "simulated_by": simulated_by,
        "simulated_at": _serialize_site_datetime(simulated_at),
        "finalization": "queued_or_recoverable",
    }
