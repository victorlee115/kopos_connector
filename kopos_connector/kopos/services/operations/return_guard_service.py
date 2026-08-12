# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe
from frappe.utils import cstr


def lock_and_validate_return_quantities(
    return_id: str,
    lines: list[dict[str, Any]],
    original_sales_invoice: str | None = None,
) -> None:
    """Serialize a full commercial return against current submitted return rows."""
    normalized_lines = aggregate_return_lines(lines)
    commercial_lines = [
        line
        for line in normalized_lines
        if cstr(line.get("original_sales_invoice_item")).strip()
    ]
    if len(commercial_lines) != len(normalized_lines):
        frappe.throw(
            "Every cashier return line requires a Sales Invoice item identity",
            frappe.ValidationError,
        )
    _lock_and_validate_commercial_return_quantities(
        return_id,
        commercial_lines,
        original_sales_invoice,
    )


def _lock_and_validate_commercial_return_quantities(
    return_id: str,
    lines: list[dict[str, Any]],
    original_sales_invoice: str | None,
) -> None:
    invoice_name = cstr(original_sales_invoice).strip()
    if not invoice_name:
        frappe.throw(
            "Commercial return lines require original_sales_invoice",
            frappe.ValidationError,
        )

    locked_items = frappe.db.sql(
        """
        SELECT name, qty
        FROM `tabSales Invoice Item`
        WHERE parent = %s
            AND parenttype = 'Sales Invoice'
            AND parentfield = 'items'
        ORDER BY idx, name
        FOR UPDATE
        """,
        (invoice_name,),
        as_dict=True,
    )
    purchased_by_item = {
        cstr(_row_value(row, "name")).strip(): {
            "qty": abs(_to_decimal(_row_value(row, "qty"), "purchased quantity")),
        }
        for row in (locked_items or [])
        if cstr(_row_value(row, "name")).strip()
    }
    requested_by_item = {
        cstr(line.get("original_sales_invoice_item")).strip(): _to_decimal(
            line.get("qty_returned"), "return quantity"
        )
        for line in lines
    }
    if not purchased_by_item or set(requested_by_item) != set(purchased_by_item):
        frappe.throw(
            "Partial ERP returns are not supported; refund lines must exactly match the full Sales Invoice",
            frappe.ValidationError,
        )

    for line in lines:
        invoice_item = cstr(line.get("original_sales_invoice_item")).strip()
        purchased = purchased_by_item[invoice_item]
        if _to_decimal(line.get("qty_returned"), "return quantity") != purchased["qty"]:
            frappe.throw(
                "Partial ERP returns are not supported; refund lines must exactly match the full Sales Invoice",
                frappe.ValidationError,
            )

    invoice_item_names = sorted(purchased_by_item)
    placeholders = ", ".join(["%s"] * len(invoice_item_names))
    submitted_return_rows = frappe.db.sql(
        f"""
        SELECT
            return_line.original_sales_invoice_item,
            return_line.qty_returned,
            return_event.return_id
        FROM `tabFB Return Event Line` AS return_line
        INNER JOIN `tabFB Return Event` AS return_event
            ON return_event.name = return_line.parent
        WHERE return_line.parenttype = 'FB Return Event'
            AND return_event.docstatus = 1
            AND return_line.original_sales_invoice_item IN ({placeholders})
        ORDER BY return_line.original_sales_invoice_item, return_line.name
        FOR UPDATE
        """,
        tuple(invoice_item_names),
        as_dict=True,
    )
    returned_by_item = _aggregate_submitted_returns(
        return_id=return_id,
        rows=submitted_return_rows or [],
        identity_field="original_sales_invoice_item",
    )
    for invoice_item, requested_qty in requested_by_item.items():
        purchased_qty = purchased_by_item[invoice_item]["qty"]
        returned_qty = returned_by_item.get(invoice_item, Decimal("0"))
        if returned_qty + requested_qty > purchased_qty:
            frappe.throw(
                f"Return quantity for Sales Invoice Item {invoice_item} exceeds purchased quantity",
                frappe.ValidationError,
            )


