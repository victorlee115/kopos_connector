# pyright: reportMissingImports=false
"""Finance-only resolution for a static claim received after Maybank won.

Cashiers can create the evidence-bound claim, but only a non-device System
Manager may decide whether a second bank credit exists.  This service never
creates or changes a sale, invoice, stock movement, or winning payment.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import cint, cstr, getdate, now_datetime

from kopos_connector.api.devices import lock_device_for_operational_mutation
from kopos_connector.kopos.api.money_contract import (
    MoneyContractValidationError,
    persisted_money_to_sen,
)
from kopos_connector.kopos.services.accounting._duplicate_qr_contract import (
    MAX_PROVIDER_EVIDENCE_BYTES,
    MAYBANK_CURRENCY,
    MAYBANK_PROVIDER,
    _bounded_text,
    _strict_positive_sen,
    _text,
    _validate_winning_provider_transaction,
    _validate_winning_sales_invoice,
    _value,
)
from kopos_connector.kopos.services.accounting._duplicate_qr_journal import (
    _create_or_recover_journal,
    _find_existing_journal,
    _set_source_values,
    _validate_journal,
)
from kopos_connector.kopos.services.accounting._secondary_static_claim_contract import (
    accounts as _accounts,
    assert_exact_fields as _assert_exact_fields,
    liability_context as _liability_context,
    liability_snapshot as _liability_snapshot,
    lock_exact_row as _lock_exact_row,
    no_credit_key as _no_credit_key,
    recorded_credit_evidence as _recorded_credit_evidence,
    recorded_refund_evidence as _recorded_refund_evidence,
    refund_context as _refund_context,
    refund_snapshot as _refund_snapshot,
    require_schema_fields as _require_schema_fields,
    required as _required,
    set_exact_fields as _set_exact_fields,
    validate_evidence_file as _validate_evidence_file,
    validated_date as _validated_date,
)


CONTRACT_VERSION = "kopos.secondary-static-qr-finance.v1"
SOURCE_DOCTYPE = "Manual QR Reconciliation"
WINNER_DOCTYPE = "Maybank QR Transaction"
JOURNAL_ENTRY_DOCTYPE = "Journal Entry"
SECONDARY_CLAIM_ROLE = "secondary_possible_duplicate"

ACTION_NO_SECOND_CREDIT = "confirm_no_second_credit"
ACTION_SECOND_CREDIT = "confirm_independent_second_credit"
ACTION_REFUND = "record_independent_second_credit_refund"
ALLOWED_ACTIONS = {
    ACTION_NO_SECOND_CREDIT,
    ACTION_SECOND_CREDIT,
    ACTION_REFUND,
}

STATUS_PENDING = "pending_review"
STATUS_NO_SECOND_CREDIT = "no_second_credit"
STATUS_REFUND_REQUIRED = "refund_required"
STATUS_REFUNDED = "refunded"

CONFIRMATIONS = {
    ACTION_NO_SECOND_CREDIT: "I CONFIRM NO SECOND STATIC QR CREDIT",
    ACTION_SECOND_CREDIT: "I CONFIRM AN INDEPENDENT SECOND STATIC QR CREDIT",
    ACTION_REFUND: "I CONFIRM THE SECOND STATIC QR CREDIT WAS REFUNDED",
}
def resolve_secondary_static_claim(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Apply one exact, idempotent finance decision to a reverse-winner claim."""

    request = _parse_request(payload)
    order_doc, winner, claim = _lock_and_load_scope(request)
    identity = _validate_identity(
        request,
        order_doc=order_doc,
        winner=winner,
        claim=claim,
    )
    _require_schema_fields()

    action = request["action"]
    if action == ACTION_NO_SECOND_CREDIT:
        return _confirm_no_second_credit(claim, identity=identity, request=request)
    if action == ACTION_SECOND_CREDIT:
        return _confirm_second_credit(claim, identity=identity, request=request)
    return _record_refund(claim, identity=identity, request=request)


