# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe
from frappe.utils import cint, cstr

from kopos_connector.api.devices import (
    lock_device_for_operational_mutation,
    require_device_operational_scope,
)
from kopos_connector.utils.manager_approval import (
    canonical_context_hash,
    load_consumed_manager_approval_proof,
    verify_manager_approval_token,
)


INVENTORY_EVALUATION_INCLUDED = "included_evaluated"


@frappe.whitelist(methods=["POST"])
def process_remake() -> dict[str, Any]:
    payload = _get_request_payload()
    lock_device_for_operational_mutation(device_id=cstr(payload.get("device_id")))
    return process_remake_payload(payload, require_manager_approval=True)


def process_remake_payload(
    payload: dict[str, Any], *, require_manager_approval: bool = False
) -> dict[str, Any]:
    validated = _validate_payload(payload)
    scope = _build_remake_approval_scope(validated)
    validated["request_fingerprint"] = _request_fingerprint(validated, scope)

    existing_name = _get_existing_remake_name(validated["remake_id"])
    if existing_name:
        existing = frappe.get_doc("FB Remake Event", existing_name)
        _validate_existing_remake_matches(existing, validated)
        return _serialize_remake_response(
            "duplicate",
            existing,
            require_approval_proof=require_manager_approval,
        )

    if validated["remade_qty"] + validated["quantity"] > validated["sold_qty"]:
        frappe.throw(
            f"FB Order line {validated['line_id']} has already been remade",
            frappe.ValidationError,
        )

    approval: dict[str, Any] | None = None
    if require_manager_approval:
        approval = verify_manager_approval_token(
            cstr(validated.get("manager_approval_token")).strip(),
            device_id=scope["device_id"],
            staff_id=scope["staff_id"],
            action="stock_remake",
            shift_id=scope["shift_id"],
            resource_id=scope["resource_id"],
            amount_sen=0,
            context_hash=scope["context_hash"],
            idempotency_key=validated["remake_id"],
        )

    event = _build_remake_event(validated, approval=approval)
    try:
        event.insert(ignore_permissions=True)
        event.submit()
        frappe.db.set_value(
            "FB Order Line",
            validated["original_order_line"],
            "remade_qty",
            str(validated["remade_qty"] + validated["quantity"]),
            update_modified=False,
        )
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        existing_name = _get_existing_remake_name(validated["remake_id"])
        if not existing_name:
            raise
        event = frappe.get_doc("FB Remake Event", existing_name)
        _validate_existing_remake_matches(event, validated)
        return _serialize_remake_response(
            "duplicate",
            event,
            require_approval_proof=require_manager_approval,
        )

    event.reload()
    return _serialize_remake_response(
        "ok", event, require_approval_proof=require_manager_approval
    )


def build_remake_approval_scope(payload: dict[str, Any]) -> dict[str, Any]:
    return _build_remake_approval_scope(_validate_payload(payload))


def _get_request_payload() -> dict[str, Any]:
    request_json = None
    if getattr(frappe, "request", None):
        request_json = frappe.request.get_json(silent=True)
    if isinstance(request_json, Mapping):
        return dict(request_json)
    return dict(frappe.form_dict or {})


