import frappe
from frappe.model.document import Document

from kopos_connector.kopos.services.inventory.waste_service import (
    create_waste_stock_entry,
)


class FBWasteEvent(Document):
    def validate(self):
        if not self.waste_id:
            frappe.throw("FB Waste Event requires waste_id")
        if not self.company:
            frappe.throw("FB Waste Event requires company")
        if not self.warehouse:
            frappe.throw("FB Waste Event requires warehouse")
        if not self.get("lines"):
            frappe.throw("FB Waste Event requires at least one line")

    def on_submit(self):
        # A waste event that already posted its Material Issue must never post a
        # second one. The stored link is checked first, and the service itself is
        # keyed on this event's identity so a replay adopts the existing entry.
        existing = frappe.db.get_value("FB Waste Event", self.name, "stock_entry")
        if existing:
            frappe.throw(
                frappe._("This waste event already issued stock on {0}").format(existing),
                frappe.ValidationError,
            )
        items = []
        for line in self.get("lines") or []:
            items.append(
                {
                    "item_code": line.item,
                    "qty": line.qty,
                    "qty_decimal": getattr(line, "qty_decimal", None),
                    "uom": line.uom,
                    "cost_center": getattr(line, "cost_center", None),
                }
            )
        stock_entry = create_waste_stock_entry(
            self.company,
            self.warehouse,
            items,
            waste_id=self.waste_id,
        )
        self.db_set("stock_entry", stock_entry, update_modified=False)
        self.db_set("status", "Submitted", update_modified=False)
