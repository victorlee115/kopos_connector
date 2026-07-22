# pyright: reportMissingImports=false
"""Finalize one prepared Automatic QR sale with a static-QR confirmation.

The sale and its resolved-item snapshot were already accepted before Maybank
generation started.  This module changes only the settlement channel under the
same FB Order/payment/device locks.  Provider attempts remain immutable linked
evidence and continue through their normal polling lifecycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import cint, cstr, now_datetime

from kopos_connector.api._maybank_qr_persistence import (
    _load_linked_generation_attempts_for_update,
)
from kopos_connector.api._maybank_qr_contract import _request_fingerprint
from kopos_connector.kopos.api.fb_orders import (
    _build_submit_response,
    _normalize_order_payment,
)
from kopos_connector.kopos.api.money_contract import (
    MoneyContractValidationError,
    SEN_MONEY_CONTRACT_VERSION,
    parse_sen,
    persisted_money_to_sen,
)
from kopos_connector.kopos.services.accounting.automatic_qr_finalization_core import (
    _register_late_paid_incidents_after_sale_commit,
)
from kopos_connector.kopos.services.accounting.maybank_payment_service import (
    normalize_qr_token,
)
from kopos_connector.kopos.services.accounting._prepared_static_qr_response import (
    build_secondary_claim_response,
)
from kopos_connector.kopos.services.orders.sale_datetime import (
    normalize_site_datetime,
    resolve_order_sale_datetime,
)


CONFIRMATION_CONTRACT_VERSION = "kopos.prepared-static-qr-confirmation.v1"
STATIC_QR_CHANNEL = "static_qr"
STATIC_QR_WINNER = "static_qr"
MAYBANK_PROVIDER = "maybank_qr"
MAYBANK_CURRENCY = "MYR"
WINNING_STATIC_CLAIM_ROLE = "winning_settlement"
SECONDARY_STATIC_CLAIM_ROLE = "secondary_possible_duplicate"
ELIGIBLE_DRAFT_STATES = {
    "prepared",
    "provider_pending",
    "provider_ambiguous",
    "provider_rejected",
    "provider_paid",
    "manual_pending_reconciliation",
}


def confirm_prepared_static_qr_payment(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Submit a prepared sale once for an exact local static-QR confirmation."""

    request = _parse_request(payload)
    order_doc = _lock_order(request["fb_order"])
    payment = _validate_prepared_identity(order_doc, request)
    attempts = _lock_and_validate_attempts(
        order_doc=order_doc,
        payment=payment,
        request=request,
    )

    if cint(_value(order_doc, "docstatus")) == 1:
        winner_channel = cstr(
            _value(order_doc, "automatic_qr_winner_channel")
        ).strip()
        if winner_channel == STATIC_QR_WINNER:
            _validate_static_winner_replay(order_doc, payment, request)
            return _response("duplicate", order_doc, payment, attempts)
        winning_attempt = _validate_submitted_maybank_winner(
            order_doc,
            payment,
            attempts,
        )
        static_claim, created = _register_secondary_static_claim(
            order_doc=order_doc,
            payment=payment,
            winning_attempt=winning_attempt,
            request=request,
        )
        return build_secondary_claim_response(
            "ok" if created else "duplicate",
            order_doc,
            payment,
            attempts,
            static_claim,
            winning_attempt,
            confirmation_contract_version=CONFIRMATION_CONTRACT_VERSION,
            maybank_provider=MAYBANK_PROVIDER,
            secondary_claim_role=SECONDARY_STATIC_CLAIM_ROLE,
        )
    if cint(_value(order_doc, "docstatus")) != 0:
        frappe.throw(
            "Prepared Automatic QR sale is not eligible for static QR confirmation",
            frappe.ValidationError,
        )
    state = cstr(_value(order_doc, "automatic_qr_state")).strip()
    if state not in ELIGIBLE_DRAFT_STATES:
        frappe.throw(
            "Prepared Automatic QR sale state cannot accept static QR confirmation",
            frappe.ValidationError,
        )

    normalized_payment = request["normalized_payment"]
    payment.payment_channel_code = STATIC_QR_CHANNEL
    payment.reference_no = normalized_payment["reference_no"]
    payment.external_transaction_id = normalized_payment[
        "external_transaction_id"
    ]
    payment.is_manual_confirmation = 1
    payment.manual_confirmation_evidence_json = normalized_payment[
        "manual_confirmation_evidence_json"
    ]
    payment.reconciliation_idempotency_key = normalized_payment[
        "reconciliation_idempotency_key"
    ]
    payment.settlement_status = "pending_reconciliation"
    payment.maybank_qr_transaction = None
    payment.manual_qr_reconciliation = None
    payment.suspense_account = None

    order_doc.automatic_qr_winner_channel = STATIC_QR_WINNER
    order_doc.automatic_qr_state = "manual_pending_reconciliation"
    order_doc.save(ignore_permissions=True)
    order_doc.submit()

    static_reconciliation = cstr(
        _value(payment, "manual_qr_reconciliation")
    ).strip()
    if not static_reconciliation:
        frappe.throw(
            "Static QR confirmation did not create reconciliation evidence",
            frappe.ValidationError,
        )
    frappe.db.set_value(
        "FB Order",
        cstr(_value(order_doc, "name")).strip(),
        {
            "automatic_qr_state": "finalized",
            "automatic_qr_winner_channel": STATIC_QR_WINNER,
            "automatic_qr_static_reconciliation": static_reconciliation,
        },
        update_modified=False,
    )
    order_doc.automatic_qr_state = "finalized"
    order_doc.automatic_qr_static_reconciliation = static_reconciliation

    paid_attempts = [attempt for attempt in attempts if _attempt_is_paid(attempt)]
    incident_registration_pending = _register_late_paid_incidents_after_sale_commit(
        paid_attempts,
        order_doc=order_doc,
        winning_transaction_name="",
    )
    result = _response("ok", order_doc, payment, attempts)
    if incident_registration_pending:
        result["incident_registration_pending"] = incident_registration_pending
    return result


