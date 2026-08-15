from __future__ import annotations

import hashlib
import json

import frappe
from frappe.model.document import Document
from frappe.utils import cstr


class FBInventoryAvailabilityRule(Document):
    """Persist the outlet's explicit stock-driven availability policy."""

    def autoname(self) -> None:
        """Give new rules a bounded identity that includes the outlet scope.

        Older deployments used ``FB-RULE-{target_type}-{target_id}``, which
        made the same Item impossible to configure at a second warehouse.
        Existing names are retained; new names use a deterministic composite
        digest and the validator below remains the durable uniqueness check.
        """

        if cstr(getattr(self, "name", None)).strip():
            return
        self.name = rule_identity(
            target_type=self.target_type,
            target_id=self.target_id,
            company=self.company,
            warehouse=self.warehouse,
        )

    def validate(self) -> None:
        values = {
            "target_type": cstr(getattr(self, "target_type", None)).strip(),
            "target_id": cstr(getattr(self, "target_id", None)).strip(),
            "company": cstr(getattr(self, "company", None)).strip(),
            "warehouse": cstr(getattr(self, "warehouse", None)).strip(),
        }
        if values["target_type"] not in {"Item", "Modifier"}:
            frappe.throw("Availability rule target type must be Item or Modifier", frappe.ValidationError)
        if not all(values.values()):
            frappe.throw("Availability rule target, company, and warehouse are required", frappe.ValidationError)
        duplicate = frappe.db.exists(
            "FB Inventory Availability Rule",
            {
                **values,
                "name": ["!=", cstr(getattr(self, "name", None)).strip()],
            },
        )
        if duplicate:
            frappe.throw(
                "Only one availability rule is allowed for this target at this company and warehouse",
                frappe.ValidationError,
            )


def rule_identity(*, target_type: str, target_id: str, company: str, warehouse: str) -> str:
    """Return a deterministic, bounded identity for one scoped rule."""

    values = tuple(cstr(value).strip() for value in (company, warehouse, target_type, target_id))
    digest = hashlib.sha256(json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()
    return f"FB-RULE-{digest[:32]}"
