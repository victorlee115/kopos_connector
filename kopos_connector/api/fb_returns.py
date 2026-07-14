# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import cint, cstr, flt

from kopos_connector.api.devices import require_device_context
from kopos_connector.kopos.services.operations.return_guard_service import (
    aggregate_return_lines,
    lock_and_validate_return_quantities,
)
from kopos_connector.utils.manager_approval import (
    build_sales_invoice_approval_scope,
    canonical_context_hash,
    load_consumed_manager_approval_proof,
    verify_manager_approval_token,
)


REFUND_METHODS = {"cash", "qr", "card", "voucher"}


@frappe.whitelist(methods=["POST"])
def process_return() -> dict[str, Any]:
    payload = _get_request_payload()
    require_device_context(device_id=cstr(payload.get("device_id")))
    return process_return_payload(payload, require_manager_approval=True)


def process_return_payload(
    payload: dict[str, Any], *, require_manager_approval: bool = False
) -> dict[str, Any]:
    validated = _validate_payload(payload)
    scope = _build_refund_approval_scope(validated)
    request_fingerprint = _return_request_fingerprint(validated, scope)
    validated["request_fingerprint"] = request_fingerprint
    lock_and_validate_return_quantities(
        validated["return_id"],
        validated["lines"],
        validated["original_sales_invoice"],
    )
    existing_return = _get_existing_return_name_current(validated["return_id"])
    if existing_return:
        return_doc = frappe.get_doc("FB Return Event", existing_return)
        _validate_existing_return_matches(validated, return_doc)
        from kopos_connector.kopos.services.operations.return_service import (
            ensure_existing_return_event_settlement,
        )

        ensure_existing_return_event_settlement(return_doc)
        return_doc.reload()
        return _serialize_return_response(
            "duplicate",
            return_doc,
            require_approval_proof=require_manager_approval,
        )
    approval: dict[str, Any] | None = None
    if require_manager_approval:
        approval = verify_manager_approval_token(
            cstr(validated.get("manager_approval_token")).strip(),
            device_id=scope["device_id"],
            staff_id=scope["staff_id"],
            action="refund_order",
            shift_id=scope["shift_id"],
            resource_id=scope["resource_id"],
            amount_sen=scope["amount_sen"],
            context_hash=scope["context_hash"],
            idempotency_key=validated["return_id"],
        )
    return_doc = _build_return_event(validated, approval=approval)
    try:
        return_doc.insert(ignore_permissions=True)
        return_doc.submit()
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        existing_return = _get_existing_return_name_current(validated["return_id"])
        if not existing_return:
            raise
        return_doc = frappe.get_doc("FB Return Event", existing_return)
        _validate_existing_return_matches(validated, return_doc)
    return_doc.reload()
    return _serialize_return_response(
        "duplicate" if existing_return else "ok",
        return_doc,
        require_approval_proof=require_manager_approval,
    )


def build_refund_approval_scope(payload: dict[str, Any]) -> dict[str, Any]:
    validated = _validate_payload(payload)
    return _build_refund_approval_scope(validated)


def _get_request_payload() -> dict[str, Any]:
    request_json = None
    if getattr(frappe, "request", None):
        request_json = frappe.request.get_json(silent=True)
    if isinstance(request_json, Mapping):
        return dict(request_json)
    return dict(frappe.form_dict or {})


def _get_existing_return_name_current(return_id: str) -> str | None:
    """Read idempotency state after sale locking without an older RR snapshot."""
    rows = frappe.db.sql(
        """
        SELECT name
        FROM `tabFB Return Event`
        WHERE return_id = %s
        ORDER BY name
        LIMIT 1
        FOR UPDATE
        """,
        (return_id,),
        as_dict=True,
    )
    if not rows:
        return None
    return cstr(_row_value(rows[0], "name")).strip() or None