def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    remake_id = cstr(payload.get("remake_id") or payload.get("idempotency_key")).strip()
    device_id = cstr(payload.get("device_id")).strip()
    original_order = cstr(payload.get("original_order")).strip()
    line_reference = cstr(payload.get("original_order_line") or payload.get("line_id")).strip()
    reason_code = cstr(payload.get("reason_code")).strip() or "Other"
    reason_text = cstr(payload.get("reason_text")).strip() or None
    manager_approval_token = cstr(payload.get("manager_approval_token")).strip()

    if not remake_id:
        frappe.throw("remake_id is required", frappe.ValidationError)
    if not device_id:
        frappe.throw("device_id is required", frappe.ValidationError)
    if not original_order:
        frappe.throw("original_order is required", frappe.ValidationError)
    if not line_reference:
        frappe.throw("original_order_line is required", frappe.ValidationError)
    if not frappe.db.exists("FB Order", original_order):
        frappe.throw(f"FB Order {original_order} was not found", frappe.ValidationError)

    order = frappe.get_doc("FB Order", original_order)
    if cstr(getattr(order, "device_id", None)).strip() != device_id:
        frappe.throw(f"FB Order {original_order} belongs to another device", frappe.ValidationError)
    require_device_operational_scope(
        device_id,
        company=cstr(getattr(order, "company", None)).strip(),
        warehouse=cstr(getattr(order, "booth_warehouse", None)).strip(),
    )
    sales_invoice = cstr(getattr(order, "sales_invoice", None)).strip()
    if not sales_invoice or not frappe.db.exists("Sales Invoice", sales_invoice):
        frappe.throw(
            f"FB Order {original_order} has no submitted Sales Invoice",
            frappe.ValidationError,
        )
    if cint(frappe.db.get_value("Sales Invoice", sales_invoice, "docstatus")) != 1:
        frappe.throw(
            f"FB Order {original_order} is not eligible for a remake",
            frappe.ValidationError,
        )

    rows = frappe.db.sql(
        """
        SELECT name, parent, line_id, qty, remade_qty, resolved_sale
        FROM `tabFB Order Line`
        WHERE parent = %s AND (name = %s OR line_id = %s)
        ORDER BY idx
        LIMIT 1
        FOR UPDATE
        """,
        (original_order, line_reference, line_reference),
        as_dict=True,
    )
    if not rows:
        frappe.throw(
            f"FB Order line {line_reference} was not found on {original_order}",
            frappe.ValidationError,
        )
    line = rows[0]
    sold_qty = _positive_decimal(_row_value(line, "qty"), "sold quantity")
    requested_quantity = payload.get("quantity")
    quantity = sold_qty if requested_quantity in (None, "") else _positive_decimal(
        requested_quantity, "remake quantity"
    )
    # One complete sold line is the durable operational unit. This lets the
    # tablet derive the same idempotency key after a restart and prevents a
    # second tap from consuming ingredients twice.
    if quantity != sold_qty:
        frappe.throw(
            "A remake must cover the complete sold order line",
            frappe.ValidationError,
        )
    remade_qty = _nonnegative_decimal(_row_value(line, "remade_qty"), "remade quantity")
    resolved_sale = cstr(_row_value(line, "resolved_sale")).strip() or None

    return {
        "remake_id": remake_id,
        "device_id": device_id,
        "original_order": original_order,
        "original_order_line": cstr(_row_value(line, "name")).strip(),
        "line_id": cstr(_row_value(line, "line_id")).strip(),
        "original_resolved_sale": resolved_sale,
        "sales_invoice": sales_invoice,
        "shift_id": cstr(getattr(order, "shift", None)).strip(),
        "staff_id": cstr(getattr(order, "staff_id", None)).strip(),
        "sold_qty": sold_qty,
        "remade_qty": remade_qty,
        "quantity": quantity,
        "reason_code": reason_code,
        "reason_text": reason_text,
        "manager_approval_token": manager_approval_token,
    }


def _build_remake_approval_scope(validated: dict[str, Any]) -> dict[str, Any]:
    if not validated["shift_id"] or not validated["staff_id"]:
        frappe.throw(
            f"FB Order {validated['original_order']} has no stable shift and staff scope",
            frappe.ValidationError,
        )
    context = _approval_context(validated)
    return {
        "device_id": validated["device_id"],
        "staff_id": validated["staff_id"],
        "shift_id": validated["shift_id"],
        "resource_id": validated["line_id"],
        "amount_sen": 0,
        "context_hash": canonical_context_hash(context),
        "context": context,
    }


def _approval_context(validated: dict[str, Any]) -> dict[str, Any]:
    return {
        "original_order": validated["original_order"],
        "line_id": validated["line_id"],
        "quantity": _decimal_text(validated["quantity"]),
        "reason_code": validated["reason_code"],
        "reason_text": validated["reason_text"] or "",
    }


def _request_fingerprint(validated: dict[str, Any], scope: dict[str, Any]) -> str:
    return canonical_context_hash(
        {
            "remake_id": validated["remake_id"],
            "device_id": scope["device_id"],
            "staff_id": scope["staff_id"],
            "shift_id": scope["shift_id"],
            "resource_id": scope["resource_id"],
            "amount_sen": 0,
            "context": scope["context"],
            "inventory_evaluation": INVENTORY_EVALUATION_INCLUDED,
        }
    )


