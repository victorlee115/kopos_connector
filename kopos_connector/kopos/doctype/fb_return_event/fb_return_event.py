import frappe
from frappe.model.document import Document

from kopos_connector.kopos.services.operations.return_guard_service import (
    lock_and_validate_return_quantities,
)
from kopos_connector.kopos.services.operations.return_service import (
    process_return_event,
)


class FBReturnEvent(Document):
    def validate(self):
        if not self.return_id:
            frappe.throw("FB Return Event requires return_id")
        if not self.get("lines"):
            frappe.throw("FB Return Event requires at least one line")

    def before_submit(self):
        lock_and_validate_return_quantities(
            self.return_id,
            [
                {
                    "original_resolved_sale": line.original_resolved_sale,
                    "qty_returned": line.qty_returned,
                }
                for line in self.get("lines") or []
            ],
        )

    def on_submit(self):
        process_return_event(self)
        self.db_set("status", "Submitted", update_modified=False)
