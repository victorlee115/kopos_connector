from __future__ import annotations

from typing import Any

import frappe


def create_material_transfer(
    company: str,
    from_warehouse: str,
    to_warehouse: str,
    items: list[dict[str, Any]],
) -> str:
    doc = frappe.new_doc("Stock Entry")
    doc.company = company
    doc.stock_entry_type = "Material Transfer"
    doc.purpose = "Material Transfer"
    for item in items:
        row = dict(item)
        row.setdefault("s_warehouse", from_warehouse)
        row.setdefault("t_warehouse", to_warehouse)
        row.setdefault(
            "basic_rate", _resolve_basic_rate(row.get("item_code"), from_warehouse)
        )
        doc.append("items", row)
    doc.insert(ignore_permissions=True)
    doc.submit()
    return doc.name


def _resolve_basic_rate(item_code: Any, warehouse: str) -> float:
    if not item_code:
        return 0.0
    valuation_rate = frappe.db.get_value(
        "Bin",
        {"item_code": item_code, "warehouse": warehouse},
        "valuation_rate",
    )
    if valuation_rate not in (None, ""):
        return float(valuation_rate)
    item_values = frappe.db.get_value(
        "Item",
        item_code,
        ["valuation_rate", "standard_rate"],
        as_dict=True,
    )
    if item_values:
        if item_values.get("valuation_rate") not in (None, ""):
            return float(item_values.get("valuation_rate") or 0)
        if item_values.get("standard_rate") not in (None, ""):
            return float(item_values.get("standard_rate") or 0)
    return 0.0
