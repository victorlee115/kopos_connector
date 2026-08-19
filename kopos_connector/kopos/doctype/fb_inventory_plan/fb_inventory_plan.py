from __future__ import annotations

import frappe
from frappe.model.document import Document


class FBInventoryPlan(Document):
    """Persisted, reviewable snapshot for one replenishment decision."""

    def validate(self) -> None:
        if self.status == "Executed" and not self.execution_fingerprint:
            frappe.throw("Executed inventory plans require an execution fingerprint")