def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return_id = cstr(payload.get("return_id") or payload.get("idempotency_key"))
    device_id = cstr(payload.get("device_id")).strip() or None
    fb_order = cstr(payload.get("fb_order")) or None
    original_sales_invoice = cstr(payload.get("original_sales_invoice")) or None
    reason_code = cstr(payload.get("reason_code")) or "Other"
    reason_text = cstr(payload.get("reason_text")) or None
    refund_method = cstr(payload.get("refund_method")).strip().lower()
    return_to_stock = 1 if cint(payload.get("return_to_stock")) else 0
    lines = payload.get("lines")
    manager_approval_token = cstr(payload.get("manager_approval_token")).strip()

    if not return_id:
        frappe.throw("return_id is required", frappe.ValidationError)
    if not device_id:
        frappe.throw("device_id is required", frappe.ValidationError)
    if refund_method not in REFUND_METHODS:
        frappe.throw(
            "refund_method must be one of: cash, qr, card, voucher",
            frappe.ValidationError,
        )
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
    validated_lines = aggregate_return_lines(validated_lines)

    if not original_sales_invoice:
        resolved_sale = frappe.get_doc(
            "FB Resolved Sale", validated_lines[0]["original_resolved_sale"]
        )
        original_sales_invoice = (
            cstr(getattr(resolved_sale, "sales_invoice", None)) or None
        )
    original_sales_invoice = cstr(original_sales_invoice).strip()
    if not original_sales_invoice:
        frappe.throw("original_sales_invoice is required", frappe.ValidationError)
    if not frappe.db.exists("Sales Invoice", original_sales_invoice):
        frappe.throw(
            f"Sales Invoice {original_sales_invoice} was not found",
            frappe.ValidationError,
        )
    locked_cash_shift = _lock_sales_invoice_cash_shift(original_sales_invoice)
    locked_invoices = frappe.db.sql(
        """
        SELECT
            name, docstatus, is_return, custom_fb_order,
            custom_fb_device_id, custom_fb_shift, grand_total
        FROM `tabSales Invoice`
        WHERE name = %s
        FOR UPDATE
        """,
        (original_sales_invoice,),
        as_dict=True,
    )
    original_invoice = frappe.get_doc("Sales Invoice", original_sales_invoice)
    if locked_invoices:
        for fieldname in (
            "docstatus",
            "is_return",
            "custom_fb_order",
            "custom_fb_device_id",
            "custom_fb_shift",
            "grand_total",
        ):
            locked_value = _row_value(locked_invoices[0], fieldname)
            if locked_value is not None:
                setattr(original_invoice, fieldname, locked_value)
    invoice_cash_shift = cstr(
        getattr(original_invoice, "custom_fb_shift", None)
    ).strip()
    if not locked_cash_shift or invoice_cash_shift != locked_cash_shift:
        frappe.throw(
            f"Sales Invoice {original_sales_invoice} has no stable FB Shift cash scope; retry after repairing its shift link",
            frappe.ValidationError,
        )
    _validate_original_invoice(original_invoice, original_sales_invoice, device_id)
    resolved_fb_order = _resolve_fb_order(original_invoice, fb_order)
    if fb_order and resolved_fb_order and fb_order != resolved_fb_order:
        frappe.throw(
            f"FB Order {fb_order} does not match Sales Invoice {original_sales_invoice}",
            frappe.ValidationError,
        )
    fb_order = fb_order or resolved_fb_order
    _validate_return_lines_belong_to_invoice(
        validated_lines,
        original_sales_invoice,
        fb_order,
    )
    _validate_full_return_lines(
        validated_lines,
        original_sales_invoice,
        fb_order,
    )
    return {
        "return_id": return_id,
        "device_id": device_id,
        "fb_order": fb_order,
        "original_sales_invoice": original_sales_invoice,
        "reason_code": reason_code,
        "reason_text": reason_text,
        "refund_method": refund_method,
        "return_to_stock": return_to_stock,
        "lines": validated_lines,
        "manager_approval_token": manager_approval_token,
    }


def _lock_sales_invoice_cash_shift(sales_invoice: str) -> str:
    """Lock the shift before the invoice to keep refund cash ordering stable."""
    from kopos_connector.kopos.services.accounting.return_invoice_service import (
        lock_fb_shift_cash_scope,
    )

    shift_name = cstr(
        frappe.db.get_value("Sales Invoice", sales_invoice, "custom_fb_shift")
    ).strip()
    if not shift_name:
        return ""
    lock_fb_shift_cash_scope(shift_name)
    return shift_name


def _build_return_event(
    validated: dict[str, Any], *, approval: dict[str, Any] | None = None
):
    doc = frappe.new_doc("FB Return Event")
    doc.return_id = validated["return_id"]
    doc.fb_order = validated["fb_order"]
    doc.original_sales_invoice = validated["original_sales_invoice"]
    doc.reason_code = validated["reason_code"]
    doc.reason_text = validated["reason_text"]
    doc.refund_method = validated["refund_method"]
    doc.return_to_stock = validated["return_to_stock"]
    doc.request_fingerprint = validated["request_fingerprint"]
    if approval:
        doc.approval_token_id = approval["token_id"]
        doc.approved_by_manager = approval["manager_id"]
    doc.status = "Draft"
    for line in validated["lines"]:
        doc.append("lines", line)
    return doc


def _build_refund_approval_scope(validated: dict[str, Any]) -> dict[str, Any]:
    invoice = frappe.get_doc(
        "Sales Invoice", validated["original_sales_invoice"]
    )
    scope = build_sales_invoice_approval_scope(
        invoice,
        context=_return_approval_context(validated),
    )
    if scope["device_id"] != validated["device_id"]:
        frappe.throw(
            "Refund device_id does not match the ERP Sales Invoice",
            frappe.ValidationError,
        )
    if validated.get("fb_order") and scope["fb_order"] != validated["fb_order"]:
        frappe.throw(
            "Refund FB Order does not match the ERP Sales Invoice",
            frappe.ValidationError,
        )
    return scope