def assert_secondary_static_claim_terminal(
    claim: Any,
    *,
    order_doc: Any,
    winner: Any,
) -> dict[str, Any]:
    """Re-prove a no-credit or refunded terminal resolution without mutation."""

    identity = _validate_identity_from_records(
        order_doc=order_doc,
        winner=winner,
        claim=claim,
    )
    status = _text(_value(claim, "finance_resolution_status"))
    if status == STATUS_NO_SECOND_CREDIT:
        _assert_terminal_audit(claim, identity=identity, refunded=False)
        evidence = _recorded_credit_evidence(claim)
        expected_key = _no_credit_key(identity, evidence)
        if (
            _text(_value(claim, "finance_resolution_decision"))
            != "no_second_credit"
            or _text(_value(claim, "finance_resolution_key")) != expected_key
            or _text(_value(claim, "status")) != "reconciliation_failed"
            or _text(_value(claim, "reconciliation_failed_reason"))
            != "no_bank_transaction"
            or _text(_value(claim, "finance_liability_journal_entry"))
            or _text(_value(claim, "finance_refund_journal_entry"))
        ):
            frappe.throw(
                "Secondary static QR no-credit resolution is incomplete",
                frappe.ValidationError,
            )
        file_evidence = _validate_evidence_file(
            evidence,
            claim_name=identity["claim_name"],
        )
        return {
            "claim": identity["claim_name"],
            "finance_resolution_status": status,
            "fb_order": identity["order_name"],
            "sales_invoice": identity["invoice_name"],
            **file_evidence,
        }
    if status != STATUS_REFUNDED:
        frappe.throw(
            "Secondary static QR claim is not terminal",
            frappe.ValidationError,
        )
    _assert_terminal_audit(claim, identity=identity, refunded=True)
    liability = _assert_liability(claim, identity=identity)
    refund = _assert_refund(claim, identity=identity)
    if (
        _text(_value(claim, "status")) != "reconciled"
        or not _text(_value(claim, "finance_resolved_by"))
        or not _text(_value(claim, "finance_resolved_at"))
    ):
        frappe.throw(
            "Secondary static QR refund resolution is incomplete",
            frappe.ValidationError,
        )
    return {
        "claim": identity["claim_name"],
        "finance_resolution_status": status,
        "fb_order": identity["order_name"],
        "sales_invoice": identity["invoice_name"],
        "liability_journal_entry": liability["journal_entry"],
        "refund_journal_entry": refund["journal_entry"],
        "credit_evidence_byte_length": liability["evidence_byte_length"],
        "refund_evidence_byte_length": refund["evidence_byte_length"],
    }


def _assert_terminal_audit(
    claim: Any,
    *,
    identity: Mapping[str, Any],
    refunded: bool,
) -> None:
    resolution_idempotency_key = _text(
        _value(claim, "finance_resolution_idempotency_key")
    )
    resolution_note = _text(_value(claim, "finance_resolution_note"))
    resolved_by = _text(_value(claim, "finance_resolved_by"))
    resolved_at = _text(_value(claim, "finance_resolved_at"))
    reconciled_by = _text(_value(claim, "reconciled_by"))
    reconciled_at = _text(_value(claim, "reconciled_at"))
    if (
        not 12 <= len(resolution_idempotency_key) <= 140
        or not 20 <= len(resolution_note) <= 1000
        or not resolved_by
        or not resolved_at
        or reconciled_by != resolved_by
        or reconciled_at != resolved_at
    ):
        frappe.throw(
            "Secondary static QR terminal audit snapshot is incomplete",
            frappe.ValidationError,
        )
    if not refunded:
        reviewed_through_date = _validated_date(
            _value(claim, "finance_reviewed_through_date"),
            fieldname="finance_reviewed_through_date",
            earliest=identity["business_date"],
        )
        forbidden = (
            "finance_credit_reference",
            "finance_credit_date",
            "finance_clearing_account",
            "finance_liability_account",
            "finance_liability_journal_entry",
            "finance_refund_key",
            "finance_refund_idempotency_key",
            "finance_refund_reference",
            "finance_refund_date",
            "finance_refund_journal_entry",
        )
        if (
            _text(_value(claim, "finance_reviewed_through_date"))
            != reviewed_through_date
            or _text(_value(claim, "reconciliation_note")) != resolution_note
            or any(_text(_value(claim, fieldname)) for fieldname in forbidden)
        ):
            frappe.throw(
                "Secondary static QR no-credit audit snapshot is inconsistent",
                frappe.ValidationError,
            )
        return

    refund_idempotency_key = _text(
        _value(claim, "finance_refund_idempotency_key")
    )
    refund_note = _text(_value(claim, "finance_refund_note"))
    credit_reference = _text(_value(claim, "finance_credit_reference"))
    refund_reference = _text(_value(claim, "finance_refund_reference"))
    credit_date = _validated_date(
        _value(claim, "finance_credit_date"),
        fieldname="finance_credit_date",
        earliest=identity["business_date"],
    )
    refund_date = _validated_date(
        _value(claim, "finance_refund_date"),
        fieldname="finance_refund_date",
        earliest=credit_date,
    )
    if (
        _text(_value(claim, "finance_resolution_decision"))
        != "independent_second_credit"
        or not 12 <= len(refund_idempotency_key) <= 140
        or not 20 <= len(refund_note) <= 1000
        or _text(_value(claim, "reconciliation_note")) != refund_note
        or _text(_value(claim, "finance_credit_date")) != credit_date
        or _text(_value(claim, "finance_refund_date")) != refund_date
        or not credit_reference
        or not refund_reference
        or credit_reference == identity["winning_transaction_refno"]
        or refund_reference
        in {identity["winning_transaction_refno"], credit_reference}
    ):
        frappe.throw(
            "Secondary static QR refunded audit snapshot is inconsistent",
            frappe.ValidationError,
        )


