# pyright: reportMissingImports=false

"""One way to post and prove a two-account QR Journal Entry.

Both QR ledgers — suspense reclassification and duplicate-payment accounting — move one
exact amount between two accounts under an idempotency key, then prove the submitted GL
says exactly that. The mechanism lives here once; each caller keeps its own custom
fields, extra validations and result.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import cint, cstr

JOURNAL_ENTRY_DOCTYPE = "Journal Entry"
SEN_PER_UNIT = Decimal("100")


class JournalSide:
    """One side of the move: the ledger account and, when it matters, its cost center."""

    __slots__ = ("account", "cost_center")

    def __init__(self, account: str, cost_center: str = "") -> None:
        self.account = cstr(account).strip()
        self.cost_center = cstr(cost_center).strip()


def create_or_recover_journal(
    *,
    company: str,
    posting_date: Any,
    user_remark: str,
    fields: dict[str, Any],
    debit: JournalSide,
    credit: JournalSide,
    amount_sen: int,
    recover: Callable[[], str | None],
    savepoint: str,
) -> str:
    """Submit one two-account Journal Entry, or recover the one this key already has."""

    savepoint_created = False
    savepoint_fn = getattr(frappe.db, "savepoint", None)
    if callable(savepoint_fn):
        try:
            savepoint_fn(savepoint)
            savepoint_created = True
        except Exception:
            savepoint_created = False

    try:
        journal = frappe.new_doc(JOURNAL_ENTRY_DOCTYPE)
        journal.voucher_type = JOURNAL_ENTRY_DOCTYPE
        journal.company = company
        journal.posting_date = posting_date
        journal.user_remark = user_remark
        for fieldname, value in fields.items():
            setattr(journal, fieldname, value)
        amount = _sen_to_amount(amount_sen)
        journal.append("accounts", _account_row(debit, debit=amount, credit=Decimal("0")))
        journal.append("accounts", _account_row(credit, debit=Decimal("0"), credit=amount))
        journal.insert(ignore_permissions=True)
        journal.submit()
        if cint(_value(journal, "docstatus")) != 1:
            frappe.throw(
                f"QR Journal Entry {journal.name} was not submitted",
                frappe.ValidationError,
            )
        return cstr(journal.name).strip()
    except Exception as error:
        duplicate_error = getattr(frappe, "DuplicateEntryError", None)
        if not duplicate_error or not isinstance(error, duplicate_error) or not savepoint_created:
            raise
        frappe.db.rollback(save_point=savepoint)
        recovered = cstr(recover() or "").strip()
        if not recovered:
            raise
        return recovered


def prove_journal(
    journal_name: str,
    *,
    label: str,
    expected_fields: dict[str, Any],
    debit: JournalSide,
    credit: JournalSide,
    amount_sen: int,
    amount_field: str = "custom_kopos_qr_amount_sen",
    upper_case_fields: frozenset[str] = frozenset({"custom_kopos_qr_currency"}),
    exact_gl_rows: int | None = None,
) -> Any:
    """Re-prove a submitted Journal Entry: its stamped fields, and the GL it posted."""

    journal = frappe.get_doc(JOURNAL_ENTRY_DOCTYPE, journal_name)
    if cint(_value(journal, "docstatus")) != 1:
        frappe.throw(
            f"{label} Journal Entry {journal_name} is not submitted",
            frappe.ValidationError,
        )
    for fieldname, expected in expected_fields.items():
        actual = cstr(_value(journal, fieldname)).strip()
        if fieldname in upper_case_fields:
            actual = actual.upper()
        if actual != cstr(expected).strip():
            frappe.throw(
                f"Journal Entry {journal_name} {fieldname} does not match {label}",
                frappe.ValidationError,
            )
    if _strict_positive_sen(_value(journal, amount_field), f"Journal Entry {label} amount") != int(amount_sen):
        frappe.throw(
            f"Journal Entry {journal_name} amount does not match {label}",
            frappe.ValidationError,
        )
    _prove_gl(
        journal_name,
        label=label,
        debit=debit,
        credit=credit,
        amount_sen=int(amount_sen),
        exact_gl_rows=exact_gl_rows,
    )
    return journal


def _prove_gl(
    journal_name: str,
    *,
    label: str,
    debit: JournalSide,
    credit: JournalSide,
    amount_sen: int,
    exact_gl_rows: int | None = None,
) -> None:
    query: dict[str, Any] = {
        "filters": {
            "voucher_type": JOURNAL_ENTRY_DOCTYPE,
            "voucher_no": journal_name,
            "is_cancelled": 0,
        },
        "fields": [
            "account",
            "debit",
            "credit",
            "debit_in_account_currency",
            "credit_in_account_currency",
            "cost_center",
        ],
        "order_by": "account asc, name asc",
    }
    if exact_gl_rows is not None:
        # One more than allowed, so an extra row is seen rather than truncated away.
        query["limit_page_length"] = exact_gl_rows + 1
    rows = frappe.get_all("GL Entry", **query)
    if not rows:
        frappe.throw(
            f"Journal Entry {journal_name} has no submitted GL evidence",
            frappe.ValidationError,
        )
    if exact_gl_rows is not None and len(rows) != exact_gl_rows:
        frappe.throw(
            f"Journal Entry {journal_name} must have exactly {exact_gl_rows} active GL rows",
            frappe.ValidationError,
        )
    sides = {side.account: side for side in (debit, credit)}
    observed = {side.account: {"debit_sen": 0, "credit_sen": 0} for side in (debit, credit)}
    for row in rows:
        account = cstr(_value(row, "account")).strip()
        debit_sen, credit_sen = _gl_amounts_sen(row, account, journal_name)
        if debit_sen == 0 and credit_sen == 0:
            continue
        if account not in observed:
            frappe.throw(
                f"Journal Entry {journal_name} contains an unexpected GL account {account}",
                frappe.ValidationError,
            )
        expected_cost_center = sides[account].cost_center
        if expected_cost_center and cstr(_value(row, "cost_center")).strip() != expected_cost_center:
            frappe.throw(
                f"Journal Entry {journal_name} {label} Cost Center does not match",
                frappe.ValidationError,
            )
        observed[account]["debit_sen"] += debit_sen
        observed[account]["credit_sen"] += credit_sen

    if observed[debit.account] != {"debit_sen": amount_sen, "credit_sen": 0}:
        frappe.throw(
            f"Journal Entry {journal_name} does not exactly debit the {label} target account",
            frappe.ValidationError,
        )
    if observed[credit.account] != {"debit_sen": 0, "credit_sen": amount_sen}:
        frappe.throw(
            f"Journal Entry {journal_name} does not exactly credit the {label} source account",
            frappe.ValidationError,
        )


def _gl_amounts_sen(row: Any, account: str, journal_name: str) -> tuple[int, int]:
    """Both QR ledgers post in company currency, so a row that carries both amount pairs
    must carry the same numbers in each."""

    base = (_value(row, "debit"), _value(row, "credit"))
    in_account_currency = (
        _value(row, "debit_in_account_currency"),
        _value(row, "credit_in_account_currency"),
    )
    has_base = any(value is not None for value in base)
    has_account_currency = any(value is not None for value in in_account_currency)
    if not has_base and not has_account_currency:
        return 0, 0
    pair = base if has_base else in_account_currency
    amounts = (
        _money_to_sen(pair[0] or 0, f"{account} GL debit"),
        _money_to_sen(pair[1] or 0, f"{account} GL credit"),
    )
    if has_base and has_account_currency:
        other = (
            _money_to_sen(in_account_currency[0] or 0, f"{account} GL account-currency debit"),
            _money_to_sen(in_account_currency[1] or 0, f"{account} GL account-currency credit"),
        )
        if other != amounts:
            frappe.throw(
                f"Journal Entry {journal_name} base and account-currency GL amounts differ",
                frappe.ValidationError,
            )
    return amounts


def _account_row(side: JournalSide, *, debit: Decimal, credit: Decimal) -> dict[str, Any]:
    row: dict[str, Any] = {
        "account": side.account,
        "debit_in_account_currency": debit,
        "credit_in_account_currency": credit,
        "exchange_rate": 1,
    }
    if side.cost_center:
        row["cost_center"] = side.cost_center
    return row


def _sen_to_amount(amount_sen: Any) -> Decimal:
    return (Decimal(int(amount_sen)) / SEN_PER_UNIT).quantize(Decimal("0.01"))


def _money_to_sen(value: Any, label: str) -> int:
    try:
        amount = Decimal(cstr(value).strip() or "0")
    except Exception as error:
        frappe.throw(f"{label} is not an exact amount", frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    sen = amount * SEN_PER_UNIT
    integral_sen = sen.to_integral_value()
    if sen != integral_sen:
        frappe.throw(f"{label} contains fractional sen", frappe.ValidationError)
    return int(integral_sen)


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


def _value(doc: Any, fieldname: str) -> Any:
    getter = getattr(doc, "get", None)
    if callable(getter):
        value = getter(fieldname)
        if value is not None or (isinstance(doc, dict) and fieldname in doc):
            return value
    return getattr(doc, fieldname, None)
