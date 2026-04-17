from __future__ import annotations

from typing import Any

import frappe


def get_available_stock(item_code: str, warehouse: str) -> float:
    qty = frappe.db.get_value(
        "Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
    )
    return float(qty or 0)


def detect_stock_shortfall(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    shortfalls: list[dict[str, Any]] = []
    for component in components:
        warehouse = component.get("warehouse")
        item_code = component.get("item")
        needed = float(component.get("stock_qty") or component.get("qty") or 0)
        if not warehouse or not item_code or needed <= 0:
            continue
        available = get_available_stock(str(item_code), str(warehouse))
        if available < needed:
            shortfalls.append(
                {
                    "item": item_code,
                    "warehouse": warehouse,
                    "available": available,
                    "needed": needed,
                }
            )
    return shortfalls
