# pyright: reportMissingImports=false

"""Exact GL, Account, and stock queries for Maybank UAT evidence."""

from __future__ import annotations

from typing import Any, Callable

import frappe
from frappe.utils import cint, cstr

from kopos_connector.acceptance.maybank_uat_common import (
    AcceptanceContext,
    canonical_decimal,
    canonical_json_sha256,
    decimal_money_to_sen,
)


BUSINESS_STATE_SOURCE = "kopos_connector.acceptance.business_state"
ACCEPTANCE_FIXTURE = "maybank_qr_zero_tax_single_income_v1"
GL_QUERY_FIELDS = (
    "name",
    "posting_date",
    "account",
    "account_currency",
    "debit",
    "credit",
    "debit_in_account_currency",
    "credit_in_account_currency",
    "party_type",
    "party",
    "against",
    "voucher_type",
    "voucher_no",
    "against_voucher_type",
    "against_voucher",
    "cost_center",
    "project",
    "remarks",
    "is_cancelled",
)
GL_QUERY_ORDER = "posting_date asc, creation asc, name asc"


def _value(row: Any, fieldname: str) -> Any:
    if isinstance(row, dict):
        return row.get(fieldname)
    return getattr(row, fieldname, None)


def _fail(message: str) -> None:
    frappe.throw(message, frappe.ValidationError)
    raise AssertionError("frappe.throw must raise")


