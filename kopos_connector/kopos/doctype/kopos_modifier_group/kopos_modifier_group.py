# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class KoPOSModifierGroup(Document):
    def validate(self):
        self.validate_defaults()
        self.validate_selections()

    def validate_defaults(self):
        if self.selection_type == "single" and self.is_required:
            defaults = [opt for opt in self.options if opt.is_default and opt.is_active]

            if len(defaults) == 0:
                frappe.throw(
                    _(
                        "Single-select required groups must have exactly one default option"
                    ),
                    title=_("Validation Error"),
                )
            elif len(defaults) > 1:
                default_names = ", ".join([d.option_name for d in defaults])
                frappe.throw(
                    _(
                        "Single-select group can only have one default. Found: {0}"
                    ).format(default_names),
                    title=_("Validation Error"),
                )

    def validate_selections(self):
        if self.selection_type == "multiple":
            if self.min_selections and self.max_selections:
                if self.min_selections > self.max_selections:
                    frappe.throw(
                        _("Min Selections cannot be greater than Max Selections"),
                        title=_("Validation Error"),
                    )

            if self.is_required and (
                not self.min_selections or self.min_selections < 1
            ):
                self.min_selections = 1

    def on_trash(self):
        self.check_item_references()

    def check_item_references(self):
        linked_items = frappe.get_all(
            "KoPOS Item Modifier Group",
            filters={"modifier_group": self.name},
            fields=["parent as item_code"],
            limit=5,
        )

        if linked_items:
            items = ", ".join([item.item_code for item in linked_items])
            if len(linked_items) == 5:
                items += " ..."

            frappe.throw(
                _("Cannot delete modifier group. It is linked to items: {0}").format(
                    items
                ),
                title=_("Cannot Delete"),
            )

    @frappe.whitelist()
    def get_active_options(self):
        return [opt for opt in self.options if opt.is_active]


@frappe.whitelist()
def get_modifier_groups_with_options():
    groups = frappe.get_all(
        "KoPOS Modifier Group",
        filters={"is_active": 1},
        fields=[
            "name",
            "group_name",
            "selection_type",
            "is_required",
            "min_selections",
            "max_selections",
            "display_order",
        ],
        order_by="display_order, group_name",
    )

    for group in groups:
        group_doc = frappe.get_doc("KoPOS Modifier Group", group.name)
        group["options"] = [
            {
                "id": opt.name,
                "name": opt.option_name,
                "price_adjustment": opt.price_adjustment or 0,
                "is_default": opt.is_default,
                "display_order": opt.display_order or 0,
            }
            for opt in group_doc.options
            if opt.is_active
        ]

    return groups


@frappe.whitelist()
def get_modifier_group(group_name):
    group = frappe.get_doc("KoPOS Modifier Group", group_name)

    return {
        "name": group.name,
        "group_name": group.group_name,
        "selection_type": group.selection_type,
        "is_required": group.is_required,
        "min_selections": group.min_selections or 0,
        "max_selections": group.max_selections or 1,
        "display_order": group.display_order or 0,
        "options": [
            {
                "id": opt.name,
                "name": opt.option_name,
                "price_adjustment": opt.price_adjustment or 0,
                "is_default": opt.is_default,
                "display_order": opt.display_order or 0,
            }
            for opt in group.options
            if opt.is_active
        ],
    }
