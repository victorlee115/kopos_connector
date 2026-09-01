# pyright: reportMissingImports=false

"""Exact ERP money rendered for JiJi's beginner pages."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cstr

from kopos_connector.kopos.api.money_contract import (
    persisted_money_to_sen,
    sen_to_decimal,
)


def company_currency(company: str) -> str:
    return cstr(frappe.db.get_value("Company", company, "default_currency") or "MYR").strip()


def money_sen(value: Any, fieldname: str) -> int:
    return persisted_money_to_sen(value, fieldname)


def money_text(value_sen: int, currency: str = "MYR") -> str:
    code = cstr(currency or "MYR").strip().upper()
    prefix = "RM" if code == "MYR" else code
    return f"{prefix} {sen_to_decimal(value_sen):,.2f}"
