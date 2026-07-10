# pyright: reportMissingImports=false

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe
from frappe.utils import cstr


SETTLEMENT_DOCTYPE = "Journal Entry"
SETTLEMENT_STATUS = "Posted"
SUPPORTED_REFUND_METHODS = {"cash", "qr", "card", "voucher"}
SEN_PER_UNIT = Decimal("100")


def ensure_return_settlement(
    fb_return_event: Any,
    return_sales_invoice: str,
) -> str:
    """Create or recover one submitted, GL-proven full-refund settlement."""
    return_doc = _coerce_doc("FB Return Event", fb_return_event)
    if not return_doc:
        frappe.throw("FB Return Event is required for refund settlement")

    original_invoice_name = cstr(
        _value(return_doc, "original_sales_invoice")
    ).strip()
    if not original_invoice_name:
        frappe.throw(
            f"FB Return Event {return_doc.name} has no original Sales Invoice",
            frappe.ValidationError,
        )

    original_invoice = frappe.get_doc("Sales Invoice", original_invoice_name)
    return_invoice = frappe.get_doc("Sales Invoice", return_sales_invoice)
    refund_method = cstr(_value(return_doc, "refund_method")).strip().lower()
    if refund_method not in SUPPORTED_REFUND_METHODS:
        frappe.throw(
            f"FB Return Event {return_doc.name} has invalid refund_method",
            frappe.ValidationError,
        )

    total_sen = _validate_full_return_invoice(original_invoice, return_invoice)
    tenders = _resolve_original_tenders(
        original_invoice=original_invoice,
        refund_method=refund_method,
        expected_total_sen=total_sen,
    )
    _require_settlement_link_field()

    settlement_document = _find_existing_settlement(return_doc)
    if not settlement_document:
        pre_settlement_outstanding_sen = _money_to_sen(
            _value(return_invoice, "outstanding_amount") or 0,
            "return Sales Invoice pre-settlement outstanding_amount",
        )
        if pre_settlement_outstanding_sen != -total_sen:
            frappe.throw(
                f"Return Sales Invoice {return_invoice.name} does not expose the full unsettled customer credit",
                frappe.ValidationError,
            )
        settlement_document = _create_settlement_journal_entry(
            return_doc=return_doc,
            original_invoice=original_invoice,
            return_invoice=return_invoice,
            tenders=tenders,
            total_sen=total_sen,
        )

    evidence = _validate_settlement(
        return_doc=return_doc,
        original_invoice=original_invoice,
        return_invoice=return_invoice,
        settlement_document=settlement_document,
        tenders=tenders,
        total_sen=total_sen,
    )
    _record_settlement(
        return_doc=return_doc,
        settlement_document=settlement_document,
        total_sen=total_sen,
        tenders=tenders,
        evidence=evidence,
    )
    return settlement_document


def assert_return_settlement_posted(fb_return_event: Any) -> dict[str, Any]:
    """Re-prove persisted settlement before exposing refund success."""
    return_doc = _coerce_doc("FB Return Event", fb_return_event)
    if not return_doc:
        frappe.throw("FB Return Event is required for settlement verification")

    return_invoice_name = cstr(_value(return_doc, "return_sales_invoice")).strip()
    settlement_document = cstr(_value(return_doc, "settlement_document")).strip()
    if (
        cstr(_value(return_doc, "settlement_doctype")).strip()
        != SETTLEMENT_DOCTYPE
        or cstr(_value(return_doc, "settlement_status")).strip()
        != SETTLEMENT_STATUS
        or not return_invoice_name
        or not settlement_document
    ):
        frappe.throw(
            f"FB Return Event {return_doc.name} has no posted accounting settlement proof",
            frappe.ValidationError,
        )

    original_invoice = frappe.get_doc(
        "Sales Invoice", cstr(_value(return_doc, "original_sales_invoice")).strip()
    )
    return_invoice = frappe.get_doc("Sales Invoice", return_invoice_name)
    total_sen = _validate_full_return_invoice(original_invoice, return_invoice)
    tenders = _resolve_original_tenders(
        original_invoice=original_invoice,
        refund_method=cstr(_value(return_doc, "refund_method")).strip().lower(),
        expected_total_sen=total_sen,
    )
    return _validate_settlement(
        return_doc=return_doc,
        original_invoice=original_invoice,
        return_invoice=return_invoice,
        settlement_document=settlement_document,
        tenders=tenders,
        total_sen=total_sen,
    )