def _parse_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        frappe.throw("Static QR confirmation payload is required", frappe.ValidationError)
    contract_version = cstr(payload.get("confirmation_contract_version")).strip()
    if contract_version != CONFIRMATION_CONTRACT_VERSION:
        frappe.throw(
            f"confirmation_contract_version must be {CONFIRMATION_CONTRACT_VERSION}",
            frappe.ValidationError,
        )
    money_contract_version = cstr(payload.get("money_contract_version")).strip()
    if money_contract_version != SEN_MONEY_CONTRACT_VERSION:
        frappe.throw(
            f"money_contract_version must be {SEN_MONEY_CONTRACT_VERSION}",
            frappe.ValidationError,
        )

    required_text = (
        "device_id",
        "fb_order",
        "fb_order_payment",
        "order_id",
        "idempotency_key",
        "accepted_sale_fingerprint",
        "payment_id",
        "company",
        "currency",
        "provider_session_id",
        "payment_reference",
        "local_confirmed_at",
    )
    request = {fieldname: cstr(payload.get(fieldname)).strip() for fieldname in required_text}
    for fieldname, value in request.items():
        if not value:
            frappe.throw(f"{fieldname} is required", frappe.ValidationError)
    if request["currency"].upper() != MAYBANK_CURRENCY:
        frappe.throw("Static QR confirmation currency must be MYR", frappe.ValidationError)
    request["currency"] = MAYBANK_CURRENCY
    if not request["provider_session_id"].startswith("static-"):
        frappe.throw(
            "provider_session_id must use the static QR session namespace",
            frappe.ValidationError,
        )
    if request["payment_reference"].lower().startswith("mbqr-"):
        frappe.throw(
            "Static QR payment_reference must not use a Maybank transaction reference",
            frappe.ValidationError,
        )
    try:
        amount_sen = parse_sen(payload.get("amount_sen"), "amount_sen")
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if amount_sen <= 0:
        frappe.throw("amount_sen must be greater than 0", frappe.ValidationError)

    evidence = payload.get("manual_confirmation_evidence")
    if not isinstance(evidence, Mapping):
        frappe.throw(
            "manual_confirmation_evidence is required",
            frappe.ValidationError,
        )
    if cstr(evidence.get("local_confirmed_at")).strip() != request[
        "local_confirmed_at"
    ]:
        frappe.throw(
            "local_confirmed_at does not match manual confirmation evidence",
            frappe.ValidationError,
        )
    if cstr(evidence.get("evidence_captured_device_id")).strip() != request[
        "device_id"
    ]:
        frappe.throw(
            "Static QR evidence device does not match the request",
            frappe.ValidationError,
        )
    if cstr(evidence.get("local_confirmation_reference")).strip() != request[
        "payment_reference"
    ]:
        frappe.throw(
            "Static QR evidence reference does not match payment_reference",
            frappe.ValidationError,
        )

    try:
        normalized_payment = _normalize_order_payment(
            {
                "payment_id": request["payment_id"],
                "payment_method": "DuitNow QR",
                "payment_channel_code": STATIC_QR_CHANNEL,
                "amount_sen": amount_sen,
                "tendered_amount_sen": amount_sen,
                "change_amount_sen": 0,
                "reference_no": request["payment_reference"],
                "external_transaction_id": request["provider_session_id"],
                "manual_confirmation_evidence": dict(evidence),
            },
            1,
            SEN_MONEY_CONTRACT_VERSION,
        )
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    request["amount_sen"] = amount_sen
    request["normalized_payment"] = normalized_payment
    request["manual_confirmation_evidence"] = dict(evidence)
    return request


