# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

import frappe


def before_uninstall():
    """
    Pre-uninstall cleanup
    Remove custom fields before uninstalling app
    """
    try:
        # Remove custom fields
        remove_custom_fields()
        frappe.logger().info("KoPOS Connector: Custom fields removed successfully")
    except Exception as e:
        frappe.log_error(
            title="KoPOS Connector: Failed to remove custom fields during uninstall",
            message=frappe.get_traceback(),
        )


def remove_custom_fields():
    """Remove all custom fields created by KoPOS Connector"""
    custom_field_names = [
        # Item fields
        "Item-kopos_availability_section",
        "Item-custom_kopos_availability_mode",
        "Item-custom_kopos_track_stock",
        "Item-custom_kopos_min_qty",
        "Item-custom_kopos_is_prep_item",
        "Item-kopos_modifiers_section",
        "Item-custom_kopos_modifier_groups",
        # Legacy field name (without custom_ prefix) - remove duplicates
        "Item-kopos_modifier_groups",
        # POS Profile fields
        "POS Profile-kopos_sst_section",
        "POS Profile-custom_kopos_enable_sst",
        "POS Profile-custom_kopos_sst_rate",
        # POS Invoice fields
        "POS Invoice-custom_kopos_idempotency_key",
        "POS Invoice-custom_kopos_device_id",
        "POS Invoice-custom_kopos_refund_reason_code",
        "POS Invoice-custom_kopos_refund_reason",
        "POS Invoice-custom_kopos_promotion_snapshot_version",
        "POS Invoice-custom_kopos_pricing_mode",
        "POS Invoice-custom_kopos_promotion_reconciliation_status",
        "POS Invoice-custom_kopos_promotion_payload",
        "POS Invoice-custom_kopos_promotion_review_status",
        "POS Invoice-custom_kopos_promotion_review_decision",
        "POS Invoice-custom_kopos_promotion_reviewed_by",
        "POS Invoice-custom_kopos_promotion_reviewed_at",
        "POS Invoice-custom_kopos_promotion_review_notes",
        # POS Invoice Item fields (modifiers)
        "POS Invoice Item-custom_kopos_promotion_allocation",
        "POS Invoice Item-custom_kopos_modifiers",
        "POS Invoice Item-custom_kopos_modifier_total",
        "POS Invoice Item-custom_kopos_has_modifiers",
        "POS Invoice Item-custom_kopos_modifiers_table",
        # POS Opening/Closing fields
        "POS Opening Entry-custom_kopos_idempotency_key",
        "POS Opening Entry-custom_kopos_shift_id",
        "POS Opening Entry-custom_kopos_device_id",
        "POS Closing Entry-custom_kopos_idempotency_key",
        "POS Closing Entry-custom_kopos_shift_id",
        "POS Closing Entry-custom_kopos_device_id",
        # Sales Invoice fields
        "Sales Invoice-custom_kopos_refund_idempotency_key",
        "Sales Invoice-custom_kopos_device_id",
    ]

    for field_name in custom_field_names:
        try:
            frappe.delete_doc("Custom Field", field_name, ignore_missing=True)
        except Exception:
            pass  # Field might not exist

    frappe.db.commit()
