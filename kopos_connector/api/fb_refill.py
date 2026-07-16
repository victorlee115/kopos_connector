# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import cstr, flt

from kopos_connector.api.devices import (
    lock_device_for_operational_mutation,
    require_device_operational_scope,
)


@frappe.whitelist(methods=["POST"])
def process_refill() -> dict[str, Any]:
    payload = _get_request_payload()
    lock_device_for_operational_mutation(device_id=cstr(payload.get("device_id")))
    validated = _validate_payload(payload)
    require_device_operational_scope(
        validated["device_id"],
        company=validated["company"],
        warehouse=validated["to_warehouse"],
    )
    source_company = cstr(
        frappe.db.get_value("Warehouse", validated["from_warehouse"], "company")
    ).strip()
    if source_company != validated["company"]:
        frappe.throw(
            f"Source warehouse {validated['from_warehouse']} is outside company {validated['company']} scope",
            frappe.ValidationError,
        )
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


def _get_request_payload() -> dict[str, Any]:
    request_json = None
    if getattr(frappe, "request", None):
        request_json = frappe.request.get_json(silent=True)
    if isinstance(request_json, Mapping):
        return dict(request_json)
    return dict(frappe.form_dict or {})


def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    request_id = cstr(payload.get("request_id") or payload.get("idempotency_key"))
    device_id = cstr(payload.get("device_id")).strip()
    company = cstr(payload.get("company"))
    event_project = cstr(payload.get("event_project")) or None
    from_warehouse = cstr(payload.get("from_warehouse"))
    to_warehouse = cstr(payload.get("to_warehouse"))
    requested_by = cstr(payload.get("requested_by")) or None
    approved_by = cstr(payload.get("approved_by")) or None
    lines = payload.get("lines")
    if not request_id:
        frappe.throw("request_id is required", frappe.ValidationError)
    if not device_id:
        frappe.throw("device_id is required", frappe.ValidationError)
    if not company:
        frappe.throw("company is required", frappe.ValidationError)
    if not from_warehouse or not to_warehouse:
        frappe.throw(
            "from_warehouse and to_warehouse are required", frappe.ValidationError
        )
    if not isinstance(lines, list) or not lines:
        frappe.throw("lines must contain at least one row", frappe.ValidationError)
    line_rows = lines if isinstance(lines, list) else []
    validated_lines = []
    for index, row in enumerate(line_rows, start=1):
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
        "device_id": device_id,
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
