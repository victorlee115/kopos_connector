from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import cstr, get_datetime, now_datetime


class FBAvailabilityHold(Document):
    def validate(self) -> None:
        for fieldname in ("target_type", "target_id", "company", "warehouse", "source", "reason_code", "reason_label", "idempotency_key"):
            if not cstr(getattr(self, fieldname, None)).strip():
                frappe.throw(f"Availability hold requires {fieldname}")
        if self.source not in {"manual", "safety", "quality", "equipment", "automation"}:
            frappe.throw("Availability hold source is invalid")
        if self.expires_at and get_datetime(self.expires_at) <= now_datetime():
            frappe.throw("Availability hold expiry must be in the future")