def _lock_order(order_name: str) -> Any:
    rows = frappe.db.sql(
        "SELECT name FROM `tabFB Order` WHERE name = %s LIMIT 1 FOR UPDATE",
        (order_name,),
    )
    if len(rows or []) != 1:
        frappe.throw("Prepared Automatic QR sale was not found", frappe.ValidationError)
    return frappe.get_doc("FB Order", order_name)


def _validate_prepared_identity(order_doc: Any, request: Mapping[str, Any]) -> Any:
    expected = {
        "name": request["fb_order"],
        "device_id": request["device_id"],
        "order_id": request["order_id"],
        "external_idempotency_key": request["idempotency_key"],
        "accepted_sale_fingerprint": request["accepted_sale_fingerprint"],
        "automatic_qr_payment": request["fb_order_payment"],
        "company": request["company"],
        "currency": request["currency"],
    }
    for fieldname, expected_value in expected.items():
        actual = cstr(_value(order_doc, fieldname)).strip()
        if fieldname == "currency":
            actual = actual.upper()
        if actual != expected_value:
            frappe.throw(
                f"Prepared Automatic QR sale {fieldname} does not match",
                frappe.ValidationError,
            )
    payments = [
        row
        for row in list(_value(order_doc, "payments") or [])
        if cstr(_value(row, "name")).strip() == request["fb_order_payment"]
    ]
    if len(payments) != 1:
        frappe.throw(
            "Prepared Automatic QR payment row was not found exactly once",
            frappe.ValidationError,
        )
    payment = payments[0]
    if cstr(_value(payment, "source_payment_id")).strip() != request["payment_id"]:
        frappe.throw(
            "Prepared Automatic QR payment_id does not match",
            frappe.ValidationError,
        )
    try:
        persisted_amount_sen = persisted_money_to_sen(
            _value(payment, "amount"),
            "Prepared Automatic QR payment amount",
        )
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if persisted_amount_sen != request["amount_sen"]:
        frappe.throw(
            "Prepared Automatic QR payment amount does not match",
            frappe.ValidationError,
        )
    channel = normalize_qr_token(_value(payment, "payment_channel_code"))
    winner = cstr(_value(order_doc, "automatic_qr_winner_channel")).strip()
    if cint(_value(order_doc, "docstatus")) == 0 and channel not in {
        "maybank",
        "maybank qr",
    }:
        frappe.throw(
            "Prepared Automatic QR payment is not awaiting a provider decision",
            frappe.ValidationError,
        )
    if cint(_value(order_doc, "docstatus")) == 1 and not (
        (channel == "static qr" and winner == STATIC_QR_WINNER)
        or (channel in {"maybank", "maybank qr"} and winner == MAYBANK_PROVIDER)
    ):
        frappe.throw(
            "Prepared Automatic QR sale was finalized by another payment channel",
            frappe.ValidationError,
        )
    confirmed_by = cstr(
        request["manual_confirmation_evidence"].get("local_confirmed_by")
    ).strip()
    if confirmed_by != cstr(_value(order_doc, "staff_id")).strip():
        frappe.throw(
            "Static QR evidence confirmer does not match the prepared sale staff",
            frappe.ValidationError,
        )
    return payment


