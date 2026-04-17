import frappe
from frappe.model.document import Document

from kopos_connector.kopos.services.operations.refill_service import (
    fulfill_refill_request,
)


class FBBoothRefillRequest(Document):
    def validate(self):
        if not self.request_id:
            frappe.throw("FB Booth Refill Request requires request_id")
        if not self.company:
            frappe.throw("FB Booth Refill Request requires company")
        if not self.from_warehouse or not self.to_warehouse:
            frappe.throw(
                "FB Booth Refill Request requires from_warehouse and to_warehouse"
            )
        if not self.get("lines"):
            frappe.throw("FB Booth Refill Request requires at least one line")

    def on_submit(self):
        fulfilled_stock_entry = fulfill_refill_request(self)
        self.db_set(
            "fulfilled_stock_entry", fulfilled_stock_entry, update_modified=False
        )
        self.db_set("status", "Fulfilled", update_modified=False)
