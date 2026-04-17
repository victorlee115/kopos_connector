from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def create_fb_custom_fields():
    custom_fields = {
        "Item": [
            {
                "fieldname": "custom_fb_item_role",
                "label": "F&B Item Role",
                "fieldtype": "Select",
                "options": "\nSellable Drink\nIngredient\nPrep Item\nPackaging\nTool\nAsset Managed Gear",
                "insert_after": "item_group",
                "translatable": 0,
            },
            {
                "fieldname": "custom_fb_recipe_required",
                "label": "F&B Recipe Required",
                "fieldtype": "Check",
                "default": "0",
                "insert_after": "custom_fb_item_role",
                "translatable": 0,
            },
            {
                "fieldname": "custom_fb_default_recipe",
                "label": "F&B Default Recipe",
                "fieldtype": "Link",
                "options": "FB Recipe",
                "insert_after": "custom_fb_recipe_required",
                "depends_on": "eval:doc.custom_fb_recipe_required == 1",
                "translatable": 0,
            },
            {
                "fieldname": "custom_fb_track_theoretical_stock",
                "label": "Track Theoretical Stock",
                "fieldtype": "Check",
                "default": "0",
                "insert_after": "custom_fb_default_recipe",
                "translatable": 0,
            },
        ],
        "Sales Invoice": [
            {
                "fieldname": "custom_fb_order",
                "label": "F&B Order",
                "fieldtype": "Link",
                "options": "FB Order",
                "insert_after": "customer",
                "read_only": 1,
                "translatable": 0,
            },
            {
                "fieldname": "custom_fb_shift",
                "label": "F&B Shift",
                "fieldtype": "Link",
                "options": "FB Shift",
                "insert_after": "custom_fb_order",
                "read_only": 1,
                "translatable": 0,
            },
            {
                "fieldname": "custom_fb_device_id",
                "label": "Device ID",
                "fieldtype": "Data",
                "insert_after": "custom_fb_shift",
                "read_only": 1,
                "translatable": 0,
            },
            {
                "fieldname": "custom_fb_event_project",
                "label": "Event/Project",
                "fieldtype": "Link",
                "options": "Project",
                "insert_after": "custom_fb_device_id",
                "read_only": 1,
                "translatable": 0,
            },
            {
                "fieldname": "custom_fb_idempotency_key",
                "label": "Idempotency Key",
                "fieldtype": "Data",
                "insert_after": "custom_fb_event_project",
                "read_only": 1,
                "unique": 1,
                "translatable": 0,
            },
            {
                "fieldname": "custom_fb_operational_status",
                "label": "Operational Status",
                "fieldtype": "Select",
                "options": "\nDraft\nSubmitted\nCancelled",
                "insert_after": "custom_fb_idempotency_key",
                "read_only": 1,
                "translatable": 0,
            },
        ],
        "Sales Invoice Item": [
            {
                "fieldname": "custom_fb_order_line_ref",
                "label": "F&B Order Line Ref",
                "fieldtype": "Data",
                "insert_after": "item_name",
                "read_only": 1,
                "translatable": 0,
            },
            {
                "fieldname": "custom_fb_resolved_sale",
                "label": "F&B Resolved Sale",
                "fieldtype": "Link",
                "options": "FB Resolved Sale",
                "insert_after": "custom_fb_order_line_ref",
                "read_only": 1,
                "translatable": 0,
            },
            {
                "fieldname": "custom_fb_recipe_snapshot_json",
                "label": "Recipe Snapshot",
                "fieldtype": "Code",
                "options": "JSON",
                "insert_after": "custom_fb_resolved_sale",
                "read_only": 1,
                "translatable": 0,
            },
            {
                "fieldname": "custom_fb_resolution_hash",
                "label": "Resolution Hash",
                "fieldtype": "Data",
                "insert_after": "custom_fb_recipe_snapshot_json",
                "read_only": 1,
                "translatable": 0,
            },
        ],
        "Stock Entry": [
            {
                "fieldname": "custom_fb_order",
                "label": "F&B Order",
                "fieldtype": "Link",
                "options": "FB Order",
                "insert_after": "stock_entry_type",
                "read_only": 1,
                "translatable": 0,
            },
            {
                "fieldname": "custom_fb_resolved_sale",
                "label": "F&B Resolved Sale",
                "fieldtype": "Link",
                "options": "FB Resolved Sale",
                "insert_after": "custom_fb_order",
                "read_only": 1,
                "translatable": 0,
            },
            {
                "fieldname": "custom_fb_event_project",
                "label": "Event/Project",
                "fieldtype": "Link",
                "options": "Project",
                "insert_after": "custom_fb_resolved_sale",
                "read_only": 1,
                "translatable": 0,
            },
            {
                "fieldname": "custom_fb_shift",
                "label": "F&B Shift",
                "fieldtype": "Link",
                "options": "FB Shift",
                "insert_after": "custom_fb_event_project",
                "read_only": 1,
                "translatable": 0,
            },
            {
                "fieldname": "custom_fb_reason_code",
                "label": "Reason Code",
                "fieldtype": "Data",
                "insert_after": "custom_fb_shift",
                "read_only": 1,
                "translatable": 0,
            },
        ],
    }

    create_custom_fields(custom_fields)


def remove_fb_custom_fields():
    fields_to_remove = [
        ("Item", "custom_fb_item_role"),
        ("Item", "custom_fb_recipe_required"),
        ("Item", "custom_fb_default_recipe"),
        ("Item", "custom_fb_track_theoretical_stock"),
        ("Sales Invoice", "custom_fb_order"),
        ("Sales Invoice", "custom_fb_shift"),
        ("Sales Invoice", "custom_fb_device_id"),
        ("Sales Invoice", "custom_fb_event_project"),
        ("Sales Invoice", "custom_fb_idempotency_key"),
        ("Sales Invoice", "custom_fb_operational_status"),
        ("Sales Invoice Item", "custom_fb_order_line_ref"),
        ("Sales Invoice Item", "custom_fb_resolved_sale"),
        ("Sales Invoice Item", "custom_fb_recipe_snapshot_json"),
        ("Sales Invoice Item", "custom_fb_resolution_hash"),
        ("Stock Entry", "custom_fb_order"),
        ("Stock Entry", "custom_fb_resolved_sale"),
        ("Stock Entry", "custom_fb_event_project"),
        ("Stock Entry", "custom_fb_shift"),
        ("Stock Entry", "custom_fb_reason_code"),
    ]

    for doctype, fieldname in fields_to_remove:
        try:
            frappe.db.delete("Custom Field", {"name": f"{doctype}-{fieldname}"})
        except Exception:
            pass

    frappe.db.commit()
