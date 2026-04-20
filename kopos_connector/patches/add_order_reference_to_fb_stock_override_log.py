# pyright: reportMissingImports=false

from __future__ import annotations

import frappe


def execute() -> None:
    if not frappe.db.exists("DocType", "FB Stock Override Log"):
        return

    frappe.reload_doc("kopos_connector", "doctype", "fb_stock_override_log")
    if frappe.db.has_column("FB Stock Override Log", "order_reference"):
        return

    frappe.db.sql(
        """
        ALTER TABLE `tabFB Stock Override Log`
        ADD COLUMN `order_reference` varchar(140)
        """
    )
    frappe.db.commit()