def get_settlement_cash_adjustment_sen(fb_return_event: Any) -> int:
    """Return the proven cash GL outflow as negative sen for shift totals."""
    return_doc = _coerce_doc("FB Return Event", fb_return_event)
    if not return_doc:
        return 0
    if cstr(_value(return_doc, "refund_method")).strip().lower() != "cash":
        return 0
    evidence = assert_return_settlement_posted(return_doc)
    return -int(evidence["tender_credit_sen"])


def _validate_full_return_invoice(original_invoice: Any, return_invoice: Any) -> int:
    if int(_value(original_invoice, "docstatus") or 0) != 1:
        frappe.throw(
            f"Original Sales Invoice {original_invoice.name} is not submitted",
            frappe.ValidationError,
        )
    if int(_value(return_invoice, "docstatus") or 0) != 1:
        frappe.throw(
            f"Return Sales Invoice {return_invoice.name} is not submitted",
            frappe.ValidationError,
        )
    if not int(_value(return_invoice, "is_return") or 0):
        frappe.throw(
            f"Sales Invoice {return_invoice.name} is not a return",
            frappe.ValidationError,
        )
    if cstr(_value(return_invoice, "return_against")).strip() != cstr(
        original_invoice.name
    ).strip():
        frappe.throw(
            f"Return Sales Invoice {return_invoice.name} does not reference {original_invoice.name}",
            frappe.ValidationError,
        )

    for fieldname in ("company", "currency", "customer", "debit_to"):
        original_value = cstr(_value(original_invoice, fieldname)).strip()
        return_value = cstr(_value(return_invoice, fieldname)).strip()
        if not original_value or return_value != original_value:
            frappe.throw(
                f"Return Sales Invoice {return_invoice.name} has mismatched {fieldname}",
                frappe.ValidationError,
            )

    original_grand_total_sen = _money_to_sen(
        _value(original_invoice, "grand_total"), "original Sales Invoice grand_total"
    )
    return_grand_total_sen = _money_to_sen(
        _value(return_invoice, "grand_total"), "return Sales Invoice grand_total"
    )
    if (
        original_grand_total_sen <= 0
        or return_grand_total_sen != -original_grand_total_sen
    ):
        frappe.throw(
            "Partial ERP returns are not supported; return Sales Invoice total must exactly reverse the original invoice",
            frappe.ValidationError,
        )

    original_total_sen = _invoice_payable_total_sen(
        original_invoice, "original Sales Invoice"
    )
    return_total_sen = _invoice_payable_total_sen(
        return_invoice, "return Sales Invoice"
    )
    if original_total_sen <= 0 or return_total_sen != -original_total_sen:
        frappe.throw(
            "Return Sales Invoice rounded payable total must exactly reverse the original invoice",
            frappe.ValidationError,
        )

    original_outstanding_sen = _money_to_sen(
        _value(original_invoice, "outstanding_amount") or 0,
        "original Sales Invoice outstanding_amount",
    )
    if original_outstanding_sen != 0:
        frappe.throw(
            f"Original Sales Invoice {original_invoice.name} is not fully paid",
            frappe.ValidationError,
        )

    company_currency = cstr(
        frappe.db.get_value("Company", original_invoice.company, "default_currency")
    ).strip()
    invoice_currency = cstr(_value(original_invoice, "currency")).strip()
    conversion_rate = _decimal_value(
        _value(original_invoice, "conversion_rate") or 1,
        "Sales Invoice conversion_rate",
    )
    return_conversion_rate = _decimal_value(
        _value(return_invoice, "conversion_rate") or 1,
        "return Sales Invoice conversion_rate",
    )
    if (
        not company_currency
        or invoice_currency != company_currency
        or conversion_rate != 1
        or return_conversion_rate != 1
    ):
        frappe.throw(
            "Refund settlement currently requires a company-currency Sales Invoice with conversion_rate 1",
            frappe.ValidationError,
        )
    return original_total_sen


def _invoice_payable_total_sen(invoice: Any, label: str) -> int:
    rounded_total = _value(invoice, "rounded_total")
    if rounded_total not in (None, ""):
        rounded_total_sen = _money_to_sen(rounded_total, f"{label} rounded_total")
        if rounded_total_sen != 0:
            return rounded_total_sen
    return _money_to_sen(_value(invoice, "grand_total"), f"{label} grand_total")


