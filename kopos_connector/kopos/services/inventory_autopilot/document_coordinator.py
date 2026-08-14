from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Iterable

import frappe
from frappe.utils import cstr, now_datetime

from kopos_connector.kopos.services.inventory_autopilot.exceptions import (
    upsert_inventory_exception,
)
from kopos_connector.kopos.services.inventory_autopilot.replenishment import (
    ReplenishmentLine,
    evaluate_automation_gates,
)


def create_and_submit_material_request(
    *,
    company: str,
    purpose: str,
    required_date: date | str,
    lines: Iterable[ReplenishmentLine],
    gates: dict[str, bool],
) -> dict[str, Any]:
    allowed, failed = evaluate_automation_gates(gates, require_complete=True)
    if not allowed:
        exception = upsert_inventory_exception(
            reason_code="material_request_gate_failed",
            summary="Inventory replenishment was not created because a safety gate failed",
            next_action=f"Resolve: {', '.join(failed)}",
            severity="Warning",
            company=company,
        )
        return {"status": "blocked", "failed_gates": failed, "exception": exception}
    rows = tuple(lines)
    if not rows:
        return {"status": "not_required", "material_request": None}
    fingerprint = _fingerprint(company, purpose, required_date, rows)
    existing = _find_by_fingerprint("Material Request", fingerprint)
    if existing:
        return {"status": "duplicate", "material_request": existing}
    document = frappe.new_doc("Material Request")
    document.company = company
    document.material_request_type = purpose
    document.transaction_date = now_datetime().date()
    document.schedule_date = required_date
    _set_if_present(document, "custom_kopos_inventory_fingerprint", fingerprint)
    for row in rows:
        document.append(
            "items",
            {
                "item_code": row.item,
                "qty": row.quantity,
                "warehouse": row.warehouse,
                "schedule_date": required_date,
            },
        )
    document.insert()
    document.submit()
    return {"status": "created", "material_request": document.name, "fingerprint": fingerprint}


def create_draft_purchase_order(
    *,
    company: str,
    material_request: str,
    quotation: str,
    plan_hash: str,
    policy_hash: str,
    quotation_hash: str,
    gates: dict[str, bool],
) -> dict[str, Any]:
    safe, reason = outbound_configuration_safe()
    if not safe:
        exception = upsert_inventory_exception(
            reason_code="draft_purchase_order_outbound_configuration",
            summary="Draft Purchase Order automation is paused because an outbound ERP hook is unsafe",
            next_action=reason,
            severity="Critical",
            company=company,
        )
        return {"status": "blocked", "exception": exception}
    allowed, failed = evaluate_automation_gates(gates, require_complete=True)
    if not allowed:
        return {"status": "blocked", "failed_gates": failed}
    if not frappe.db.exists("Material Request", material_request):
        return {"status": "blocked", "reason": "submitted_material_request_missing"}
    if not frappe.db.exists("Supplier Quotation", quotation):
        return {"status": "blocked", "reason": "submitted_supplier_quotation_missing"}
    fingerprint = _fingerprint(company, material_request, quotation, plan_hash, policy_hash, quotation_hash)
    existing = _find_by_fingerprint("Purchase Order", fingerprint)
    if existing:
        return {"status": "duplicate", "purchase_order": existing}
    quotation_doc = frappe.get_doc("Supplier Quotation", quotation)
    material_request_doc = frappe.get_doc("Material Request", material_request)
    if material_request_doc.docstatus != 1:
        return {"status": "blocked", "reason": "material_request_not_submitted"}
    if quotation_doc.docstatus != 1:
        return {"status": "blocked", "reason": "supplier_quotation_not_submitted"}
    document = frappe.new_doc("Purchase Order")
    document.supplier = quotation_doc.supplier
    document.company = company
    document.currency = quotation_doc.currency
    document.schedule_date = quotation_doc.transaction_date
    _set_if_present(document, "custom_kopos_inventory_fingerprint", fingerprint)
    _set_if_present(document, "custom_kopos_material_request", material_request)
    _set_if_present(document, "custom_kopos_plan_hash", plan_hash)
    _set_if_present(document, "custom_kopos_policy_hash", policy_hash)
    _set_if_present(document, "custom_kopos_quotation_hash", quotation_hash)
    for item in quotation_doc.items:
        document.append(
            "items",
            {
                "item_code": item.item_code,
                "qty": item.qty,
                "rate": item.rate,
                "uom": item.uom,
                "schedule_date": item.schedule_date or quotation_doc.transaction_date,
                "warehouse": item.warehouse,
            },
        )
    document.insert()
    _ensure_purchase_order_todo(document.name, company)
    # Deliberately do not submit, email, print, send, or call a supplier API.
    return {"status": "created_draft", "purchase_order": document.name, "docstatus": document.docstatus}


def outbound_configuration_safe() -> tuple[bool, str]:
    risky: list[str] = []
    for doctype, fields in (
        ("Notification", ["enabled", "channel", "document_type"]),
        ("Webhook", ["enabled", "request_url"]),
        ("Server Script", ["disabled", "script_type"]),
        ("Assignment Rule", ["disabled"]),
        ("Workflow", ["is_active"]),
    ):
        if not frappe.db.exists("DocType", doctype):
            continue
        meta = frappe.get_meta(doctype)
        available = [field for field in fields if meta.has_field(field)]
        if not available:
            continue
        filters: dict[str, Any] = {}
        if "enabled" in available:
            filters["enabled"] = 1
        if "disabled" in available:
            filters["disabled"] = 0
        if "is_active" in available:
            filters["is_active"] = 1
        try:
            rows = frappe.get_all(doctype, filters=filters, fields=available, limit_page_length=100)
        except Exception:
            return False, f"Unable to inspect {doctype} automation configuration"
        if rows:
            risky.append(doctype)
    return (not risky, "Disable or review: " + ", ".join(risky)) if risky else (True, "")


def _find_by_fingerprint(doctype: str, fingerprint: str) -> str | None:
    if not frappe.get_meta(doctype).has_field("custom_kopos_inventory_fingerprint"):
        return None
    return cstr(
        frappe.db.get_value(doctype, {"custom_kopos_inventory_fingerprint": fingerprint}, "name")
    ).strip() or None


def _set_if_present(document: Any, fieldname: str, value: Any) -> None:
    if frappe.get_meta(document.doctype).has_field(fieldname):
        setattr(document, fieldname, value)


def _ensure_purchase_order_todo(purchase_order: str, company: str) -> None:
    if not frappe.db.exists("DocType", "ToDo"):
        return
    if frappe.db.exists(
        "ToDo",
        {"reference_type": "Purchase Order", "reference_name": purchase_order, "status": "Open"},
    ):
        return
    frappe.get_doc({
        "doctype": "ToDo",
        "description": f"Review inventory Draft Purchase Order {purchase_order} for {company}",
        "reference_type": "Purchase Order",
        "reference_name": purchase_order,
        "owner": cstr(getattr(frappe.session, "user", None)).strip() or "Administrator",
        "status": "Open",
    }).insert(ignore_permissions=True)


def _fingerprint(*values: Any) -> str:
    encoded = json.dumps(values, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
