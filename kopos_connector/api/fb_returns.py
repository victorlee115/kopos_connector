from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import cint, cstr, flt

from kopos_connector.kopos.services.operations.return_service import (
    process_return_event,
)


@frappe.whitelist()
def process_return() -> dict[str, Any]:
    payload = _get_request_payload()
    validated = _validate_payload(payload)
    existing_return = frappe.db.get_value(
        "FB Return Event", {"return_id": validated["return_id"]}, "name"
    )
    if existing_return:
        return_doc = frappe.get_doc("FB Return Event", existing_return)
        return {
            "status": "duplicate",
            "return_event": return_doc.name,
            "return_sales_invoice": cstr(
                getattr(return_doc, "return_sales_invoice", None)
            )
            or None,
            "return_to_stock": cint(getattr(return_doc, "return_to_stock", 0)),
            "reversal_stock_entries": [
                cstr(getattr(line, "reversal_stock_entry", None))
                for line in (return_doc.get("lines") or [])
                if cstr(getattr(line, "reversal_stock_entry", None))
            ],
        }
    return_doc = _build_return_event(validated)
    return_doc.insert(ignore_permissions=True)
    return_doc.submit()
    return_doc.reload()
    return {
        "status": "ok",
        "return_event": return_doc.name,
        "return_sales_invoice": cstr(getattr(return_doc, "return_sales_invoice", None))
        or None,
        "return_to_stock": cint(getattr(return_doc, "return_to_stock", 0)),
        "reversal_stock_entries": [
            cstr(getattr(line, "reversal_stock_entry", None))
            for line in (return_doc.get("lines") or [])
            if cstr(getattr(line, "reversal_stock_entry", None))
        ],
    }


def validate_fb_return_event(doc=None, method=None):
    if not doc:
        return
    if not cstr(getattr(doc, "return_id", None)):
        frappe.throw("FB Return Event requires return_id", frappe.ValidationError)
    if not (doc.get("lines") or []):
        frappe.throw(
            "FB Return Event requires at least one line", frappe.ValidationError
        )
    for index, line in enumerate(doc.get("lines") or [], start=1):
        if not cstr(getattr(line, "original_resolved_sale", None)):
            frappe.throw(
                f"Return line {index} requires original_resolved_sale",
                frappe.ValidationError,
            )
        if flt(getattr(line, "qty_returned", 0)) <= 0:
            frappe.throw(
                f"Return line {index} requires qty_returned greater than 0",
                frappe.ValidationError,
            )


def on_submit_fb_return_event(doc=None, method=None):
    if not doc:
        return
    process_return_event(doc)
    doc.db_set("status", "Submitted", update_modified=False)


def _get_request_payload() -> dict[str, Any]:
    request_json = None
    if getattr(frappe, "request", None):
        request_json = frappe.request.get_json(silent=True)
    if isinstance(request_json, Mapping):
        return dict(request_json)
    return dict(frappe.form_dict or {})


def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return_id = cstr(payload.get("return_id") or payload.get("idempotency_key"))
    fb_order = cstr(payload.get("fb_order")) or None
    original_sales_invoice = cstr(payload.get("original_sales_invoice")) or None
    reason_code = cstr(payload.get("reason_code")) or "Other"
    reason_text = cstr(payload.get("reason_text")) or None
    return_to_stock = 1 if cint(payload.get("return_to_stock")) else 0
    lines = payload.get("lines")

    if not return_id:
        frappe.throw("return_id is required", frappe.ValidationError)
    if not isinstance(lines, list) or not lines:
        if not fb_order:
            frappe.throw("lines must contain at least one row", frappe.ValidationError)
        resolved_sales = frappe.get_all(
            "FB Resolved Sale",
            filters={"fb_order": fb_order},
            fields=["name", "qty", "sales_invoice"],
            order_by="creation asc",
        )
        if not resolved_sales:
            frappe.throw(
                f"FB Order {fb_order} has no resolved sales to return",
                frappe.ValidationError,
            )
        lines = [
            {
                "original_resolved_sale": cstr(row.get("name")),
                "qty_returned": flt(row.get("qty") or 0),
            }
            for row in resolved_sales
        ]
        if not original_sales_invoice:
            original_sales_invoice = (
                cstr(resolved_sales[0].get("sales_invoice")) or None
            )

    validated_lines = []
    for index, row in enumerate(lines, start=1):
        if not isinstance(row, Mapping):
            frappe.throw(f"lines[{index}] must be an object", frappe.ValidationError)
        original_resolved_sale = cstr(
            row.get("original_resolved_sale") or row.get("resolved_sale_id")
        )
        qty_returned = flt(row.get("qty_returned") or row.get("qty"))
        if not original_resolved_sale:
            frappe.throw(
                f"lines[{index}].original_resolved_sale is required",
                frappe.ValidationError,
            )
        if qty_returned <= 0:
            frappe.throw(
                f"lines[{index}].qty_returned must be greater than 0",
                frappe.ValidationError,
            )
        if not frappe.db.exists("FB Resolved Sale", original_resolved_sale):
            frappe.throw(
                f"FB Resolved Sale {original_resolved_sale} was not found",
                frappe.ValidationError,
            )
        validated_lines.append(
            {
                "original_resolved_sale": original_resolved_sale,
                "qty_returned": qty_returned,
            }
        )

    if not original_sales_invoice:
        resolved_sale = frappe.get_doc(
            "FB Resolved Sale", validated_lines[0]["original_resolved_sale"]
        )
        original_sales_invoice = (
            cstr(getattr(resolved_sale, "sales_invoice", None)) or None
        )
    if not original_sales_invoice:
        frappe.throw("original_sales_invoice is required", frappe.ValidationError)
    if not frappe.db.exists("Sales Invoice", original_sales_invoice):
        frappe.throw(
            f"Sales Invoice {original_sales_invoice} was not found",
            frappe.ValidationError,
        )

    return {
        "return_id": return_id,
        "fb_order": fb_order,
        "original_sales_invoice": original_sales_invoice,
        "reason_code": reason_code,
        "reason_text": reason_text,
        "return_to_stock": return_to_stock,
        "lines": validated_lines,
    }


def _build_return_event(validated: dict[str, Any]):
    doc = frappe.new_doc("FB Return Event")
    doc.return_id = validated["return_id"]
    doc.fb_order = validated["fb_order"]
    doc.original_sales_invoice = validated["original_sales_invoice"]
    doc.reason_code = validated["reason_code"]
    doc.reason_text = validated["reason_text"]
    doc.return_to_stock = validated["return_to_stock"]
    doc.status = "Draft"
    for line in validated["lines"]:
        doc.append("lines", line)
    return doc
