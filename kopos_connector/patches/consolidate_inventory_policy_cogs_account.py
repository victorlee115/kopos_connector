# pyright: reportMissingImports=false

"""Collapse the two inventory COGS account fields into one.

FB Inventory Policy carried both ``expense_account`` (labelled "Expense / COGS
Account") and ``cogs_account``. Only one consumer read them, and it preferred
``expense_account`` and silently fell back to ``cogs_account`` -- two persisted
answers to a single question about where ingredient consumption is booked.

``expense_account`` wins because it is the field the projection worker already
reads first and it names the ERPNext Stock Entry concept it maps onto. Any value
that survives only in ``cogs_account`` is copied across before the field is
retired so no director's configuration is lost.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr


def execute() -> None:
    if not frappe.db.exists("DocType", "FB Inventory Policy"):
        return
    meta = frappe.get_meta("FB Inventory Policy")
    if not meta.has_field("cogs_account"):
        return

    rows = frappe.get_all(
        "FB Inventory Policy",
        fields=["name", "expense_account", "cogs_account"],
    )
    migrated = 0
    for row in rows:
        expense_account = cstr(row.get("expense_account")).strip()
        cogs_account = cstr(row.get("cogs_account")).strip()
        if expense_account or not cogs_account:
            continue
        frappe.db.set_value(
            "FB Inventory Policy",
            row["name"],
            "expense_account",
            cogs_account,
            update_modified=False,
        )
        migrated += 1

    if migrated:
        frappe.logger().info(
            "consolidate_inventory_policy_cogs_account: moved %s cogs_account value(s) into expense_account",
            migrated,
        )
