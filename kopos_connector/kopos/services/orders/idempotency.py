from __future__ import annotations

import frappe


def find_existing_order(idempotency_key: str) -> str | None:
    if not idempotency_key:
        return None
    existing = frappe.db.get_value(
        "FB Order",
        {"external_idempotency_key": idempotency_key},
        "name",
    )
    return str(existing) if existing else None
