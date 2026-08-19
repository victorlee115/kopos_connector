import frappe
from frappe.model.document import Document

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
        # Historical rows remain readable, but this custom document can no
        # longer move stock. Older tablets use ``process_refill`` which creates
        # one Draft standard Material Request for director review; current
        # tablets execute only the two-stage guided transfer flow.
        frappe.throw(
            "Legacy refill submission is retired. Create a standard Draft Material Request for Transfer instead.",
            frappe.ValidationError,
        )
