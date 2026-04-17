from __future__ import annotations

import frappe


def count_failed_projection_logs() -> int:
    return int(frappe.db.count("FB Projection Log", {"state": "Failed"}))
