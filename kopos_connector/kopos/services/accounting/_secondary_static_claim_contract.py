# pyright: reportMissingImports=false
"""Evidence, accounting-context, and schema helpers for secondary static claims."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import cint, cstr, getdate, now_datetime

from kopos_connector.kopos.services.accounting._duplicate_qr_contract import (
    COMPANY_CLEARING_ACCOUNT_FIELD,
    COMPANY_LIABILITY_ACCOUNT_FIELD,
    LIABILITY_RECOGNITION_STAGE,
    MAX_PROVIDER_EVIDENCE_BYTES,
    MAYBANK_PROVIDER,
    REFUND_STAGE,
    _text,
    _validate_account,
    _value,
)
from kopos_connector.kopos.services.accounting._duplicate_qr_journal import (
    _set_source_values,
)
from kopos_connector.kopos.services.accounting._duplicate_qr_terminal_evidence import (
    _validate_private_provider_evidence_file,
)


SOURCE_DOCTYPE = "Manual QR Reconciliation"
JOURNAL_ENTRY_DOCTYPE = "Journal Entry"
LIABILITY_KEY_NAMESPACE = "kopos:secondary-static-liability:v1:"
NO_CREDIT_KEY_NAMESPACE = "kopos:secondary-static-no-credit:v1:"
REFUND_KEY_NAMESPACE = "kopos:secondary-static-refund:v1:"

REQUIRED_SOURCE_FIELDS = (
    "finance_resolution_status",
    "finance_resolution_decision",
    "finance_resolution_key",
    "finance_resolution_idempotency_key",
    "finance_reviewed_through_date",
    "finance_credit_reference",
    "finance_credit_date",
    "finance_credit_evidence_reference",
    "finance_credit_evidence_file",
    "finance_credit_evidence_sha256",
    "finance_credit_evidence_byte_length",
    "finance_clearing_account",
    "finance_liability_account",
    "finance_liability_journal_entry",
    "finance_resolution_note",
    "finance_refund_key",
    "finance_refund_idempotency_key",
    "finance_refund_reference",
    "finance_refund_date",
    "finance_refund_evidence_reference",
    "finance_refund_evidence_file",
    "finance_refund_evidence_sha256",
    "finance_refund_evidence_byte_length",
    "finance_refund_journal_entry",
    "finance_refund_note",
    "finance_resolved_by",
    "finance_resolved_at",
)


def liability_context(
    claim: Any,
    *,
    identity: dict[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    clearing, liability = accounts(claim, identity=identity)
    raw_key = "|".join(
        [
            identity["claim_name"],
            identity["winning_transaction"],
            evidence["credit_reference"],
            evidence["credit_date"],
            evidence["reference"],
            evidence["file"],
            evidence["sha256"],
            _text(evidence["byte_length"]),
            _text(identity["amount_sen"]),
            identity["currency"],
        ]
    )
    key = LIABILITY_KEY_NAMESPACE + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return journal_context(
        claim,
        identity=identity,
        stage=LIABILITY_RECOGNITION_STAGE,
        key=key,
        posting_date=evidence["credit_date"],
        clearing=clearing,
        liability=liability,
        evidence=evidence,
    )


def refund_context(
    claim: Any,
    *,
    identity: dict[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    clearing, liability = accounts(claim, identity=identity)
    liability_key = _text(_value(claim, "finance_resolution_key"))
    raw_key = "|".join(
        [
            liability_key,
            evidence["refund_reference"],
            evidence["refund_date"],
            evidence["reference"],
            evidence["file"],
            evidence["sha256"],
            _text(evidence["byte_length"]),
        ]
    )
    key = REFUND_KEY_NAMESPACE + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return journal_context(
        claim,
        identity=identity,
        stage=REFUND_STAGE,
        key=key,
        posting_date=evidence["refund_date"],
        clearing=clearing,
        liability=liability,
        evidence=evidence,
    )


def journal_context(
    claim: Any,
    *,
    identity: dict[str, Any],
    stage: str,
    key: str,
    posting_date: str,
    clearing: str,
    liability: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    is_refund = stage == REFUND_STAGE
    return {
        "source": claim,
        "source_name": identity["claim_name"],
        "source_doctype": SOURCE_DOCTYPE,
        "provider_transaction": identity["winning_transaction"],
        "stage": stage,
        "journal_key": key,
        "recognition_key": _text(_value(claim, "finance_resolution_key")) or key,
        "winning_channel": MAYBANK_PROVIDER,
        "winning_transaction": identity["winning_transaction"],
        "winning_static_reconciliation": "",
        "legacy_dynamic_winner_metadata": False,
        "order_name": identity["order_name"],
        "invoice_name": identity["invoice_name"],
        "company": identity["company"],
        "currency": identity["currency"],
        "amount_sen": identity["amount_sen"],
        "posting_date": posting_date,
        "clearing_account": clearing,
        "liability_account": liability,
        "debit_account": liability if is_refund else clearing,
        "credit_account": clearing if is_refund else liability,
        "evidence_reference": evidence["reference"],
        "evidence_file": evidence["file"],
        "evidence_sha256": evidence["sha256"],
    }


def accounts(claim: Any, *, identity: Mapping[str, Any]) -> tuple[str, str]:
    clearing = _text(_value(claim, "finance_clearing_account")) or _text(
        frappe.db.get_value(
            "Company", identity["company"], COMPANY_CLEARING_ACCOUNT_FIELD
        )
    )
    liability = _text(_value(claim, "finance_liability_account")) or _text(
        frappe.db.get_value(
            "Company", identity["company"], COMPANY_LIABILITY_ACCOUNT_FIELD
        )
    )
    if not clearing or not liability or clearing == liability:
        frappe.throw(
            "Company duplicate QR clearing and customer liability accounts must be configured",
            frappe.ValidationError,
        )
    _validate_account(
        clearing,
        company=identity["company"],
        currency=identity["currency"],
        expected_root_type="Asset",
        role="secondary static QR bank/clearing",
        forbidden_account_types={"Receivable"},
    )
    _validate_account(
        liability,
        company=identity["company"],
        currency=identity["currency"],
        expected_root_type="Liability",
        role="secondary static QR customer liability",
        forbidden_account_types={"Payable"},
    )
    return clearing, liability


def liability_snapshot(
    *,
    request: Mapping[str, Any],
    evidence: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "finance_resolution_decision": "independent_second_credit",
        "finance_resolution_key": context["journal_key"],
        "finance_resolution_idempotency_key": request["idempotency_key"],
        "finance_credit_reference": evidence["credit_reference"],
        "finance_credit_date": evidence["credit_date"],
        "finance_credit_evidence_reference": evidence["reference"],
        "finance_credit_evidence_file": evidence["file"],
        "finance_credit_evidence_sha256": evidence["sha256"],
        "finance_credit_evidence_byte_length": evidence["byte_length"],
        "finance_clearing_account": context["clearing_account"],
        "finance_liability_account": context["liability_account"],
        "finance_resolution_note": request["note"],
    }


def refund_snapshot(
    *,
    request: Mapping[str, Any],
    evidence: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "finance_refund_key": context["journal_key"],
        "finance_refund_idempotency_key": request["idempotency_key"],
        "finance_refund_reference": evidence["refund_reference"],
        "finance_refund_date": evidence["refund_date"],
        "finance_refund_evidence_reference": evidence["reference"],
        "finance_refund_evidence_file": evidence["file"],
        "finance_refund_evidence_sha256": evidence["sha256"],
        "finance_refund_evidence_byte_length": evidence["byte_length"],
        "finance_refund_note": request["note"],
    }


def recorded_credit_evidence(claim: Any) -> dict[str, Any]:
    return {
        "reference": _text(_value(claim, "finance_credit_evidence_reference")),
        "file": _text(_value(claim, "finance_credit_evidence_file")),
        "sha256": _text(_value(claim, "finance_credit_evidence_sha256")),
        "byte_length": cint(_value(claim, "finance_credit_evidence_byte_length")),
        "credit_reference": _text(_value(claim, "finance_credit_reference")),
        "credit_date": _text(_value(claim, "finance_credit_date")),
        "reviewed_through_date": _text(
            _value(claim, "finance_reviewed_through_date")
        ),
    }


def recorded_refund_evidence(claim: Any) -> dict[str, Any]:
    return {
        "reference": _text(_value(claim, "finance_refund_evidence_reference")),
        "file": _text(_value(claim, "finance_refund_evidence_file")),
        "sha256": _text(_value(claim, "finance_refund_evidence_sha256")),
        "byte_length": cint(_value(claim, "finance_refund_evidence_byte_length")),
        "refund_reference": _text(_value(claim, "finance_refund_reference")),
        "refund_date": _text(_value(claim, "finance_refund_date")),
    }


def validate_evidence_file(
    evidence: Mapping[str, Any],
    *,
    claim_name: str,
) -> dict[str, Any]:
    if not evidence["reference"] or not evidence["file"] or not evidence["sha256"]:
        frappe.throw("Finance evidence snapshot is incomplete", frappe.ValidationError)
    result = _validate_private_provider_evidence_file(
        evidence["file"],
        expected_sha256=evidence["sha256"],
        source_name=claim_name,
        source_doctype=SOURCE_DOCTYPE,
    )
    if cint(result["provider_evidence_byte_length"]) != cint(evidence["byte_length"]):
        frappe.throw("Finance evidence byte length does not match retained File", frappe.ValidationError)
    return {
        "evidence_file": result["provider_evidence_file"],
        "evidence_sha256": result["provider_evidence_sha256"],
        "evidence_byte_length": result["provider_evidence_byte_length"],
    }


def no_credit_key(identity: Mapping[str, Any], evidence: Mapping[str, Any]) -> str:
    raw = "|".join(
        [
            identity["claim_name"],
            identity["winning_transaction"],
            evidence["reviewed_through_date"],
            evidence["reference"],
            evidence["file"],
            evidence["sha256"],
            _text(evidence["byte_length"]),
            _text(identity["amount_sen"]),
            identity["currency"],
        ]
    )
    return NO_CREDIT_KEY_NAMESPACE + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validated_date(value: Any, *, fieldname: str, earliest: str) -> str:
    text = _text(value)
    try:
        resolved = getdate(text)
        earliest_date = getdate(earliest)
        today = getdate(str(now_datetime())[:10])
    except Exception as error:
        frappe.throw(f"{fieldname} must be a valid ISO date", frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if text != resolved.isoformat() or resolved < earliest_date or resolved > today:
        frappe.throw(
            f"{fieldname} must use YYYY-MM-DD between the sale date and today",
            frappe.ValidationError,
        )
    return resolved.isoformat()


def set_exact_fields(claim: Any, expected: Mapping[str, Any], label: str) -> None:
    updates: dict[str, Any] = {}
    for fieldname, expected_value in expected.items():
        current = _text(_value(claim, fieldname))
        if current and current != _text(expected_value):
            frappe.throw(f"{label} {fieldname} conflicts with its snapshot", frappe.ValidationError)
        if not current:
            updates[fieldname] = expected_value
    if updates:
        _set_source_values(claim, updates)


def assert_exact_fields(claim: Any, expected: Mapping[str, Any], label: str) -> None:
    for fieldname, expected_value in expected.items():
        if _text(_value(claim, fieldname)) != _text(expected_value):
            frappe.throw(f"{label} {fieldname} does not match", frappe.ValidationError)


def require_schema_fields() -> None:
    source_meta = frappe.get_meta(SOURCE_DOCTYPE)
    missing_source = [field for field in REQUIRED_SOURCE_FIELDS if not source_meta.has_field(field)]
    journal_meta = frappe.get_meta(JOURNAL_ENTRY_DOCTYPE)
    required_journal = (
        "custom_kopos_qr_duplicate_key",
        "custom_kopos_qr_duplicate_stage",
        "custom_kopos_qr_provider_transaction",
        "custom_kopos_qr_winning_transaction",
        "custom_kopos_qr_winning_channel",
        "custom_kopos_qr_provider_evidence_reference",
        "custom_kopos_qr_provider_evidence_file",
        "custom_kopos_qr_provider_evidence_sha256",
        "custom_kopos_qr_source_doctype",
        "custom_kopos_qr_source_name",
        "custom_kopos_qr_fb_order",
        "custom_kopos_qr_sales_invoice",
        "custom_kopos_qr_amount_sen",
        "custom_kopos_qr_currency",
    )
    missing_journal = [field for field in required_journal if not journal_meta.has_field(field)]
    if missing_source or missing_journal:
        frappe.throw(
            "Secondary static QR finance fields are missing; run bench migrate: "
            + ", ".join([*missing_source, *missing_journal]),
            frappe.ValidationError,
        )


def required(payload: Mapping[str, Any], fieldname: str) -> str:
    value = cstr(payload.get(fieldname)).strip()
    if not value:
        frappe.throw(f"{fieldname} is required", frappe.ValidationError)
    return value


def lock_exact_row(doctype: str, name: str, label: str) -> None:
    rows = frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` WHERE name = %s LIMIT 1 FOR UPDATE",
        (name,),
    )
    if len(rows or []) != 1:
        frappe.throw(f"Secondary static QR {label} was not found", frappe.ValidationError)
