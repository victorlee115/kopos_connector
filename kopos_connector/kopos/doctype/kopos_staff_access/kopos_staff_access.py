from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import cint, cstr

from kopos_connector.kopos.services.inventory_autopilot.staff_access import (
    central_staff_access_signature,
    invalidate_devices_for_staff_access,
    staff_identity_issue,
)
from kopos_connector.utils.pin import hash_pin, is_supported_pin_hash


class KoPOSStaffAccess(Document):
    """Central POS access record; only a one-way PIN verifier is stored."""

    def validate(self) -> None:
        self._validate_director_assignment_scope()
        if cstr(self.access_level).strip() not in {"Staff", "Manager"}:
            frappe.throw("POS access level must be Staff or Manager")
        if not self.user or not self.employee:
            frappe.throw("POS staff access requires both a User and Employee")
        if cint(self.active):
            identity_issue = staff_identity_issue(
                user=cstr(self.user).strip(),
                employee=cstr(self.employee).strip(),
            )
            if identity_issue:
                frappe.throw(identity_issue, frappe.ValidationError)
        if not self.outlet_assignments:
            frappe.throw("Assign at least one outlet before activating POS access")
        pin = cstr(getattr(self, "pin", None)).strip()
        pin_hash = cstr(getattr(self, "pin_hash", None)).strip()
        if pin:
            pin_hash = hash_pin(pin)
        if cint(self.active) and not is_supported_pin_hash(pin_hash):
            frappe.throw("Active POS staff access requires a supported PIN verifier")
        self.pin_hash = pin_hash or None
        self.pin = None
        previous = self.get_doc_before_save() if hasattr(self, "get_doc_before_save") else None
        if previous is None:
            self.revision = max(1, cint(getattr(self, "revision", 0)))
        elif central_staff_access_signature(previous) != central_staff_access_signature(self):
            self.revision = max(1, cint(getattr(previous, "revision", 0))) + 1

    def _validate_director_assignment_scope(self) -> None:
        """A director may never move a global staff record outside their cafés."""

        user = cstr(getattr(frappe.session, "user", None)).strip()
        roles = {cstr(role).strip() for role in frappe.get_roles(user)}
        if "System Manager" in roles or "Company Director" not in roles:
            return
        from kopos_connector.api.director_scope import allowed_companies

        permitted = set(allowed_companies(user))
        assigned = {
            cstr(getattr(row, "company", None)).strip()
            for row in list(self.outlet_assignments or ())
            if cstr(getattr(row, "company", None)).strip()
        }
        employee_company = cstr(
            frappe.db.get_value("Employee", cstr(self.employee).strip(), "company")
        ).strip()
        if not assigned or not assigned.issubset(permitted) or (
            employee_company and employee_company not in permitted
        ):
            frappe.throw(
                "You may give tablet access only within your permitted company.",
                frappe.PermissionError,
            )

    def after_insert(self) -> None:
        # A central record changes the authority source for every bound
        # tablet.  The helper bumps each exact device config and clears its
        # previous inventory report so a stale tablet cannot remain trusted.
        invalidate_devices_for_staff_access(self)

    def on_update(self) -> None:
        previous = self.get_doc_before_save() if hasattr(self, "get_doc_before_save") else None
        if previous is not None and central_staff_access_signature(previous) != central_staff_access_signature(self):
            invalidate_devices_for_staff_access(self, previous=previous, force=True)
