from decimal import Decimal, InvalidOperation

import frappe
from frappe.model.document import Document

from kopos_connector.kopos.services.operations.remake_service import (
    create_remake_stock_entry,
)


class FBRemakeEvent(Document):
    def validate(self):
        if not self.remake_id:
            frappe.throw("FB Remake Event requires remake_id")
        if not self.original_order:
            frappe.throw("FB Remake Event requires original_order")
        if not self.original_order_line:
            frappe.throw("FB Remake Event requires original_order_line")
        try:
            quantity = Decimal(str(self.quantity))
        except (InvalidOperation, TypeError, ValueError):
            quantity = Decimal("0")
        if not quantity.is_finite() or quantity <= 0:
            frappe.throw("FB Remake Event requires a positive quantity")

    def on_submit(self):
        replacement_stock_entry = create_remake_stock_entry(self)
        if replacement_stock_entry:
            self.db_set(
                "replacement_stock_entry",
                replacement_stock_entry,
                update_modified=False,
            )
        self.db_set("status", "Submitted", update_modified=False)
