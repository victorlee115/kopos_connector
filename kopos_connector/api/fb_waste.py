from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import cstr, flt

from kopos_connector.kopos.services.inventory.waste_service import (
    create_waste_stock_entry,
)


@frappe.whitelist()
def process_waste() -> dict[str, Any]:
    payload = _get_request_payload()
    validated = _validate_payload(payload)
    doc = _build_waste_event(validated)
    doc.insert(ignore_permissions=True)
    doc.submit()
    doc.reload()
    return {
        "status": "ok",
        "waste_event": doc.name,
        "stock_entry": cstr(getattr(doc, "stock_entry", None)) or None,
    }


def validate_fb_waste_event(doc=None, method=None):
    if not doc:
        return
    if not cstr(getattr(doc, "waste_id", None)):
        frappe.throw("FB Waste Event requires waste_id", frappe.ValidationError)
    if not cstr(getattr(doc, "warehouse", None)):
        frappe.throw("FB Waste Event requires warehouse", frappe.ValidationError)
    if not (doc.get("lines") or []):
        frappe.throw(
            "FB Waste Event requires at least one line", frappe.ValidationError
        )


def on_submit_fb_waste_event(doc=None, method=None):
    if not doc:
        return
    items = []
    for line in doc.get("lines") or []:
        items.append(
            {
                "item_code": line.item,
                "qty": line.qty,
                "uom": line.uom,
                "cost_center": getattr(line, "cost_center", None),
            }
        )
    stock_entry = create_waste_stock_entry(doc.company, doc.warehouse, items)
    doc.db_set("stock_entry", stock_entry, update_modified=False)
    doc.db_set("status", "Submitted", update_modified=False)


def _get_request_payload() -> dict[str, Any]:
    request_json = None
    if getattr(frappe, "request", None):
        request_json = frappe.request.get_json(silent=True)
    if isinstance(request_json, Mapping):
        return dict(request_json)
    return dict(frappe.form_dict or {})


def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    waste_id = cstr(payload.get("waste_id") or payload.get("idempotency_key"))
    company = cstr(payload.get("company"))
    warehouse = cstr(payload.get("warehouse"))
    event_project = cstr(payload.get("event_project")) or None
    shift = cstr(payload.get("shift")) or None
    reason_code = cstr(payload.get("reason_code")) or "Other"
    reason_text = cstr(payload.get("reason_text")) or None
    lines = payload.get("lines")
    if not waste_id:
        frappe.throw("waste_id is required", frappe.ValidationError)
    if not company:
        frappe.throw("company is required", frappe.ValidationError)
    if not warehouse:
        frappe.throw("warehouse is required", frappe.ValidationError)
    if not isinstance(lines, list) or not lines:
        frappe.throw("lines must contain at least one row", frappe.ValidationError)
    validated_lines = []
    for index, row in enumerate(lines, start=1):
        if not isinstance(row, Mapping):
            frappe.throw(f"lines[{index}] must be an object", frappe.ValidationError)
        item = cstr(row.get("item") or row.get("item_code"))
        qty = flt(row.get("qty"))
        uom = cstr(row.get("uom"))
        cost_center = cstr(row.get("cost_center")) or None
        if not item:
            frappe.throw(f"lines[{index}].item is required", frappe.ValidationError)
        if qty <= 0:
            frappe.throw(
                f"lines[{index}].qty must be greater than 0", frappe.ValidationError
            )
        if not uom:
            frappe.throw(f"lines[{index}].uom is required", frappe.ValidationError)
        validated_lines.append(
            {"item": item, "qty": qty, "uom": uom, "cost_center": cost_center}
        )
    return {
        "waste_id": waste_id,
        "company": company,
        "warehouse": warehouse,
        "event_project": event_project,
        "shift": shift,
        "reason_code": reason_code,
        "reason_text": reason_text,
        "lines": validated_lines,
    }


def _build_waste_event(validated: dict[str, Any]):
    doc = frappe.new_doc("FB Waste Event")
    doc.waste_id = validated["waste_id"]
    doc.company = validated["company"]
    doc.warehouse = validated["warehouse"]
    doc.event_project = validated["event_project"]
    doc.shift = validated["shift"]
    doc.reason_code = validated["reason_code"]
    doc.reason_text = validated["reason_text"]
    doc.status = "Draft"
    for line in validated["lines"]:
        doc.append("lines", line)
    return doc
