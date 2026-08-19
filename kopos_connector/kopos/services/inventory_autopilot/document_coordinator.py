from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

import frappe
from frappe.utils import cstr, now_datetime

from kopos_connector.kopos.services.inventory_autopilot.exceptions import (
    upsert_inventory_exception,
)
from kopos_connector.kopos.services.inventory_autopilot.automation_identity import (
    AutomationIdentityError,
    inventory_automation_identity,
    purchase_review_owner,
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
    plan_hash: str,
    policy_hash: str,
    transit_warehouse: str | None = None,
    warehouse: str | None = None,
) -> dict[str, Any]:
    plan, failed = _execution_plan(company=company, plan_hash=plan_hash, policy_hash=policy_hash)
    exception_warehouse = cstr((plan or {}).get("warehouse")).strip() or cstr(warehouse).strip()
    if not plan:
        return _blocked_plan_action(
            company=company,
            warehouse=exception_warehouse,
            reason="material_request_plan_gate_failed",
            summary="Inventory replenishment was not created because its saved plan is no longer safe",
            failed=failed,
        )
    rows = tuple(lines)
    if not _plan_lines_match(plan, purpose=purpose, lines=rows):
        return _blocked_plan_action(
            company=company,
            warehouse=exception_warehouse,
            reason="material_request_plan_mismatch",
            summary="Inventory replenishment was not created because the requested lines differ from the saved plan",
            failed=("input_hash_match",),
        )
    allowed, failed = evaluate_automation_gates(_parse_gate_results(plan.get("gate_results")), require_complete=True)
    if not allowed:
        return _blocked_plan_action(
            company=company,
            warehouse=exception_warehouse,
            reason="material_request_plan_gate_failed",
            summary="Inventory replenishment was not created because a saved safety gate failed",
            failed=failed,
        )
    if not rows:
        return {"status": "not_required", "material_request": None}
    if not _has_inventory_fingerprint_field("Material Request"):
        return _blocked_plan_action(
            company=company,
            warehouse=exception_warehouse,
            reason="material_request_idempotency_field_missing",
            summary="Inventory replenishment is paused until its idempotency field is installed",
            failed=("input_hash_match",),
        )
    action = _plan_action_for_purpose(purpose)
    material_request_type = _erp_material_request_type(purpose)
    normalized_transit = cstr(transit_warehouse).strip()
    if action == "Transfer":
        if not normalized_transit:
            return _blocked_plan_action(
                company=company,
                warehouse=exception_warehouse,
                reason="material_request_transit_route_missing",
                summary="The transfer request was not created because its transit warehouse is not configured",
                failed=("source_current",),
            )
        if not frappe.get_meta("Material Request").has_field("custom_kopos_transit_warehouse"):
            return _blocked_plan_action(
                company=company,
                warehouse=exception_warehouse,
                reason="material_request_transit_route_field_missing",
                summary="The transfer request was not created because its protected transit-route field is missing",
                failed=("input_hash_match",),
            )
        transit = frappe.db.get_value(
            "Warehouse",
            normalized_transit,
            ["company", "is_group", "disabled"],
            as_dict=True,
        ) or {}
        if (
            cstr(transit.get("company")).strip() != company
            or bool(transit.get("is_group"))
            or bool(transit.get("disabled"))
        ):
            return _blocked_plan_action(
                company=company,
                warehouse=exception_warehouse,
                reason="material_request_transit_route_invalid",
                summary="The transfer request was not created because its transit warehouse belongs to another company",
                failed=("source_current",),
            )
        for row in rows:
            source = cstr(row.source_warehouse).strip()
            destination = cstr(row.warehouse).strip()
            if not source or not destination or source == destination:
                return _blocked_plan_action(
                    company=company,
                    warehouse=exception_warehouse,
                    reason="material_request_transfer_route_missing",
                    summary="The transfer request was not created because every line needs a distinct approved source and destination warehouse",
                    failed=("source_current",),
                )
            if cstr(frappe.db.get_value("Warehouse", source, "company")).strip() != company or cstr(
                frappe.db.get_value("Warehouse", destination, "company")
            ).strip() != company:
                return _blocked_plan_action(
                    company=company,
                    warehouse=exception_warehouse,
                    reason="material_request_transfer_route_invalid",
                    summary="The transfer request was not created because its source or destination warehouse belongs to another company",
                    failed=("source_current",),
                )
    # The persisted ``quantity_decimal`` value on the plan is the authority.
    # Do not hash the dataclass representation here: Decimal exponent/trailing
    # zero differences (or a legacy Float display value) must not produce a
    # second intent for the same replenishment.
    fingerprint = _fingerprint(
        company,
        action,
        required_date,
        normalized_transit,
        _canonical_replenishment_lines(rows),
    )
    existing = _find_by_fingerprint("Material Request", fingerprint)
    if existing:
        _record_plan_document(plan, {"doctype": "Material Request", "name": existing})
        return {"status": "duplicate", "material_request": existing}
    open_intent = _find_open_material_request_intent(
        company=company,
        purpose=material_request_type,
        required_date=required_date,
        rows=rows,
    )
    if open_intent:
        return _blocked_plan_action(
            company=company,
            warehouse=exception_warehouse,
            reason="material_request_open_intent",
            summary="Inventory replenishment was not created because an equivalent open Material Request already exists",
            failed=("intent_not_open",),
            source_doctype="Material Request",
            source_name=open_intent,
        )
    document = frappe.new_doc("Material Request")
    document.company = company
    document.material_request_type = material_request_type
    document.transaction_date = now_datetime().date()
    document.schedule_date = required_date
    _set_if_present(document, "custom_kopos_inventory_fingerprint", fingerprint)
    if normalized_transit:
        _set_if_present(document, "custom_kopos_transit_warehouse", normalized_transit)
    planned_authorities = _plan_line_authorities(plan, purpose=purpose, lines=rows)
    for row in rows:
        exact_quantity = _plan_quantity(row)
        if exact_quantity is None:
            return _blocked_plan_action(
                company=company,
                warehouse=exception_warehouse,
                reason="material_request_quantity_invalid",
                summary="Inventory replenishment was not created because a plan quantity is not a positive Decimal",
                failed=("input_hash_match",),
            )
        uom_key = _plan_line_key(row, quantity=exact_quantity)
        authority = planned_authorities.get(uom_key)
        if not authority:
            return _blocked_plan_action(
                company=company,
                warehouse=exception_warehouse,
                reason="material_request_uom_authority_missing",
                summary="Inventory replenishment was not created because its saved plan UOM cannot be resolved",
                failed=("recipe_uom_complete",),
            )
        item_payload = {
            "item_code": row.item,
            # Assign the same exact Decimal used by plan comparison and the
            # idempotency fingerprint. Frappe may apply its declared DocField
            # precision at the standard document boundary; this layer must not
            # invent a different precision (such as six places).
            "qty": exact_quantity,
            "warehouse": row.warehouse,
            "schedule_date": required_date,
            "uom": authority["uom"],
            "stock_uom": authority["stock_uom"],
            "conversion_factor": authority["conversion_factor"],
        }
        if action == "Transfer":
            item_payload["from_warehouse"] = cstr(row.source_warehouse).strip()
        document.append("items", item_payload)
    try:
        with inventory_automation_identity(
            company=company,
            warehouse=cstr(plan.get("warehouse")).strip(),
            create_doctypes=("Material Request",),
            submit_doctypes=("Material Request",) if action != "Transfer" else (),
        ):
            try:
                document.insert()
            except frappe.DuplicateEntryError:
                existing = _find_by_fingerprint("Material Request", fingerprint)
                if existing:
                    _record_plan_document(plan, {"doctype": "Material Request", "name": existing})
                    return {"status": "duplicate", "material_request": existing, "fingerprint": fingerprint}
                raise
            if action != "Transfer":
                document.submit()
    except AutomationIdentityError as error:
        return _blocked_plan_action(
            company=company,
            warehouse=exception_warehouse,
            reason="inventory_automation_identity",
            summary="Inventory replenishment is paused because its dedicated automation user is not safe to use",
            failed=("automation_identity",),
        )
    _record_plan_document(plan, {"doctype": "Material Request", "name": document.name})
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
    forecast_evidence: dict[str, Any] | None = None,
    gates: dict[str, bool],
    lines: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Persist one explainable plan snapshot before any document action."""

    if not frappe.db.exists("DocType", "FB Inventory Plan"):
        return {"status": "blocked", "reason": "FB Inventory Plan is not installed"}
    line_rows = tuple(line for line in lines if isinstance(line, dict))
    # Gate evidence is part of the snapshot identity.  A plan blocked by a
    # stale tablet must be regenerated when that tablet becomes current even
    # when its forecast inputs did not otherwise change.
    fingerprint = _fingerprint(
        company,
        warehouse,
        planning_date,
        input_hash,
        policy_hash,
        forecast_state,
        forecast_evidence or {},
        gates,
        _canonical_plan_lines(line_rows),
    )
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
    document.forecast_evidence = json.dumps(
        forecast_evidence or {},
        sort_keys=True,
        separators=(",", ":"),
    )
    document.status = "Ready" if allowed else "Blocked"
    document.gate_results = json.dumps(gates, sort_keys=True, separators=(",", ":"))
    document.execution_fingerprint = fingerprint
    for line in line_rows:
        exact_quantity = _plan_quantity(line)
        if exact_quantity is None or exact_quantity <= 0:
            raise ValueError("inventory plan line quantity must be a positive finite decimal")
        document.append("lines", {
            "item": cstr(line.get("item")),
            "action": cstr(line.get("action") or "Purchase"),
            "warehouse": cstr(line.get("warehouse") or warehouse),
            "source_warehouse": cstr(line.get("source_warehouse")),
            # ``quantity`` is retained as the standard ERP display field. The
            # hidden text field is the authority used when a plan is reloaded
            # for document execution, so Frappe Float precision cannot alter
            # the saved plan identity.
            "quantity": str(exact_quantity),
            "quantity_decimal": _plain_decimal(exact_quantity),
            "uom": cstr(line.get("uom") or "Nos"),
            "stock_quantity_decimal": cstr(
                line.get("stock_quantity_decimal") or _plain_decimal(exact_quantity)
            ),
            "stock_uom": cstr(line.get("stock_uom") or line.get("uom") or "Nos"),
            "conversion_factor_decimal": cstr(line.get("conversion_factor_decimal") or "1"),
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
    warehouse: str | None = None,
) -> dict[str, Any]:
    fallback_warehouse = cstr(warehouse).strip()
    safe, reason = outbound_configuration_safe()
    if not safe:
        exception = upsert_inventory_exception(
            reason_code="draft_purchase_order_outbound_configuration",
            summary="Draft Purchase Order automation is paused because an outbound ERP hook is unsafe",
            next_action=reason,
            severity="Critical",
            company=company,
            warehouse=fallback_warehouse,
        )
        return {"status": "blocked", "exception": exception}
    plan, failed = _execution_plan(company=company, plan_hash=plan_hash, policy_hash=policy_hash)
    exception_warehouse = cstr((plan or {}).get("warehouse")).strip() or fallback_warehouse
    if not plan:
        return _blocked_plan_action(
            company=company,
            warehouse=exception_warehouse,
            reason="draft_purchase_order_plan_gate_failed",
            summary="Draft Purchase Order automation is paused because its saved plan is no longer safe",
            failed=failed,
        )
    if not frappe.db.exists("Material Request", material_request):
        return {"status": "blocked", "reason": "submitted_material_request_missing"}
    if not frappe.db.exists("Supplier Quotation", quotation):
        return {"status": "blocked", "reason": "submitted_supplier_quotation_missing"}
    if not plan_hash or not policy_hash or not quotation_hash:
        return {"status": "blocked", "reason": "document_provenance_hash_missing"}
    if not _has_inventory_fingerprint_field("Purchase Order"):
        return {"status": "blocked", "reason": "purchase_order_idempotency_field_missing"}
    fingerprint = _fingerprint(company, material_request, quotation, plan_hash, policy_hash, quotation_hash)
    existing = _find_by_fingerprint("Purchase Order", fingerprint)
    if existing:
        _record_plan_document(plan, {"doctype": "Purchase Order", "name": existing})
        return {"status": "duplicate", "purchase_order": existing}
    quotation_doc = frappe.get_doc("Supplier Quotation", quotation)
    material_request_doc = frappe.get_doc("Material Request", material_request)
    if material_request_doc.docstatus != 1:
        return {"status": "blocked", "reason": "material_request_not_submitted"}
    if quotation_doc.docstatus != 1:
        return {"status": "blocked", "reason": "supplier_quotation_not_submitted"}
    valid_till = _as_date(getattr(quotation_doc, "valid_till", None))
    if valid_till and valid_till < now_datetime().date():
        return {"status": "blocked", "reason": "supplier_quotation_expired"}
    current_quotes = _current_matching_quotations(quotation_doc)
    if len(current_quotes) != 1 or current_quotes[0] != quotation:
        return {"status": "blocked", "reason": "supplier_quotation_is_not_unique_current_authority"}
    if quotation_snapshot_hash(quotation_doc) != quotation_hash:
        return {"status": "blocked", "reason": "supplier_quotation_changed"}
    authority_error = _validate_material_request_quotation_authority(material_request_doc, quotation_doc)
    if authority_error:
        return {"status": "blocked", "reason": authority_error}
    if not _plan_references_material_request(plan, material_request):
        return {"status": "blocked", "reason": "material_request_is_not_bound_to_saved_plan"}
    review_owner = purchase_review_owner(company=company, warehouse=cstr(plan.get("warehouse")).strip())
    if not review_owner:
        return _blocked_plan_action(
            company=company,
            warehouse=exception_warehouse,
            reason="draft_purchase_order_review_owner_missing",
            summary="Draft Purchase Order automation is paused because no enabled Company Director is assigned to review it",
            failed=("automation_identity",),
        )
    document = frappe.new_doc("Purchase Order")
    document.supplier = quotation_doc.supplier
    document.company = company
    document.currency = quotation_doc.currency
    document.schedule_date = getattr(material_request_doc, "schedule_date", None) or quotation_doc.transaction_date
    _set_if_present(document, "supplier_quotation", quotation)
    _set_if_present(document, "material_request", material_request)
    for fieldname in ("buying_price_list", "price_list_currency", "taxes_and_charges", "tax_category", "shipping_rule", "incoterm", "tc_name", "terms"):
        _set_if_present(document, fieldname, getattr(quotation_doc, fieldname, None))
    _set_if_present(document, "custom_kopos_inventory_fingerprint", fingerprint)
    _set_if_present(document, "custom_kopos_material_request", material_request)
    _set_if_present(document, "custom_kopos_plan_hash", plan_hash)
    _set_if_present(document, "custom_kopos_policy_hash", policy_hash)
    _set_if_present(document, "custom_kopos_quotation_hash", quotation_hash)
    for item in quotation_doc.items:
        item_payload = {
            "item_code": item.item_code,
            "qty": item.qty,
            "rate": item.rate,
            "uom": item.uom,
            "schedule_date": getattr(item, "schedule_date", None) or getattr(material_request_doc, "schedule_date", None) or quotation_doc.transaction_date,
            "warehouse": item.warehouse,
            "supplier_quotation": quotation,
            "supplier_quotation_item": item.name,
        }
        for fieldname in (
            "conversion_factor",
            "stock_uom",
            "item_tax_template",
            "manufacturer",
            "manufacturer_part_no",
            "description",
            "material_request",
            "material_request_item",
        ):
            if getattr(item, fieldname, None) not in (None, ""):
                item_payload[fieldname] = getattr(item, fieldname)
        document.append("items", item_payload)
    try:
        with inventory_automation_identity(
            company=company,
            warehouse=cstr(plan.get("warehouse")).strip(),
            create_doctypes=("Purchase Order",),
        ):
            try:
                document.insert()
            except frappe.DuplicateEntryError:
                existing = _find_by_fingerprint("Purchase Order", fingerprint)
                if existing:
                    _record_plan_document(plan, {"doctype": "Purchase Order", "name": existing})
                    return {"status": "duplicate", "purchase_order": existing}
                raise
    except AutomationIdentityError:
        return _blocked_plan_action(
            company=company,
            warehouse=exception_warehouse,
            reason="inventory_automation_identity",
            summary="Draft Purchase Order automation is paused because its dedicated automation user is not safe to use",
            failed=("automation_identity",),
        )
    _ensure_purchase_order_todo(document.name, company, review_owner)
    _record_plan_document(plan, {"doctype": "Purchase Order", "name": document.name})
    # Deliberately do not submit, email, print, send, or call a supplier API.
    return {"status": "created_draft", "purchase_order": document.name, "docstatus": document.docstatus}


def create_eligible_draft_purchase_order(
    *,
    company: str,
    material_request: str,
    plan_hash: str,
    policy_hash: str,
    warehouse: str | None = None,
) -> dict[str, Any]:
    """Create a Draft PO only when exactly one submitted quote is authoritative."""

    if not material_request or not frappe.db.exists("Material Request", material_request):
        return _blocked_plan_action(
            company=company,
            warehouse=cstr(warehouse).strip(),
            reason="draft_purchase_order_material_request_missing",
            summary="Draft Purchase Order automation is waiting for its submitted Purchase Material Request",
            failed=("input_hash_match",),
        )
    request = frappe.get_doc("Material Request", material_request)
    if request.docstatus != 1 or cstr(getattr(request, "material_request_type", "")).strip() != "Purchase":
        return _blocked_plan_action(
            company=company,
            warehouse=cstr(warehouse).strip(),
            reason="draft_purchase_order_material_request_invalid",
            summary="Draft Purchase Order automation is waiting for a submitted Purchase Material Request",
            failed=("input_hash_match",),
            source_doctype="Material Request",
            source_name=material_request,
        )
    candidates: list[Any] = []
    for row in frappe.get_all(
        "Supplier Quotation",
        filters={"docstatus": 1},
        fields=["name", "valid_till"],
        limit_page_length=500,
    ):
        valid_till = _as_date(row.get("valid_till"))
        if valid_till and valid_till < now_datetime().date():
            continue
        quotation = frappe.get_doc("Supplier Quotation", cstr(row.get("name")))
        quotation_company = cstr(getattr(quotation, "company", None)).strip()
        if quotation_company and quotation_company != company:
            continue
        if _validate_material_request_quotation_authority(request, quotation) is None:
            candidates.append(quotation)
    if len(candidates) != 1:
        return _blocked_plan_action(
            company=company,
            warehouse=cstr(warehouse).strip(),
            reason="draft_purchase_order_quotation_authority",
            summary="Draft Purchase Order automation needs exactly one current submitted Supplier Quotation",
            failed=("source_current",),
            source_doctype="Material Request",
            source_name=material_request,
        )
    quotation = candidates[0]
    return create_draft_purchase_order(
        company=company,
        material_request=material_request,
        quotation=cstr(quotation.name),
        plan_hash=plan_hash,
        policy_hash=policy_hash,
        quotation_hash=quotation_snapshot_hash(quotation),
        warehouse=warehouse,
    )


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
    hooks = frappe.get_hooks("doc_events") or {}
    if isinstance(hooks, dict):
        for target in ("Purchase Order", "*"):
            if _hook_has_handlers(hooks.get(target)):
                risky.append(f"doc_events:{target}")
    for hook_name in ("override_doctype_class", "extend_doctype_class"):
        mappings = frappe.get_hooks(hook_name) or {}
        if isinstance(mappings, dict) and _hook_has_handlers(mappings.get("Purchase Order")):
            risky.append(hook_name)
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
            "conversion_factor": cstr(getattr(item, "conversion_factor", "")),
            "stock_uom": cstr(getattr(item, "stock_uom", "")),
            "item_tax_template": cstr(getattr(item, "item_tax_template", "")),
        })
    return _fingerprint({
        "supplier": cstr(getattr(quotation, "supplier", "")),
        "currency": cstr(getattr(quotation, "currency", "")),
        "transaction_date": cstr(getattr(quotation, "transaction_date", "")),
        "valid_till": cstr(getattr(quotation, "valid_till", "")),
        "taxes_and_charges": cstr(getattr(quotation, "taxes_and_charges", "")),
        "tax_category": cstr(getattr(quotation, "tax_category", "")),
        "incoterm": cstr(getattr(quotation, "incoterm", "")),
        "terms": cstr(getattr(quotation, "terms", "")),
        "items": rows,
    })


def _find_plan_for_hash(*, company: str, plan_hash: str, policy_hash: str) -> dict[str, Any] | None:
    if not plan_hash or not policy_hash or not frappe.db.exists("DocType", "FB Inventory Plan"):
        return None
    rows = frappe.get_all(
        "FB Inventory Plan",
        filters={"company": company, "input_hash": plan_hash, "policy_hash": policy_hash},
        fields=["name", "gate_results", "status", "input_hash", "policy_hash"],
        order_by="modified desc",
        limit_page_length=2,
    )
    if len(rows) != 1 or cstr(rows[0].get("status")) not in {"Ready", "Executed"}:
        return None
    return frappe.get_doc("FB Inventory Plan", cstr(rows[0].get("name"))).as_dict()


def _execution_plan(*, company: str, plan_hash: str, policy_hash: str) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    plan = _find_plan_for_hash(company=company, plan_hash=plan_hash, policy_hash=policy_hash)
    if not plan:
        return None, ("input_hash_match",)
    allowed, failed = evaluate_automation_gates(_parse_gate_results(plan.get("gate_results")), require_complete=True)
    return (plan, ()) if allowed else (None, failed)


def _blocked_plan_action(
    *,
    company: str,
    warehouse: str | None = None,
    reason: str,
    summary: str,
    failed: tuple[str, ...],
    source_doctype: str | None = None,
    source_name: str | None = None,
) -> dict[str, Any]:
    exception = upsert_inventory_exception(
        reason_code=reason,
        summary=summary,
        next_action=f"Resolve: {', '.join(failed)}",
        severity="Warning",
        company=company,
        warehouse=cstr(warehouse).strip() or None,
        source_doctype=source_doctype,
        source_name=source_name,
    )
    return {"status": "blocked", "failed_gates": failed, "exception": exception}


def _plan_lines_match(plan: dict[str, Any], *, purpose: str, lines: tuple[ReplenishmentLine, ...]) -> bool:
    expected = sorted(
        (
            _plan_action_for_purpose(purpose),
            *_plan_line_key(row),
        )
        for row in lines
    )
    action = _plan_action_for_purpose(purpose)
    actual = sorted(
        (
            _plan_action_for_purpose(cstr(row.get("action"))),
            *_plan_line_key(row),
        )
        for row in (plan.get("lines") or [])
        if _plan_action_for_purpose(cstr(row.get("action"))) == action
    )
    return expected == actual


def _plan_line_uoms(
    plan: dict[str, Any], *, purpose: str, lines: tuple[ReplenishmentLine, ...]
) -> dict[tuple[str, str, str, Decimal], str]:
    return {
        key: value["uom"]
        for key, value in _plan_line_authorities(plan, purpose=purpose, lines=lines).items()
    }


def _plan_line_authorities(
    plan: dict[str, Any], *, purpose: str, lines: tuple[ReplenishmentLine, ...]
) -> dict[tuple[str, str, str, Decimal], dict[str, Any]]:
    authorities: dict[tuple[str, str, str, Decimal], dict[str, Any]] = {}
    for row in plan.get("lines") or []:
        if _plan_action_for_purpose(cstr(row.get("action"))) != _plan_action_for_purpose(purpose):
            continue
        key = _plan_line_key(row)
        if key[-1] is None:
            continue
        uom = cstr(row.get("uom")).strip() or cstr(
            frappe.db.get_value("Item", key[0], "stock_uom")
        ).strip()
        stock_uom = cstr(row.get("stock_uom")).strip() or uom
        conversion = _decimal(row.get("conversion_factor_decimal") or "1")
        if not uom or not stock_uom or conversion is None or conversion <= 0:
            continue
        authorities[key] = {
            "uom": uom,
            "stock_uom": stock_uom,
            "conversion_factor": conversion,
        }
    missing = [
        row.item
        for row in lines
        if _plan_line_key(row) not in authorities
    ]
    if missing:
        raise ValueError("saved plan does not contain a UOM for " + ", ".join(sorted(set(missing))))
    return authorities


def _plan_line_key(
    value: Any,
    *,
    quantity: Decimal | None = None,
) -> tuple[str, str, str, Decimal | None]:
    """Build the one identity used for plan line equality and UOM lookup.

    ``Decimal`` equality/hash semantics intentionally make ``1``, ``1.0`` and
    ``1.000`` the same quantity while retaining every significant digit. No
    fixed-place quantization is appropriate here because the ERP document
    field is the eventual, explicit precision boundary.
    """

    if isinstance(value, ReplenishmentLine):
        item = value.item
        warehouse = value.warehouse
        source_warehouse = value.source_warehouse
    else:
        item = value.get("item") if isinstance(value, dict) else getattr(value, "item", "")
        warehouse = value.get("warehouse") if isinstance(value, dict) else getattr(value, "warehouse", "")
        source_warehouse = (
            value.get("source_warehouse") if isinstance(value, dict) else getattr(value, "source_warehouse", "")
        )
    parsed_quantity = quantity if quantity is not None else _plan_quantity(value)
    return (
        cstr(item).strip(),
        cstr(warehouse).strip(),
        cstr(source_warehouse).strip(),
        parsed_quantity,
    )


def _canonical_replenishment_lines(lines: Iterable[ReplenishmentLine]) -> tuple[dict[str, Any], ...]:
    """Return stable, exact line data for the Material Request fingerprint."""

    canonical: list[dict[str, Any]] = []
    for row in lines:
        quantity = _plan_quantity(row)
        canonical.append({
            "item": cstr(row.item).strip(),
            "warehouse": cstr(row.warehouse).strip(),
            "source_warehouse": cstr(row.source_warehouse).strip(),
            "quantity_decimal": _plain_decimal(quantity) if quantity is not None else None,
            "uom": cstr(row.uom).strip(),
            "stock_uom": cstr(row.stock_uom).strip(),
            "conversion_factor_decimal": (
                _plain_decimal(row.conversion_factor)
                if row.conversion_factor is not None
                else None
            ),
            "stock_quantity_decimal": (
                _plain_decimal(row.stock_quantity)
                if row.stock_quantity is not None
                else None
            ),
        })
    return tuple(sorted(canonical, key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))))


def _find_open_material_request_intent(
    *, company: str, purpose: str, required_date: date | str, rows: tuple[ReplenishmentLine, ...]
) -> str | None:
    if not frappe.db.exists("DocType", "Material Request"):
        return None
    expected = sorted(
        (
            cstr(row.item).strip(),
            cstr(row.warehouse).strip(),
            cstr(row.source_warehouse).strip(),
            _plan_quantity(row),
        )
        for row in rows
    )
    requested_date = _as_date(required_date)
    candidates = frappe.get_all(
        "Material Request",
        filters={
            "company": company,
            "material_request_type": _erp_material_request_type(purpose),
            "docstatus": ["in", [0, 1]],
        },
        fields=["name", "schedule_date", "status"],
        limit_page_length=500,
    )
    for candidate in candidates:
        if cstr(candidate.get("status")) in {"Cancelled", "Stopped", "Completed"}:
            continue
        if requested_date and candidate.get("schedule_date") and _as_date(candidate.get("schedule_date")) != requested_date:
            continue
        document = frappe.get_doc("Material Request", cstr(candidate.get("name")))
        actual = sorted(
            (
                cstr(item.item_code).strip(),
                cstr(item.warehouse).strip(),
                cstr(getattr(item, "from_warehouse", None)).strip(),
                _decimal(item.qty),
            )
            for item in document.items
        )
        if actual == expected:
            return cstr(document.name)
    return None


def has_open_material_request_intent(
    *, company: str, purpose: str, required_date: date | str, lines: Iterable[ReplenishmentLine]
) -> bool:
    """Expose the same open-intent test used by the document executor."""

    return bool(_find_open_material_request_intent(
        company=company,
        purpose=purpose,
        required_date=required_date,
        rows=tuple(lines),
    ))


def _plan_references_material_request(plan: dict[str, Any], material_request: str) -> bool:
    try:
        documents = json.loads(cstr(plan.get("created_documents") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return any(
        isinstance(document, dict)
        and cstr(document.get("doctype")) == "Material Request"
        and cstr(document.get("name")) == material_request
        for document in documents
    )


def _record_plan_document(plan: dict[str, Any], document_reference: dict[str, str]) -> None:
    document = frappe.get_doc("FB Inventory Plan", cstr(plan.get("name")))
    try:
        previous = json.loads(cstr(document.get("created_documents") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        previous = []
    if not isinstance(previous, list):
        previous = []
    if document_reference not in previous:
        previous.append(document_reference)
    document.created_documents = json.dumps(previous, sort_keys=True, separators=(",", ":"))
    document.status = "Executed"
    document.save(ignore_permissions=True)


def _plan_action_for_purpose(value: str) -> str:
    normalized = cstr(value).strip().lower()
    if normalized in {"transfer", "material transfer"}:
        return "Transfer"
    if normalized == "manufacture":
        return "Manufacture"
    return "Purchase"


def _erp_material_request_type(value: str) -> str:
    """Return the standard ERPNext Material Request option for an action."""

    action = _plan_action_for_purpose(value)
    return "Material Transfer" if action == "Transfer" else action


def _has_inventory_fingerprint_field(doctype: str) -> bool:
    return frappe.get_meta(doctype).has_field("custom_kopos_inventory_fingerprint")


def _hook_has_handlers(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_hook_has_handlers(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_hook_has_handlers(item) for item in value)
    return bool(cstr(value).strip())


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(cstr(value)[:10])
    except ValueError:
        return None


def _parse_gate_results(raw: Any) -> dict[str, bool]:
    try:
        value = json.loads(cstr(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return {cstr(key): bool(result) for key, result in value.items()} if isinstance(value, dict) else {}


def _current_matching_quotations(quotation: Any) -> list[str]:
    today = now_datetime().date()
    rows = frappe.get_all(
        "Supplier Quotation",
        filters={"supplier": quotation.supplier, "docstatus": 1},
        fields=["name", "valid_till"],
        limit_page_length=500,
    )
    expected_items = {
        (cstr(item.item_code), cstr(item.uom), cstr(item.warehouse), _decimal(item.qty))
        for item in quotation.items
    }
    matches: list[str] = []
    for row in rows:
        valid_till = row.get("valid_till")
        if valid_till and _as_date(valid_till) and _as_date(valid_till) < today:
            continue
        candidate = frappe.get_doc("Supplier Quotation", row["name"])
        candidate_items = {
            (cstr(item.item_code), cstr(item.uom), cstr(item.warehouse), _decimal(item.qty))
            for item in candidate.items
        }
        if candidate_items == expected_items:
            matches.append(cstr(row["name"]))
    return matches


def _validate_material_request_quotation_authority(material_request: Any, quotation: Any) -> str | None:
    requested = {
        cstr(item.name): (
            cstr(item.item_code),
            cstr(item.warehouse),
            cstr(getattr(item, "uom", "")),
            _decimal(item.qty),
            _decimal(getattr(item, "conversion_factor", 1)),
        )
        for item in getattr(material_request, "items", []) or []
    }
    quoted_rows = list(getattr(quotation, "items", []) or [])
    if len(quoted_rows) != len(requested):
        return "supplier_quotation_does_not_exactly_match_material_request"
    quoted: dict[str, tuple[str, str, str, Decimal, Decimal]] = {}
    for item in quoted_rows:
        if cstr(getattr(item, "material_request", "")) != cstr(material_request.name):
            return "supplier_quotation_does_not_reference_material_request"
        request_item = cstr(getattr(item, "material_request_item", ""))
        if not request_item or request_item in quoted:
            return "supplier_quotation_does_not_exactly_match_material_request"
        quoted[request_item] = (
            cstr(item.item_code),
            cstr(item.warehouse),
            cstr(getattr(item, "uom", "")),
            _decimal(item.qty),
            _decimal(getattr(item, "conversion_factor", 1)),
        )
    if set(quoted) != set(requested):
        return "supplier_quotation_does_not_reference_material_request"
    if requested != quoted:
        return "supplier_quotation_does_not_exactly_match_material_request"
    return None


def _decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value).strip())
        if not parsed.is_finite():
            return Decimal("-1")
        # Keep the full Decimal value. The previous fixed six-place
        # quantization made distinct high-precision requests look identical
        # to the open-intent check and could also disagree with plan UOM keys.
        return parsed.normalize()
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("-1")


def _plan_quantity(value: Any) -> Decimal | None:
    """Read the exact persisted plan quantity before the Float display field."""

    if isinstance(value, dict):
        exact = value.get("quantity_decimal")
        raw = exact if exact not in (None, "") else value.get("quantity")
    else:
        exact = getattr(value, "quantity_decimal", None)
        legacy = getattr(value, "quantity", None)
        raw = exact if exact not in (None, "") else legacy
    if raw in (None, ""):
        return None
    try:
        result = Decimal(str(raw).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite() or result <= 0:
        return None
    return result.normalize()


def _plain_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized != 0 else "0"


def _canonical_plan_lines(lines: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Canonicalize plan-line identity without the rounded Float display field."""

    canonical: list[dict[str, Any]] = []
    for row in lines:
        quantity = _plan_quantity(row)
        canonical.append({
            "item": cstr(row.get("item")).strip(),
            "action": _plan_action_for_purpose(cstr(row.get("action"))),
            "warehouse": cstr(row.get("warehouse")).strip(),
            "source_warehouse": cstr(row.get("source_warehouse")).strip(),
            "quantity_decimal": _plain_decimal(quantity) if quantity is not None else None,
            "uom": cstr(row.get("uom")).strip(),
            "stock_quantity_decimal": cstr(row.get("stock_quantity_decimal")).strip(),
            "stock_uom": cstr(row.get("stock_uom")).strip(),
            "conversion_factor_decimal": cstr(row.get("conversion_factor_decimal")).strip(),
        })
    return tuple(sorted(canonical, key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))))


def _find_by_fingerprint(doctype: str, fingerprint: str) -> str | None:
    if not frappe.get_meta(doctype).has_field("custom_kopos_inventory_fingerprint"):
        return None
    return cstr(
        frappe.db.get_value(doctype, {"custom_kopos_inventory_fingerprint": fingerprint}, "name")
    ).strip() or None


def _set_if_present(document: Any, fieldname: str, value: Any) -> None:
    if frappe.get_meta(document.doctype).has_field(fieldname):
        setattr(document, fieldname, value)


def _ensure_purchase_order_todo(purchase_order: str, company: str, owner: str) -> None:
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
        "owner": owner,
        "status": "Open",
    }).insert(ignore_permissions=True)


def _fingerprint(*values: Any) -> str:
    encoded = json.dumps(values, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
