from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import cint, cstr

from kopos_connector.utils.pin import hash_pin, is_supported_pin_hash


class KoPOSStaffAccess(Document):
    """Central POS access record; only a one-way PIN verifier is stored."""

    def validate(self) -> None:
        if cstr(self.access_level).strip() not in {"Staff", "Manager"}:
            frappe.throw("POS access level must be Staff or Manager")
        if not self.user or not self.employee:
            frappe.throw("POS staff access requires both a User and Employee")
        if not self.outlet_assignments:
            frappe.throw("Assign at least one outlet before activating POS access")
        pin = cstr(getattr(self, "pin", None)).strip()
        pin_hash = cstr(getattr(self, "pin_hash", None)).strip()
        if pin:
            if not pin.isdigit() or len(pin) != 4:
                frappe.throw("POS staff PIN must be exactly 4 digits")
            pin_hash = hash_pin(pin)
        if cint(self.active) and not is_supported_pin_hash(pin_hash):
            frappe.throw("Active POS staff access requires a supported PIN verifier")
        self.pin_hash = pin_hash or None
        self.pin = None
