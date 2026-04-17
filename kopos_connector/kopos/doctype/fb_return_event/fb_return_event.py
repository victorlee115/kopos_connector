import frappe
from frappe.model.document import Document

from kopos_connector.kopos.services.operations.return_service import (
    process_return_event,
)


class FBReturnEvent(Document):
    def validate(self):
        if not self.return_id:
            frappe.throw("FB Return Event requires return_id")
        if not self.get("lines"):
            frappe.throw("FB Return Event requires at least one line")

    def on_submit(self):
        process_return_event(self)
        self.db_set("status", "Submitted", update_modified=False)
