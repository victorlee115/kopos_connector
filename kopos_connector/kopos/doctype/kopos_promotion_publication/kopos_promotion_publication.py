"""Immutable result of one user-approved publication, including retry identity."""
import frappe
from frappe.model.document import Document


class KoPOSPromotionPublication(Document):
    def validate(self):
        if not self.is_new():
            frappe.throw("An offer publication receipt cannot be changed", frappe.ValidationError)

    def on_trash(self):
        frappe.throw("Retain offer publication receipts so retries cannot repeat old actions", frappe.ValidationError)
