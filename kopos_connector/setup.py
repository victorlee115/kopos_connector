# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

import frappe
from frappe import _


def _sample_group_code(group_name: str) -> str:
    slug = "-".join(group_name.lower().replace("&", "and").split())
    return f"sample-{slug}"


def _sample_modifier_code(group_name: str, option_name: str) -> str:
    group_slug = "-".join(group_name.lower().replace("&", "and").split())
    option_slug = "-".join(option_name.lower().replace("&", "and").split())
    return f"sample-{group_slug}-{option_slug}"


def _ensure_sample_fb_modifier_group(group_data: dict) -> tuple[str, bool]:
    group_code = _sample_group_code(group_data["group_name"])
    group_name = frappe.db.get_value(
        "FB Modifier Group", {"group_code": group_code}, "name"
    )
    created = False

    if group_name:
        group_doc = frappe.get_doc("FB Modifier Group", group_name)
        changed = False
        group_payload = {
            "group_code": group_code,
            "group_name": group_data["group_name"],
            "selection_type": "Multiple"
            if str(group_data["selection_type"]).lower() == "multiple"
            else "Single",
            "is_required": group_data["is_required"],
            "min_selection": group_data["min_selections"],
            "max_selection": group_data["max_selections"],
            "display_order": group_data["display_order"],
            "active": 1,
            "default_resolution_policy": "Auto Apply Default",
        }
        for fieldname, value in group_payload.items():
            if getattr(group_doc, fieldname, None) != value:
                setattr(group_doc, fieldname, value)
                changed = True
        if changed:
            group_doc.save(ignore_permissions=True)
    else:
        group_doc = frappe.new_doc("FB Modifier Group")
        group_doc.group_code = group_code
        group_doc.group_name = group_data["group_name"]
        group_doc.selection_type = (
            "Multiple"
            if str(group_data["selection_type"]).lower() == "multiple"
            else "Single"
        )
        group_doc.is_required = group_data["is_required"]
        group_doc.min_selection = group_data["min_selections"]
        group_doc.max_selection = group_data["max_selections"]
        group_doc.display_order = group_data["display_order"]
        group_doc.active = 1
        group_doc.default_resolution_policy = "Auto Apply Default"
        group_doc.insert(ignore_permissions=True)
        created = True

    for option_data in group_data["options"]:
        _ensure_sample_fb_modifier(
            group_doc.name, group_data["group_name"], option_data
        )

    return group_doc.name, created


def _ensure_sample_fb_modifier(
    group_name: str, group_label: str, option_data: dict
) -> str:
    modifier_code = _sample_modifier_code(group_label, option_data["option_name"])
    existing_name = frappe.db.get_value(
        "FB Modifier", {"modifier_code": modifier_code}, "name"
    )
    modifier_payload = {
        "modifier_code": modifier_code,
        "modifier_name": option_data["option_name"],
        "modifier_group": group_name,
        "kind": "Instruction Only",
        "price_adjustment": option_data["price_adjustment"],
        "is_default": option_data["is_default"],
        "display_order": option_data["display_order"],
        "active": 1,
    }

    if existing_name:
        modifier_doc = frappe.get_doc("FB Modifier", existing_name)
        changed = False
        for fieldname, value in modifier_payload.items():
            if getattr(modifier_doc, fieldname, None) != value:
                setattr(modifier_doc, fieldname, value)
                changed = True
        if changed:
            modifier_doc.save(ignore_permissions=True)
        return modifier_doc.name

    modifier_doc = frappe.new_doc("FB Modifier")
    for fieldname, value in modifier_payload.items():
        setattr(modifier_doc, fieldname, value)
    modifier_doc.insert(ignore_permissions=True)
    return modifier_doc.name


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
        _group_name, created = _ensure_sample_fb_modifier_group(group_data)
        if created:
            created_count += 1
        else:
            skipped_count += 1

    frappe.db.commit()

    message = f"Sample modifiers created: {created_count}"
    if skipped_count > 0:
        message += f", skipped (already exist): {skipped_count}"

    frappe.msgprint(_(message), title="Sample Modifiers Created")

    return {"created": created_count, "skipped": skipped_count}


@frappe.whitelist()
def link_sample_modifiers_to_items():
    modifier_groups = frappe.get_all(
        "FB Modifier Group", filters={"active": 1}, pluck="name"
    )

    if not modifier_groups:
        frappe.throw(
            _("No active modifier groups found. Please create sample modifiers first.")
        )

    recipes = frappe.get_all("FB Recipe", filters={"status": "Active"}, pluck="name")

    if not recipes:
        frappe.throw(_("No active FB recipes found."))

    updated_count = 0

    for recipe_name in recipes:
        recipe = frappe.get_doc("FB Recipe", recipe_name)
        changed = False
        for idx, group_name in enumerate(modifier_groups, start=1):
            existing_row = next(
                (
                    row
                    for row in (recipe.get("allowed_modifier_groups") or [])
                    if getattr(row, "modifier_group", None) == group_name
                ),
                None,
            )
            if existing_row:
                if getattr(existing_row, "display_order", None) != idx:
                    existing_row.display_order = idx
                    changed = True
                continue

            recipe.append(
                "allowed_modifier_groups",
                {
                    "modifier_group": group_name,
                    "display_order": idx,
                    "always_prompt": 0,
                },
            )
            changed = True

        if changed:
            recipe.save(ignore_permissions=True)
            updated_count += 1

    frappe.db.commit()

    frappe.msgprint(
        _("Linked {0} modifier groups to {1} recipes").format(
            len(modifier_groups), updated_count
        ),
        title="Modifiers Linked",
    )

    return {"modifier_groups": len(modifier_groups), "recipes_updated": updated_count}
