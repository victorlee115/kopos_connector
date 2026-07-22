# pyright: reportMissingImports=false

"""Shared context construction and validation for QR reconciliation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe
from frappe.utils import cstr


JOURNAL_ENTRY_DOCTYPE = "Journal Entry"
MAYBANK_TRANSACTION_DOCTYPE = "Maybank QR Transaction"
MANUAL_RECONCILIATION_DOCTYPE = "Manual QR Reconciliation"
SECONDARY_STATIC_CLAIM_ROLE = "secondary_possible_duplicate"
SUPPORTED_SOURCE_DOCTYPES = {
    MAYBANK_TRANSACTION_DOCTYPE,
    MANUAL_RECONCILIATION_DOCTYPE,
}
RECONCILABLE_STATUSES = {"pending_reconciliation", "reconciled"}
FAILURE_RECONCILABLE_STATUSES = {
    "pending_reconciliation",
    "reconciliation_failed",
}
RECONCILIATION_FAILED_STATUS = "reconciliation_failed"
RECONCILIATION_FAILED_REASONS = {
    "no_bank_transaction",
    "amount_mismatch",
    "duplicate",
    "wrong_device",
    "customer_dispute",
    "other",
}
COMPANY_FAILURE_ACCOUNT_FIELD = "custom_kopos_qr_failure_variance_account"
FAILURE_KEY_NAMESPACE = "kopos:qr-failure:v1:"
SEN_PER_UNIT = Decimal("100")

REQUIRED_JOURNAL_FIELDS = (
    "custom_kopos_qr_reconciliation_key",
    "custom_kopos_qr_source_doctype",
    "custom_kopos_qr_source_name",
    "custom_kopos_qr_fb_order",
    "custom_kopos_qr_sales_invoice",
    "custom_kopos_qr_amount_sen",
    "custom_kopos_qr_currency",
)
REQUIRED_FAILURE_JOURNAL_FIELDS = (
    "custom_kopos_qr_failure_key",
    "custom_kopos_qr_disposition",
    "custom_kopos_qr_fb_order_payment",
    "custom_kopos_qr_target_account",
    "custom_kopos_qr_cost_center",
    "custom_kopos_qr_failure_reason",
)
REQUIRED_FAILURE_RECOVERY_JOURNAL_FIELDS = (
    "custom_kopos_qr_source_account",
    "custom_kopos_qr_prior_failure_journal",
)
REQUIRED_FAILURE_SOURCE_FIELDS = (
    "failure_accounting_key",
    "failure_variance_account",
    "failure_cost_center",
    "failure_accounting_reason",
    "failure_journal_entry",
)


def _lock_and_reload_source(value: Any) -> Any:
    doctype = cstr(_value(value, "doctype")).strip()
    name = cstr(_value(value, "name")).strip()
    if doctype not in SUPPORTED_SOURCE_DOCTYPES:
        frappe.throw(
            "QR reconciliation source must be a Maybank QR Transaction "
            "or Manual QR Reconciliation",
            frappe.ValidationError,
        )
    if not name:
        frappe.throw("QR reconciliation source name is required", frappe.ValidationError)

    frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` WHERE name = %s FOR UPDATE",
        (name,),
    )
    return frappe.get_doc(doctype, name)


def _build_context(
    source: Any,
    *,
    disposition: str = "reconciled",
    failure_reason: str | None = None,
    allow_provider_paid_history: bool = False,
) -> dict[str, Any]:
    source_doctype = cstr(_value(source, "doctype")).strip()
    source_name = cstr(_value(source, "name")).strip()
    if (
        source_doctype == MANUAL_RECONCILIATION_DOCTYPE
        and cstr(_value(source, "claim_role")).strip()
        == SECONDARY_STATIC_CLAIM_ROLE
    ):
        frappe.throw(
            "Secondary static QR claim must remain pending duplicate-payment review; "
            "it cannot reclassify the completed Maybank sale",
            frappe.ValidationError,
        )
    status_field = (
        "manual_reconciliation_status"
        if source_doctype == MAYBANK_TRANSACTION_DOCTYPE
        else "status"
    )
    source_status = cstr(_value(source, status_field)).strip()
    allowed_statuses = (
        FAILURE_RECONCILABLE_STATUSES
        if disposition == RECONCILIATION_FAILED_STATUS
        else RECONCILABLE_STATUSES
    )
    if source_status not in allowed_statuses:
        frappe.throw(
            f"{source_doctype} {source_name} is not pending reconciliation",
            frappe.ValidationError,
        )

    if disposition == RECONCILIATION_FAILED_STATUS:
        normalized_failure_reason = cstr(failure_reason).strip()
        if normalized_failure_reason not in RECONCILIATION_FAILED_REASONS:
            frappe.throw("QR reconciliation failure reason is invalid", frappe.ValidationError)
        if (
            source_doctype == MAYBANK_TRANSACTION_DOCTYPE
            and cstr(_value(source, "status")).strip() == "paid"
            and not allow_provider_paid_history
        ):
            frappe.throw(
                "Provider-paid Maybank QR truth cannot be disposed as reconciliation_failed",
                frappe.ValidationError,
            )
        if cstr(_value(source, "reclassification_journal_entry")).strip():
            frappe.throw(
                "QR reconciliation already has posted bank accounting evidence",
                frappe.ValidationError,
            )
        terminal_reason = cstr(
            _value(source, "reconciliation_failed_reason")
        ).strip()
        if (
            source_status == RECONCILIATION_FAILED_STATUS
            and terminal_reason != normalized_failure_reason
        ):
            frappe.throw(
                f"{source_doctype} {source_name} failure reason does not match",
                frappe.ValidationError,
            )
    else:
        normalized_failure_reason = ""

    order_name = _required_text(source, "fb_order")
    invoice_name = _required_text(source, "sales_invoice")
    payment_row_name = _required_text(source, "fb_order_payment")
    company = _required_text(source, "company")
    currency = _required_text(source, "currency").upper()
    suspense_account = _required_text(source, "suspense_account")
    amount_field = (
        "sale_amount_sen"
        if source_doctype == MAYBANK_TRANSACTION_DOCTYPE
        else "amount_sen"
    )
    amount_sen = _strict_positive_sen(_value(source, amount_field), amount_field)

    order = frappe.get_doc("FB Order", order_name)
    invoice = frappe.get_doc("Sales Invoice", invoice_name)
    if int(_value(order, "docstatus") or 0) != 1:
        frappe.throw(f"FB Order {order_name} is not submitted", frappe.ValidationError)
    if int(_value(invoice, "docstatus") or 0) != 1:
        frappe.throw(
            f"Sales Invoice {invoice_name} is not submitted", frappe.ValidationError
        )
    if int(_value(invoice, "is_return") or 0):
        frappe.throw(
            f"Sales Invoice {invoice_name} must not be a return",
            frappe.ValidationError,
        )

    _require_equal("FB Order company", _value(order, "company"), company)
    _require_equal("Sales Invoice company", _value(invoice, "company"), company)
    _require_equal(
        "FB Order currency", cstr(_value(order, "currency")).upper(), currency
    )
    _require_equal(
        "Sales Invoice currency",
        cstr(_value(invoice, "currency")).upper(),
        currency,
    )
    _require_equal(
        "Sales Invoice FB Order link", _value(invoice, "custom_fb_order"), order_name
    )
    linked_invoice = cstr(_value(order, "sales_invoice")).strip()
    if linked_invoice and linked_invoice != invoice_name:
        frappe.throw(
            f"FB Order {order_name} references another Sales Invoice",
            frappe.ValidationError,
        )

    if _money_to_sen(_value(order, "grand_total"), "FB Order grand_total") != amount_sen:
        frappe.throw(
            f"FB Order {order_name} total does not match QR reconciliation",
            frappe.ValidationError,
        )
    if _invoice_total_sen(invoice) != amount_sen:
        frappe.throw(
            f"Sales Invoice {invoice_name} total does not match QR reconciliation",
            frappe.ValidationError,
        )
    if _money_to_sen(
        _value(invoice, "outstanding_amount") or 0,
        "Sales Invoice outstanding_amount",
    ) != 0:
        frappe.throw(
            f"Sales Invoice {invoice_name} is not fully paid",
            frappe.ValidationError,
        )
    if _decimal_value(
        _value(invoice, "conversion_rate") or 1, "Sales Invoice conversion_rate"
    ) != Decimal("1"):
        frappe.throw(
            "QR reconciliation requires a company-currency Sales Invoice",
            frappe.ValidationError,
        )

    company_currency = cstr(
        frappe.db.get_value("Company", company, "default_currency")
    ).strip().upper()
    if not company_currency or company_currency != currency:
        frappe.throw(
            "QR reconciliation currency must equal the company currency",
            frappe.ValidationError,
        )

    payment = _find_order_payment(order, payment_row_name)
    _validate_payment_source(source, payment, source_doctype)
    if _money_to_sen(_value(payment, "amount"), "FB Order Payment amount") != amount_sen:
        frappe.throw(
            f"FB Order Payment {payment_row_name} amount does not match QR reconciliation",
            frappe.ValidationError,
        )
    _require_equal(
        "FB Order Payment suspense account",
        _value(payment, "suspense_account"),
        suspense_account,
    )
    settlement_status = cstr(_value(payment, "settlement_status")).strip()
    if settlement_status not in allowed_statuses:
        frappe.throw(
            f"FB Order Payment {payment_row_name} is not pending reconciliation",
            frappe.ValidationError,
        )

    source_payment_id = _required_text(payment, "source_payment_id")
    mode_of_payment = _required_text(payment, "payment_method")
    mode_token = f"{mode_of_payment} {cstr(_value(payment, 'payment_channel_code'))}".lower()
    if not any(token in mode_token for token in ("duitnow", "maybank", "qr")):
        frappe.throw(
            "QR reconciliation requires a DuitNow, Maybank, or QR Mode of Payment",
            frappe.ValidationError,
        )

    invoice_payment = _find_invoice_payment(invoice, source_payment_id)
    _require_equal(
        "Sales Invoice payment mode",
        _value(invoice_payment, "mode_of_payment"),
        mode_of_payment,
    )
    _require_equal(
        "Sales Invoice payment suspense account",
        _value(invoice_payment, "account"),
        suspense_account,
    )
    if _money_to_sen(
        _value(invoice_payment, "amount"), "Sales Invoice Payment amount"
    ) != amount_sen:
        frappe.throw(
            f"Sales Invoice {invoice_name} payment amount does not match QR reconciliation",
            frappe.ValidationError,
        )

    _validate_ledger_account(
        suspense_account,
        company=company,
        currency=currency,
        role="manual QR suspense",
        require_bank_or_clearing=False,
    )
    if disposition == RECONCILIATION_FAILED_STATUS:
        target_account = _configured_failure_variance_account(
            source,
            company=company,
            currency=currency,
        )
        failure_cost_center = _configured_failure_cost_center(
            source,
            company=company,
        )
    else:
        target_account = _configured_mode_of_payment_account(mode_of_payment, company)
        failure_cost_center = ""
    if target_account == suspense_account:
        frappe.throw(
            "QR target bank/clearing account must differ from the suspense account",
            frappe.ValidationError,
        )
    _validate_ledger_account(
        target_account,
        company=company,
        currency=currency,
        role=(
            "QR failure variance"
            if disposition == RECONCILIATION_FAILED_STATUS
            else "QR target"
        ),
        required_root_type=(
            "Expense"
            if disposition == RECONCILIATION_FAILED_STATUS
            else "Asset"
        ),
        require_bank_or_clearing=(disposition != RECONCILIATION_FAILED_STATUS),
    )

    posting_date = cstr(
        _value(source, "business_date") or _value(invoice, "posting_date")
    ).strip()
    if not posting_date:
        frappe.throw("QR reconciliation posting date is required", frappe.ValidationError)

    context = {
        "source": source,
        "source_doctype": source_doctype,
        "source_name": source_name,
        "order": order,
        "order_name": order_name,
        "invoice": invoice,
        "invoice_name": invoice_name,
        "payment": payment,
        "payment_row_name": payment_row_name,
        "amount_sen": amount_sen,
        "company": company,
        "currency": currency,
        "suspense_account": suspense_account,
        "target_account": target_account,
        "failure_cost_center": failure_cost_center,
        "posting_date": posting_date,
        "disposition": disposition,
        "failure_reason": normalized_failure_reason,
        "credit_account": suspense_account,
        "credit_cost_center": "",
        "prior_failure_journal_entry": "",
    }
    if disposition == RECONCILIATION_FAILED_STATUS:
        context["reconciliation_key"] = _failure_accounting_key(context)
        _validate_failure_source_snapshot(context)
    else:
        reconciliation_key = cstr(
            _value(source, "reconciliation_idempotency_key")
        ).strip()
        if not reconciliation_key or len(reconciliation_key) > 140:
            frappe.throw(
                f"{source_doctype} {source_name} requires a valid reconciliation idempotency key",
                frappe.ValidationError,
            )
        context["reconciliation_key"] = reconciliation_key
    return context


def _validate_payment_source(source: Any, payment: Any, source_doctype: str) -> None:
    source_name = _required_text(source, "name")
    payment_row_name = _required_text(source, "fb_order_payment")
    _require_equal("FB Order Payment row", _value(payment, "name"), payment_row_name)

    if source_doctype == MAYBANK_TRANSACTION_DOCTYPE:
        transaction_ref = _required_text(source, "transaction_refno")
        _require_equal(
            "FB Order Payment Maybank transaction",
            _value(payment, "maybank_qr_transaction"),
            source_name,
        )
    else:
        transaction_ref = _required_text(source, "provider_session_id")
        _require_equal(
            "FB Order Payment manual reconciliation",
            _value(payment, "manual_qr_reconciliation"),
            source_name,
        )

    _require_equal(
        "FB Order Payment provider transaction",
        _value(payment, "external_transaction_id"),
        transaction_ref,
    )
    receipt_reference = cstr(_value(payment, "reference_no")).strip()
    if receipt_reference and receipt_reference != transaction_ref:
        frappe.throw(
            "FB Order Payment reference does not match QR transaction",
            frappe.ValidationError,
        )


def _find_order_payment(order: Any, payment_row_name: str) -> Any:
    matches = [
        payment
        for payment in list(_value(order, "payments") or [])
        if cstr(_value(payment, "name")).strip() == payment_row_name
    ]
    if len(matches) != 1:
        frappe.throw(
            f"FB Order {order.name} does not contain exactly one payment row {payment_row_name}",
            frappe.ValidationError,
        )
    return matches[0]


def _find_invoice_payment(invoice: Any, source_payment_id: str) -> Any:
    matches = [
        payment
        for payment in list(_value(invoice, "payments") or [])
        if cstr(_value(payment, "custom_fb_source_payment_id")).strip()
        == source_payment_id
    ]
    if len(matches) != 1:
        frappe.throw(
            f"Sales Invoice {invoice.name} does not contain exactly one "
            f"payment for {source_payment_id}",
            frappe.ValidationError,
        )
    return matches[0]


def _validate_ledger_account(
    account: str,
    *,
    company: str,
    currency: str,
    role: str,
    required_root_type: str = "Asset",
    require_bank_or_clearing: bool,
) -> None:
    row = frappe.db.get_value(
        "Account",
        account,
        [
            "company",
            "is_group",
            "disabled",
            "root_type",
            "account_type",
            "account_currency",
        ],
        as_dict=True,
    )
    if not row:
        frappe.throw(f"{role} account {account} was not found", frappe.ValidationError)
    _require_equal(f"{role} account company", _value(row, "company"), company)
    if int(_value(row, "is_group") or 0) or int(_value(row, "disabled") or 0):
        frappe.throw(
            f"{role} account {account} must be an enabled ledger account",
            frappe.ValidationError,
        )
    if cstr(_value(row, "root_type")).strip() != required_root_type:
        frappe.throw(
            f"{role} account {account} must be an {required_root_type} ledger",
            frappe.ValidationError,
        )
    account_currency = cstr(_value(row, "account_currency")).strip().upper()
    if account_currency and account_currency != currency:
        frappe.throw(
            f"{role} account {account} currency does not match",
            frappe.ValidationError,
        )
    account_type = cstr(_value(row, "account_type")).strip()
    if require_bank_or_clearing and account_type not in {"", "Bank"}:
        frappe.throw(
            f"{role} account {account} must be a Bank account or untyped Asset clearing account",
            frappe.ValidationError,
        )


def _configured_failure_variance_account(
    source: Any,
    *,
    company: str,
    currency: str,
) -> str:
    snapshot_account = cstr(
        _value(source, "failure_variance_account")
    ).strip()
    if snapshot_account:
        return snapshot_account

    company_meta = frappe.get_meta("Company")
    if not company_meta.has_field(COMPANY_FAILURE_ACCOUNT_FIELD):
        frappe.throw(
            "Company QR failure variance account field is missing; run bench migrate",
            frappe.ValidationError,
        )
    account = cstr(
        frappe.db.get_value("Company", company, COMPANY_FAILURE_ACCOUNT_FIELD)
    ).strip()
    if not account:
        frappe.throw(
            f"Company {company} requires {COMPANY_FAILURE_ACCOUNT_FIELD}; "
            "QR reconciliation remains pending",
            frappe.ValidationError,
        )
    if account == cstr(_value(source, "suspense_account")).strip():
        frappe.throw(
            "QR failure variance account must differ from the suspense account",
            frappe.ValidationError,
        )
    return account


def _configured_failure_cost_center(source: Any, *, company: str) -> str:
    snapshot_cost_center = cstr(
        _value(source, "failure_cost_center")
    ).strip()
    cost_center = snapshot_cost_center or cstr(
        frappe.db.get_value("Company", company, "cost_center")
    ).strip()
    if not cost_center:
        frappe.throw(
            f"Company {company} requires a default Cost Center; "
            "QR reconciliation remains pending",
            frappe.ValidationError,
        )
    row = frappe.db.get_value(
        "Cost Center",
        cost_center,
        ["company", "is_group", "disabled"],
        as_dict=True,
    )
    if not row:
        frappe.throw(
            f"QR failure Cost Center {cost_center} was not found",
            frappe.ValidationError,
        )
    _require_equal(
        "QR failure Cost Center company",
        _value(row, "company"),
        company,
    )
    if int(_value(row, "is_group") or 0) or int(_value(row, "disabled") or 0):
        frappe.throw(
            f"QR failure Cost Center {cost_center} must be an enabled ledger Cost Center",
            frappe.ValidationError,
        )
    return cost_center


def _failure_accounting_key(context: dict[str, Any]) -> str:
    fingerprint = {
        "version": 1,
        "disposition": RECONCILIATION_FAILED_STATUS,
        "source_doctype": context["source_doctype"],
        "source_name": context["source_name"],
        "fb_order": context["order_name"],
        "fb_order_payment": context["payment_row_name"],
        "sales_invoice": context["invoice_name"],
        "amount_sen": int(context["amount_sen"]),
        "company": context["company"],
        "currency": context["currency"],
        "suspense_account": context["suspense_account"],
        "target_account": context["target_account"],
        "cost_center": context["failure_cost_center"],
        "failure_reason": context["failure_reason"],
    }
    encoded = json.dumps(
        fingerprint,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return FAILURE_KEY_NAMESPACE + hashlib.sha256(encoded).hexdigest()


def _validate_failure_source_snapshot(context: dict[str, Any]) -> None:
    source = context["source"]
    expected = {
        "failure_accounting_key": context["reconciliation_key"],
        "failure_variance_account": context["target_account"],
        "failure_cost_center": context["failure_cost_center"],
        "failure_accounting_reason": context["failure_reason"],
    }
    populated = {
        fieldname: cstr(_value(source, fieldname)).strip()
        for fieldname in expected
        if cstr(_value(source, fieldname)).strip()
    }
    if populated and len(populated) != len(expected):
        frappe.throw(
            f"{context['source_doctype']} {context['source_name']} has an incomplete "
            "QR failure accounting snapshot",
            frappe.ValidationError,
        )
    for fieldname, actual in populated.items():
        if actual != expected[fieldname]:
            frappe.throw(
                f"{context['source_doctype']} {context['source_name']} {fieldname} "
                "does not match QR failure disposition",
                frappe.ValidationError,
            )


def _configured_mode_of_payment_account(mode_of_payment: str, company: str) -> str:
    try:
        from erpnext.accounts.doctype.sales_invoice.sales_invoice import (
            get_mode_of_payment_info,
        )
    except Exception as error:
        raise RuntimeError(
            "ERPNext mode-of-payment accounting resolver is unavailable"
        ) from error

    mode_info = get_mode_of_payment_info(mode_of_payment, company)
    if not mode_info:
        frappe.throw(
            f"Mode of Payment {mode_of_payment} is not configured for company {company}",
            frappe.ValidationError,
        )
    payment_meta = mode_info[0]
    account = cstr(
        _value(payment_meta, "account")
        or _value(payment_meta, "default_account")
    ).strip()
    if not account:
        frappe.throw(
            f"Mode of Payment {mode_of_payment} has no ledger account for company {company}",
            frappe.ValidationError,
        )
    return account


def _validate_invoice_suspense_receipt(context: dict[str, Any]) -> None:
    invoice_name = context["invoice_name"]
    suspense_account = context["suspense_account"]
    expected_sen = int(context["amount_sen"])
    rows = frappe.get_all(
        "GL Entry",
        filters={
            "voucher_type": "Sales Invoice",
            "voucher_no": invoice_name,
            "is_cancelled": 0,
            "account": suspense_account,
        },
        fields=[
            "account",
            "debit",
            "credit",
            "debit_in_account_currency",
            "credit_in_account_currency",
        ],
        order_by="name asc",
    )
    if not rows:
        frappe.throw(
            f"Sales Invoice {invoice_name} has no posted suspense GL receipt",
            frappe.ValidationError,
        )

    observed_sen = 0
    for row in rows:
        debit_sen, credit_sen = _gl_amounts_sen(row, suspense_account)
        if debit_sen <= 0 or credit_sen != 0:
            frappe.throw(
                f"Sales Invoice {invoice_name} suspense GL must be a pure debit",
                frappe.ValidationError,
            )
        observed_sen += debit_sen
    if observed_sen != expected_sen:
        frappe.throw(
            f"Sales Invoice {invoice_name} suspense GL amount does not match QR reconciliation",
            frappe.ValidationError,
        )


def _require_journal_fields(
    *,
    require_failure_fields: bool = False,
    require_recovery_fields: bool = False,
) -> None:
    meta = frappe.get_meta(JOURNAL_ENTRY_DOCTYPE)
    required = REQUIRED_JOURNAL_FIELDS + (
        REQUIRED_FAILURE_JOURNAL_FIELDS if require_failure_fields else ()
    ) + (
        REQUIRED_FAILURE_RECOVERY_JOURNAL_FIELDS
        if require_recovery_fields
        else ()
    )
    missing = [fieldname for fieldname in required if not meta.has_field(fieldname)]
    if missing:
        frappe.throw(
            "Journal Entry QR reconciliation fields are missing; run bench migrate: "
            + ", ".join(missing),
            frappe.ValidationError,
        )


def _require_failure_source_fields(source_doctype: str) -> None:
    meta = frappe.get_meta(source_doctype)
    missing = [
        fieldname
        for fieldname in REQUIRED_FAILURE_SOURCE_FIELDS
        if not meta.has_field(fieldname)
    ]
    if missing:
        frappe.throw(
            f"{source_doctype} QR failure accounting fields are missing; "
            "run bench migrate: " + ", ".join(missing),
            frappe.ValidationError,
        )


def _find_existing_journal(
    source: Any,
    reconciliation_key: str,
    *,
    link_field: str = "reclassification_journal_entry",
    key_field: str = "custom_kopos_qr_reconciliation_key",
) -> str | None:
    linked_name = cstr(
        _value(source, link_field)
    ).strip()
    recovered_name = cstr(
        frappe.db.get_value(
            JOURNAL_ENTRY_DOCTYPE,
            {key_field: reconciliation_key},
            "name",
        )
    ).strip()
    if linked_name and recovered_name and linked_name != recovered_name:
        frappe.throw(
            f"QR reconciliation {reconciliation_key} references conflicting Journal Entries",
            frappe.ValidationError,
        )
    return linked_name or recovered_name or None


def _invoice_total_sen(invoice: Any) -> int:
    rounded_total = _value(invoice, "rounded_total")
    if rounded_total not in (None, ""):
        rounded_sen = _money_to_sen(rounded_total, "Sales Invoice rounded_total")
        if rounded_sen != 0:
            return rounded_sen
    grand_total_sen = _money_to_sen(
        _value(invoice, "grand_total"), "Sales Invoice grand_total"
    )
    write_off_sen = _money_to_sen(
        _value(invoice, "write_off_amount") or 0,
        "Sales Invoice write_off_amount",
    )
    return grand_total_sen - write_off_sen


def _gl_amounts_sen(row: Any, account: str) -> tuple[int, int]:
    debit = _row_value(row, "debit_in_account_currency")
    credit = _row_value(row, "credit_in_account_currency")
    if debit is None and credit is None:
        debit = _row_value(row, "debit")
        credit = _row_value(row, "credit")
    return (
        _money_to_sen(debit or 0, f"{account} GL debit"),
        _money_to_sen(credit or 0, f"{account} GL credit"),
    )


def _strict_positive_sen(value: Any, label: str) -> int:
    if isinstance(value, bool):
        frappe.throw(f"{label} must be a positive integer", frappe.ValidationError)
    text = cstr(value).strip()
    if not text.isdigit():
        frappe.throw(f"{label} must be a positive integer", frappe.ValidationError)
    result = int(text)
    if result <= 0:
        frappe.throw(f"{label} must be greater than 0", frappe.ValidationError)
    return result


def _money_to_sen(value: Any, label: str) -> int:
    amount = _decimal_value(value, label)
    sen = amount * SEN_PER_UNIT
    integral_sen = sen.to_integral_value()
    if sen != integral_sen:
        frappe.throw(f"{label} contains fractional sen", frappe.ValidationError)
    return int(integral_sen)


def _sen_to_amount(value_sen: int) -> Decimal:
    return Decimal(value_sen) / SEN_PER_UNIT


def _decimal_value(value: Any, label: str) -> Decimal:
    try:
        return Decimal(cstr(value or 0).strip() or "0")
    except (InvalidOperation, ValueError) as error:
        frappe.throw(f"Invalid {label}: {value}", frappe.ValidationError)
        raise ValueError(f"Invalid {label}: {value}") from error


def _required_text(doc: Any, fieldname: str) -> str:
    value = cstr(_value(doc, fieldname)).strip()
    if not value:
        frappe.throw(f"{fieldname} is required for QR reconciliation", frappe.ValidationError)
    return value


def _require_equal(label: str, actual: Any, expected: Any) -> None:
    if cstr(actual).strip() != cstr(expected).strip():
        frappe.throw(f"{label} does not match QR reconciliation", frappe.ValidationError)


def _value(doc: Any, fieldname: str) -> Any:
    getter = getattr(doc, "get", None)
    if callable(getter):
        value = getter(fieldname)
        if value is not None or (isinstance(doc, Mapping) and fieldname in doc):
            return value
    return getattr(doc, fieldname, None)


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return _value(row, fieldname)
