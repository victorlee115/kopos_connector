from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import cstr

from kopos_connector.kopos.services.operations.remake_service import (
    create_remake_stock_entry,
)


@frappe.whitelist()
def process_remake() -> dict[str, Any]:
    payload = _get_request_payload()
    validated = _validate_payload(payload)
    doc = _build_remake_event(validated)
    doc.insert(ignore_permissions=True)
    doc.submit()
    doc.reload()
    return {
        "status": "ok",
        "remake_event": doc.name,
        "replacement_stock_entry": cstr(getattr(doc, "replacement_stock_entry", None))
        or None,
    }


def validate_fb_remake_event(doc=None, method=None):
    if not doc:
        return
    if not cstr(getattr(doc, "remake_id", None)):
        frappe.throw("FB Remake Event requires remake_id", frappe.ValidationError)
    if not cstr(getattr(doc, "original_order", None)):
        frappe.throw("FB Remake Event requires original_order", frappe.ValidationError)
    if not cstr(getattr(doc, "original_resolved_sale", None)):
        frappe.throw(
            "FB Remake Event requires original_resolved_sale", frappe.ValidationError
        )


def on_submit_fb_remake_event(doc=None, method=None):
    if not doc:
        return
    replacement_stock_entry = create_remake_stock_entry(doc)
    if replacement_stock_entry:
        doc.db_set(
            "replacement_stock_entry", replacement_stock_entry, update_modified=False
        )
    doc.db_set("status", "Submitted", update_modified=False)


def _get_request_payload() -> dict[str, Any]:
    request_json = None
    if getattr(frappe, "request", None):
        request_json = frappe.request.get_json(silent=True)
    if isinstance(request_json, Mapping):
        return dict(request_json)
    return dict(frappe.form_dict or {})


def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    remake_id = cstr(payload.get("remake_id") or payload.get("idempotency_key"))
    original_order = cstr(payload.get("original_order"))
    original_order_line = cstr(payload.get("original_order_line")) or None
    original_resolved_sale = cstr(
        payload.get("original_resolved_sale") or payload.get("resolved_sale_id")
    )
    reason_code = cstr(payload.get("reason_code")) or "Other"
    reason_text = cstr(payload.get("reason_text")) or None

    if not remake_id:
        frappe.throw("remake_id is required", frappe.ValidationError)
    if not original_order:
        if original_resolved_sale and frappe.db.exists(
            "FB Resolved Sale", original_resolved_sale
        ):
            resolved = frappe.get_doc("FB Resolved Sale", original_resolved_sale)
            original_order = cstr(getattr(resolved, "fb_order", None))
    if not original_order:
        frappe.throw("original_order is required", frappe.ValidationError)
    if not original_resolved_sale:
        frappe.throw("original_resolved_sale is required", frappe.ValidationError)
    if not frappe.db.exists("FB Order", original_order):
        frappe.throw(f"FB Order {original_order} was not found", frappe.ValidationError)
    if not frappe.db.exists("FB Resolved Sale", original_resolved_sale):
        frappe.throw(
            f"FB Resolved Sale {original_resolved_sale} was not found",
            frappe.ValidationError,
        )

    return {
        "remake_id": remake_id,
        "original_order": original_order,
        "original_order_line": original_order_line,
        "original_resolved_sale": original_resolved_sale,
        "reason_code": reason_code,
        "reason_text": reason_text,
    }


def _build_remake_event(validated: dict[str, Any]):
    doc = frappe.new_doc("FB Remake Event")
    doc.remake_id = validated["remake_id"]
    doc.original_order = validated["original_order"]
    doc.original_order_line = validated["original_order_line"]
    doc.original_resolved_sale = validated["original_resolved_sale"]
    doc.reason_code = validated["reason_code"]
    doc.reason_text = validated["reason_text"]
    doc.status = "Draft"
    return doc
