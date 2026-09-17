# pyright: reportMissingImports=false

from __future__ import annotations

from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import cint

from kopos_connector.kopos.services.accounting._qr_journal_entry import (
    JournalSide,
    create_or_recover_journal,
    prove_journal,
)
from kopos_connector.kopos.services.accounting._duplicate_qr_contract import (
    JOURNAL_ENTRY_DOCTYPE,
    MAYBANK_TRANSACTION_DOCTYPE,
    _money_to_sen,
    _sen_to_amount,
    _strict_positive_sen,
    _text,
    _value,
)


def _snapshot_recognition_context(
    transaction: Any,
    context: dict[str, Any],
) -> None:
    expected = {
        "duplicate_accounting_key": context["recognition_key"],
        "duplicate_clearing_account": context["clearing_account"],
        "duplicate_liability_account": context["liability_account"],
    }
    _snapshot_exact_values(transaction, expected, "Duplicate QR liability")


def _snapshot_refund_context(
    transaction: Any,
    context: dict[str, Any],
    refund: dict[str, Any],
) -> None:
    expected = {
        "duplicate_refund_key": context["journal_key"],
        "duplicate_refund_reference": refund["provider_refund_reference"],
        "duplicate_refund_evidence_reference": refund[
            "provider_evidence_reference"
        ],
        "duplicate_refund_evidence_file": refund["provider_evidence_file"],
        "duplicate_refund_evidence_sha256": refund[
            "provider_evidence_sha256"
        ],
        "duplicate_refund_amount_sen": refund["amount_sen"],
        "duplicate_refund_currency": refund["currency"],
        "duplicate_refund_date": refund["refund_date"],
        "duplicate_refund_note": refund["note"],
    }
    _snapshot_exact_values(transaction, expected, "Duplicate QR refund")


def _snapshot_exact_values(
    transaction: Any,
    expected: dict[str, Any],
    label: str,
) -> None:
    updates: dict[str, Any] = {}
    for fieldname, expected_value in expected.items():
        current = _value(transaction, fieldname)
        current_text = _text(current)
        expected_text = _text(expected_value)
        if current_text and current_text != expected_text:
            frappe.throw(
                f"{label} {fieldname} does not match the immutable incident snapshot",
                frappe.ValidationError,
            )
        if not current_text:
            updates[fieldname] = expected_value
    if updates:
        _set_source_values(transaction, updates)


def _assert_exact_refund_snapshot(
    transaction: Any,
    refund: dict[str, Any],
) -> None:
    expected = {
        "duplicate_refund_reference": refund["provider_refund_reference"],
        "duplicate_refund_evidence_reference": refund[
            "provider_evidence_reference"
        ],
        "duplicate_refund_evidence_file": refund["provider_evidence_file"],
        "duplicate_refund_evidence_sha256": refund[
            "provider_evidence_sha256"
        ],
        "duplicate_refund_amount_sen": refund["amount_sen"],
        "duplicate_refund_currency": refund["currency"],
        "duplicate_refund_date": refund["refund_date"],
        "duplicate_refund_note": refund["note"],
    }
    for fieldname, expected_value in expected.items():
        if _text(_value(transaction, fieldname)) != _text(expected_value):
            frappe.throw(
                "Refunded duplicate Automatic QR incident does not match this exact retry",
                frappe.ValidationError,
            )

def _find_existing_journal(
    transaction: Any,
    journal_key: str,
    *,
    link_field: str,
) -> str | None:
    linked = _text(_value(transaction, link_field))
    by_key = _text(
        frappe.db.get_value(
            JOURNAL_ENTRY_DOCTYPE,
            {"custom_kopos_qr_duplicate_key": journal_key},
            "name",
        )
    )
    if linked and by_key and linked != by_key:
        frappe.throw(
            "Duplicate QR accounting source and Journal Entry key disagree",
            frappe.ValidationError,
        )
    return linked or by_key or None