def _resolve_original_tenders(
    original_invoice: Any,
    refund_method: str,
    expected_total_sen: int,
) -> list[dict[str, Any]]:
    if refund_method not in SUPPORTED_REFUND_METHODS:
        frappe.throw(
            "refund_method must be one of: cash, qr, card, voucher",
            frappe.ValidationError,
        )

    payment_accounts: dict[str, set[str]] = {}
    observed_methods: set[str] = set()
    for payment in list(_value(original_invoice, "payments") or []):
        mode_of_payment = cstr(_value(payment, "mode_of_payment")).strip()
        payment_type = cstr(_value(payment, "type")).strip()
        account = cstr(_value(payment, "account")).strip()
        if not mode_of_payment or not account:
            frappe.throw(
                f"Sales Invoice {original_invoice.name} has payment rows without mode/account provenance",
                frappe.ValidationError,
            )
        method = _categorize_tender(mode_of_payment, payment_type)
        if not method:
            frappe.throw(
                f"Unsupported original tender mode: {mode_of_payment}",
                frappe.ValidationError,
            )
        observed_methods.add(method)
        payment_accounts.setdefault(account, set()).add(method)

    if not payment_accounts:
        frappe.throw(
            f"Sales Invoice {original_invoice.name} has no original tender rows",
            frappe.ValidationError,
        )
    if observed_methods != {refund_method}:
        frappe.throw(
            "refund_method does not exactly match the original tender mix; mixed-tender refunds are not supported",
            frappe.ValidationError,
        )
    if any(len(methods) != 1 for methods in payment_accounts.values()):
        frappe.throw(
            "Original tender account maps to multiple refund methods",
            frappe.ValidationError,
        )

    account_names = sorted(payment_accounts)
    gl_rows = frappe.get_all(
        "GL Entry",
        filters={
            "voucher_type": "Sales Invoice",
            "voucher_no": original_invoice.name,
            "is_cancelled": 0,
            "account": ["in", account_names],
        },
        fields=[
            "account",
            "debit",
            "credit",
            "debit_in_account_currency",
            "credit_in_account_currency",
        ],
        order_by="account asc, name asc",
    )
    if not gl_rows:
        frappe.throw(
            f"Sales Invoice {original_invoice.name} has no posted tender GL evidence",
            frappe.ValidationError,
        )

    net_by_account_sen = {account: 0 for account in account_names}
    for row in gl_rows:
        account = cstr(_row_value(row, "account")).strip()
        if account not in net_by_account_sen:
            continue
        debit_value = _row_value(row, "debit_in_account_currency")
        credit_value = _row_value(row, "credit_in_account_currency")
        if debit_value is None and credit_value is None:
            debit_value = _row_value(row, "debit")
            credit_value = _row_value(row, "credit")
        net_by_account_sen[account] += _money_to_sen(
            debit_value or 0, f"{account} tender debit"
        ) - _money_to_sen(credit_value or 0, f"{account} tender credit")

    tenders = []
    for account, amount_sen in sorted(net_by_account_sen.items()):
        if amount_sen <= 0:
            frappe.throw(
                f"Original tender account {account} has no positive posted receipt",
                frappe.ValidationError,
            )
        account_type = cstr(
            frappe.db.get_value("Account", account, "account_type")
        ).strip()
        expected_account_type = {
            "cash": "Cash",
            "qr": "Bank",
            "card": "Bank",
        }.get(refund_method)
        if expected_account_type and account_type != expected_account_type:
            frappe.throw(
                f"Original {refund_method} tender account {account} must use a {expected_account_type} account",
                frappe.ValidationError,
            )
        tenders.append(
            {
                "account": account,
                "refund_method": refund_method,
                "amount_sen": amount_sen,
            }
        )

    if sum(int(tender["amount_sen"]) for tender in tenders) != expected_total_sen:
        frappe.throw(
            "Original tender GL total does not exactly match the full Sales Invoice total",
            frappe.ValidationError,
        )
    return tenders


