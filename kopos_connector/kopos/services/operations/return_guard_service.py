# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import cstr, flt


def lock_and_validate_return_quantities(
    return_id: str,
    lines: list[dict[str, Any]],
) -> None:
    """Serialize returns per resolved sale and validate against committed returns."""
    resolved_sale_names = sorted(
        {
            cstr(line.get("original_resolved_sale")).strip()
            for line in lines
            if cstr(line.get("original_resolved_sale")).strip()
        }
    )
    if not resolved_sale_names:
        frappe.throw(
            "Return requires at least one FB Resolved Sale",
            frappe.ValidationError,
        )

    placeholders = ", ".join(["%s"] * len(resolved_sale_names))
    locked_rows = frappe.db.sql(
        f"""
        SELECT name
        FROM `tabFB Resolved Sale`
        WHERE name IN ({placeholders})
        ORDER BY name
        FOR UPDATE
        """,
        tuple(resolved_sale_names),
        as_dict=True,
    )
    locked_names = {
        cstr(_row_value(row, "name")).strip() for row in (locked_rows or [])
    }
    missing_names = set(resolved_sale_names) - locked_names
    if missing_names:
        frappe.throw(
            "FB Resolved Sale {0} was not found".format(
                ", ".join(sorted(missing_names))
            ),
            frappe.ValidationError,
        )

    _validate_return_quantities(return_id, lines)


def _validate_return_quantities(
    return_id: str,
    lines: list[dict[str, Any]],
) -> None:
    for line in lines:
        resolved_sale_name = cstr(line.get("original_resolved_sale")).strip()
        resolved_sale = frappe.get_doc("FB Resolved Sale", resolved_sale_name)
        purchased_qty = flt(getattr(resolved_sale, "qty", 0))
        requested_qty = flt(line.get("qty_returned"))
        returned_qty = _get_existing_returned_qty(
            return_id=return_id,
            resolved_sale_name=resolved_sale_name,
        )
        if returned_qty + requested_qty > purchased_qty:
            frappe.throw(
                f"Return quantity for FB Resolved Sale {resolved_sale_name} exceeds purchased quantity",
                frappe.ValidationError,
            )


def _get_existing_returned_qty(return_id: str, resolved_sale_name: str) -> float:
    rows = frappe.get_all(
        "FB Return Event Line",
        filters={
            "original_resolved_sale": resolved_sale_name,
            "parenttype": "FB Return Event",
        },
        fields=["parent", "qty_returned"],
    )
    total = 0.0
    for row in rows or []:
        parent = cstr(_row_value(row, "parent")).strip()
        if not parent:
            continue
        return_doc = frappe.get_doc("FB Return Event", parent)
        if cstr(getattr(return_doc, "return_id", "")).strip() == return_id:
            continue
        if cstr(getattr(return_doc, "status", "")).strip() == "Cancelled":
            continue
        total += flt(_row_value(row, "qty_returned"))
    return total


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)
