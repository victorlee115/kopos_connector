# pyright: reportMissingImports=false

from __future__ import annotations

from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import cint

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


def _create_or_recover_journal(context: dict[str, Any]) -> str:
    savepoint = "kopos_duplicate_qr_journal"
    savepoint_fn = getattr(frappe.db, "savepoint", None)
    savepoint_created = callable(savepoint_fn)
    if savepoint_created:
        savepoint_fn(savepoint)
    try:
        journal = frappe.new_doc(JOURNAL_ENTRY_DOCTYPE)
        journal.voucher_type = "Journal Entry"
        journal.company = context["company"]
        journal.posting_date = context["posting_date"]
        journal.user_remark = (
            "KoPOS duplicate Automatic QR "
            f"{context['stage']} for {context['source_name']}"
        )
        journal.custom_kopos_qr_duplicate_key = context["journal_key"]
        journal.custom_kopos_qr_duplicate_stage = context["stage"]
        journal.custom_kopos_qr_provider_transaction = context["source_name"]
        journal.custom_kopos_qr_winning_transaction = context[
            "winning_transaction"
        ]
        journal.custom_kopos_qr_provider_evidence_reference = context[
            "evidence_reference"
        ]
        journal.custom_kopos_qr_provider_evidence_file = (
            context["evidence_file"] or None
        )
        journal.custom_kopos_qr_provider_evidence_sha256 = context.get(
            "evidence_sha256"
        )
        journal.custom_kopos_qr_source_doctype = MAYBANK_TRANSACTION_DOCTYPE
        journal.custom_kopos_qr_source_name = context["source_name"]
        journal.custom_kopos_qr_fb_order = context["order_name"]
        journal.custom_kopos_qr_sales_invoice = context["invoice_name"]
        journal.custom_kopos_qr_amount_sen = context["amount_sen"]
        journal.custom_kopos_qr_currency = context["currency"]
        journal.append(
            "accounts",
            {
                "account": context["debit_account"],
                "debit_in_account_currency": _sen_to_amount(
                    context["amount_sen"]
                ),
                "credit_in_account_currency": Decimal("0"),
                "exchange_rate": 1,
            },
        )
        journal.append(
            "accounts",
            {
                "account": context["credit_account"],
                "debit_in_account_currency": Decimal("0"),
                "credit_in_account_currency": _sen_to_amount(
                    context["amount_sen"]
                ),
                "exchange_rate": 1,
            },
        )
        journal.insert(ignore_permissions=True)
        journal.submit()
        if cint(_value(journal, "docstatus")) != 1:
            frappe.throw(
                f"Duplicate QR Journal Entry {journal.name} was not submitted",
                frappe.ValidationError,
            )
        return _text(journal.name)
    except Exception as error:
        duplicate_error = getattr(frappe, "DuplicateEntryError", None)
        if not duplicate_error or not isinstance(error, duplicate_error):
            raise
        if not savepoint_created:
            raise
        frappe.db.rollback(save_point=savepoint)
        recovered = _text(
            frappe.db.get_value(
                JOURNAL_ENTRY_DOCTYPE,
                {"custom_kopos_qr_duplicate_key": context["journal_key"]},
                "name",
            )
        )
        if not recovered:
            raise
        return recovered