def _lock_and_validate_attempts(
    *,
    order_doc: Any,
    payment: Any,
    request: Mapping[str, Any],
) -> list[Any]:
    attempts = _load_linked_generation_attempts_for_update(
        request["fb_order"],
        request["fb_order_payment"],
    )
    for attempt in attempts:
        expected = {
            "fb_order": request["fb_order"],
            "fb_order_payment": request["fb_order_payment"],
            "device_id": request["device_id"],
            "company": request["company"],
            "currency": MAYBANK_CURRENCY,
            "provider": MAYBANK_PROVIDER,
        }
        for fieldname, expected_value in expected.items():
            actual = cstr(_value(attempt, fieldname)).strip()
            if fieldname == "currency":
                actual = actual.upper()
            elif fieldname == "provider":
                actual = actual.lower()
            if actual != expected_value:
                frappe.throw(
                    f"Maybank QR attempt {fieldname} does not match the prepared sale",
                    frappe.ValidationError,
                )
        raw_amount = _value(attempt, "sale_amount_sen")
        try:
            attempt_amount_sen = parse_sen(raw_amount, "Maybank QR attempt sale_amount_sen")
        except MoneyContractValidationError as error:
            frappe.throw(str(error), frappe.ValidationError)
            raise AssertionError("frappe.throw must raise") from error
        if attempt_amount_sen != request["amount_sen"]:
            frappe.throw(
                "Maybank QR attempt amount does not match the prepared sale",
                frappe.ValidationError,
            )
        attempt_key = cstr(_value(attempt, "idempotency_key")).strip()
        if not attempt_key:
            frappe.throw(
                "Maybank QR attempt idempotency key is missing",
                frappe.ValidationError,
            )
        expected_fingerprint = _request_fingerprint(
            request["device_id"],
            attempt_key,
            fb_order=request["fb_order"],
            fb_order_payment=request["fb_order_payment"],
            accepted_sale_fingerprint=request["accepted_sale_fingerprint"],
            amount_sen=request["amount_sen"],
            currency=MAYBANK_CURRENCY,
            replacement_reason=cstr(
                _value(attempt, "replacement_reason")
            ).strip(),
            replaces_transaction_refno=cstr(
                _value(attempt, "replaces_transaction_refno")
            ).strip(),
        )
        if cstr(_value(attempt, "request_fingerprint")).strip() != (
            expected_fingerprint
        ):
            frappe.throw(
                "Maybank QR attempt fingerprint does not match the prepared sale",
                frappe.ValidationError,
            )
        if _attempt_is_paid(attempt) and (
            not cstr(_value(attempt, "transaction_refno")).strip()
            or cstr(_value(attempt, "transaction_refno"))
            .strip()
            .lower()
            .startswith("static-")
            or not cstr(_value(attempt, "qr_data")).strip()
            or not cstr(_value(attempt, "outlet_id")).strip()
            or not cstr(_value(attempt, "paid_at")).strip()
        ):
            frappe.throw(
                "Paid Maybank QR attempt lacks exact provider evidence",
                frappe.ValidationError,
            )
        if (
            cstr(_value(attempt, "maybank_status")).strip() == "1"
            and cstr(_value(attempt, "status")).strip().lower() != "paid"
        ):
            frappe.throw(
                "Maybank QR attempt has inconsistent provider-paid state",
                frappe.ValidationError,
            )
    return attempts


def _validate_static_winner_replay(
    order_doc: Any,
    payment: Any,
    request: Mapping[str, Any],
) -> None:
    normalized = request["normalized_payment"]
    expected = {
        "payment_channel_code": STATIC_QR_CHANNEL,
        "reference_no": normalized["reference_no"],
        "external_transaction_id": normalized["external_transaction_id"],
        "manual_confirmation_evidence_json": normalized[
            "manual_confirmation_evidence_json"
        ],
        "reconciliation_idempotency_key": normalized[
            "reconciliation_idempotency_key"
        ],
    }
    for fieldname, expected_value in expected.items():
        actual = cstr(_value(payment, fieldname)).strip()
        if fieldname == "payment_channel_code":
            actual = normalize_qr_token(actual).replace(" ", "_")
        if actual != cstr(expected_value).strip():
            frappe.throw(
                "Static QR confirmation replay has different settlement evidence",
                frappe.ValidationError,
            )
    if not cint(_value(payment, "is_manual_confirmation")):
        frappe.throw(
            "Static QR confirmation replay lacks manual confirmation evidence",
            frappe.ValidationError,
        )
    if cstr(_value(payment, "settlement_status")).strip() not in {
        "pending_reconciliation",
        "reconciled",
        "reconciliation_failed",
    }:
        frappe.throw(
            "Static QR confirmation replay settlement state is invalid",
            frappe.ValidationError,
        )
    reconciliation = cstr(_value(payment, "manual_qr_reconciliation")).strip()
    if (
        not reconciliation
        or reconciliation
        != cstr(_value(order_doc, "automatic_qr_static_reconciliation")).strip()
    ):
        frappe.throw(
            "Static QR confirmation replay reconciliation identity does not match",
            frappe.ValidationError,
        )


