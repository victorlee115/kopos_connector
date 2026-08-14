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
