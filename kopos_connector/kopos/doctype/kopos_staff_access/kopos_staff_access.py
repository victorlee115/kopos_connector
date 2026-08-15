from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import cstr


class KoPOSStaffAccess(Document):
    """Central POS access record; the PIN verifier is never stored here."""

    def validate(self) -> None:
        if cstr(self.access_level).strip() not in {"Staff", "Manager"}:
            frappe.throw("POS access level must be Staff or Manager")
        if not self.user or not self.employee:
            frappe.throw("POS staff access requires both a User and Employee")
        if not self.outlet_assignments:
            frappe.throw("Assign at least one outlet before activating POS access")
