from __future__ import annotations

from typing import Any

import frappe


def create_waste_stock_entry(
    company: str, warehouse: str, items: list[dict[str, Any]]
) -> str:
    doc = frappe.new_doc("Stock Entry")
    doc.company = company
    doc.stock_entry_type = "Material Issue"
    doc.purpose = "Material Issue"
    for item in items:
        row = dict(item)
        row.setdefault("s_warehouse", warehouse)
        doc.append("items", row)
    doc.insert(ignore_permissions=True)
    doc.submit()
    return doc.name