def _validate_journal(
    context: dict[str, Any],
    journal_name: str,
) -> dict[str, Any]:
    journal = frappe.get_doc(JOURNAL_ENTRY_DOCTYPE, journal_name)
    if cint(_value(journal, "docstatus")) != 1:
        frappe.throw(
            f"Duplicate QR Journal Entry {journal_name} is not submitted",
            frappe.ValidationError,
        )
    expected_fields = {
        "custom_kopos_qr_duplicate_key": context["journal_key"],
        "custom_kopos_qr_duplicate_stage": context["stage"],
        "custom_kopos_qr_provider_transaction": context["source_name"],
        "custom_kopos_qr_winning_transaction": context[
            "winning_transaction"
        ],
        "custom_kopos_qr_provider_evidence_reference": context[
            "evidence_reference"
        ],
        "custom_kopos_qr_provider_evidence_file": context["evidence_file"],
        "custom_kopos_qr_provider_evidence_sha256": context.get(
            "evidence_sha256"
        ),
        "custom_kopos_qr_source_doctype": MAYBANK_TRANSACTION_DOCTYPE,
        "custom_kopos_qr_source_name": context["source_name"],
        "custom_kopos_qr_fb_order": context["order_name"],
        "custom_kopos_qr_sales_invoice": context["invoice_name"],
        "custom_kopos_qr_currency": context["currency"],
        "company": context["company"],
        "posting_date": context["posting_date"],
    }
    for fieldname, expected in expected_fields.items():
        actual = _text(_value(journal, fieldname))
        if fieldname == "custom_kopos_qr_currency":
            actual = actual.upper()
        if actual != _text(expected):
            frappe.throw(
                f"Journal Entry {journal_name} {fieldname} does not match duplicate QR accounting",
                frappe.ValidationError,
            )
    if _strict_positive_sen(
        _value(journal, "custom_kopos_qr_amount_sen"),
        "Journal Entry duplicate QR amount_sen",
    ) != context["amount_sen"]:
        frappe.throw(
            f"Journal Entry {journal_name} amount does not match duplicate QR accounting",
            frappe.ValidationError,
        )

    gl_rows = frappe.get_all(
        "GL Entry",
        filters={
            "voucher_type": JOURNAL_ENTRY_DOCTYPE,
            "voucher_no": journal_name,
            "is_cancelled": 0,
        },
        fields=[
            "account",
            "debit",
            "credit",
            "debit_in_account_currency",
            "credit_in_account_currency",
        ],
        order_by="account asc, name asc",
        limit_page_length=3,
    )
    if len(gl_rows) != 2:
        frappe.throw(
            f"Journal Entry {journal_name} must have exactly two active GL rows",
            frappe.ValidationError,
        )
    observed = {
        context["debit_account"]: {"debit_sen": 0, "credit_sen": 0},
        context["credit_account"]: {"debit_sen": 0, "credit_sen": 0},
    }
    for row in gl_rows:
        account = _text(_value(row, "account"))
        debit_sen = _money_to_sen(_value(row, "debit"), "GL debit")
        credit_sen = _money_to_sen(_value(row, "credit"), "GL credit")
        account_currency_debit_sen = _money_to_sen(
            _value(row, "debit_in_account_currency"),
            "GL account-currency debit",
        )
        account_currency_credit_sen = _money_to_sen(
            _value(row, "credit_in_account_currency"),
            "GL account-currency credit",
        )
        if (
            debit_sen != account_currency_debit_sen
            or credit_sen != account_currency_credit_sen
        ):
            frappe.throw(
                f"Journal Entry {journal_name} base and account-currency GL amounts differ",
                frappe.ValidationError,
            )
        if account not in observed:
            frappe.throw(
                f"Journal Entry {journal_name} contains unexpected GL account {account}",
                frappe.ValidationError,
            )
        observed[account]["debit_sen"] += debit_sen
        observed[account]["credit_sen"] += credit_sen

    expected_sen = context["amount_sen"]
    if observed[context["debit_account"]] != {
        "debit_sen": expected_sen,
        "credit_sen": 0,
    }:
        frappe.throw(
            f"Journal Entry {journal_name} does not exactly debit the duplicate QR target account",
            frappe.ValidationError,
        )
    if observed[context["credit_account"]] != {
        "debit_sen": 0,
        "credit_sen": expected_sen,
    }:
        frappe.throw(
            f"Journal Entry {journal_name} does not exactly credit the duplicate QR source account",
            frappe.ValidationError,
        )
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
    frappe.db.set_value(
        MAYBANK_TRANSACTION_DOCTYPE,
        source_name,
        values,
        update_modified=False,
    )
    if isinstance(transaction, dict):
        transaction.update(values)
        return
    for fieldname, value in values.items():
        setattr(transaction, fieldname, value)
