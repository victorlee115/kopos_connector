from __future__ import annotations

import frappe


def count_pending_stock_reconciliations() -> int:
    return int(frappe.db.count("Stock Reconciliation", {"docstatus": 0}))