def _return_approval_context(validated: dict[str, Any]) -> dict[str, Any]:
    lines = sorted(
        (
            {
                "original_resolved_sale": cstr(
                    line["original_resolved_sale"]
                ).strip(),
                "qty_returned": _canonical_decimal(line["qty_returned"]),
            }
            for line in validated["lines"]
        ),
        key=lambda line: line["original_resolved_sale"],
    )
    return {
        "reason_code": cstr(validated["reason_code"]).strip(),
        "reason_text": cstr(validated.get("reason_text")).strip() or None,
        "refund_method": cstr(validated["refund_method"]).strip().lower(),
        "return_to_stock": cint(validated["return_to_stock"]),
        "lines": lines,
    }


def _return_request_fingerprint(
    validated: dict[str, Any], scope: dict[str, Any]
) -> str:
    return canonical_context_hash(
        {
            "return_id": validated["return_id"],
            "device_id": scope["device_id"],
            "staff_id": scope["staff_id"],
            "shift_id": scope["shift_id"],
            "resource_id": scope["resource_id"],
            "amount_sen": scope["amount_sen"],
            "fb_order": scope["fb_order"],
            "context": _return_approval_context(validated),
        }
    )


def _canonical_decimal(value: Any) -> str:
    amount = Decimal(cstr(value).strip())
    return format(amount.normalize(), "f")


def _validate_original_invoice(
    original_invoice: Any, original_sales_invoice: str, device_id: str | None
) -> None:
    if cint(getattr(original_invoice, "docstatus", 0)) != 1:
        frappe.throw(
            f"Sales Invoice {original_sales_invoice} is not submitted",
            frappe.ValidationError,
        )
    if cint(getattr(original_invoice, "is_return", 0)):
        frappe.throw(
            f"Sales Invoice {original_sales_invoice} is already a return invoice",
            frappe.ValidationError,
        )
    invoice_device_id = cstr(getattr(original_invoice, "custom_fb_device_id", "")).strip()
    if not invoice_device_id:
        frappe.throw(
            f"Sales Invoice {original_sales_invoice} has no device ownership context",
            frappe.ValidationError,
        )
    if invoice_device_id != device_id:
        frappe.throw(
            f"Sales Invoice {original_sales_invoice} belongs to another device",
            frappe.ValidationError,
        )


def _resolve_fb_order(original_invoice: Any, fb_order: str | None) -> str | None:
    if fb_order:
        return fb_order
    return cstr(getattr(original_invoice, "custom_fb_order", None)).strip() or None


def _validate_return_lines_belong_to_invoice(
    lines: list[dict[str, Any]], original_sales_invoice: str, fb_order: str | None
) -> None:
    for line in lines:
        resolved_sale_name = line["original_resolved_sale"]
        resolved_sale = frappe.get_doc("FB Resolved Sale", resolved_sale_name)
        resolved_sale_invoice = cstr(getattr(resolved_sale, "sales_invoice", None)).strip()
        if resolved_sale_invoice and resolved_sale_invoice != original_sales_invoice:
            frappe.throw(
                f"FB Resolved Sale {resolved_sale_name} does not belong to Sales Invoice {original_sales_invoice}",
                frappe.ValidationError,
            )
        resolved_sale_order = cstr(getattr(resolved_sale, "fb_order", None)).strip()
        if fb_order and resolved_sale_order and resolved_sale_order != fb_order:
            frappe.throw(
                f"FB Resolved Sale {resolved_sale_name} does not belong to FB Order {fb_order}",
                frappe.ValidationError,
            )


