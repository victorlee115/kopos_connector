# pyright: reportMissingImports=false

"""Read a persisted money value as exact sen, and refuse anything else.

`kopos/api/money_contract.py` owns the rules and is deliberately framework-free.
This is the one place that states them as Frappe validation errors, so a service
does not have to keep its own copy of the same five lines.

Two kinds of stored value need reading, and they are not the same thing:

- a Currency field, which Frappe hands back as a decimal and which is empty when
  the document simply has none (an invoice with no write-off) — `money_to_sen`;
- an Int field already counted in sen, which Frappe may hand back as an integral
  Decimal rather than an int — `integer_sen`, and `positive_sen` where zero is
  not a legitimate amount.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import frappe
from frappe.utils import cstr

from kopos_connector.kopos.api.money_contract import (
    MAX_SAFE_INTEGER,
    MoneyContractValidationError,
    persisted_money_to_sen,
)


def money_to_sen(value: Any, fieldname: str) -> int:
    """Read a persisted currency amount as exact sen; absent means zero."""

    try:
        return persisted_money_to_sen(
            0 if value is None or value == "" else value,
            fieldname,
        )
    except MoneyContractValidationError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error


def integer_sen(value: Any, fieldname: str) -> int:
    """Read a stored Int sen value that Frappe may materialize as a Decimal."""

    if isinstance(value, bool) or value is None:
        frappe.throw(
            f"{fieldname} must be an integer number of sen",
            frappe.ValidationError,
        )
    try:
        amount = Decimal(cstr(value).strip())
    except (InvalidOperation, ValueError) as error:
        frappe.throw(
            f"{fieldname} must be an integer number of sen",
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise") from error
    if not amount.is_finite() or amount != amount.to_integral_value():
        frappe.throw(
            f"{fieldname} must be an integer number of sen",
            frappe.ValidationError,
        )
    if abs(amount) > MAX_SAFE_INTEGER:
        frappe.throw(
            f"{fieldname} must be a safe integer number of sen",
            frappe.ValidationError,
        )
    return int(amount)


def positive_sen(value: Any, fieldname: str) -> int:
    """Read a stored Int sen value that must be greater than zero."""

    resolved = integer_sen(value, fieldname)
    if resolved <= 0:
        frappe.throw(
            f"{fieldname} must be greater than 0",
            frappe.ValidationError,
        )
    return resolved