def lock_and_assert_secondary_static_claim_terminal(
    claim_name: str,
    *,
    expected_order_name: str,
    expected_device_id: str,
) -> dict[str, Any]:
    """Acquire exact order/winner/claim locks and re-prove terminal finance evidence."""

    claim_link = frappe.db.get_value(
        SOURCE_DOCTYPE,
        claim_name,
        ["name", "fb_order", "device_id", "winning_maybank_qr_transaction"],
        as_dict=True,
    )
    winner_name = _text(_value(claim_link, "winning_maybank_qr_transaction"))
    if (
        _text(_value(claim_link, "name")) != claim_name
        or _text(_value(claim_link, "fb_order")) != expected_order_name
        or _text(_value(claim_link, "device_id")) != expected_device_id
        or not winner_name
    ):
        frappe.throw(
            "Secondary static QR terminal evidence scope does not match",
            frappe.ValidationError,
        )
    _lock_exact_row("FB Order", expected_order_name, "FB Order")
    _lock_exact_row(WINNER_DOCTYPE, winner_name, "winning Maybank transaction")
    _lock_exact_row(SOURCE_DOCTYPE, claim_name, "secondary static claim")
    order_doc = frappe.get_doc("FB Order", expected_order_name)
    winner = frappe.get_doc(WINNER_DOCTYPE, winner_name)
    claim = frappe.get_doc(SOURCE_DOCTYPE, claim_name)
    for fieldname in (
        "finance_liability_journal_entry",
        "finance_refund_journal_entry",
    ):
        journal_name = _text(_value(claim, fieldname))
        if journal_name:
            _lock_exact_row(JOURNAL_ENTRY_DOCTYPE, journal_name, "finance Journal Entry")
    for fieldname in (
        "finance_credit_evidence_file",
        "finance_refund_evidence_file",
    ):
        file_name = _text(_value(claim, fieldname))
        if file_name:
            _lock_exact_row("File", file_name, "finance evidence File")
    return assert_secondary_static_claim_terminal(
        claim,
        order_doc=order_doc,
        winner=winner,
    )