def _create_settlement_journal_entry(
    return_doc: Any,
    original_invoice: Any,
    return_invoice: Any,
    tenders: list[dict[str, Any]],
    total_sen: int,
) -> str:
    customer = cstr(_value(original_invoice, "customer")).strip()
    receivable_account = cstr(_value(original_invoice, "debit_to")).strip()
    if not customer or not receivable_account:
        frappe.throw(
            f"Sales Invoice {original_invoice.name} lacks customer/receivable settlement context",
            frappe.ValidationError,
        )

    journal = frappe.new_doc(SETTLEMENT_DOCTYPE)
    journal.voucher_type = SETTLEMENT_DOCTYPE
    journal.company = original_invoice.company
    journal.posting_date = _value(return_invoice, "posting_date")
    journal.user_remark = (
        f"KoPOS full refund settlement for {return_doc.name}; "
        f"credit note {return_invoice.name}"
    )
    journal.custom_fb_return_event = return_doc.name
    journal.append(
        "accounts",
        {
            "account": receivable_account,
            "party_type": "Customer",
            "party": customer,
            "debit_in_account_currency": _sen_to_amount(total_sen),
            "credit_in_account_currency": Decimal("0"),
            "exchange_rate": 1,
            "reference_type": "Sales Invoice",
            "reference_name": return_invoice.name,
        },
    )
    for tender in tenders:
        journal.append(
            "accounts",
            {
                "account": tender["account"],
                "debit_in_account_currency": Decimal("0"),
                "credit_in_account_currency": _sen_to_amount(
                    int(tender["amount_sen"])
                ),
                "exchange_rate": 1,
            },
        )

    journal.insert(ignore_permissions=True)
    journal.submit()
    if int(_value(journal, "docstatus") or 0) != 1:
        frappe.throw(
            f"Refund settlement Journal Entry {journal.name} was not submitted",
            frappe.ValidationError,
        )
    return cstr(journal.name).strip()


def _validate_settlement(
    return_doc: Any,
    original_invoice: Any,
    return_invoice: Any,
    settlement_document: str,
    tenders: list[dict[str, Any]],
    total_sen: int,
) -> dict[str, Any]:
    settlement = frappe.get_doc(SETTLEMENT_DOCTYPE, settlement_document)
    if int(_value(settlement, "docstatus") or 0) != 1:
        frappe.throw(
            f"Refund settlement Journal Entry {settlement_document} is not submitted",
            frappe.ValidationError,
        )
    if cstr(_value(settlement, "custom_fb_return_event")).strip() != cstr(
        return_doc.name
    ).strip():
        frappe.throw(
            f"Journal Entry {settlement_document} is not linked to FB Return Event {return_doc.name}",
            frappe.ValidationError,
        )

    gl_rows = frappe.get_all(
        "GL Entry",
        filters={
            "voucher_type": SETTLEMENT_DOCTYPE,
            "voucher_no": settlement_document,
            "is_cancelled": 0,
        },
        fields=[
            "account",
            "party_type",
            "party",
            "debit",
            "credit",
            "debit_in_account_currency",
            "credit_in_account_currency",
            "against_voucher_type",
            "against_voucher",
        ],
        order_by="account asc, name asc",
    )
    if not gl_rows:
        frappe.throw(
            f"Journal Entry {settlement_document} has no submitted GL evidence",
            frappe.ValidationError,
        )

    expected_by_account = {
        cstr(tender["account"]).strip(): int(tender["amount_sen"])
        for tender in tenders
    }
    tender_credit_by_account = {account: 0 for account in expected_by_account}
    customer_debit_sen = 0
    receivable_account = cstr(_value(original_invoice, "debit_to")).strip()
    customer = cstr(_value(original_invoice, "customer")).strip()

    for row in gl_rows:
        account = cstr(_row_value(row, "account")).strip()
        debit_sen, credit_sen = _gl_amounts_sen(row, account)
        if debit_sen == 0 and credit_sen == 0:
            continue

        is_customer_settlement = (
            account == receivable_account
            and cstr(_row_value(row, "party_type")).strip() == "Customer"
            and cstr(_row_value(row, "party")).strip() == customer
            and cstr(_row_value(row, "against_voucher_type")).strip()
            == "Sales Invoice"
            and cstr(_row_value(row, "against_voucher")).strip()
            == cstr(return_invoice.name).strip()
        )
        if is_customer_settlement:
            if debit_sen <= 0 or credit_sen != 0:
                frappe.throw(
                    f"Journal Entry {settlement_document} customer settlement must be a pure debit",
                    frappe.ValidationError,
                )
            customer_debit_sen += debit_sen
            continue

        if account in tender_credit_by_account:
            has_party_or_reference = any(
                cstr(_row_value(row, fieldname)).strip()
                for fieldname in (
                    "party_type",
                    "party",
                    "against_voucher_type",
                    "against_voucher",
                )
            )
            if debit_sen != 0 or credit_sen <= 0 or has_party_or_reference:
                frappe.throw(
                    f"Journal Entry {settlement_document} tender settlement must be an unreferenced pure credit",
                    frappe.ValidationError,
                )
            tender_credit_by_account[account] += credit_sen
            continue

        frappe.throw(
            f"Journal Entry {settlement_document} contains an unexpected GL posting on {account or 'an unnamed account'}",
            frappe.ValidationError,
        )

    if tender_credit_by_account != expected_by_account:
        frappe.throw(
            f"Journal Entry {settlement_document} tender GL credits do not match the original tender accounts",
            frappe.ValidationError,
        )
    if customer_debit_sen != total_sen:
        frappe.throw(
            f"Journal Entry {settlement_document} does not settle the customer credit note",
            frappe.ValidationError,
        )

    return_invoice.reload()
    outstanding_sen = _money_to_sen(
        _value(return_invoice, "outstanding_amount") or 0,
        "return Sales Invoice outstanding_amount",
    )
    if outstanding_sen != 0:
        frappe.throw(
            f"Return Sales Invoice {return_invoice.name} remains unsettled",
            frappe.ValidationError,
        )

    tender_credit_sen = sum(tender_credit_by_account.values())
    return {
        "settlement_doctype": SETTLEMENT_DOCTYPE,
        "settlement_document": settlement_document,
        "settlement_status": SETTLEMENT_STATUS,
        "settlement_amount_sen": total_sen,
        "tender_credit_sen": tender_credit_sen,
        "customer_debit_sen": customer_debit_sen,
        "return_outstanding_sen": outstanding_sen,
        "tenders": tenders,
    }


