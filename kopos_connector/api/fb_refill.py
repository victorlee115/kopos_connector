from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import cstr, flt

from kopos_connector.kopos.services.operations.refill_service import (
    fulfill_refill_request,
)


@frappe.whitelist()
def process_refill() -> dict[str, Any]:
    payload = _get_request_payload()
    validated = _validate_payload(payload)
    doc = _build_refill_request(validated)
    doc.insert(ignore_permissions=True)
    doc.submit()
    doc.reload()
    return {
        "status": "ok",
        "refill_request": doc.name,
        "fulfilled_stock_entry": cstr(getattr(doc, "fulfilled_stock_entry", None))
        or None,
    }


def validate_fb_refill_request(doc=None, method=None):
    if not doc:
        return
    if not cstr(getattr(doc, "request_id", None)):
        frappe.throw(
            "FB Booth Refill Request requires request_id", frappe.ValidationError
        )
    if not cstr(getattr(doc, "company", None)):
        frappe.throw("FB Booth Refill Request requires company", frappe.ValidationError)
    if not cstr(getattr(doc, "from_warehouse", None)) or not cstr(
        getattr(doc, "to_warehouse", None)
    ):
        frappe.throw(
            "FB Booth Refill Request requires from_warehouse and to_warehouse",
            frappe.ValidationError,
        )
    if not (doc.get("lines") or []):
        frappe.throw(
            "FB Booth Refill Request requires at least one line", frappe.ValidationError
        )


def on_submit_fb_refill_request(doc=None, method=None):
    if not doc:
        return
    fulfilled_stock_entry = fulfill_refill_request(doc)
    doc.db_set("fulfilled_stock_entry", fulfilled_stock_entry, update_modified=False)
    doc.db_set("status", "Fulfilled", update_modified=False)


def _get_request_payload() -> dict[str, Any]:
    request_json = None
    if getattr(frappe, "request", None):
        request_json = frappe.request.get_json(silent=True)
    if isinstance(request_json, Mapping):
        return dict(request_json)
    return dict(frappe.form_dict or {})


def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    request_id = cstr(payload.get("request_id") or payload.get("idempotency_key"))
    company = cstr(payload.get("company"))
    event_project = cstr(payload.get("event_project")) or None
    from_warehouse = cstr(payload.get("from_warehouse"))
    to_warehouse = cstr(payload.get("to_warehouse"))
    requested_by = cstr(payload.get("requested_by")) or None
    approved_by = cstr(payload.get("approved_by")) or None
    lines = payload.get("lines")
    if not request_id:
        frappe.throw("request_id is required", frappe.ValidationError)
    if not company:
        frappe.throw("company is required", frappe.ValidationError)
    if not from_warehouse or not to_warehouse:
        frappe.throw(
            "from_warehouse and to_warehouse are required", frappe.ValidationError
        )
    if not isinstance(lines, list) or not lines:
        frappe.throw("lines must contain at least one row", frappe.ValidationError)
    validated_lines = []
    for index, row in enumerate(lines, start=1):
        if not isinstance(row, Mapping):
            frappe.throw(f"lines[{index}] must be an object", frappe.ValidationError)
        item = cstr(row.get("item") or row.get("item_code"))
        qty = flt(row.get("qty"))
        uom = cstr(row.get("uom"))
        urgency = cstr(row.get("urgency")) or "Normal"
        remarks = cstr(row.get("remarks")) or None
        if not item:
            frappe.throw(f"lines[{index}].item is required", frappe.ValidationError)
        if qty <= 0:
            frappe.throw(
                f"lines[{index}].qty must be greater than 0", frappe.ValidationError
            )
        if not uom:
            frappe.throw(f"lines[{index}].uom is required", frappe.ValidationError)
        validated_lines.append(
            {
                "item": item,
                "qty": qty,
                "uom": uom,
                "urgency": urgency,
                "remarks": remarks,
            }
        )
    return {
        "request_id": request_id,
        "company": company,
        "event_project": event_project,
        "from_warehouse": from_warehouse,
        "to_warehouse": to_warehouse,
        "requested_by": requested_by,
        "approved_by": approved_by,
        "lines": validated_lines,
    }


def _build_refill_request(validated: dict[str, Any]):
    doc = frappe.new_doc("FB Booth Refill Request")
    doc.request_id = validated["request_id"]
    doc.company = validated["company"]
    doc.event_project = validated["event_project"]
    doc.from_warehouse = validated["from_warehouse"]
    doc.to_warehouse = validated["to_warehouse"]
    doc.requested_by = validated["requested_by"]
    doc.approved_by = validated["approved_by"]
    doc.status = "Approved"
    for line in validated["lines"]:
        doc.append("lines", line)
    return doc