def _parse_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        frappe.throw("Finance resolution payload is required", frappe.ValidationError)
    request = {
        "contract_version": _required(payload, "contract_version"),
        "action": _required(payload, "action"),
        "claim": _required(payload, "claim"),
        "fb_order": _required(payload, "fb_order"),
        "sales_invoice": _required(payload, "sales_invoice"),
        "fb_order_payment": _required(payload, "fb_order_payment"),
        "winning_maybank_qr_transaction": _required(
            payload, "winning_maybank_qr_transaction"
        ),
        "device_id": _required(payload, "device_id"),
        "currency": _required(payload, "currency").upper(),
        "idempotency_key": _bounded_text(
            payload.get("idempotency_key"),
            "idempotency_key",
            minimum=12,
            maximum=140,
        ),
        "confirmation": _required(payload, "confirmation"),
        "note": _bounded_text(
            payload.get("note"), "note", minimum=20, maximum=1000
        ),
    }
    if request["contract_version"] != CONTRACT_VERSION:
        frappe.throw(
            f"contract_version must be {CONTRACT_VERSION}",
            frappe.ValidationError,
        )
    if request["action"] not in ALLOWED_ACTIONS:
        frappe.throw("Finance resolution action is invalid", frappe.ValidationError)
    if request["confirmation"] != CONFIRMATIONS[request["action"]]:
        frappe.throw(
            "Finance resolution confirmation phrase does not match the action",
            frappe.ValidationError,
        )
    if request["currency"] != MAYBANK_CURRENCY:
        frappe.throw("Finance resolution currency must be MYR", frappe.ValidationError)
    request["amount_sen"] = _strict_positive_sen(
        payload.get("amount_sen"), "amount_sen"
    )
    request["evidence"] = _parse_evidence(payload)
    action = request["action"]
    if action == ACTION_NO_SECOND_CREDIT:
        request["reviewed_through_date"] = _required(
            payload, "reviewed_through_date"
        )
        request["credit_reference"] = ""
        request["credit_date"] = ""
        if cstr(payload.get("credit_reference")).strip():
            frappe.throw(
                "credit_reference must be empty when no second credit exists",
                frappe.ValidationError,
            )
    elif action == ACTION_SECOND_CREDIT:
        request["reviewed_through_date"] = ""
        request["credit_reference"] = _bounded_text(
            payload.get("credit_reference"),
            "credit_reference",
            minimum=6,
            maximum=140,
        )
        request["credit_date"] = _required(payload, "credit_date")
    else:
        request["reviewed_through_date"] = ""
        request["credit_reference"] = ""
        request["credit_date"] = ""
        request["refund_reference"] = _bounded_text(
            payload.get("refund_reference"),
            "refund_reference",
            minimum=6,
            maximum=140,
        )
        request["refund_date"] = _required(payload, "refund_date")
    return request


def _parse_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    sha256 = _required(payload, "evidence_sha256")
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        frappe.throw(
            "evidence_sha256 must be exactly 64 lowercase hexadecimal characters",
            frappe.ValidationError,
        )
    byte_length = cint(payload.get("evidence_byte_length"))
    if byte_length <= 0 or byte_length > MAX_PROVIDER_EVIDENCE_BYTES:
        frappe.throw("evidence_byte_length is invalid", frappe.ValidationError)
    return {
        "reference": _bounded_text(
            payload.get("evidence_reference"),
            "evidence_reference",
            minimum=6,
            maximum=140,
        ),
        "file": _required(payload, "evidence_file"),
        "sha256": sha256,
        "byte_length": byte_length,
    }