def _find_existing_settlement(return_doc: Any) -> str | None:
    linked_doctype = cstr(_value(return_doc, "settlement_doctype")).strip()
    linked_document = cstr(_value(return_doc, "settlement_document")).strip()
    if linked_document:
        if linked_doctype != SETTLEMENT_DOCTYPE:
            frappe.throw(
                f"FB Return Event {return_doc.name} references unsupported settlement doctype {linked_doctype}",
                frappe.ValidationError,
            )
        return linked_document

    recovered = frappe.db.get_value(
        SETTLEMENT_DOCTYPE,
        {"custom_fb_return_event": return_doc.name, "docstatus": 1},
        "name",
    )
    return cstr(recovered).strip() or None


def _record_settlement(
    return_doc: Any,
    settlement_document: str,
    total_sen: int,
    tenders: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> None:
    serialized_tenders = json.dumps(
        {
            "refund_method": cstr(_value(return_doc, "refund_method")).strip(),
            "settlement_amount_sen": total_sen,
            "customer_debit_sen": int(evidence["customer_debit_sen"]),
            "return_outstanding_sen": int(evidence["return_outstanding_sen"]),
            "tenders": tenders,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    _set_doc_field(return_doc, "settlement_doctype", SETTLEMENT_DOCTYPE)
    _set_doc_field(return_doc, "settlement_document", settlement_document)
    _set_doc_field(return_doc, "settlement_status", SETTLEMENT_STATUS)
    _set_doc_field(return_doc, "settlement_amount", _sen_to_amount(total_sen))
    _set_doc_field(return_doc, "settlement_tenders_json", serialized_tenders)


def _require_settlement_link_field() -> None:
    meta = frappe.get_meta(SETTLEMENT_DOCTYPE)
    if not meta.has_field("custom_fb_return_event"):
        frappe.throw(
            "Journal Entry custom_fb_return_event is missing; run bench migrate before processing refunds",
            frappe.ValidationError,
        )


def _categorize_tender(mode_of_payment: str, payment_type: str) -> str:
    token = f"{mode_of_payment} {payment_type}".strip().lower()
    if "voucher" in token:
        return "voucher"
    if "duitnow" in token or " qr" in f" {token}" or token.startswith("qr"):
        return "qr"
    if "card" in token:
        return "card"
    if payment_type.strip().lower() == "cash" or mode_of_payment.strip().lower() == "cash":
        return "cash"
    return ""


def _gl_amounts_sen(row: Any, account: str) -> tuple[int, int]:
    debit_value = _row_value(row, "debit_in_account_currency")
    credit_value = _row_value(row, "credit_in_account_currency")
    if debit_value is None and credit_value is None:
        debit_value = _row_value(row, "debit")
        credit_value = _row_value(row, "credit")
    return (
        _money_to_sen(debit_value or 0, f"{account} GL debit"),
        _money_to_sen(credit_value or 0, f"{account} GL credit"),
    )


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


def _coerce_doc(doctype: str, value: Any):
    if not value:
        return None
    if getattr(value, "doctype", None) == doctype:
        return value
    return frappe.get_doc(doctype, value)


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
    return getattr(row, fieldname, None)


def _set_doc_field(doc: Any, fieldname: str, value: Any) -> None:
    db_set = getattr(doc, "db_set", None)
    if callable(db_set):
        db_set(fieldname, value, update_modified=False)
    else:
        setattr(doc, fieldname, value)
