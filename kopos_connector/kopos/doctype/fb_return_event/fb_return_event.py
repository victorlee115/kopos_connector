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
        for index, line in enumerate(self.get("lines") or [], start=1):
            invoice_item = str(
                getattr(line, "original_sales_invoice_item", "") or ""
            ).strip()
            resolved_sale = str(
                getattr(line, "original_resolved_sale", "") or ""
            ).strip()
            if not invoice_item and not resolved_sale:
                frappe.throw(
                    f"FB Return Event line {index} requires a commercial or legacy sale identity",
                    frappe.ValidationError,
                )
            # Sales Invoice Item is the immutable commercial identity. Older
            # invoices may predate FB Order line references, so that reference
            # is useful secondary evidence but is never required for a locked,
            # exact full-invoice refund.
        if self.refund_method not in {"cash", "qr", "card", "voucher"}:
            frappe.throw(
                "FB Return Event refund_method must be cash, qr, card, or voucher",
                frappe.ValidationError,
            )

    def before_submit(self):
        lock_and_validate_return_quantities(
            self.return_id,
            [
                {
                    "original_sales_invoice_item": getattr(
                        line, "original_sales_invoice_item", None
                    ),
                    "original_fb_order_line_ref": getattr(
                        line, "original_fb_order_line_ref", None
                    ),
                    "original_resolved_sale": line.original_resolved_sale,
                    "qty_returned": line.qty_returned,
                    "commercial_modifier_snapshot_json": getattr(
                        line, "commercial_modifier_snapshot_json", None
                    ),
                }
                for line in self.get("lines") or []
            ],
            self.original_sales_invoice,
        )

    def on_submit(self):
        process_return_event(self)
        self.db_set("status", "Submitted", update_modified=False)
