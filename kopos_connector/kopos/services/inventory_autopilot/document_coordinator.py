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
    if purpose not in {"Transfer", "Material Transfer"}:
        document.submit()
    return {
        "status": "created",
        "material_request": document.name,
        "fingerprint": fingerprint,
        "docstatus": document.docstatus,
    }


def persist_inventory_plan(
    *,
    company: str,
    warehouse: str,
    planning_date: Any,
    input_hash: str,
    policy_hash: str,
    forecast_state: str,
    gates: dict[str, bool],
    lines: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Persist one explainable plan snapshot before any document action."""

    if not frappe.db.exists("DocType", "FB Inventory Plan"):
        return {"status": "blocked", "reason": "FB Inventory Plan is not installed"}
    line_rows = tuple(line for line in lines if isinstance(line, dict))
    fingerprint = _fingerprint(company, warehouse, planning_date, input_hash, policy_hash, line_rows)
    existing = frappe.db.get_value("FB Inventory Plan", {"execution_fingerprint": fingerprint}, "name")
    if existing:
        return {"status": "duplicate", "plan": existing, "fingerprint": fingerprint}
    allowed, failed = evaluate_automation_gates(gates, require_complete=True)
    document = frappe.new_doc("FB Inventory Plan")
    document.company = company
    document.warehouse = warehouse
    document.planning_date = planning_date
    document.input_hash = input_hash
    document.policy_hash = policy_hash
    document.forecast_state = forecast_state if forecast_state in {"Reliable", "Please check", "Not ready"} else "Not ready"
    document.status = "Ready" if allowed else "Blocked"
    document.gate_results = json.dumps(gates, sort_keys=True, separators=(",", ":"))
    document.execution_fingerprint = fingerprint
    for line in line_rows:
        document.append("lines", {
            "item": cstr(line.get("item")),
            "action": cstr(line.get("action") or "Purchase"),
            "warehouse": cstr(line.get("warehouse") or warehouse),
            "quantity": line.get("quantity"),
            "uom": cstr(line.get("uom") or "Nos"),
            "reason": cstr(line.get("reason") or "Review replenishment plan"),
        })
    document.insert(ignore_permissions=True)
    if not allowed:
        upsert_inventory_exception(
            reason_code="inventory_plan_gate_failed",
            summary="A replenishment plan needs review before JiJi can create work",
            next_action=f"Resolve: {', '.join(failed)}",
            severity="Warning",
            company=company,
            warehouse=warehouse,
            source_doctype="FB Inventory Plan",
            source_name=document.name,
        )
    frappe.db.commit()
    return {"status": "created", "plan": document.name, "fingerprint": fingerprint, "failed_gates": failed}


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
    if not plan_hash or not policy_hash or not quotation_hash:
        return {"status": "blocked", "reason": "document_provenance_hash_missing"}
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
    if quotation_snapshot_hash(quotation_doc) != quotation_hash:
        return {"status": "blocked", "reason": "supplier_quotation_changed"}
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
    checks = (
        ("Notification", ("enabled",), ("document_type", "reference_doctype", "doctype"), ("enabled", "document_type", "reference_doctype", "doctype")),
        ("Webhook", ("enabled",), ("doctype", "document_type", "reference_doctype", "webhook_doctype"), ("enabled", "request_url", "doctype", "document_type", "reference_doctype", "webhook_doctype")),
        ("Server Script", ("disabled",), ("reference_doctype", "document_type", "doctype"), ("disabled", "reference_doctype", "document_type", "doctype", "script")),
        ("Assignment Rule", ("disabled",), ("document_type", "reference_doctype", "doctype"), ("disabled", "document_type", "reference_doctype", "doctype")),
        ("Workflow", ("is_active",), ("document_type", "reference_doctype", "doctype"), ("is_active", "document_type", "reference_doctype", "doctype")),
    )
    for doctype, enabled_fields, target_fields, fields in checks:
        if not frappe.db.exists("DocType", doctype):
            continue
        meta = frappe.get_meta(doctype)
        target_field = next((field for field in target_fields if meta.has_field(field)), None)
        if target_field is None:
            return False, f"Unable to identify the Purchase Order target field on {doctype}"
        available = [field for field in fields if meta.has_field(field)]
        filters = {target_field: "Purchase Order"}
        for field in enabled_fields:
            if meta.has_field(field):
                filters[field] = 1 if field == "enabled" or field == "is_active" else 0
        try:
            rows = frappe.get_all(doctype, filters=filters, fields=available, limit_page_length=100)
        except Exception:
            return False, f"Unable to inspect {doctype} automation configuration"
        if doctype == "Server Script" and rows:
            risky_rows = [row for row in rows if any(token in cstr(row.get("script")).lower() for token in ("submit", "email", "send", "print"))]
            rows = risky_rows
        if rows:
            risky.append(doctype)
    return (not risky, "Disable or review: " + ", ".join(risky)) if risky else (True, "")


def quotation_snapshot_hash(quotation: Any) -> str:
    """Hash the submitted quotation fields that become PO authority."""

    rows = []
    for item in getattr(quotation, "items", []) or []:
        rows.append({
            "item_code": cstr(getattr(item, "item_code", "")),
            "qty": cstr(getattr(item, "qty", "")),
            "rate": cstr(getattr(item, "rate", "")),
            "uom": cstr(getattr(item, "uom", "")),
            "schedule_date": cstr(getattr(item, "schedule_date", "")),
            "warehouse": cstr(getattr(item, "warehouse", "")),
        })
    return _fingerprint({
        "supplier": cstr(getattr(quotation, "supplier", "")),
        "currency": cstr(getattr(quotation, "currency", "")),
        "transaction_date": cstr(getattr(quotation, "transaction_date", "")),
        "items": rows,
    })


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
