from __future__ import annotations

import frappe


LEGACY_REPORT_NAME = "Modifier Sales Analytics"
LEGACY_SCHEDULED_METHOD = "kopos_connector.api.modifiers.aggregate_modifier_stats"


def execute() -> None:
    """Disable historical UI/scheduler surfaces without deleting audit data."""
    frappe.db.delete("Scheduled Job Type", {"method": LEGACY_SCHEDULED_METHOD})

    if frappe.db.exists("Report", LEGACY_REPORT_NAME):
        frappe.db.set_value(
            "Report",
            LEGACY_REPORT_NAME,
            "disabled",
            1,
            update_modified=False,
        )