def _get_existing_remake_name(remake_id: str) -> str | None:
    rows = frappe.db.sql(
        """
        SELECT name
        FROM `tabFB Remake Event`
        WHERE remake_id = %s
        ORDER BY name
        LIMIT 1
        FOR UPDATE
        """,
        (remake_id,),
        as_dict=True,
    )
    return cstr(_row_value(rows[0], "name")).strip() if rows else None


def _validate_existing_remake_matches(event: Any, validated: dict[str, Any]) -> None:
    fingerprint = cstr(getattr(event, "request_fingerprint", None)).strip()
    if fingerprint and fingerprint != validated["request_fingerprint"]:
        frappe.throw(
            f"remake_id {validated['remake_id']} was already used for a different request",
            frappe.ValidationError,
        )
    if not fingerprint:
        legacy_matches = (
            cstr(getattr(event, "original_order", None)).strip() == validated["original_order"]
            and cstr(getattr(event, "original_order_line", None)).strip()
            == validated["original_order_line"]
            and cstr(getattr(event, "reason_code", None)).strip() == validated["reason_code"]
            and cstr(getattr(event, "reason_text", None)).strip()
            == cstr(validated["reason_text"]).strip()
        )
        if not legacy_matches:
            frappe.throw(
                f"remake_id {validated['remake_id']} was already used for a different request",
                frappe.ValidationError,
            )


def _build_remake_event(validated: dict[str, Any], *, approval: dict[str, Any] | None):
    event = frappe.new_doc("FB Remake Event")
    event.remake_id = validated["remake_id"]
    event.original_order = validated["original_order"]
    event.original_order_line = validated["original_order_line"]
    event.original_resolved_sale = validated["original_resolved_sale"]
    event.quantity = str(validated["quantity"])
    event.reason_code = validated["reason_code"]
    event.reason_text = validated["reason_text"]
    event.request_fingerprint = validated["request_fingerprint"]
    event.inventory_evaluation = INVENTORY_EVALUATION_INCLUDED
    event.inventory_status = "Pending"
    if approval:
        event.approval_token_id = approval["token_id"]
        event.approved_by_manager = approval["manager_id"]
    event.status = "Draft"
    return event


def _serialize_remake_response(
    status: str, event: Any, *, require_approval_proof: bool
) -> dict[str, Any]:
    response = {
        "status": status,
        "remake_event": event.name,
        "replacement_stock_entry": cstr(
            getattr(event, "replacement_stock_entry", None)
        ).strip()
        or None,
        "inventory_evaluation": INVENTORY_EVALUATION_INCLUDED,
        "inventory_status": cstr(getattr(event, "inventory_status", None)).strip()
        or "Exception",
        "inventory_exception": cstr(
            getattr(event, "inventory_exception", None)
        ).strip()
        or None,
    }
    if require_approval_proof:
        response.update(
            load_consumed_manager_approval_proof(
                approval_token_id=cstr(
                    getattr(event, "approval_token_id", None)
                ).strip(),
                approval_manager_id=cstr(
                    getattr(event, "approved_by_manager", None)
                ).strip(),
                action="stock_remake",
                idempotency_key=cstr(getattr(event, "remake_id", None)).strip(),
                resource_id=cstr(
                    frappe.db.get_value(
                        "FB Order Line",
                        cstr(getattr(event, "original_order_line", None)).strip(),
                        "line_id",
                    )
                ).strip(),
            )
        )
    return response


def _positive_decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        frappe.throw(f"{label} must be a decimal quantity", frappe.ValidationError)
        raise AssertionError("unreachable")
    if not result.is_finite() or result <= 0:
        frappe.throw(f"{label} must be greater than 0", frappe.ValidationError)
    return result


def _nonnegative_decimal(value: Any, label: str) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        frappe.throw(f"{label} must be a decimal quantity", frappe.ValidationError)
        raise AssertionError("unreachable")
    if not result.is_finite() or result < 0:
        frappe.throw(f"{label} cannot be negative", frappe.ValidationError)
    return result


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)