def _journal_fields(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "custom_kopos_qr_duplicate_key": context["journal_key"],
        "custom_kopos_qr_duplicate_stage": context["stage"],
        "custom_kopos_qr_provider_transaction": context.get(
            "provider_transaction", context["source_name"]
        )
        or None,
        "custom_kopos_qr_winning_transaction": context["winning_transaction"] or None,
        "custom_kopos_qr_winning_channel": context["winning_channel"],
        "custom_kopos_qr_winning_static_reconciliation": context[
            "winning_static_reconciliation"
        ]
        or None,
        "custom_kopos_qr_provider_evidence_reference": context["evidence_reference"],
        "custom_kopos_qr_provider_evidence_file": context["evidence_file"] or None,
        "custom_kopos_qr_provider_evidence_sha256": context.get("evidence_sha256"),
        "custom_kopos_qr_source_doctype": context.get(
            "source_doctype", MAYBANK_TRANSACTION_DOCTYPE
        ),
        "custom_kopos_qr_source_name": context["source_name"],
        "custom_kopos_qr_fb_order": context["order_name"],
        "custom_kopos_qr_sales_invoice": context["invoice_name"],
        "custom_kopos_qr_amount_sen": context["amount_sen"],
        "custom_kopos_qr_currency": context["currency"],
    }


def _create_or_recover_journal(context: dict[str, Any]) -> str:
    return create_or_recover_journal(
        company=context["company"],
        posting_date=context["posting_date"],
        user_remark=(
            "KoPOS duplicate Automatic QR "
            f"{context['stage']} for {context['source_name']}"
        ),
        fields=_journal_fields(context),
        debit=JournalSide(context["debit_account"]),
        credit=JournalSide(context["credit_account"]),
        amount_sen=context["amount_sen"],
        savepoint="kopos_duplicate_qr_journal",
        recover=lambda: _text(
            frappe.db.get_value(
                JOURNAL_ENTRY_DOCTYPE,
                {"custom_kopos_qr_duplicate_key": context["journal_key"]},
                "name",
            )
        ),
    )


def _validate_journal(
    context: dict[str, Any],
    journal_name: str,
) -> dict[str, Any]:
    fields = _journal_fields(context)
    fields.pop("custom_kopos_qr_amount_sen")
    # A legacy dynamic winner may predate the channel stamp, so it is proven apart.
    expected_winning_channel = fields.pop("custom_kopos_qr_winning_channel")
    prove_journal(
        journal_name,
        label="duplicate QR accounting",
        expected_fields={**fields, "company": context["company"], "posting_date": context["posting_date"]},
        debit=JournalSide(context["debit_account"]),
        credit=JournalSide(context["credit_account"]),
        amount_sen=context["amount_sen"],
        exact_gl_rows=2,
    )
    journal = frappe.get_doc(JOURNAL_ENTRY_DOCTYPE, journal_name)
    actual_winning_channel = _text(_value(journal, "custom_kopos_qr_winning_channel"))
    allowed_winning_channels = (
        {"", "maybank_qr"}
        if context.get("legacy_dynamic_winner_metadata")
        and _text(expected_winning_channel) == "maybank_qr"
        else {_text(expected_winning_channel)}
    )
    if actual_winning_channel not in allowed_winning_channels:
        frappe.throw(
            f"Journal Entry {journal_name} custom_kopos_qr_winning_channel does not match duplicate QR accounting",
            frappe.ValidationError,
        )
    expected_sen = context["amount_sen"]

    return {
        "journal_entry": journal_name,
        "stage": context["stage"],
        "journal_key": context["journal_key"],
        "transaction": context["source_name"],
        "fb_order": context["order_name"],
        "sales_invoice": context["invoice_name"],
        "amount_sen": expected_sen,
        "currency": context["currency"],
        "debit_account": context["debit_account"],
        "credit_account": context["credit_account"],
    }

def _set_source_values(transaction: Any, values: dict[str, Any]) -> None:
    source_name = _text(_value(transaction, "name"))
    if not source_name:
        frappe.throw(
            "Duplicate Automatic QR source name is required",
            frappe.ValidationError,
        )
    source_doctype = _text(_value(transaction, "doctype")) or (
        MAYBANK_TRANSACTION_DOCTYPE
    )
    frappe.db.set_value(
        source_doctype,
        source_name,
        values,
        update_modified=False,
    )
    if isinstance(transaction, dict):
        transaction.update(values)
        return
    for fieldname, value in values.items():
        setattr(transaction, fieldname, value)