def _validate_existing_return_matches(validated: dict[str, Any], return_doc: Any) -> None:
    existing_fingerprint = cstr(
        getattr(return_doc, "request_fingerprint", None)
    ).strip()
    if not existing_fingerprint:
        frappe.throw(
            "Existing FB Return Event has no request fingerprint; retry cannot be verified",
            frappe.ValidationError,
        )
    if existing_fingerprint != validated["request_fingerprint"]:
        frappe.throw(
            "return_id was already used with a different canonical payload",
            frappe.ValidationError,
        )
    existing_invoice = cstr(getattr(return_doc, "original_sales_invoice", None)).strip()
    if existing_invoice and existing_invoice != validated["original_sales_invoice"]:
        frappe.throw(
            "return_id was already used for a different Sales Invoice",
            frappe.ValidationError,
        )
    existing_order = cstr(getattr(return_doc, "fb_order", None)).strip()
    if existing_order and validated.get("fb_order") and existing_order != validated["fb_order"]:
        frappe.throw(
            "return_id was already used for a different FB Order",
            frappe.ValidationError,
        )
    if cint(getattr(return_doc, "return_to_stock", 0)) != cint(validated["return_to_stock"]):
        frappe.throw(
            "return_id was already used with different return_to_stock intent",
            frappe.ValidationError,
        )
    existing_refund_method = cstr(
        getattr(return_doc, "refund_method", "")
    ).strip().lower()
    if existing_refund_method and existing_refund_method != validated["refund_method"]:
        frappe.throw(
            "return_id was already used with a different refund_method",
            frappe.ValidationError,
        )
    if not existing_refund_method:
        db_set = getattr(return_doc, "db_set", None)
        if callable(db_set):
            db_set("refund_method", validated["refund_method"], update_modified=False)
        else:
            return_doc.refund_method = validated["refund_method"]
    existing_lines = {
        cstr(line["original_resolved_sale"]).strip(): flt(line["qty_returned"])
        for line in aggregate_return_lines(
            [
                {
                    "original_resolved_sale": getattr(
                        line, "original_resolved_sale", ""
                    ),
                    "qty_returned": getattr(line, "qty_returned", 0),
                }
                for line in (return_doc.get("lines") or [])
            ]
        )
    }
    requested_lines = {
        cstr(line["original_resolved_sale"]).strip(): flt(line["qty_returned"])
        for line in validated["lines"]
    }
    if existing_lines != requested_lines:
        frappe.throw(
            "return_id was already used with different return lines",
            frappe.ValidationError,
        )


def _validate_full_return_lines(
    lines: list[dict[str, Any]],
    original_sales_invoice: str,
    fb_order: str | None,
) -> None:
    filters: dict[str, Any] = {"sales_invoice": original_sales_invoice}
    if fb_order:
        filters["fb_order"] = fb_order
    resolved_sales = frappe.get_all(
        "FB Resolved Sale",
        filters=filters,
        fields=["name", "qty"],
        order_by="name asc",
    )
    expected = {
        cstr(_row_value(row, "name")).strip(): flt(_row_value(row, "qty"))
        for row in resolved_sales or []
        if cstr(_row_value(row, "name")).strip()
    }
    requested = {
        cstr(line.get("original_resolved_sale")).strip(): flt(
            line.get("qty_returned")
        )
        for line in lines
        if cstr(line.get("original_resolved_sale")).strip()
    }
    if not expected or requested != expected:
        frappe.throw(
            "Partial ERP returns are not supported; refund lines must exactly match the full Sales Invoice",
            frappe.ValidationError,
        )


def _serialize_return_response(
    status: str,
    return_doc: Any,
    *,
    require_approval_proof: bool = False,
) -> dict[str, Any]:
    from kopos_connector.kopos.services.accounting.return_settlement_service import (
        assert_return_settlement_posted,
    )

    assert_return_settlement_posted(return_doc)
    return_sales_invoice = cstr(
        getattr(return_doc, "return_sales_invoice", None)
    ).strip()
    settlement_doctype = cstr(
        getattr(return_doc, "settlement_doctype", None)
    ).strip()
    settlement_document = cstr(
        getattr(return_doc, "settlement_document", None)
    ).strip()
    settlement_status = cstr(
        getattr(return_doc, "settlement_status", None)
    ).strip()
    if (
        not return_sales_invoice
        or settlement_doctype not in {"Payment Entry", "Journal Entry"}
        or not settlement_document
        or settlement_status != "Posted"
    ):
        frappe.throw(
            f"FB Return Event {return_doc.name} has no posted accounting settlement proof",
            frappe.ValidationError,
        )

    response = {
        "status": status,
        "return_event": return_doc.name,
        "return_sales_invoice": return_sales_invoice,
        "settlement_doctype": settlement_doctype,
        "settlement_document": settlement_document,
        "settlement_status": settlement_status,
        "return_to_stock": cint(getattr(return_doc, "return_to_stock", 0)),
        "reversal_stock_entries": [
            cstr(getattr(line, "reversal_stock_entry", None))
            for line in (return_doc.get("lines") or [])
            if cstr(getattr(line, "reversal_stock_entry", None))
        ],
    }
    if require_approval_proof:
        response.update(
            load_consumed_manager_approval_proof(
                approval_token_id=cstr(
                    getattr(return_doc, "approval_token_id", None)
                ).strip(),
                approval_manager_id=cstr(
                    getattr(return_doc, "approved_by_manager", None)
                ).strip(),
                action="refund_order",
                idempotency_key=cstr(getattr(return_doc, "return_id", None)).strip(),
                resource_id=cstr(
                    getattr(return_doc, "original_sales_invoice", None)
                ).strip(),
            )
        )
    return response


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)
