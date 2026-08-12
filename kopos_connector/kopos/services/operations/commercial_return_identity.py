# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe
from frappe.utils import cstr


def build_full_commercial_return_lines(
    original_sales_invoice: str,
    fb_order: str | None,
) -> list[dict[str, Any]]:
    """Bind a full refund to immutable invoice and order-line evidence only."""
    del fb_order
    invoice_items = frappe.get_all(
        "Sales Invoice Item",
        filters={
            "parent": original_sales_invoice,
            "parenttype": "Sales Invoice",
            "parentfield": "items",
        },
        fields=[
            "name",
            "qty",
            "custom_fb_order_line_ref",
        ],
        order_by="idx asc, name asc",
    )
    if not invoice_items:
        return []

    commercial_lines: list[dict[str, Any]] = []
    for invoice_item in invoice_items:
        invoice_item_name = cstr(_row_value(invoice_item, "name")).strip()
        order_line_ref = cstr(
            _row_value(invoice_item, "custom_fb_order_line_ref")
        ).strip()
        if not invoice_item_name:
            return []
        invoice_qty = abs(
            _quantity_decimal(
                _row_value(invoice_item, "qty"),
                f"Sales Invoice Item {invoice_item_name} quantity",
            )
        )
        if invoice_qty <= 0:
            frappe.throw(
                f"Sales Invoice Item {invoice_item_name} quantity must be positive",
                frappe.ValidationError,
            )

        commercial_lines.append(
            {
                "original_sales_invoice_item": invoice_item_name,
                "original_fb_order_line_ref": order_line_ref or None,
                # Sales Invoice Item identity is sufficient commercial proof.
                # Never make the public refund path depend on an optional
                # recipe-era FB Resolved Sale row.
                "original_resolved_sale": None,
                "qty_returned": float(invoice_qty),
                # Modifier decoration is deliberately absent from refund
                # authority. The credit note copies any valid invoice display
                # fields independently, without parsing them here.
                "commercial_modifier_snapshot_json": "",
            }
        )
    return commercial_lines


def _quantity_decimal(value: Any, label: str) -> Decimal:
    try:
        return Decimal(cstr(value or 0).strip() or "0")
    except (InvalidOperation, ValueError) as error:
        frappe.throw(f"Invalid {label}: {value}", frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)
