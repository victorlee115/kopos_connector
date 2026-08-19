# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any

import frappe
from frappe.utils import cstr, nowdate

from kopos_connector.api.devices import (
    lock_device_for_operational_mutation,
    require_device_operational_scope,
)


@frappe.whitelist(methods=["POST"])
def process_refill() -> dict[str, Any]:
    """Compatibility adapter for older tablets.

    Refill execution is no longer owned by ``FB Booth Refill Request``.  An
    older tablet may only ask ERP to record one idempotent *Draft* standard
    Material Request.  A Company Director still reviews/submits it and the
    current guided transfer endpoints execute the submitted authority.
    """

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
    fingerprint = _request_fingerprint(validated["request_id"])
    existing_name = frappe.db.get_value(
        "Material Request",
        {"custom_kopos_inventory_fingerprint": fingerprint},
        "name",
    )
    if existing_name:
        existing = frappe.get_doc("Material Request", existing_name)
        if _canonical_request(existing) != _canonical_payload(validated):
            frappe.throw(
                "request_id was reused with different refill content",
                frappe.ValidationError,
            )
        return _response(existing, replayed=True)

    doc = _build_material_request(validated, fingerprint=fingerprint)
    try:
        doc.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        # The unique fingerprint is the concurrency boundary.  A second
        # request may race the first insert; once the winner is visible, it
        # must follow the same exact-content replay path as a normal retry.
        existing_name = frappe.db.get_value(
            "Material Request",
            {"custom_kopos_inventory_fingerprint": fingerprint},
            "name",
        )
        if not existing_name:
            raise
        existing = frappe.get_doc("Material Request", existing_name)
        if _canonical_request(existing) != _canonical_payload(validated):
            frappe.throw(
                "request_id was reused with different refill content",
                frappe.ValidationError,
            )
        return _response(existing, replayed=True)
    frappe.db.commit()
    return _response(doc, replayed=False)


def _response(doc: Any, *, replayed: bool) -> dict[str, Any]:
    return {
        "status": "replayed" if replayed else "ok",
        "material_request": cstr(doc.name),
        # Keep the old response key for in-place tablet upgrades. It now names
        # the standard Draft Material Request and never a custom stock action.
        "refill_request": cstr(doc.name),
        "docstatus": int(getattr(doc, "docstatus", 0) or 0),
        "fulfilled_stock_entry": None,
    }


def _get_request_payload() -> dict[str, Any]:
    request_json = None
    if getattr(frappe, "request", None):
        request_json = frappe.request.get_json(silent=True)
    if isinstance(request_json, Mapping):
        return dict(request_json)
    return dict(frappe.form_dict or {})


