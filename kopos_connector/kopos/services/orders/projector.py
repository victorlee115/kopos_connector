from __future__ import annotations

from typing import Any


def project_order(doc: Any) -> tuple[str | None, str | None]:
    sales_invoice = (
        doc.create_projection_entry("Sales Invoice")
        if hasattr(doc, "create_projection_entry")
        else None
    )
    stock_issue = (
        doc.create_projection_entry("Stock Issue")
        if hasattr(doc, "create_projection_entry")
        else None
    )
    return sales_invoice, stock_issue