def _lock_and_validate_legacy_return_quantities(
    return_id: str,
    lines: list[dict[str, Any]],
    original_sales_invoice: str | None,
) -> None:
    requested_by_sale = _aggregate_requested_quantities(lines)
    resolved_sale_names = sorted(requested_by_sale)
    if not resolved_sale_names:
        frappe.throw(
            "Return requires at least one FB Resolved Sale",
            frappe.ValidationError,
        )

    if cstr(original_sales_invoice).strip():
        locked_sales = frappe.db.sql(
            """
            SELECT name, qty
            FROM `tabFB Resolved Sale`
            WHERE sales_invoice = %s
            ORDER BY name
            FOR UPDATE
            """,
            (cstr(original_sales_invoice).strip(),),
            as_dict=True,
        )
    else:
        placeholders = ", ".join(["%s"] * len(resolved_sale_names))
        locked_sales = frappe.db.sql(
            f"""
            SELECT name, qty
            FROM `tabFB Resolved Sale`
            WHERE name IN ({placeholders})
            ORDER BY name
            FOR UPDATE
            """,
            tuple(resolved_sale_names),
            as_dict=True,
        )
    purchased_by_sale = {
        cstr(_row_value(row, "name")).strip(): _to_decimal(
            _row_value(row, "qty"), "purchased quantity"
        )
        for row in (locked_sales or [])
        if cstr(_row_value(row, "name")).strip()
    }
    missing_names = set(resolved_sale_names) - set(purchased_by_sale)
    if missing_names:
        frappe.throw(
            "FB Resolved Sale {0} was not found".format(
                ", ".join(sorted(missing_names))
            ),
            frappe.ValidationError,
        )
    if cstr(original_sales_invoice).strip() and (
        set(requested_by_sale) != set(purchased_by_sale)
        or any(
            requested_by_sale[name] != purchased_by_sale[name]
            for name in requested_by_sale
            if name in purchased_by_sale
        )
    ):
        frappe.throw(
            "Partial ERP returns are not supported; refund lines must exactly match the full Sales Invoice",
            frappe.ValidationError,
        )

    resolved_sale_names = sorted(purchased_by_sale)
    placeholders = ", ".join(["%s"] * len(resolved_sale_names))

    # This must remain a locking/current read. A plain get_all after waiting for
    # the sale lock can reuse an older REPEATABLE READ snapshot and miss a
    # return committed by the transaction that previously held the lock.
    submitted_return_rows = frappe.db.sql(
        f"""
        SELECT
            return_line.original_resolved_sale,
            return_line.qty_returned,
            return_event.return_id
        FROM `tabFB Return Event Line` AS return_line
        INNER JOIN `tabFB Return Event` AS return_event
            ON return_event.name = return_line.parent
        WHERE return_line.parenttype = 'FB Return Event'
            AND return_event.docstatus = 1
            AND return_line.original_resolved_sale IN ({placeholders})
        ORDER BY return_line.original_resolved_sale, return_line.name
        FOR UPDATE
        """,
        tuple(resolved_sale_names),
        as_dict=True,
    )
    returned_by_sale = _aggregate_submitted_returns(
        return_id=return_id,
        rows=submitted_return_rows or [],
        identity_field="original_resolved_sale",
    )

    for resolved_sale_name, requested_qty in requested_by_sale.items():
        purchased_qty = purchased_by_sale[resolved_sale_name]
        returned_qty = returned_by_sale.get(resolved_sale_name, Decimal("0"))
        if returned_qty + requested_qty > purchased_qty:
            frappe.throw(
                f"Return quantity for FB Resolved Sale {resolved_sale_name} exceeds purchased quantity",
                frappe.ValidationError,
            )


def aggregate_return_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one deterministic row per commercial or legacy sale identity."""
    commercial_lines = [
        line
        for line in lines
        if cstr(line.get("original_sales_invoice_item")).strip()
    ]
    if commercial_lines:
        if len(commercial_lines) != len(lines):
            frappe.throw(
                "Return lines cannot mix commercial and legacy sale identities",
                frappe.ValidationError,
            )
        aggregated: dict[str, dict[str, Any]] = {}
        for line in commercial_lines:
            invoice_item = cstr(line.get("original_sales_invoice_item")).strip()
            existing = aggregated.get(invoice_item)
            if not existing:
                existing = {
                    "original_sales_invoice_item": invoice_item,
                    "original_fb_order_line_ref": cstr(
                        line.get("original_fb_order_line_ref")
                    ).strip(),
                    "original_resolved_sale": cstr(
                        line.get("original_resolved_sale")
                    ).strip()
                    or None,
                    "qty_returned": Decimal("0"),
                    "commercial_modifier_snapshot_json": "",
                }
                aggregated[invoice_item] = existing
            existing["qty_returned"] += _to_decimal(
                line.get("qty_returned"), "return quantity"
            )
        return [
            {
                **line,
                "qty_returned": float(line["qty_returned"]),
            }
            for _, line in sorted(aggregated.items())
        ]

    requested_by_sale = _aggregate_requested_quantities(lines)
    return [
        {
            "original_resolved_sale": resolved_sale_name,
            "qty_returned": float(quantity),
        }
        for resolved_sale_name, quantity in requested_by_sale.items()
    ]


def _aggregate_requested_quantities(
    lines: list[dict[str, Any]],
) -> dict[str, Decimal]:
    requested_by_sale: dict[str, Decimal] = {}
    for line in lines:
        resolved_sale_name = cstr(line.get("original_resolved_sale")).strip()
        if not resolved_sale_name:
            continue
        quantity = _to_decimal(line.get("qty_returned"), "return quantity")
        requested_by_sale[resolved_sale_name] = (
            requested_by_sale.get(resolved_sale_name, Decimal("0")) + quantity
        )
    return requested_by_sale


def _aggregate_submitted_returns(
    return_id: str,
    rows: list[Any],
    *,
    identity_field: str,
) -> dict[str, Decimal]:
    returned_by_identity: dict[str, Decimal] = {}
    normalized_return_id = cstr(return_id).strip()
    for row in rows:
        if cstr(_row_value(row, "return_id")).strip() == normalized_return_id:
            continue
        identity = cstr(_row_value(row, identity_field)).strip()
        if not identity:
            continue
        returned_by_identity[identity] = returned_by_identity.get(
            identity, Decimal("0")
        ) + _to_decimal(_row_value(row, "qty_returned"), "submitted return quantity")
    return returned_by_identity


def _to_decimal(value: Any, label: str) -> Decimal:
    try:
        return Decimal(cstr(value or 0).strip() or "0")
    except (InvalidOperation, ValueError) as error:
        frappe.throw(f"Invalid {label}: {value}", frappe.ValidationError)
        raise ValueError(f"Invalid {label}: {value}") from error


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)