def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    request_id = cstr(
        payload.get("request_id") or payload.get("idempotency_key")
    ).strip()
    device_id = cstr(payload.get("device_id")).strip()
    company = cstr(payload.get("company")).strip()
    event_project = cstr(payload.get("event_project")).strip() or None
    from_warehouse = cstr(payload.get("from_warehouse")).strip()
    to_warehouse = cstr(payload.get("to_warehouse")).strip()
    requested_by = cstr(payload.get("requested_by")).strip() or None
    approved_by = cstr(payload.get("approved_by")).strip() or None
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
    if from_warehouse == to_warehouse:
        frappe.throw(
            "from_warehouse and to_warehouse must be different",
            frappe.ValidationError,
        )
    if not isinstance(lines, list) or not lines:
        frappe.throw("lines must contain at least one row", frappe.ValidationError)
    line_rows = lines if isinstance(lines, list) else []
    validated_lines = []
    for index, row in enumerate(line_rows, start=1):
        if not isinstance(row, Mapping):
            frappe.throw(f"lines[{index}] must be an object", frappe.ValidationError)
        item = cstr(row.get("item") or row.get("item_code")).strip()
        qty = _positive_decimal(row.get("qty"), f"lines[{index}].qty")
        uom = cstr(row.get("uom")).strip()
        urgency = cstr(row.get("urgency")).strip() or "Normal"
        remarks = cstr(row.get("remarks")).strip() or None
        if not item:
            frappe.throw(f"lines[{index}].item is required", frappe.ValidationError)
        if not uom:
            frappe.throw(f"lines[{index}].uom is required", frappe.ValidationError)
        if not frappe.db.exists("Item", item):
            frappe.throw(f"lines[{index}].item does not exist", frappe.ValidationError)
        stock_uom, conversion_factor = _stock_uom_conversion(item, uom)
        validated_lines.append(
            {
                "item": item,
                "qty": qty,
                "uom": uom,
                "stock_uom": stock_uom,
                "conversion_factor": conversion_factor,
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


def _build_material_request(validated: dict[str, Any], *, fingerprint: str) -> Any:
    if not frappe.get_meta("Material Request").has_field(
        "custom_kopos_inventory_fingerprint"
    ):
        frappe.throw(
            "Run the KoPOS inventory migration before recording refill requests",
            frappe.ValidationError,
        )
    doc = frappe.new_doc("Material Request")
    doc.material_request_type = "Material Transfer"
    doc.company = validated["company"]
    doc.transaction_date = nowdate()
    doc.schedule_date = nowdate()
    doc.custom_kopos_inventory_fingerprint = fingerprint
    doc.set_warehouse = validated["to_warehouse"]
    if frappe.get_meta("Material Request").has_field("set_from_warehouse"):
        doc.set_from_warehouse = validated["from_warehouse"]
    doc.remarks = (
        "KoPOS legacy refill compatibility request. Director review and "
        "submission are required before POS transfer execution."
    )
    for line in validated["lines"]:
        doc.append(
            "items",
            {
                "item_code": line["item"],
                "qty": line["qty"],
                "uom": line["uom"],
                "stock_uom": line["stock_uom"],
                "conversion_factor": line["conversion_factor"],
                "warehouse": validated["to_warehouse"],
                "schedule_date": nowdate(),
                "description": line["remarks"] or "",
            },
        )
        item_row = doc.items[-1]
        if frappe.get_meta("Material Request Item").has_field("from_warehouse"):
            item_row.from_warehouse = validated["from_warehouse"]
    return doc


def _request_fingerprint(request_id: str) -> str:
    return "legacy-refill-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()


def _canonical_payload(value: dict[str, Any]) -> str:
    return json.dumps(
        {
            "company": value["company"],
            "from_warehouse": value["from_warehouse"],
            "to_warehouse": value["to_warehouse"],
            "lines": [
                {
                    "item": line["item"],
                    "qty": _decimal_text(line["qty"]),
                    "uom": line["uom"],
                    "conversion_factor": _decimal_text(line["conversion_factor"]),
                }
                for line in value["lines"]
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_request(doc: Any) -> str:
    lines = list(getattr(doc, "items", None) or [])
    return json.dumps(
        {
            "company": cstr(getattr(doc, "company", None)).strip(),
            "from_warehouse": cstr(
                getattr(doc, "set_from_warehouse", None)
                or (getattr(lines[0], "from_warehouse", None) if lines else None)
            ).strip(),
            "to_warehouse": cstr(
                getattr(doc, "set_warehouse", None)
                or (getattr(lines[0], "warehouse", None) if lines else None)
            ).strip(),
            "lines": [
                {
                    "item": cstr(getattr(line, "item_code", None)).strip(),
                    "qty": _decimal_text(getattr(line, "qty", None)),
                    "uom": cstr(getattr(line, "uom", None)).strip(),
                    "conversion_factor": _decimal_text(
                        getattr(line, "conversion_factor", None)
                    ),
                }
                for line in lines
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _stock_uom_conversion(item: str, uom: str) -> tuple[str, Decimal]:
    stock_uom = cstr(frappe.db.get_value("Item", item, "stock_uom")).strip()
    if not stock_uom:
        frappe.throw(f"Item {item} has no Stock UOM", frappe.ValidationError)
    if uom == stock_uom:
        return stock_uom, Decimal("1")
    factor = frappe.db.get_value(
        "UOM Conversion Detail",
        {"parent": item, "uom": uom},
        "conversion_factor",
    )
    conversion = _positive_decimal(factor, f"Item {item} UOM conversion")
    return stock_uom, conversion


def _positive_decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        frappe.throw(f"{label} must be a finite decimal", frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if not parsed.is_finite() or parsed <= 0:
        frappe.throw(f"{label} must be greater than 0", frappe.ValidationError)
    return parsed


def _decimal_text(value: Any) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("stored Material Request contains an invalid decimal") from error
    if not parsed.is_finite():
        raise ValueError("stored Material Request contains a non-finite decimal")
    normalized = format(parsed.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized
