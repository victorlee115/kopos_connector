from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import cstr


class FBInventoryPolicy(Document):
    """Own one outlet's cutover identity and automation lifecycle."""

    _CUTOVER_FIELDS = (
        "company",
        "warehouse",
        "inventory_contract_version",
        "cutover_token",
        "cutover_at",
        "opening_stock_reconciliation",
    )

    def validate(self) -> None:
        token = cstr(getattr(self, "cutover_token", None)).strip()
        cutover_at = getattr(self, "cutover_at", None)
        if bool(token) != bool(cutover_at):
            frappe.throw(
                "Inventory cutover token and cutover time must be provided together",
                frappe.ValidationError,
            )
        if cstr(getattr(self, "automation_state", None)).strip() == "Active" and not token:
            frappe.throw(
                "Inventory automation cannot be Active before an immutable cutover is recorded",
                frappe.ValidationError,
            )
        if cstr(getattr(self, "automation_state", None)).strip() == "Active" and not cstr(getattr(self, "opening_stock_reconciliation", None)).strip():
            frappe.throw(
                "Inventory automation cannot be Active before the opening Stock Reconciliation is submitted",
                frappe.ValidationError,
            )
        max_age = int(getattr(self, "max_source_age_minutes", 30) or 30)
        if max_age < 1 or max_age > 24 * 60:
            frappe.throw("Maximum source age must be between 1 and 1440 minutes", frappe.ValidationError)

        before = self.get_doc_before_save()
        previous_token = cstr(getattr(before, "cutover_token", None)).strip() if before else ""
        if not previous_token:
            return
        changed = [
            fieldname
            for fieldname in self._CUTOVER_FIELDS
            if str(getattr(before, fieldname, None) or "")
            != str(getattr(self, fieldname, None) or "")
        ]
        if changed:
            frappe.throw(
                "Activated inventory cutover identity is immutable; changed fields: "
                + ", ".join(changed),
                frappe.ValidationError,
            )
