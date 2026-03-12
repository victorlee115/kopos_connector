# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, flt


class KoPOSPromotion(Document):
    def validate(self):
        self.validate_date_range()
        self.validate_outlet_scope()
        self.validate_discount_rule()
        self.set_defaults()

    def set_defaults(self):
        if not self.display_label:
            self.display_label = self.promotion_name
        if not self.customer_message and self.discount_type == "percentage":
            if self.promotion_type in {"nth_item_discount", "buy_x_get_y"}:
                self.customer_message = _(
                    "{0}% off the cheaper drink. Add-ons excluded."
                ).format(flt(self.discount_value))
            elif self.promotion_type == "order_discount":
                self.customer_message = _("{0}% off the order total.").format(
                    flt(self.discount_value)
                )

    def validate_date_range(self):
        if self.valid_from and self.valid_upto and self.valid_from > self.valid_upto:
            frappe.throw(
                _("Valid Up To must be on or after Valid From"),
                frappe.ValidationError,
            )

    def validate_outlet_scope(self):
        if (
            cstr(self.outlet_scope_mode or "all_pos_profiles")
            != "selected_pos_profiles"
        ):
            return
        if not any(cstr(row.pos_profile) for row in (self.eligible_pos_profiles or [])):
            frappe.throw(
                _("Selected POS profile scope requires at least one POS Profile"),
                frappe.ValidationError,
            )

    def validate_discount_rule(self):
        if (
            self.discount_type in {"percentage", "fixed_amount", "fixed_price"}
            and flt(self.discount_value) <= 0
        ):
            frappe.throw(
                _("Discount Value must be greater than 0"),
                frappe.ValidationError,
            )

        if self.promotion_type in {"nth_item_discount", "buy_x_get_y"}:
            if int(self.buy_qty or 0) <= 0:
                frappe.throw(
                    _("Buy Qty must be greater than 0"), frappe.ValidationError
                )
            if int(self.discount_qty or 0) <= 0:
                frappe.throw(
                    _("Discount Qty must be greater than 0"), frappe.ValidationError
                )