def _exact_text(value: Any, fieldname: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        _fail(f"{fieldname} must be a nonempty exact value")
    return value


def _nullable_text(value: Any) -> str | None:
    text = cstr(value).strip()
    return text or None


def _get_value(
    doctype: str,
    name: str,
    fields: tuple[str, ...],
) -> dict[str, Any]:
    row = frappe.db.get_value(
        doctype,
        name,
        list(fields),
        as_dict=True,
    )
    if not row:
        _fail(f"{doctype} evidence is missing")
    return {
        fieldname: _value(row, fieldname)
        for fieldname in fields
    }


def _get_all(
    doctype: str,
    *,
    filters: dict[str, Any],
    fields: tuple[str, ...],
    order_by: str,
) -> list[dict[str, Any]]:
    rows = frappe.get_all(
        doctype,
        filters=filters,
        fields=list(fields),
        order_by=order_by,
        limit_page_length=0,
    )
    return [
        {
            fieldname: _value(row, fieldname)
            for fieldname in fields
        }
        for row in (rows or [])
    ]


def _count(doctype: str, filters: dict[str, Any]) -> int:
    value = frappe.db.count(doctype, filters=filters)
    count = cint(value)
    if count < 0 or cstr(value).strip() != cstr(count):
        _fail(f"{doctype} count is invalid")
    return count


def _account_rows(
    account_names: set[str],
    context: AcceptanceContext,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    fields = ("name", "account_type", "account_currency", "company")
    rows = _get_all(
        "Account",
        filters={"name": ["in", sorted(account_names)]},
        fields=fields,
        order_by="name asc",
    )
    by_name = {
        cstr(row.get("name")): row
        for row in rows
    }
    if set(by_name) != account_names or len(by_name) != len(rows):
        _fail("The settlement Account lookup is missing or ambiguous")
    output: list[dict[str, Any]] = []
    for name in sorted(by_name):
        row = by_name[name]
        account_type = _exact_text(
            row.get("account_type"),
            f"Account {name} account_type",
        )
        if (
            cstr(row.get("account_currency")).upper() != context.currency
            or cstr(row.get("company")) != context.company
        ):
            _fail(f"Account {name} belongs to another company or currency")
        output.append(
            {
                "name": name,
                "accountType": account_type,
                "accountCurrency": context.currency,
                "company": context.company,
            }
        )
    return output, by_name


def settlement_gl_state(
    context: AcceptanceContext,
    invoice: dict[str, Any],
    income_account: str,
    payment_account: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    invoice_name = cstr(invoice.get("name"))
    filters = {
        "voucher_type": "Sales Invoice",
        "voucher_no": invoice_name,
    }
    raw_rows = _get_all(
        "GL Entry",
        filters=filters,
        fields=GL_QUERY_FIELDS,
        order_by=GL_QUERY_ORDER,
    )
    total_count = _count("GL Entry", filters)
    if total_count != len(raw_rows) or total_count != 4:
        _fail("The acceptance Sales Invoice must have exactly four GL rows")
    account_names = {
        _exact_text(row.get("account"), "GL Entry account")
        for row in raw_rows
    }
    accounts, accounts_by_name = _account_rows(account_names, context)

    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows):
        account_name = cstr(raw.get("account"))
        account = accounts_by_name[account_name]
        debit_sen = decimal_money_to_sen(
            raw.get("debit"),
            f"GL Entry {index} debit",
        )
        credit_sen = decimal_money_to_sen(
            raw.get("credit"),
            f"GL Entry {index} credit",
        )
        debit_account_sen = decimal_money_to_sen(
            raw.get("debit_in_account_currency"),
            f"GL Entry {index} debit_in_account_currency",
        )
        credit_account_sen = decimal_money_to_sen(
            raw.get("credit_in_account_currency"),
            f"GL Entry {index} credit_in_account_currency",
        )
        if (
            (debit_sen > 0) == (credit_sen > 0)
            or debit_sen != debit_account_sen
            or credit_sen != credit_account_sen
            or cstr(raw.get("account_currency")).upper()
            != context.currency
            or cstr(raw.get("voucher_type")) != "Sales Invoice"
            or cstr(raw.get("voucher_no")) != invoice_name
            or cint(raw.get("is_cancelled")) != 0
        ):
            _fail(f"GL Entry {index} is malformed or outside the sale")
        rows.append(
            {
                "name": _exact_text(
                    raw.get("name"),
                    f"GL Entry {index} name",
                ),
                "postingDate": _exact_text(
                    cstr(raw.get("posting_date")),
                    f"GL Entry {index} posting_date",
                ),
                "account": account_name,
                "accountCurrency": context.currency,
                "accountType": cstr(account.get("account_type")),
                "debitSen": debit_sen,
                "creditSen": credit_sen,
                "debitInAccountCurrencySen": debit_account_sen,
                "creditInAccountCurrencySen": credit_account_sen,
                "partyType": _nullable_text(raw.get("party_type")),
                "party": _nullable_text(raw.get("party")),
                "against": _exact_text(
                    raw.get("against"),
                    f"GL Entry {index} against",
                ),
                "voucherType": "Sales Invoice",
                "voucherNo": invoice_name,
                "againstVoucherType": _nullable_text(
                    raw.get("against_voucher_type")
                ),
                "againstVoucher": _nullable_text(
                    raw.get("against_voucher")
                ),
                "costCenter": _nullable_text(raw.get("cost_center")),
                "project": _nullable_text(raw.get("project")),
                "remarks": _exact_text(
                    raw.get("remarks"),
                    f"GL Entry {index} remarks",
                ),
                "isCancelled": False,
            }
        )

    customer = cstr(invoice.get("customer"))
    debit_to = cstr(invoice.get("debit_to"))
    expected: tuple[Callable[[dict[str, Any]], bool], ...] = (
        lambda row: (
            row["account"] == debit_to
            and row["accountType"] == "Receivable"
            and row["debitSen"] == context.amount_sen
            and row["creditSen"] == 0
            and row["partyType"] == "Customer"
            and row["party"] == customer
            and row["against"] == income_account
            and row["againstVoucherType"] == "Sales Invoice"
            and row["againstVoucher"] == invoice_name
        ),
        lambda row: (
            row["account"] == income_account
            and row["accountType"] == "Income Account"
            and row["debitSen"] == 0
            and row["creditSen"] == context.amount_sen
            and row["partyType"] is None
            and row["party"] is None
            and row["against"] == customer
            and row["againstVoucherType"] is None
            and row["againstVoucher"] is None
        ),
        lambda row: (
            row["account"] == payment_account
            and row["accountType"] == "Bank"
            and row["debitSen"] == context.amount_sen
            and row["creditSen"] == 0
            and row["partyType"] is None
            and row["party"] is None
            and row["against"] == customer
            and row["againstVoucherType"] is None
            and row["againstVoucher"] is None
        ),
        lambda row: (
            row["account"] == debit_to
            and row["accountType"] == "Receivable"
            and row["debitSen"] == 0
            and row["creditSen"] == context.amount_sen
            and row["partyType"] == "Customer"
            and row["party"] == customer
            and row["against"] == payment_account
            and row["againstVoucherType"] == "Sales Invoice"
            and row["againstVoucher"] == invoice_name
        ),
    )
    if not (
        all(sum(1 for row in rows if matcher(row)) == 1 for matcher in expected)
        and all(
            sum(1 for matcher in expected if matcher(row)) == 1
            for row in rows
        )
        and sum(row["debitSen"] for row in rows)
        == context.amount_sen * 2
        and sum(row["creditSen"] for row in rows)
        == context.amount_sen * 2
    ):
        _fail(
            "The full Sales Invoice GL result is not the exact balanced Bank settlement"
        )

    identity = {
        "schemaVersion": "1",
        "source": BUSINESS_STATE_SOURCE,
        "acceptanceFixture": ACCEPTANCE_FIXTURE,
        "doctype": "GL Entry",
        "filters": filters,
        "fields": list(GL_QUERY_FIELDS),
        "orderBy": GL_QUERY_ORDER,
        "accountTypeLookup": {
            "doctype": "Account",
            "keyField": "name",
            "valueField": "account_type",
        },
        "resultProjection": "integer_sen_camel_case_with_account_type_v1",
        "erpArtifactSha256": context.bindings.erp_artifact_sha256,
        "runNonce": context.bindings.run_nonce,
    }
    query_sha256 = canonical_json_sha256(identity)
    query = {
        "schemaVersion": "1",
        "identity": identity,
        "querySha256": query_sha256,
        "totalRowCount": total_count,
        "returnedRowCount": len(rows),
        "complete": True,
        "resultSha256": canonical_json_sha256(rows),
    }
    return query, rows, accounts


def stock_state(
    context: AcceptanceContext,
    order: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    stock_entry_name = _exact_text(
        order.get("ingredient_stock_entry"),
        "FB Order ingredient_stock_entry",
    )
    fields = (
        "name",
        "docstatus",
        "purpose",
        "stock_entry_type",
        "company",
        "custom_fb_order",
    )
    stock_entry = _get_value("Stock Entry", stock_entry_name, fields)
    if (
        cint(stock_entry.get("docstatus")) != 1
        or cstr(stock_entry.get("purpose")) != "Material Issue"
        or cstr(stock_entry.get("stock_entry_type")) != "Material Issue"
        or cstr(stock_entry.get("company")) != context.company
        or cstr(stock_entry.get("custom_fb_order")) != cstr(order.get("name"))
    ):
        _fail("The FB Order ingredient stock issue is invalid")
    stock_entries = [
        {
            "name": stock_entry_name,
            "fbOrder": cstr(order.get("name")),
            "salesInvoice": cstr(order.get("sales_invoice")),
            "docstatus": 1,
            "purpose": "Material Issue",
            "stockEntryType": "Material Issue",
            "company": context.company,
        }
    ]

    ledger_fields = (
        "name",
        "voucher_type",
        "voucher_no",
        "is_cancelled",
        "actual_qty",
    )
    raw_ledger = _get_all(
        "Stock Ledger Entry",
        filters={
            "voucher_type": "Stock Entry",
            "voucher_no": stock_entry_name,
        },
        fields=ledger_fields,
        order_by="posting_date asc, posting_time asc, creation asc, name asc",
    )
    if not raw_ledger:
        _fail("The ingredient stock issue has no Stock Ledger Entry")
    ledger: list[dict[str, Any]] = []
    for index, row in enumerate(raw_ledger):
        actual_qty = canonical_decimal(
            row.get("actual_qty"),
            f"Stock Ledger Entry {index} actual_qty",
        )
        if (
            cstr(row.get("voucher_type")) != "Stock Entry"
            or cstr(row.get("voucher_no")) != stock_entry_name
            or cint(row.get("is_cancelled")) != 0
            or not actual_qty.startswith("-")
            or actual_qty in {"-0", "0"}
        ):
            _fail(
                f"Stock Ledger Entry {index} does not prove ingredient issue"
            )
        ledger.append(
            {
                "name": _exact_text(
                    row.get("name"),
                    f"Stock Ledger Entry {index} name",
                ),
                "voucherType": "Stock Entry",
                "voucherNo": stock_entry_name,
                "isCancelled": False,
                "actualQty": actual_qty,
            }
        )
    return stock_entries, ledger
