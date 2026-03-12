# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

import frappe
from frappe import _


def create_sample_modifiers():
    """
    Create sample modifier groups for testing/demo purposes

    Creates:
    - Size (Small/Medium/Large)
    - Milk Type (Regular/Oat/Almond/Soy)
    - Ice Level (No Ice/Less Ice/Normal/Extra Ice)
    - Sugar Level (No Sugar/25%/50%/75%/100%)
    - Add-ons (Boba, Jelly, Pudding)
    """

    sample_groups = [
        {
            "group_name": "Size",
            "selection_type": "single",
            "is_required": 1,
            "min_selections": 0,
            "max_selections": 1,
            "display_order": 1,
            "options": [
                {
                    "option_name": "Small",
                    "price_adjustment": 0,
                    "is_default": 1,
                    "display_order": 1,
                },
                {
                    "option_name": "Medium",
                    "price_adjustment": 1.50,
                    "is_default": 0,
                    "display_order": 2,
                },
                {
                    "option_name": "Large",
                    "price_adjustment": 3.00,
                    "is_default": 0,
                    "display_order": 3,
                },
            ],
        },
        {
            "group_name": "Milk Type",
            "selection_type": "single",
            "is_required": 1,
            "min_selections": 0,
            "max_selections": 1,
            "display_order": 2,
            "options": [
                {
                    "option_name": "Regular Milk",
                    "price_adjustment": 0,
                    "is_default": 1,
                    "display_order": 1,
                },
                {
                    "option_name": "Oat Milk",
                    "price_adjustment": 2.00,
                    "is_default": 0,
                    "display_order": 2,
                },
                {
                    "option_name": "Almond Milk",
                    "price_adjustment": 2.00,
                    "is_default": 0,
                    "display_order": 3,
                },
                {
                    "option_name": "Soy Milk",
                    "price_adjustment": 2.00,
                    "is_default": 0,
                    "display_order": 4,
                },
            ],
        },
        {
            "group_name": "Ice Level",
            "selection_type": "single",
            "is_required": 0,
            "min_selections": 0,
            "max_selections": 1,
            "display_order": 3,
            "options": [
                {
                    "option_name": "No Ice",
                    "price_adjustment": 0,
                    "is_default": 0,
                    "display_order": 1,
                },
                {
                    "option_name": "Less Ice",
                    "price_adjustment": 0,
                    "is_default": 0,
                    "display_order": 2,
                },
                {
                    "option_name": "Normal Ice",
                    "price_adjustment": 0,
                    "is_default": 1,
                    "display_order": 3,
                },
                {
                    "option_name": "Extra Ice",
                    "price_adjustment": 0,
                    "is_default": 0,
                    "display_order": 4,
                },
            ],
        },
        {
            "group_name": "Sugar Level",
            "selection_type": "single",
            "is_required": 0,
            "min_selections": 0,
            "max_selections": 1,
            "display_order": 4,
            "options": [
                {
                    "option_name": "No Sugar (0%)",
                    "price_adjustment": 0,
                    "is_default": 0,
                    "display_order": 1,
                },
                {
                    "option_name": "25% Sugar",
                    "price_adjustment": 0,
                    "is_default": 0,
                    "display_order": 2,
                },
                {
                    "option_name": "50% Sugar",
                    "price_adjustment": 0,
                    "is_default": 0,
                    "display_order": 3,
                },
                {
                    "option_name": "75% Sugar",
                    "price_adjustment": 0,
                    "is_default": 0,
                    "display_order": 4,
                },
                {
                    "option_name": "100% Sugar",
                    "price_adjustment": 0,
                    "is_default": 1,
                    "display_order": 5,
                },
            ],
        },
        {
            "group_name": "Add-ons",
            "selection_type": "multiple",
            "is_required": 0,
            "min_selections": 0,
            "max_selections": 3,
            "display_order": 5,
            "options": [
                {
                    "option_name": "Boba (Pearls)",
                    "price_adjustment": 2.00,
                    "is_default": 0,
                    "display_order": 1,
                },
                {
                    "option_name": "Grass Jelly",
                    "price_adjustment": 1.50,
                    "is_default": 0,
                    "display_order": 2,
                },
                {
                    "option_name": "Egg Pudding",
                    "price_adjustment": 2.50,
                    "is_default": 0,
                    "display_order": 3,
                },
                {
                    "option_name": "Red Bean",
                    "price_adjustment": 1.50,
                    "is_default": 0,
                    "display_order": 4,
                },
                {
                    "option_name": "Whipped Cream",
                    "price_adjustment": 1.00,
                    "is_default": 0,
                    "display_order": 5,
                },
            ],
        },
    ]

    created_count = 0
    skipped_count = 0

    for group_data in sample_groups:
        # Check if modifier group already exists
        if frappe.db.exists("KoPOS Modifier Group", group_data["group_name"]):
            skipped_count += 1
            continue

        # Create modifier group
        group = frappe.new_doc("KoPOS Modifier Group")
        group.group_name = group_data["group_name"]
        group.selection_type = group_data["selection_type"]
        group.is_required = group_data["is_required"]
        group.min_selections = group_data["min_selections"]
        group.max_selections = group_data["max_selections"]
        group.display_order = group_data["display_order"]
        group.is_active = 1

        # Add options
        for option_data in group_data["options"]:
            group.append(
                "options",
                {
                    "option_name": option_data["option_name"],
                    "price_adjustment": option_data["price_adjustment"],
                    "is_default": option_data["is_default"],
                    "display_order": option_data["display_order"],
                    "is_active": 1,
                },
            )

        group.save()
        created_count += 1

    frappe.db.commit()

    message = f"Sample modifiers created: {created_count}"
    if skipped_count > 0:
        message += f", skipped (already exist): {skipped_count}"

    frappe.msgprint(_(message), title="Sample Modifiers Created")

    return {"created": created_count, "skipped": skipped_count}


@frappe.whitelist()
def link_sample_modifiers_to_items():
    """
    Link sample modifiers to all active items for testing
    This is useful for quickly testing the modifier flow
    """

    # Get all modifier groups
    modifier_groups = frappe.get_all(
        "KoPOS Modifier Group", filters={"is_active": 1}, pluck="name"
    )

    if not modifier_groups:
        frappe.throw(
            _("No active modifier groups found. Please create sample modifiers first.")
        )

    # Get all active items
    items = frappe.get_all(
        "Item", filters={"is_sales_item": 1, "disabled": 0}, pluck="name"
    )

    if not items:
        frappe.throw(_("No active items found."))

    updated_count = 0

    for item_code in items:
        item = frappe.get_doc("Item", item_code)

        # Clear existing modifier groups
        item.modifier_groups = []

        # Add all modifier groups
        for idx, group_name in enumerate(modifier_groups):
            item.append(
                "modifier_groups",
                {
                    "modifier_group": group_name,
                    "display_order": idx,
                    "always_prompt": 0,
                },
            )

        item.save()
        updated_count += 1

    frappe.db.commit()

    frappe.msgprint(
        _("Linked {0} modifier groups to {1} items").format(
            len(modifier_groups), updated_count
        ),
        title="Modifiers Linked",
    )

    return {"modifier_groups": len(modifier_groups), "items_updated": updated_count}