def _lock_and_load_scope(request: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    claim_link = frappe.db.get_value(
        SOURCE_DOCTYPE,
        request["claim"],
        ["name", "fb_order", "device_id", "winning_maybank_qr_transaction"],
        as_dict=True,
    )
    if not claim_link:
        frappe.throw("Secondary static QR claim was not found", frappe.ValidationError)
    device_id = _text(_value(claim_link, "device_id"))
    order_name = _text(_value(claim_link, "fb_order"))
    winner_name = _text(_value(claim_link, "winning_maybank_qr_transaction"))
    if (
        device_id != request["device_id"]
        or order_name != request["fb_order"]
        or winner_name != request["winning_maybank_qr_transaction"]
    ):
        frappe.throw(
            "Secondary static QR claim scope does not match the request",
            frappe.ValidationError,
        )
    locked_device = lock_device_for_operational_mutation(device_id=device_id)
    if _text(_value(locked_device, "device_id")) != device_id:
        frappe.throw("Device binding changed while locking", frappe.ValidationError)
    _lock_exact_row("FB Order", order_name, "FB Order")
    _lock_exact_row(WINNER_DOCTYPE, winner_name, "winning Maybank transaction")
    _lock_exact_row(SOURCE_DOCTYPE, request["claim"], "secondary static claim")
    order_doc = frappe.get_doc("FB Order", order_name)
    winner = frappe.get_doc(WINNER_DOCTYPE, winner_name)
    claim = frappe.get_doc(SOURCE_DOCTYPE, request["claim"])
    return order_doc, winner, claim


def _validate_identity(
    request: Mapping[str, Any],
    *,
    order_doc: Any,
    winner: Any,
    claim: Any,
) -> dict[str, Any]:
    identity = _validate_identity_from_records(
        order_doc=order_doc,
        winner=winner,
        claim=claim,
    )
    expected = {
        "claim": identity["claim_name"],
        "fb_order": identity["order_name"],
        "sales_invoice": identity["invoice_name"],
        "fb_order_payment": identity["payment_row_name"],
        "winning_maybank_qr_transaction": identity["winning_transaction"],
        "device_id": identity["device_id"],
        "currency": identity["currency"],
        "amount_sen": identity["amount_sen"],
    }
    for fieldname, expected_value in expected.items():
        if _text(request[fieldname]) != _text(expected_value):
            frappe.throw(
                f"Finance resolution {fieldname} does not match the claim",
                frappe.ValidationError,
            )
    date_field = (
        "reviewed_through_date"
        if request["action"] == ACTION_NO_SECOND_CREDIT
        else "credit_date"
        if request["action"] == ACTION_SECOND_CREDIT
        else "refund_date"
    )
    request[date_field] = _validated_date(
        request[date_field],
        fieldname=date_field,
        earliest=identity["business_date"],
    )
    if request["action"] == ACTION_SECOND_CREDIT and (
        request["credit_reference"] == identity["winning_transaction_refno"]
    ):
        frappe.throw(
            "Independent second-credit reference must differ from the winning Maybank reference",
            frappe.ValidationError,
        )
    if request["action"] == ACTION_REFUND and request["refund_reference"] in {
        identity["winning_transaction_refno"],
        _text(_value(claim, "finance_credit_reference")),
    }:
        frappe.throw(
            "Refund reference must differ from the winning and independent-credit references",
            frappe.ValidationError,
        )
    _lock_exact_row("File", request["evidence"]["file"], "finance evidence File")
    _validate_evidence_file(request["evidence"], claim_name=identity["claim_name"])
    return identity


def _validate_identity_from_records(
    *,
    order_doc: Any,
    winner: Any,
    claim: Any,
) -> dict[str, Any]:
    claim_name = _text(_value(claim, "name"))
    order_name = _text(_value(order_doc, "name"))
    invoice_name = _text(_value(order_doc, "sales_invoice"))
    payment_row_name = _text(_value(claim, "fb_order_payment"))
    winner_name = _text(_value(winner, "name"))
    winner_transaction_refno = _text(_value(winner, "transaction_refno"))
    company = _text(_value(order_doc, "company"))
    currency = _text(_value(order_doc, "currency")).upper()
    device_id = _text(_value(order_doc, "device_id"))
    if (
        not all(
            (
                claim_name,
                order_name,
                invoice_name,
                payment_row_name,
                winner_name,
                winner_transaction_refno,
                company,
                device_id,
            )
        )
        or currency != MAYBANK_CURRENCY
        or cint(_value(order_doc, "docstatus")) != 1
        or _text(_value(order_doc, "automatic_qr_winner_channel"))
        != MAYBANK_PROVIDER
        or _text(_value(claim, "claim_role")) != SECONDARY_CLAIM_ROLE
        or _text(_value(claim, "winning_maybank_qr_transaction")) != winner_name
        or _text(_value(claim, "fb_order")) != order_name
        or _text(_value(claim, "sales_invoice")) != invoice_name
        or _text(_value(claim, "device_id")) != device_id
        or _text(_value(claim, "company")) != company
        or _text(_value(claim, "currency")).upper() != currency
        or _text(_value(claim, "suspense_account"))
        or _text(_value(claim, "reclassification_journal_entry"))
        or _text(_value(claim, "failure_journal_entry"))
    ):
        frappe.throw(
            "Secondary static QR claim does not match the immutable Maybank winning sale",
            frappe.ValidationError,
        )
    payments = [
        payment
        for payment in list(_value(order_doc, "payments") or [])
        if _text(_value(payment, "name")) == payment_row_name
    ]
    if len(payments) != 1:
        frappe.throw("Winning payment row was not found exactly once", frappe.ValidationError)
    payment = payments[0]
    if (
        _text(_value(payment, "maybank_qr_transaction")) != winner_name
        or _text(_value(payment, "settlement_status")) not in {"verified", "reconciled"}
        or cint(_value(payment, "is_manual_confirmation"))
    ):
        frappe.throw("Winning Maybank payment evidence is invalid", frappe.ValidationError)
    try:
        payment_amount_sen = persisted_money_to_sen(
            _value(payment, "amount"), "Winning payment amount"
        )
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    claim_amount_sen = _strict_positive_sen(
        _value(claim, "amount_sen"), "Static claim amount_sen"
    )
    if payment_amount_sen != claim_amount_sen:
        frappe.throw("Static claim amount does not match the sale", frappe.ValidationError)
    identity = {
        "claim_name": claim_name,
        "source_name": claim_name,
        "source_doctype": SOURCE_DOCTYPE,
        "order_name": order_name,
        "invoice_name": invoice_name,
        "payment_row_name": payment_row_name,
        "winning_transaction": winner_name,
        "winning_transaction_refno": winner_transaction_refno,
        "winning_channel": MAYBANK_PROVIDER,
        "winning_static_reconciliation": "",
        "company": company,
        "currency": currency,
        "device_id": device_id,
        "amount_sen": claim_amount_sen,
        "business_date": getdate(_value(claim, "business_date")).isoformat(),
    }
    _validate_winning_provider_transaction(identity)
    _validate_winning_sales_invoice(
        frappe.db.get_value(
            "Sales Invoice",
            invoice_name,
            [
                "name",
                "docstatus",
                "is_return",
                "custom_fb_order",
                "custom_fb_idempotency_key",
                "custom_fb_device_id",
                "custom_fb_void_idempotency_key",
                "custom_fb_void_request_fingerprint",
                "custom_fb_void_manager",
                "custom_fb_void_approval_token_id",
                "company",
                "currency",
            ],
            as_dict=True,
        ),
        order_doc=order_doc,
        identity=identity,
    )
    return identity


def _confirm_no_second_credit(
    claim: Any,
    *,
    identity: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    status = _finance_status(claim)
    evidence = {
        **request["evidence"],
        "reviewed_through_date": request["reviewed_through_date"],
    }
    resolution_key = _no_credit_key(identity, evidence)
    expected = {
        "finance_resolution_status": STATUS_NO_SECOND_CREDIT,
        "finance_resolution_decision": "no_second_credit",
        "finance_resolution_key": resolution_key,
        "finance_resolution_idempotency_key": request["idempotency_key"],
        "finance_reviewed_through_date": request["reviewed_through_date"],
        "finance_credit_evidence_reference": evidence["reference"],
        "finance_credit_evidence_file": evidence["file"],
        "finance_credit_evidence_sha256": evidence["sha256"],
        "finance_credit_evidence_byte_length": evidence["byte_length"],
        "finance_resolution_note": request["note"],
    }
    if status == STATUS_NO_SECOND_CREDIT:
        _assert_exact_fields(claim, expected, "No-second-credit replay")
        terminal = assert_secondary_static_claim_terminal(
            claim,
            order_doc=frappe.get_doc("FB Order", identity["order_name"]),
            winner=frappe.get_doc(WINNER_DOCTYPE, identity["winning_transaction"]),
        )
        return _result("already_resolved", claim, identity, terminal)
    if status != STATUS_PENDING:
        frappe.throw("Secondary static QR claim already has another finance decision", frappe.ValidationError)
    resolved_at = now_datetime()
    _set_source_values(
        claim,
        {
            **expected,
            "status": "reconciliation_failed",
            "reconciliation_failed_reason": "no_bank_transaction",
            "reconciled_by": _session_user(),
            "reconciled_at": resolved_at,
            "reconciliation_note": request["note"],
            "finance_resolved_by": _session_user(),
            "finance_resolved_at": resolved_at,
        },
    )
    _write_audit_comment(claim, action=ACTION_NO_SECOND_CREDIT, request=request)
    terminal = assert_secondary_static_claim_terminal(
        claim,
        order_doc=frappe.get_doc("FB Order", identity["order_name"]),
        winner=frappe.get_doc(WINNER_DOCTYPE, identity["winning_transaction"]),
    )
    return _result("resolved", claim, identity, terminal)


def _confirm_second_credit(
    claim: Any,
    *,
    identity: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    status = _finance_status(claim)
    evidence = {
        **request["evidence"],
        "credit_reference": request["credit_reference"],
        "credit_date": request["credit_date"],
    }
    context = _liability_context(claim, identity=identity, evidence=evidence)
    expected = _liability_snapshot(
        request=request,
        evidence=evidence,
        context=context,
    )
    if status in {STATUS_REFUND_REQUIRED, STATUS_REFUNDED}:
        _assert_exact_fields(claim, expected, "Second-credit replay")
        liability = _assert_liability(claim, identity=identity)
        return _result("already_recorded", claim, identity, liability)
    if status != STATUS_PENDING:
        frappe.throw("Secondary static QR claim already has another finance decision", frappe.ValidationError)
    _set_exact_fields(claim, expected, "Second-credit liability")
    journal_name = _find_existing_journal(
        claim,
        context["journal_key"],
        link_field="finance_liability_journal_entry",
    )
    if not journal_name:
        journal_name = _create_or_recover_journal(context)
    journal = _validate_journal(context, journal_name)
    _set_source_values(
        claim,
        {
            "finance_resolution_status": STATUS_REFUND_REQUIRED,
            "finance_resolution_decision": "independent_second_credit",
            "finance_liability_journal_entry": journal_name,
            "finance_resolution_note": request["note"],
        },
    )
    _write_audit_comment(claim, action=ACTION_SECOND_CREDIT, request=request)
    return _result("refund_required", claim, identity, journal)


def _record_refund(
    claim: Any,
    *,
    identity: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    status = _finance_status(claim)
    if status not in {STATUS_REFUND_REQUIRED, STATUS_REFUNDED}:
        frappe.throw(
            "Independent second credit must be proven before its refund",
            frappe.ValidationError,
        )
    liability = _assert_liability(claim, identity=identity)
    credit_date = _text(_value(claim, "finance_credit_date"))
    request["refund_date"] = _validated_date(
        request["refund_date"], fieldname="refund_date", earliest=credit_date
    )
    evidence = {
        **request["evidence"],
        "refund_reference": request["refund_reference"],
        "refund_date": request["refund_date"],
    }
    context = _refund_context(claim, identity=identity, evidence=evidence)
    expected = _refund_snapshot(request=request, evidence=evidence, context=context)
    if status == STATUS_REFUNDED:
        _assert_exact_fields(claim, expected, "Second-credit refund replay")
        terminal = assert_secondary_static_claim_terminal(
            claim,
            order_doc=frappe.get_doc("FB Order", identity["order_name"]),
            winner=frappe.get_doc(WINNER_DOCTYPE, identity["winning_transaction"]),
        )
        return _result("already_refunded", claim, identity, terminal)
    _set_exact_fields(claim, expected, "Second-credit refund")
    journal_name = _find_existing_journal(
        claim,
        context["journal_key"],
        link_field="finance_refund_journal_entry",
    )
    if not journal_name:
        journal_name = _create_or_recover_journal(context)
    journal = _validate_journal(context, journal_name)
    resolved_at = now_datetime()
    _set_source_values(
        claim,
        {
            "finance_resolution_status": STATUS_REFUNDED,
            "finance_refund_journal_entry": journal_name,
            "finance_refund_note": request["note"],
            "finance_resolved_by": _session_user(),
            "finance_resolved_at": resolved_at,
            "status": "reconciled",
            "reconciliation_failed_reason": None,
            "reconciled_by": _session_user(),
            "reconciled_at": resolved_at,
            "reconciliation_note": request["note"],
        },
    )
    _write_audit_comment(claim, action=ACTION_REFUND, request=request)
    terminal = assert_secondary_static_claim_terminal(
        claim,
        order_doc=frappe.get_doc("FB Order", identity["order_name"]),
        winner=frappe.get_doc(WINNER_DOCTYPE, identity["winning_transaction"]),
    )
    return _result("refunded", claim, identity, {**liability, **journal, **terminal})


def _assert_liability(claim: Any, *, identity: dict[str, Any]) -> dict[str, Any]:
    evidence = _recorded_credit_evidence(claim)
    context = _liability_context(claim, identity=identity, evidence=evidence)
    if _text(_value(claim, "finance_resolution_key")) != context["journal_key"]:
        frappe.throw("Secondary static QR liability key does not match", frappe.ValidationError)
    journal_name = _find_existing_journal(
        claim,
        context["journal_key"],
        link_field="finance_liability_journal_entry",
    )
    if not journal_name:
        frappe.throw("Secondary static QR liability Journal Entry is missing", frappe.ValidationError)
    journal = _validate_journal(context, journal_name)
    file_evidence = _validate_evidence_file(evidence, claim_name=identity["claim_name"])
    return {**journal, **file_evidence}


def _assert_refund(claim: Any, *, identity: dict[str, Any]) -> dict[str, Any]:
    evidence = _recorded_refund_evidence(claim)
    context = _refund_context(claim, identity=identity, evidence=evidence)
    if _text(_value(claim, "finance_refund_key")) != context["journal_key"]:
        frappe.throw("Secondary static QR refund key does not match", frappe.ValidationError)
    journal_name = _find_existing_journal(
        claim,
        context["journal_key"],
        link_field="finance_refund_journal_entry",
    )
    if not journal_name:
        frappe.throw("Secondary static QR refund Journal Entry is missing", frappe.ValidationError)
    journal = _validate_journal(context, journal_name)
    file_evidence = _validate_evidence_file(evidence, claim_name=identity["claim_name"])
    return {**journal, **file_evidence}


def _finance_status(claim: Any) -> str:
    return _text(_value(claim, "finance_resolution_status")) or STATUS_PENDING


def _write_audit_comment(claim: Any, *, action: str, request: Mapping[str, Any]) -> None:
    comment = frappe.get_doc(
        {
            "doctype": "Comment",
            "comment_type": "Info",
            "reference_doctype": SOURCE_DOCTYPE,
            "reference_name": _text(_value(claim, "name")),
            "content": frappe.as_json(
                {
                    "event": "secondary_static_qr_finance_resolution",
                    "action": action,
                    "idempotency_key": request["idempotency_key"],
                    "resolved_by": _session_user(),
                }
            ),
        }
    )
    comment.insert(ignore_permissions=True)


def _result(
    status: str,
    claim: Any,
    identity: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "status": status,
        "contract_version": CONTRACT_VERSION,
        "claim": identity["claim_name"],
        "finance_resolution_status": _finance_status(claim),
        "fb_order": identity["order_name"],
        "sales_invoice": identity["invoice_name"],
        "fb_order_payment": identity["payment_row_name"],
        "winning_maybank_qr_transaction": identity["winning_transaction"],
        "amount_sen": identity["amount_sen"],
        "currency": identity["currency"],
        "liability_journal_entry": _text(
            _value(claim, "finance_liability_journal_entry")
        )
        or None,
        "refund_journal_entry": _text(
            _value(claim, "finance_refund_journal_entry")
        )
        or None,
        "sales_invoice_created": False,
        "evidence": dict(evidence),
    }


def _session_user() -> str:
    user = _text(getattr(frappe.session, "user", None))
    if not user:
        frappe.throw("Authenticated System Manager user is required", frappe.ValidationError)
    return user
