# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
from typing import Any

import frappe
from frappe.utils import cint

from kopos_connector.kopos.services.accounting._duplicate_qr_contract import (
    JOURNAL_ENTRY_DOCTYPE,
    LIABILITY_RECOGNITION_STAGE,
    MAX_PROVIDER_EVIDENCE_BYTES,
    MAYBANK_TRANSACTION_DOCTYPE,
    REFUNDED_STATUS,
    REFUND_STAGE,
    _build_accounting_context,
    _require_schema_fields,
    _strict_positive_sen,
    _text,
    _validate_duplicate_identity,
    _validated_refund_date,
    _value,
)
from kopos_connector.kopos.services.accounting._duplicate_qr_journal import (
    _find_existing_journal,
    _validate_journal,
)


def _assert_recorded_refund_accounting(
    transaction: Any,
    *,
    order_doc: Any,
    identity: dict[str, Any],
) -> dict[str, Any]:
    refund = {
        "provider_refund_reference": _text(
            _value(transaction, "duplicate_refund_reference")
        ),
        "provider_evidence_reference": _text(
            _value(transaction, "duplicate_refund_evidence_reference")
        ),
        "provider_evidence_file": _text(
            _value(transaction, "duplicate_refund_evidence_file")
        ),
        "provider_evidence_sha256": _text(
            _value(transaction, "duplicate_refund_evidence_sha256")
        ),
        "amount_sen": _strict_positive_sen(
            _value(transaction, "duplicate_refund_amount_sen"),
            "Recorded duplicate QR refund amount_sen",
        ),
        "currency": _text(
            _value(transaction, "duplicate_refund_currency")
        ).upper(),
        "refund_date": _text(_value(transaction, "duplicate_refund_date")),
        "note": _text(_value(transaction, "duplicate_refund_note")),
    }
    if (
        not refund["provider_refund_reference"]
        or refund["provider_refund_reference"] == identity["transaction_refno"]
        or not refund["provider_evidence_reference"]
        or not refund["provider_evidence_file"]
        or len(refund["provider_evidence_sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in refund["provider_evidence_sha256"]
        )
        or not 20 <= len(refund["note"]) <= 1000
        or refund["amount_sen"] != identity["amount_sen"]
        or refund["currency"] != identity["currency"]
        or not refund["refund_date"]
    ):
        frappe.throw(
            "Refunded duplicate Automatic QR incident has incomplete or mismatched evidence",
            frappe.ValidationError,
        )
    refund["refund_date"] = _validated_refund_date(
        refund["refund_date"],
        identity=identity,
    )
    context = _build_accounting_context(
        transaction,
        order_doc=order_doc,
        identity=identity,
        stage=REFUND_STAGE,
        refund=refund,
    )
    journal_link = _text(
        _value(transaction, "duplicate_refund_journal_entry")
    )
    if (
        not journal_link
        or _text(_value(transaction, "duplicate_refund_key"))
        != context["journal_key"]
    ):
        frappe.throw(
            "Refunded duplicate Automatic QR incident key does not match its evidence",
            frappe.ValidationError,
        )
    journal_name = _find_existing_journal(
        transaction,
        context["journal_key"],
        link_field="duplicate_refund_journal_entry",
    )
    if not journal_name:
        frappe.throw(
            "Refunded duplicate Automatic QR incident has no Journal Entry",
            frappe.ValidationError,
        )
    journal_evidence = _validate_journal(context, journal_name)
    file_evidence = _validate_private_provider_evidence_file(
        refund["provider_evidence_file"],
        expected_sha256=refund["provider_evidence_sha256"],
        source_name=identity["source_name"],
    )
    return {**journal_evidence, **file_evidence}


def _assert_recorded_liability_accounting(
    transaction: Any,
    *,
    order_doc: Any,
    identity: dict[str, Any],
) -> dict[str, Any]:
    snapshot = {
        "key": _text(_value(transaction, "duplicate_accounting_key")),
        "clearing_account": _text(
            _value(transaction, "duplicate_clearing_account")
        ),
        "liability_account": _text(
            _value(transaction, "duplicate_liability_account")
        ),
        "journal_entry": _text(
            _value(transaction, "duplicate_liability_journal_entry")
        ),
    }
    if (
        not all(snapshot.values())
        or snapshot["clearing_account"] == snapshot["liability_account"]
    ):
        frappe.throw(
            "Refunded duplicate Automatic QR incident has incomplete liability evidence",
            frappe.ValidationError,
        )
    context = _build_accounting_context(
        transaction,
        order_doc=order_doc,
        identity=identity,
        stage=LIABILITY_RECOGNITION_STAGE,
    )
    if snapshot["key"] != context["journal_key"]:
        frappe.throw(
            "Refunded duplicate Automatic QR liability key does not match its evidence",
            frappe.ValidationError,
        )
    journal_name = _find_existing_journal(
        transaction,
        context["journal_key"],
        link_field="duplicate_liability_journal_entry",
    )
    if not journal_name:
        frappe.throw(
            "Refunded duplicate Automatic QR incident has no liability Journal Entry",
            frappe.ValidationError,
        )
    return _validate_journal(context, journal_name)


def assert_duplicate_refund_terminal_evidence(
    transaction: Any,
    *,
    order_doc: Any,
    identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-prove a refunded duplicate incident without creating or mutating records."""

    _require_schema_fields()
    resolved_identity = identity or _validate_duplicate_identity(
        transaction,
        order_doc=order_doc,
        winning_transaction_name=_text(
            _value(transaction, "duplicate_winning_transaction")
        ),
        require_submitted_sale=True,
    )
    if (
        _text(_value(transaction, "duplicate_payment_status"))
        != REFUNDED_STATUS
        or not _text(_value(transaction, "duplicate_refunded_by"))
        or not _text(_value(transaction, "duplicate_refunded_at"))
    ):
        frappe.throw(
            "Duplicate Automatic QR refund is not in an exactly audited terminal state",
            frappe.ValidationError,
        )
    liability = _assert_recorded_liability_accounting(
        transaction,
        order_doc=order_doc,
        identity=resolved_identity,
    )
    refund = _assert_recorded_refund_accounting(
        transaction,
        order_doc=order_doc,
        identity=resolved_identity,
    )
    return {
        "transaction": resolved_identity["source_name"],
        "fb_order": resolved_identity["order_name"],
        "sales_invoice": resolved_identity["invoice_name"],
        "winning_transaction": resolved_identity["winning_transaction"],
        "winning_channel": resolved_identity["winning_channel"],
        "winning_static_reconciliation": resolved_identity[
            "winning_static_reconciliation"
        ]
        or None,
        "amount_sen": resolved_identity["amount_sen"],
        "currency": resolved_identity["currency"],
        "liability_journal_entry": liability["journal_entry"],
        "refund_journal_entry": refund["journal_entry"],
        "provider_evidence_file": refund["provider_evidence_file"],
        "provider_evidence_sha256": refund["provider_evidence_sha256"],
        "provider_evidence_byte_length": refund["provider_evidence_byte_length"],
    }


def lock_and_assert_duplicate_refund_terminal_evidence(
    source_name: str,
    *,
    expected_order_name: str,
    expected_device_id: str,
) -> dict[str, Any]:
    """Lock Order then provider/accounting evidence and perform exact reproof."""

    source = _text(source_name)
    order_name = _text(expected_order_name)
    device_id = _text(expected_device_id)
    if not source or not order_name or not device_id:
        frappe.throw(
            "Duplicate Automatic QR terminal evidence scope is incomplete",
            frappe.ValidationError,
        )
    _lock_exact_row("FB Order", order_name, "winning FB Order")
    _lock_exact_row(
        MAYBANK_TRANSACTION_DOCTYPE,
        source,
        "duplicate provider transaction",
    )
    order_doc = frappe.get_doc("FB Order", order_name)
    transaction = frappe.get_doc(MAYBANK_TRANSACTION_DOCTYPE, source)
    if (
        _text(_value(transaction, "fb_order")) != order_name
        or _text(_value(transaction, "device_id")) != device_id
        or _text(_value(order_doc, "device_id")) != device_id
    ):
        frappe.throw(
            "Duplicate Automatic QR terminal evidence changed while locks were acquired",
            frappe.ValidationError,
        )

    static_reconciliation = _text(
        _value(transaction, "duplicate_winning_static_reconciliation")
    )
    if static_reconciliation:
        _lock_exact_row(
            "Manual QR Reconciliation",
            static_reconciliation,
            "winning static QR reconciliation",
        )
    for fieldname, label in (
        ("duplicate_liability_journal_entry", "liability Journal Entry"),
        ("duplicate_refund_journal_entry", "refund Journal Entry"),
    ):
        linked_name = _text(_value(transaction, fieldname))
        if linked_name:
            _lock_exact_row(JOURNAL_ENTRY_DOCTYPE, linked_name, label)
    evidence_file = _text(
        _value(transaction, "duplicate_refund_evidence_file")
    )
    if evidence_file:
        _lock_exact_row("File", evidence_file, "provider evidence File")
    return assert_duplicate_refund_terminal_evidence(
        transaction,
        order_doc=order_doc,
    )


def _lock_exact_row(doctype: str, name: str, label: str) -> None:
    rows = frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` WHERE name = %s LIMIT 1 FOR UPDATE",
        (name,),
    )
    if len(rows or []) != 1:
        frappe.throw(
            f"Duplicate Automatic QR {label} was not found",
            frappe.ValidationError,
        )

def _validate_private_provider_evidence_file(
    file_name: str,
    *,
    expected_sha256: str,
    source_name: str,
    source_doctype: str = MAYBANK_TRANSACTION_DOCTYPE,
) -> dict[str, Any]:
    try:
        evidence_file = frappe.get_doc("File", file_name)
    except Exception as error:
        frappe.throw(
            "Provider refund evidence File was not found",
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise") from error
    if (
        _text(_value(evidence_file, "name")) != file_name
        or not cint(_value(evidence_file, "is_private"))
        or _text(_value(evidence_file, "attached_to_doctype"))
        != source_doctype
        or _text(_value(evidence_file, "attached_to_name")) != source_name
    ):
        message = (
            "Provider refund evidence must be a private File attached to the "
            "duplicate transaction"
            if source_doctype == MAYBANK_TRANSACTION_DOCTYPE
            else "Payment evidence must be a private File attached to the exact "
            "resolution record"
        )
        frappe.throw(
            message,
            frappe.ValidationError,
        )
    declared_size = cint(_value(evidence_file, "file_size"))
    if declared_size <= 0 or declared_size > MAX_PROVIDER_EVIDENCE_BYTES:
        frappe.throw(
            "Provider refund evidence file size is invalid",
            frappe.ValidationError,
        )
    get_content = getattr(evidence_file, "get_content", None)
    if not callable(get_content):
        frappe.throw(
            "Provider refund evidence file content is unavailable",
            frappe.ValidationError,
        )
    content = get_content()
    if isinstance(content, str):
        content_bytes = content.encode("utf-8")
    elif isinstance(content, bytes):
        content_bytes = content
    else:
        frappe.throw(
            "Provider refund evidence file content is invalid",
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise")
    if len(content_bytes) != declared_size:
        frappe.throw(
            "Provider refund evidence file size does not match retained content",
            frappe.ValidationError,
        )
    observed_sha256 = hashlib.sha256(content_bytes).hexdigest()
    if observed_sha256 != expected_sha256:
        frappe.throw(
            "Provider refund evidence SHA-256 does not match retained private File bytes",
            frappe.ValidationError,
        )
    return {
        "provider_evidence_file": file_name,
        "provider_evidence_sha256": observed_sha256,
        "provider_evidence_byte_length": declared_size,
    }
