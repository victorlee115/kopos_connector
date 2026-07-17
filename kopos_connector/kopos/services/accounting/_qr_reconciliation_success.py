# pyright: reportMissingImports=false

"""Successful QR reconciliation disposition and Journal Entry proof."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import cstr

from ._qr_reconciliation_context import (
    JOURNAL_ENTRY_DOCTYPE,
    RECONCILIATION_FAILED_STATUS,
    _build_context,
    _find_existing_journal,
    _gl_amounts_sen,
    _lock_and_reload_source,
    _require_failure_source_fields,
    _require_journal_fields,
    _row_value,
    _sen_to_amount,
    _strict_positive_sen,
    _validate_invoice_suspense_receipt,
    _value,
)


def ensure_qr_suspense_reclassification(
    reconciliation_source: Any,
) -> dict[str, Any]:
    """Create or recover one submitted QR suspense-to-bank Journal Entry.

    The source reconciliation row is the lock and idempotency aggregate. This
    function deliberately does not update that row, its FB Order Payment, or
    its Sales Invoice. The caller may mark reconciliation complete only after
    this function returns submitted GL evidence.
    """
    source = _lock_and_reload_source(reconciliation_source)
    context = _build_context(source)
    _require_journal_fields()
    _validate_invoice_suspense_receipt(context)
    _apply_historical_failure_recovery(context)

    journal_name = _find_existing_journal(source, context["reconciliation_key"])
    if not journal_name:
        journal_name = _create_or_recover_journal(context)
    return _validate_journal(context, journal_name)


def assert_qr_suspense_reclassification(
    reconciliation_source: Any,
) -> dict[str, Any]:
    """Re-prove the exact submitted Journal Entry without creating one."""
    source = _lock_and_reload_source(reconciliation_source)
    context = _build_context(source)
    _require_journal_fields()
    _validate_invoice_suspense_receipt(context)
    _apply_historical_failure_recovery(context)
    journal_name = _find_existing_journal(source, context["reconciliation_key"])
    if not journal_name:
        frappe.throw(
            f"QR reconciliation {context['reconciliation_key']} has no Journal Entry",
            frappe.ValidationError,
        )
    return _validate_journal(context, journal_name)


def _apply_historical_failure_recovery(context: dict[str, Any]) -> None:
    source = context["source"]
    snapshot_fields = (
        "failure_accounting_key",
        "failure_variance_account",
        "failure_cost_center",
        "failure_accounting_reason",
        "failure_journal_entry",
    )
    snapshots = {
        fieldname: cstr(_value(source, fieldname)).strip()
        for fieldname in snapshot_fields
    }
    populated = {fieldname for fieldname, value in snapshots.items() if value}
    if not populated:
        return
    if len(populated) != len(snapshot_fields):
        frappe.throw(
            f"{context['source_doctype']} {context['source_name']} has incomplete "
            "historical QR failure accounting evidence",
            frappe.ValidationError,
        )

    failure_context = _build_context(
        source,
        disposition=RECONCILIATION_FAILED_STATUS,
        failure_reason=snapshots["failure_accounting_reason"],
        allow_provider_paid_history=True,
    )
    _require_failure_source_fields(context["source_doctype"])
    _require_journal_fields(
        require_failure_fields=True,
        require_recovery_fields=True,
    )
    _validate_invoice_suspense_receipt(failure_context)
    failure_journal = _find_existing_journal(
        source,
        failure_context["reconciliation_key"],
        link_field="failure_journal_entry",
        key_field="custom_kopos_qr_failure_key",
    )
    if not failure_journal or failure_journal != snapshots["failure_journal_entry"]:
        frappe.throw(
            f"{context['source_doctype']} {context['source_name']} has no exact "
            "historical QR failure Journal Entry",
            frappe.ValidationError,
        )
    _validate_journal(failure_context, failure_journal)

    context["credit_account"] = failure_context["target_account"]
    context["credit_cost_center"] = failure_context["failure_cost_center"]
    context["prior_failure_journal_entry"] = failure_journal
    if context["target_account"] == context["credit_account"]:
        frappe.throw(
            "Late QR settlement bank account must differ from the failure variance account",
            frappe.ValidationError,
        )


def _create_or_recover_journal(context: dict[str, Any]) -> str:
    savepoint = "kopos_qr_reclassification"
    savepoint_created = False
    try:
        frappe.db.savepoint(savepoint)
        savepoint_created = True
    except Exception:
        savepoint_created = False

    try:
        journal = frappe.new_doc(JOURNAL_ENTRY_DOCTYPE)
        journal.voucher_type = JOURNAL_ENTRY_DOCTYPE
        journal.company = context["company"]
        journal.posting_date = context["posting_date"]
        journal.user_remark = (
            f"KoPOS QR suspense {context['disposition']} for {context['source_doctype']} "
            f"{context['source_name']}; Sales Invoice {context['invoice_name']}"
        )
        if context["disposition"] == RECONCILIATION_FAILED_STATUS:
            journal.custom_kopos_qr_failure_key = context["reconciliation_key"]
        else:
            journal.custom_kopos_qr_reconciliation_key = context["reconciliation_key"]
        journal.custom_kopos_qr_source_doctype = context["source_doctype"]
        journal.custom_kopos_qr_source_name = context["source_name"]
        journal.custom_kopos_qr_fb_order = context["order_name"]
        journal.custom_kopos_qr_sales_invoice = context["invoice_name"]
        journal.custom_kopos_qr_amount_sen = context["amount_sen"]
        journal.custom_kopos_qr_currency = context["currency"]
        if context["disposition"] == RECONCILIATION_FAILED_STATUS:
            journal.custom_kopos_qr_disposition = context["disposition"]
            journal.custom_kopos_qr_fb_order_payment = context["payment_row_name"]
            journal.custom_kopos_qr_target_account = context["target_account"]
            journal.custom_kopos_qr_cost_center = context["failure_cost_center"]
            journal.custom_kopos_qr_failure_reason = context["failure_reason"]
        elif context["prior_failure_journal_entry"]:
            journal.custom_kopos_qr_source_account = context["credit_account"]
            journal.custom_kopos_qr_prior_failure_journal = context[
                "prior_failure_journal_entry"
            ]
            journal.custom_kopos_qr_cost_center = context["credit_cost_center"]
        journal.append(
            "accounts",
            {
                "account": context["target_account"],
                "debit_in_account_currency": _sen_to_amount(context["amount_sen"]),
                "credit_in_account_currency": Decimal("0"),
                "exchange_rate": 1,
                **(
                    {"cost_center": context["failure_cost_center"]}
                    if context["disposition"] == RECONCILIATION_FAILED_STATUS
                    else {}
                ),
            },
        )
        journal.append(
            "accounts",
            {
                "account": context["credit_account"],
                "debit_in_account_currency": Decimal("0"),
                "credit_in_account_currency": _sen_to_amount(context["amount_sen"]),
                "exchange_rate": 1,
                **(
                    {"cost_center": context["credit_cost_center"]}
                    if context["credit_cost_center"]
                    else {}
                ),
            },
        )
        journal.insert(ignore_permissions=True)
        journal.submit()
        if int(_value(journal, "docstatus") or 0) != 1:
            frappe.throw(
                f"QR reclassification Journal Entry {journal.name} was not submitted",
                frappe.ValidationError,
            )
        return cstr(journal.name).strip()
    except Exception as error:
        duplicate_error = getattr(frappe, "DuplicateEntryError", None)
        if not duplicate_error or not isinstance(error, duplicate_error):
            raise
        if not savepoint_created:
            raise
        frappe.db.rollback(save_point=savepoint)
        recovered = _find_existing_journal(
            context["source"],
            context["reconciliation_key"],
            link_field=(
                "failure_journal_entry"
                if context["disposition"] == RECONCILIATION_FAILED_STATUS
                else "reclassification_journal_entry"
            ),
            key_field=(
                "custom_kopos_qr_failure_key"
                if context["disposition"] == RECONCILIATION_FAILED_STATUS
                else "custom_kopos_qr_reconciliation_key"
            ),
        )
        if not recovered:
            raise
        return recovered


def _validate_journal(context: dict[str, Any], journal_name: str) -> dict[str, Any]:
    journal = frappe.get_doc(JOURNAL_ENTRY_DOCTYPE, journal_name)
    if int(_value(journal, "docstatus") or 0) != 1:
        frappe.throw(
            f"QR reclassification Journal Entry {journal_name} is not submitted",
            frappe.ValidationError,
        )

    journal_key_field = (
        "custom_kopos_qr_failure_key"
        if context["disposition"] == RECONCILIATION_FAILED_STATUS
        else "custom_kopos_qr_reconciliation_key"
    )
    expected_fields = {
        journal_key_field: context["reconciliation_key"],
        "custom_kopos_qr_source_doctype": context["source_doctype"],
        "custom_kopos_qr_source_name": context["source_name"],
        "custom_kopos_qr_fb_order": context["order_name"],
        "custom_kopos_qr_sales_invoice": context["invoice_name"],
        "custom_kopos_qr_currency": context["currency"],
        "company": context["company"],
    }
    for fieldname, expected in expected_fields.items():
        actual = cstr(_value(journal, fieldname)).strip()
        if fieldname == "custom_kopos_qr_currency":
            actual = actual.upper()
        if actual != cstr(expected).strip():
            frappe.throw(
                f"Journal Entry {journal_name} {fieldname} does not match QR reconciliation",
                frappe.ValidationError,
            )
    if context["disposition"] == RECONCILIATION_FAILED_STATUS:
        expected_failure_fields = {
            "custom_kopos_qr_disposition": RECONCILIATION_FAILED_STATUS,
            "custom_kopos_qr_fb_order_payment": context["payment_row_name"],
            "custom_kopos_qr_target_account": context["target_account"],
            "custom_kopos_qr_cost_center": context["failure_cost_center"],
            "custom_kopos_qr_failure_reason": context["failure_reason"],
        }
        for fieldname, expected in expected_failure_fields.items():
            if cstr(_value(journal, fieldname)).strip() != cstr(expected).strip():
                frappe.throw(
                    f"Journal Entry {journal_name} {fieldname} does not match "
                    "QR failure disposition",
                    frappe.ValidationError,
                )
    elif context["prior_failure_journal_entry"]:
        expected_recovery_fields = {
            "custom_kopos_qr_source_account": context["credit_account"],
            "custom_kopos_qr_prior_failure_journal": context[
                "prior_failure_journal_entry"
            ],
            "custom_kopos_qr_cost_center": context["credit_cost_center"],
        }
        for fieldname, expected in expected_recovery_fields.items():
            if cstr(_value(journal, fieldname)).strip() != cstr(expected).strip():
                frappe.throw(
                    f"Journal Entry {journal_name} {fieldname} does not match "
                    "late QR settlement recovery",
                    frappe.ValidationError,
                )
    if _strict_positive_sen(
        _value(journal, "custom_kopos_qr_amount_sen"),
        "Journal Entry QR amount_sen",
    ) != context["amount_sen"]:
        frappe.throw(
            f"Journal Entry {journal_name} amount does not match QR reconciliation",
            frappe.ValidationError,
        )

    rows = frappe.get_all(
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
            "cost_center",
        ],
        order_by="account asc, name asc",
    )
    if not rows:
        frappe.throw(
            f"Journal Entry {journal_name} has no submitted GL evidence",
            frappe.ValidationError,
        )

    observed = {
        context["target_account"]: {"debit_sen": 0, "credit_sen": 0},
        context["credit_account"]: {"debit_sen": 0, "credit_sen": 0},
    }
    for row in rows:
        account = cstr(_row_value(row, "account")).strip()
        debit_sen, credit_sen = _gl_amounts_sen(row, account)
        if debit_sen == 0 and credit_sen == 0:
            continue
        if account not in observed:
            frappe.throw(
                f"Journal Entry {journal_name} contains an unexpected GL account {account}",
                frappe.ValidationError,
            )
        if (
            context["disposition"] == RECONCILIATION_FAILED_STATUS
            and account == context["target_account"]
            and cstr(_row_value(row, "cost_center")).strip()
            != context["failure_cost_center"]
        ):
            frappe.throw(
                f"Journal Entry {journal_name} QR failure Cost Center does not match",
                frappe.ValidationError,
            )
        if (
            context["prior_failure_journal_entry"]
            and account == context["credit_account"]
            and cstr(_row_value(row, "cost_center")).strip()
            != context["credit_cost_center"]
        ):
            frappe.throw(
                f"Journal Entry {journal_name} late-settlement Cost Center does not match",
                frappe.ValidationError,
            )
        observed[account]["debit_sen"] += debit_sen
        observed[account]["credit_sen"] += credit_sen

    expected_sen = int(context["amount_sen"])
    target = observed[context["target_account"]]
    credit = observed[context["credit_account"]]
    if target != {"debit_sen": expected_sen, "credit_sen": 0}:
        frappe.throw(
            f"Journal Entry {journal_name} does not exactly debit the QR target account",
            frappe.ValidationError,
        )
    if credit != {"debit_sen": 0, "credit_sen": expected_sen}:
        frappe.throw(
            f"Journal Entry {journal_name} does not exactly credit the QR source account",
            frappe.ValidationError,
        )

    result = {
        "journal_entry": journal_name,
        "reconciliation_key": context["reconciliation_key"],
        "source_doctype": context["source_doctype"],
        "source_name": context["source_name"],
        "fb_order": context["order_name"],
        "sales_invoice": context["invoice_name"],
        "amount_sen": expected_sen,
        "company": context["company"],
        "currency": context["currency"],
        "suspense_account": context["suspense_account"],
        "target_account": context["target_account"],
    }
    if context["disposition"] == RECONCILIATION_FAILED_STATUS:
        result.update(
            {
                "disposition": context["disposition"],
                "failure_reason": context["failure_reason"],
                "cost_center": context["failure_cost_center"],
            }
        )
    elif context["prior_failure_journal_entry"]:
        result.update(
            {
                "source_account": context["credit_account"],
                "prior_failure_journal_entry": context[
                    "prior_failure_journal_entry"
                ],
                "cost_center": context["credit_cost_center"],
            }
        )
    return result