def _validate_submitted_maybank_winner(
    order_doc: Any,
    payment: Any,
    attempts: list[Any],
) -> Any:
    order_name = cstr(_value(order_doc, "name")).strip()
    payment_name = cstr(_value(payment, "name")).strip()
    invoice_name = cstr(_value(order_doc, "sales_invoice")).strip()
    winning_transaction_name = cstr(
        _value(payment, "maybank_qr_transaction")
    ).strip()
    transaction_reference = cstr(
        _value(payment, "external_transaction_id")
    ).strip()
    if (
        cstr(_value(order_doc, "automatic_qr_state")).strip() != "finalized"
        or cstr(_value(order_doc, "automatic_qr_winner_channel")).strip()
        != MAYBANK_PROVIDER
        or normalize_qr_token(_value(payment, "payment_channel_code"))
        not in {"maybank", "maybank qr"}
        or cint(_value(payment, "is_manual_confirmation"))
        or cstr(_value(payment, "settlement_status")).strip() != "verified"
        or not winning_transaction_name
        or not transaction_reference
        or cstr(_value(payment, "reference_no")).strip()
        != transaction_reference
        or cstr(_value(payment, "manual_qr_reconciliation")).strip()
        or cstr(_value(payment, "suspense_account")).strip()
        or not invoice_name
    ):
        frappe.throw(
            "Submitted Maybank QR winner lacks exact immutable settlement evidence",
            frappe.ValidationError,
        )

    winners = [
        attempt
        for attempt in attempts
        if cstr(_value(attempt, "name")).strip() == winning_transaction_name
    ]
    if len(winners) != 1:
        frappe.throw(
            "Winning Maybank QR transaction was not found exactly once",
            frappe.ValidationError,
        )
    winner = winners[0]
    expected = {
        "transaction_refno": transaction_reference,
        "fb_order": order_name,
        "fb_order_payment": payment_name,
        "sales_invoice": invoice_name,
        "consumption_key": order_name,
        "invoice_consumption_key": invoice_name,
    }
    for fieldname, expected_value in expected.items():
        if cstr(_value(winner, fieldname)).strip() != expected_value:
            frappe.throw(
                f"Winning Maybank QR transaction {fieldname} does not match the submitted sale",
                frappe.ValidationError,
            )
    if not _attempt_is_paid(winner):
        frappe.throw(
            "Winning Maybank QR transaction lacks authoritative paid evidence",
            frappe.ValidationError,
        )
    if not cstr(_value(winner, "consumed_at")).strip():
        frappe.throw(
            "Winning Maybank QR transaction lacks durable sale consumption evidence",
            frappe.ValidationError,
        )
    return winner


