from __future__ import annotations

from typing import Any

import frappe

from kopos_connector.api.devices import elevate_device_api_user
from kopos_connector.kopos.services.accounting.return_invoice_service import (
    create_return_sales_invoice,
)
from kopos_connector.kopos.services.inventory.stock_reversal_service import (
    create_reversal_stock_entry,
)


def process_return_event(doc: Any) -> tuple[str | None, str | None]:
    with elevate_device_api_user():
        return_invoice = create_return_sales_invoice(doc)
        reversal_entry = create_reversal_stock_entry(doc)
    _update_resolved_sale_statuses(doc)
    return return_invoice, reversal_entry


def _update_resolved_sale_statuses(doc: Any) -> None:
    for line in doc.get("lines") or []:
        resolved_sale_name = getattr(line, "original_resolved_sale", None)
        qty_returned = float(getattr(line, "qty_returned", 0) or 0)
        if not resolved_sale_name or qty_returned <= 0:
            continue
        resolved_sale = frappe.get_doc("FB Resolved Sale", resolved_sale_name)
        total_qty = float(getattr(resolved_sale, "qty", 0) or 0)
        next_status = "Returned" if qty_returned >= total_qty else "Partially Returned"
        resolved_sale.db_set("status", next_status, update_modified=False)