def _register_secondary_static_claim(
    *,
    order_doc: Any,
    payment: Any,
    winning_attempt: Any,
    request: Mapping[str, Any],
) -> tuple[Any, bool]:
    normalized = request["normalized_payment"]
    idempotency_key = cstr(
        normalized["reconciliation_idempotency_key"]
    ).strip()
    existing = _load_secondary_static_claim_for_update(idempotency_key)
    if existing:
        _validate_secondary_static_claim(
            existing,
            order_doc=order_doc,
            payment=payment,
            winning_attempt=winning_attempt,
            request=request,
        )
        return existing, False

    evidence = request["manual_confirmation_evidence"]
    try:
        captured_at = normalize_site_datetime(
            evidence.get("captured_at"),
            fieldname="secondary static QR evidence captured_at",
        )
    except ValueError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    values = {
        "doctype": "Manual QR Reconciliation",
        "status": "pending_reconciliation",
        "claim_role": SECONDARY_STATIC_CLAIM_ROLE,
        "finance_resolution_status": "pending_review",
        "winning_maybank_qr_transaction": cstr(
            _value(winning_attempt, "name")
        ).strip(),
        "fb_order": cstr(_value(order_doc, "name")).strip(),
        "sales_invoice": cstr(_value(order_doc, "sales_invoice")).strip(),
        "fb_order_payment": cstr(_value(payment, "name")).strip(),
        "device_id": cstr(_value(order_doc, "device_id")).strip(),
        "staff_id": cstr(_value(order_doc, "staff_id")).strip(),
        "company": cstr(_value(order_doc, "company")).strip(),
        "currency": cstr(_value(order_doc, "currency")).strip().upper(),
        "business_date": resolve_order_sale_datetime(order_doc).date().isoformat(),
        "amount_sen": request["amount_sen"],
        "payment_reference": request["payment_reference"],
        "provider_session_id": request["provider_session_id"],
        "reconciliation_idempotency_key": idempotency_key,
        "suspense_account": None,
        "evidence_kind": cstr(evidence.get("evidence_kind")).strip(),
        "evidence_captured_at": captured_at,
        "evidence_json": normalized["manual_confirmation_evidence_json"],
        "created_at": now_datetime(),
    }
    claim = frappe.get_doc(values)
    try:
        claim.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        existing = _load_secondary_static_claim_for_update(idempotency_key)
        if not existing:
            frappe.throw(
                "Static QR claim conflicts with existing reconciliation evidence",
                frappe.ValidationError,
            )
        _validate_secondary_static_claim(
            existing,
            order_doc=order_doc,
            payment=payment,
            winning_attempt=winning_attempt,
            request=request,
        )
        return existing, False
    return claim, True


def _load_secondary_static_claim_for_update(idempotency_key: str) -> Any | None:
    rows = frappe.db.sql(
        """
        SELECT
            name, status, claim_role, winning_maybank_qr_transaction,
            finance_resolution_status, finance_resolution_decision,
            fb_order, sales_invoice, fb_order_payment, device_id, staff_id,
            company, currency, business_date, amount_sen, payment_reference,
            provider_session_id, reconciliation_idempotency_key,
            suspense_account, evidence_kind, evidence_captured_at, evidence_json,
            reclassification_journal_entry, failure_journal_entry
        FROM `tabManual QR Reconciliation`
        WHERE reconciliation_idempotency_key = %s
        LIMIT 1
        FOR UPDATE
        """,
        (idempotency_key,),
        as_dict=True,
    )
    return rows[0] if rows else None


def _validate_secondary_static_claim(
    claim: Any,
    *,
    order_doc: Any,
    payment: Any,
    winning_attempt: Any,
    request: Mapping[str, Any],
) -> None:
    normalized = request["normalized_payment"]
    evidence = request["manual_confirmation_evidence"]
    expected = {
        "claim_role": SECONDARY_STATIC_CLAIM_ROLE,
        "winning_maybank_qr_transaction": cstr(
            _value(winning_attempt, "name")
        ).strip(),
        "fb_order": cstr(_value(order_doc, "name")).strip(),
        "sales_invoice": cstr(_value(order_doc, "sales_invoice")).strip(),
        "fb_order_payment": cstr(_value(payment, "name")).strip(),
        "device_id": cstr(_value(order_doc, "device_id")).strip(),
        "staff_id": cstr(_value(order_doc, "staff_id")).strip(),
        "company": cstr(_value(order_doc, "company")).strip(),
        "currency": cstr(_value(order_doc, "currency")).strip().upper(),
        "business_date": resolve_order_sale_datetime(order_doc).date().isoformat(),
        "payment_reference": request["payment_reference"],
        "provider_session_id": request["provider_session_id"],
        "reconciliation_idempotency_key": cstr(
            normalized["reconciliation_idempotency_key"]
        ).strip(),
        "evidence_kind": cstr(evidence.get("evidence_kind")).strip(),
        "evidence_json": normalized["manual_confirmation_evidence_json"],
    }
    for fieldname, expected_value in expected.items():
        actual = cstr(_value(claim, fieldname)).strip()
        if fieldname == "currency":
            actual = actual.upper()
        if actual != cstr(expected_value).strip():
            frappe.throw(
                f"Secondary static QR claim {fieldname} does not match this retry",
                frappe.ValidationError,
            )
    try:
        claim_amount_sen = parse_sen(
            _value(claim, "amount_sen"),
            "Secondary static QR claim amount_sen",
        )
        expected_captured_at = normalize_site_datetime(
            evidence.get("captured_at"),
            fieldname="secondary static QR evidence captured_at",
        )
        claim_captured_at = normalize_site_datetime(
            _value(claim, "evidence_captured_at"),
            fieldname="stored secondary static QR evidence captured_at",
        )
    except (MoneyContractValidationError, ValueError) as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if claim_amount_sen != request["amount_sen"]:
        frappe.throw(
            "Secondary static QR claim amount does not match this retry",
            frappe.ValidationError,
        )
    if claim_captured_at != expected_captured_at:
        frappe.throw(
            "Secondary static QR claim captured_at does not match this retry",
            frappe.ValidationError,
        )
    if (
        cstr(_value(claim, "suspense_account")).strip()
        or cstr(_value(claim, "reclassification_journal_entry")).strip()
        or cstr(_value(claim, "failure_journal_entry")).strip()
    ):
        frappe.throw(
            "Secondary static QR claim must remain unposted pending duplicate-payment review",
            frappe.ValidationError,
        )
    finance_status = cstr(
        _value(claim, "finance_resolution_status")
    ).strip() or "pending_review"
    claim_status = cstr(_value(claim, "status")).strip()
    allowed_status_pairs = {
        "pending_review": "pending_reconciliation",
        "no_second_credit": "reconciliation_failed",
        "refund_required": "pending_reconciliation",
        "refunded": "reconciled",
    }
    if allowed_status_pairs.get(finance_status) != claim_status:
        frappe.throw(
            "Secondary static QR claim finance state does not match its reconciliation state",
            frappe.ValidationError,
        )


def _response(
    status: str,
    order_doc: Any,
    payment: Any,
    attempts: list[Any],
) -> dict[str, Any]:
    projection = _build_submit_response(status, order_doc)
    return {
        "status": status,
        "partial_failure": projection["partial_failure"],
        "confirmation_contract_version": CONFIRMATION_CONTRACT_VERSION,
        "settlement_state": cstr(
            _value(payment, "settlement_status")
        ).strip(),
        "winner_channel": STATIC_QR_WINNER,
        "static_claim_role": WINNING_STATIC_CLAIM_ROLE,
        "static_claim_status": cstr(
            _value(payment, "settlement_status")
        ).strip(),
        "static_claim_registered": True,
        "static_claim_is_sale_winner": True,
        "winning_maybank_qr_transaction": None,
        "winning_payment_settlement_state": None,
        "automatic_qr_state": "finalized",
        "fb_order": cstr(_value(order_doc, "name")).strip(),
        "fb_order_payment": cstr(_value(payment, "name")).strip(),
        "order_id": cstr(_value(order_doc, "order_id")).strip(),
        "idempotency_key": cstr(
            _value(order_doc, "external_idempotency_key")
        ).strip(),
        "accepted_sale_fingerprint": cstr(
            _value(order_doc, "accepted_sale_fingerprint")
        ).strip(),
        "payment_id": cstr(_value(payment, "source_payment_id")).strip(),
        "manual_qr_reconciliation": cstr(
            _value(payment, "manual_qr_reconciliation")
        ).strip(),
        "reconciliation_idempotency_key": cstr(
            _value(payment, "reconciliation_idempotency_key")
        ).strip(),
        "sales_invoice": cstr(_value(order_doc, "sales_invoice")).strip() or None,
        "ingredient_stock_entry": cstr(
            _value(order_doc, "ingredient_stock_entry")
        ).strip()
        or None,
        "invoice_status": cstr(_value(order_doc, "invoice_status")).strip() or None,
        "stock_status": cstr(_value(order_doc, "stock_status")).strip() or None,
        "order_status": cstr(_value(order_doc, "status")).strip() or None,
        "sale_datetime": projection["sale_datetime"],
        "projection_status": projection["projection_status"],
        "failed_subsystem": projection["failed_subsystem"],
        "diagnostics": projection["diagnostics"],
        "message": projection["message"],
        "projections": projection["projections"],
        "maybank_attempt_count": len(attempts),
        "maybank_attempts_retained": True,
    }


def _attempt_is_paid(attempt: Any) -> bool:
    return (
        cstr(_value(attempt, "status")).strip().lower() == "paid"
        and cstr(_value(attempt, "maybank_status")).strip() == "1"
    )


def _value(document: Any, fieldname: str) -> Any:
    if isinstance(document, Mapping):
        return document.get(fieldname)
    getter = getattr(document, "get", None)
    if callable(getter):
        value = getter(fieldname)
        if value is not None:
            return value
    return getattr(document, fieldname, None)
