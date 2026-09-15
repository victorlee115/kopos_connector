# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import cint, cstr, flt

from kopos_connector.api.devices import lock_device_for_operational_mutation
from kopos_connector.api.operational_errors import RefundChangedError
from kopos_connector.kopos.services.accounting.refund_allocation import (
    REFUND_ALLOCATION_VERSION,
    RefundAllocation,
    RefundAllocationError,
    RefundableSale,
    RefundableSaleLine,
    allocate_refund,
    parse_stored_allocation,
    refund_position,
    serialize_allocation,
)
from kopos_connector.kopos.services.operations.commercial_return_identity import (
    build_full_commercial_return_lines as _build_full_commercial_return_lines,
)
from kopos_connector.kopos.services.operations.return_guard_service import (
    aggregate_return_lines,
    lock_and_validate_return_quantities,
)
from kopos_connector.utils.manager_approval import (
    build_sales_invoice_approval_scope,
    canonical_context_hash,
    load_consumed_manager_approval_proof,
    money_to_sen,
    parse_integer_sen,
    verify_manager_approval_token,
)


REFUND_METHODS = {"cash", "qr", "card", "voucher"}
INVENTORY_EVALUATION_INCLUDED = "included_evaluated"
LEGACY_INVENTORY_EVALUATION_EXCLUDED = "excluded_not_evaluated"
REFUND_ALLOCATION_MESSAGES = {
    "unknown_line": "A refund line is not part of this sale.",
    "duplicate_line": "A refund line is listed twice.",
    "invalid_quantity": "Refund quantities must be whole numbers above zero.",
    "exceeds_remaining": "Those items have already been refunded.",
    "nothing_to_refund": "These items have no paid amount left to refund. Add another item.",
    "invalid_sale": "This sale's saved amounts cannot be used for a part refund. Ask support.",
}


@frappe.whitelist(methods=["POST"])
def process_return() -> dict[str, Any]:
    payload = _get_request_payload()
    lock_device_for_operational_mutation(device_id=cstr(payload.get("device_id")))
    return process_return_payload(payload, require_manager_approval=True)


def process_return_payload(
    payload: dict[str, Any], *, require_manager_approval: bool = False
) -> dict[str, Any]:
    validated = _validate_payload(payload)
    scope = _build_refund_approval_scope(validated)
    request_fingerprint = _return_request_fingerprint(validated, scope)
    validated["request_fingerprint"] = request_fingerprint
    validated["legacy_request_fingerprint"] = _return_request_fingerprint(
        validated,
        scope,
        include_inventory_evaluation=False,
    )
    legacy_stock_validated = dict(validated)
    legacy_stock_validated["return_to_stock"] = 1
    validated["legacy_stock_request_fingerprint"] = _return_request_fingerprint(
        legacy_stock_validated,
        scope,
        include_inventory_evaluation=False,
    )
    partial = bool(validated.get("refund_allocation_version"))
    returned_by_item = lock_and_validate_return_quantities(
        validated["return_id"],
        validated["lines"],
        validated["original_sales_invoice"],
        partial=partial,
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
    allocation = None
    if partial:
        # Only a new refund is priced. A replay keeps the allocation it was
        # created with, whatever other refunds of the sale came later.
        allocation = _allocate_partial_refund(validated, returned_by_item)
        if allocation.amount_sen != validated["amount_sen"]:
            frappe.throw(
                "The refund amount changed. Review the refund again.",
                RefundChangedError,
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
    return_doc = _build_return_event(validated, approval=approval, allocation=allocation)
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
    if validated.get("refund_allocation_version"):
        # Ask a manager only for an amount ERP would post now. Processing prices
        # the refund again under locks before anything is created.
        allocation = _allocate_partial_refund(
            validated,
            _submitted_returned_quantities(validated),
        )
        if allocation.amount_sen != validated["amount_sen"]:
            frappe.throw(
                "The refund amount changed. Review the refund again.",
                RefundChangedError,
            )
    return _build_refund_approval_scope(validated)


def list_order_refunds(sales_invoice: str) -> dict[str, Any]:
    """Submitted refunds of one sale, oldest first, so its tablet can record any it missed."""
    rows = frappe.get_all(
        "FB Return Event",
        filters={"original_sales_invoice": sales_invoice, "docstatus": 1},
        fields=[
            "name",
            "return_id",
            "return_sales_invoice",
            "settlement_status",
            "settlement_amount",
            "refund_method",
            "reason_text",
            "approved_by_manager",
            "refund_allocation",
        ],
        order_by="creation asc, name asc",
    )
    refunds = []
    for row in rows or []:
        allocation = parse_stored_allocation(_row_value(row, "refund_allocation"))
        refunds.append(
            {
                "return_id": cstr(_row_value(row, "return_id")).strip(),
                "return_event": cstr(_row_value(row, "name")).strip(),
                "return_sales_invoice": cstr(_row_value(row, "return_sales_invoice")).strip() or None,
                "settlement_status": cstr(_row_value(row, "settlement_status")).strip(),
                "amount_sen": money_to_sen(
                    _row_value(row, "settlement_amount") or 0,
                    "FB Return Event settlement_amount",
                ),
                "refund_method": cstr(_row_value(row, "refund_method")).strip().lower(),
                "reason_text": cstr(_row_value(row, "reason_text")).strip() or None,
                "approval_manager_id": cstr(_row_value(row, "approved_by_manager")).strip() or None,
                "refund_allocation": allocation.to_wire() if allocation is not None else None,
            }
        )
    return {"status": "ok", "sales_invoice": sales_invoice, "refunds": refunds}


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
    requested_return_to_stock = 1 if cint(payload.get("return_to_stock")) else 0
    inventory_evaluation = cstr(payload.get("inventory_evaluation")).strip()
    legacy_return_to_stock_normalized = bool(
        requested_return_to_stock and not inventory_evaluation
    )
    lines = payload.get("lines")
    manager_approval_token = cstr(payload.get("manager_approval_token")).strip()
    refund_allocation_version = cstr(payload.get("refund_allocation_version")).strip()
    requested_amount_sen = None
    partial_lines: list[tuple[str, int]] = []
    if refund_allocation_version:
        if refund_allocation_version != REFUND_ALLOCATION_VERSION:
            frappe.throw("This refund allocation version is not supported", frappe.ValidationError)
        requested_amount_sen = parse_integer_sen(payload.get("amount_sen"))
        if requested_amount_sen <= 0:
            frappe.throw("amount_sen must be greater than 0", frappe.ValidationError)
        partial_lines = _parse_partial_refund_lines(lines)

    if not return_id:
        frappe.throw("return_id is required", frappe.ValidationError)
    if not device_id:
        frappe.throw("device_id is required", frappe.ValidationError)
    if refund_method not in REFUND_METHODS:
        frappe.throw(
            "refund_method must be one of: cash, qr, card, voucher",
            frappe.ValidationError,
        )
    if inventory_evaluation and inventory_evaluation not in {
        INVENTORY_EVALUATION_INCLUDED,
        LEGACY_INVENTORY_EVALUATION_EXCLUDED,
    }:
        frappe.throw(
            "inventory_evaluation must be included_evaluated for this commercial refund",
            frappe.ValidationError,
        )
    if requested_return_to_stock and inventory_evaluation:
        frappe.throw(
            "This refund does not change stock. Send return_to_stock as false and try again.",
            frappe.ValidationError,
        )
    # Older tablets may already have queued `return_to_stock: true` or the
    # superseded exclusion marker. Preserve the commercial refund, but
    # normalize both onto the included/evaluated no-stock-change contract. New
    # clients are rejected when they send contradictory stock-return intent.
    return_to_stock = 0
    if not original_sales_invoice and fb_order:
        original_sales_invoice = cstr(
            frappe.db.get_value("FB Order", fb_order, "sales_invoice")
        ).strip() or None
    legacy_recipe_lines = (
        isinstance(lines, list)
        and bool(lines)
        and all(
            isinstance(row, Mapping)
            and not cstr(row.get("original_sales_invoice_item")).strip()
            and bool(
                cstr(
                    row.get("original_resolved_sale") or row.get("resolved_sale_id")
                ).strip()
            )
            for row in lines
        )
    )
    if refund_allocation_version:
        # Tablet line identities are bound to invoice items once it is locked.
        lines = []
    elif not isinstance(lines, list) or not lines or legacy_recipe_lines:
        if not original_sales_invoice:
            frappe.throw("lines must contain at least one row", frappe.ValidationError)
        # Older offline payloads named recipe-era FB Resolved Sale rows. A
        # cashier refund is always a full-invoice operation, so rebuild its
        # immutable line set from the submitted Sales Invoice instead of
        # reading the optional recipe subsystem or rejecting the queued work.
        lines = _build_full_commercial_return_lines(
            original_sales_invoice,
            fb_order,
        )
        if not lines:
            frappe.throw(
                f"Sales Invoice {original_sales_invoice} has no durable commercial lines to return",
                frappe.ValidationError,
            )

    validated_lines = []
    for index, row in enumerate(lines, start=1):
        if not isinstance(row, Mapping):
            frappe.throw(f"lines[{index}] must be an object", frappe.ValidationError)
        original_sales_invoice_item = cstr(
            row.get("original_sales_invoice_item")
        ).strip()
        original_fb_order_line_ref = cstr(
            row.get("original_fb_order_line_ref")
        ).strip()
        qty_returned = flt(row.get("qty_returned") or row.get("qty"))
        if not original_sales_invoice_item:
            frappe.throw(
                f"lines[{index}] requires a Sales Invoice item identity",
                frappe.ValidationError,
            )
        if qty_returned <= 0:
            frappe.throw(
                f"lines[{index}].qty_returned must be greater than 0",
                frappe.ValidationError,
            )
        validated_lines.append(
            {
                "original_sales_invoice_item": original_sales_invoice_item or None,
                "original_fb_order_line_ref": original_fb_order_line_ref or None,
                "original_resolved_sale": None,
                "qty_returned": qty_returned,
                "commercial_modifier_snapshot_json": cstr(
                    row.get("commercial_modifier_snapshot_json")
                ).strip(),
            }
        )
    validated_lines = aggregate_return_lines(validated_lines)

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
    if refund_allocation_version:
        validated_lines = _map_partial_refund_lines(partial_lines, original_invoice)
    _validate_return_lines_belong_to_invoice(
        validated_lines,
        original_sales_invoice,
        fb_order,
    )
    if not refund_allocation_version:
        validated_lines = _validate_full_return_lines(
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
        "inventory_evaluation": INVENTORY_EVALUATION_INCLUDED,
        "legacy_return_to_stock_normalized": legacy_return_to_stock_normalized,
        "lines": validated_lines,
        "manager_approval_token": manager_approval_token,
        "refund_allocation_version": refund_allocation_version or None,
        "amount_sen": requested_amount_sen,
    }


def _parse_partial_refund_lines(lines: Any) -> list[tuple[str, int]]:
    if not isinstance(lines, list) or not lines:
        frappe.throw("Choose at least one item to refund", frappe.ValidationError)
    parsed: list[tuple[str, int]] = []
    seen: set[str] = set()
    for index, row in enumerate(lines, start=1):
        if not isinstance(row, Mapping):
            frappe.throw(f"lines[{index}] must be an object", frappe.ValidationError)
        line_ref = cstr(row.get("original_fb_order_line_ref")).strip()
        quantity = row.get("qty_returned")
        if not line_ref:
            frappe.throw(f"lines[{index}] requires original_fb_order_line_ref", frappe.ValidationError)
        if line_ref in seen:
            frappe.throw(REFUND_ALLOCATION_MESSAGES["duplicate_line"], frappe.ValidationError)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            frappe.throw(REFUND_ALLOCATION_MESSAGES["invalid_quantity"], frappe.ValidationError)
        seen.add(line_ref)
        parsed.append((line_ref, quantity))
    return parsed


def _map_partial_refund_lines(
    partial_lines: list[tuple[str, int]], original_invoice: Any
) -> list[dict[str, Any]]:
    """Bind tablet line identities to the invoice's own items, never to client fields."""
    items_by_ref: dict[str, list[str]] = {}
    for item in original_invoice.get("items") or []:
        line_ref = cstr(getattr(item, "custom_fb_order_line_ref", None)).strip()
        if line_ref:
            items_by_ref.setdefault(line_ref, []).append(cstr(item.name).strip())
    mapped = []
    for line_ref, quantity in partial_lines:
        item_names = items_by_ref.get(line_ref) or []
        if len(item_names) != 1:
            frappe.throw(REFUND_ALLOCATION_MESSAGES["unknown_line"], frappe.ValidationError)
        mapped.append(
            {
                "original_sales_invoice_item": item_names[0],
                "original_fb_order_line_ref": line_ref,
                "original_resolved_sale": None,
                "qty_returned": float(quantity),
                "commercial_modifier_snapshot_json": "",
            }
        )
    return aggregate_return_lines(mapped)


def _refundable_sale(validated: dict[str, Any]) -> RefundableSale:
    """The paid sale as ERP posted it, cross-checked against its FB Order."""
    invoice = frappe.get_doc("Sales Invoice", validated["original_sales_invoice"])
    lines = []
    for item in invoice.get("items") or []:
        line_ref = cstr(getattr(item, "custom_fb_order_line_ref", None)).strip()
        quantity = Decimal(cstr(getattr(item, "qty", None) or 0))
        if not line_ref or quantity <= 0 or quantity != quantity.to_integral_value():
            frappe.throw(REFUND_ALLOCATION_MESSAGES["invalid_sale"], frappe.ValidationError)
        lines.append(
            RefundableSaleLine(
                line_ref,
                int(quantity),
                money_to_sen(getattr(item, "amount", None), "Sales Invoice Item amount"),
            )
        )
    write_off_sen = money_to_sen(getattr(invoice, "write_off_amount", None) or 0, "Sales Invoice write_off_amount")
    grand_total_sen = money_to_sen(getattr(invoice, "grand_total", None), "Sales Invoice grand_total")
    sale = RefundableSale(
        lines=tuple(lines),
        tax_sen=money_to_sen(getattr(invoice, "total_taxes_and_charges", None) or 0, "Sales Invoice total_taxes_and_charges"),
        rounding_sen=-write_off_sen,
        total_sen=grand_total_sen - write_off_sen,
        cash_unit_sen=5 if validated["refund_method"] == "cash" else 1,
    )
    order = frappe.get_doc("FB Order", validated["fb_order"]) if validated.get("fb_order") else None
    if order is not None:
        order_lines = {
            cstr(getattr(row, "line_id", None)).strip(): money_to_sen(getattr(row, "line_total", None), "FB Order line_total")
            for row in order.get("items") or []
        }
        if (
            order_lines != {line.line_id: line.line_total_sen for line in sale.lines}
            or money_to_sen(getattr(order, "tax_total", None) or 0, "FB Order tax_total") != sale.tax_sen
            or money_to_sen(getattr(order, "rounding_adjustment", None) or 0, "FB Order rounding_adjustment") != sale.rounding_sen
            or money_to_sen(getattr(order, "grand_total", None), "FB Order grand_total") != sale.total_sen
        ):
            frappe.throw(REFUND_ALLOCATION_MESSAGES["invalid_sale"], frappe.ValidationError)
    return sale


def _submitted_returned_quantities(validated: dict[str, Any]) -> dict[str, Decimal]:
    rows = frappe.db.sql(
        """
        SELECT return_line.original_sales_invoice_item, return_line.qty_returned
        FROM `tabFB Return Event Line` AS return_line
        INNER JOIN `tabFB Return Event` AS return_event
            ON return_event.name = return_line.parent
        WHERE return_line.parenttype = 'FB Return Event'
            AND return_event.docstatus = 1
            AND return_event.original_sales_invoice = %s
            AND return_event.return_id != %s
        """,
        (validated["original_sales_invoice"], validated["return_id"]),
        as_dict=True,
    )
    returned: dict[str, Decimal] = {}
    for row in rows or []:
        item = cstr(_row_value(row, "original_sales_invoice_item")).strip()
        returned[item] = returned.get(item, Decimal("0")) + Decimal(cstr(_row_value(row, "qty_returned") or 0))
    return returned


def _allocate_partial_refund(
    validated: dict[str, Any], returned_by_item: Mapping[str, Decimal]
) -> RefundAllocation:
    sale = _refundable_sale(validated)
    invoice = frappe.get_doc("Sales Invoice", validated["original_sales_invoice"])
    line_by_item = {
        cstr(item.name).strip(): cstr(getattr(item, "custom_fb_order_line_ref", None)).strip()
        for item in invoice.get("items") or []
    }
    before = []
    for item, quantity in returned_by_item.items():
        if quantity != quantity.to_integral_value() or item not in line_by_item:
            frappe.throw(REFUND_ALLOCATION_MESSAGES["invalid_sale"], frappe.ValidationError)
        before.append({"line_id": line_by_item[item], "quantity": int(quantity)})
    request = [
        {"line_id": cstr(line["original_fb_order_line_ref"]).strip(), "quantity": int(Decimal(cstr(line["qty_returned"])))}
        for line in validated["lines"]
    ]
    try:
        position = refund_position(sale, before)
        allocation = allocate_refund(sale, before, request)
    except RefundAllocationError as error:
        frappe.throw(REFUND_ALLOCATION_MESSAGES.get(error.code, str(error)), frappe.ValidationError)
        raise AssertionError("unreachable") from error
    if _posted_refund_amount_sen(validated) != position["amount_sen"]:
        frappe.throw(
            "Earlier refunds of this sale do not match its saved amounts. Ask support.",
            frappe.ValidationError,
        )
    return allocation


def _posted_refund_amount_sen(validated: dict[str, Any]) -> int:
    rows = frappe.get_all(
        "FB Return Event",
        filters={
            "original_sales_invoice": validated["original_sales_invoice"],
            "docstatus": 1,
            "return_id": ["!=", validated["return_id"]],
        },
        fields=["settlement_amount"],
    )
    return sum(money_to_sen(_row_value(row, "settlement_amount") or 0, "FB Return Event settlement_amount") for row in rows or [])


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
    validated: dict[str, Any],
    *,
    approval: dict[str, Any] | None = None,
    allocation: RefundAllocation | None = None,
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
    if allocation is not None:
        doc.refund_allocation_version = allocation.version
        doc.refund_allocation = serialize_allocation(allocation)
        doc.refund_amount = Decimal(allocation.amount_sen) / Decimal(100)
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
    if validated.get("refund_allocation_version"):
        scope["amount_sen"] = validated["amount_sen"]
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
        (_canonical_return_line(line) for line in validated["lines"]),
        key=lambda line: cstr(
            line.get("original_sales_invoice_item")
            or line.get("original_resolved_sale")
        ),
    )
    context = {
        "reason_code": cstr(validated["reason_code"]).strip(),
        "reason_text": cstr(validated.get("reason_text")).strip() or None,
        "refund_method": cstr(validated["refund_method"]).strip().lower(),
        "return_to_stock": cint(validated["return_to_stock"]),
        "lines": lines,
    }
    if validated.get("refund_allocation_version"):
        # Historical full-refund hashes stay byte-identical without this key.
        context["refund_allocation_version"] = validated["refund_allocation_version"]
    return context


def _return_request_fingerprint(
    validated: dict[str, Any],
    scope: dict[str, Any],
    *,
    include_inventory_evaluation: bool = True,
) -> str:
    context = _return_approval_context(validated)
    if include_inventory_evaluation:
        # Inventory policy is server-owned, so it is deliberately absent from
        # the short-lived manager approval scope. Bind it here to the durable
        # request identity while keeping pre-deployment approval tokens valid.
        context["inventory_evaluation"] = cstr(
            validated["inventory_evaluation"]
        ).strip()
    return canonical_context_hash(
        {
            "return_id": validated["return_id"],
            "device_id": scope["device_id"],
            "staff_id": scope["staff_id"],
            "shift_id": scope["shift_id"],
            "resource_id": scope["resource_id"],
            "amount_sen": scope["amount_sen"],
            "fb_order": scope["fb_order"],
            "context": context,
        }
    )


def _canonical_decimal(value: Any) -> str:
    amount = Decimal(cstr(value).strip())
    return format(amount.normalize(), "f")


def _canonical_return_line(line: Any) -> dict[str, Any]:
    invoice_item = cstr(_line_value(line, "original_sales_invoice_item")).strip()
    if invoice_item:
        return {
            "original_sales_invoice_item": invoice_item,
            "qty_returned": _canonical_decimal(
                _line_value(line, "qty_returned")
            ),
        }
    return {
        "original_resolved_sale": cstr(
            _line_value(line, "original_resolved_sale")
        ).strip(),
        "qty_returned": _canonical_decimal(_line_value(line, "qty_returned")),
    }


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
    del original_sales_invoice, fb_order
    if any(
        not cstr(line.get("original_sales_invoice_item")).strip() for line in lines
    ):
        frappe.throw(
            "Every return line requires a Sales Invoice item identity",
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
    existing_return_to_stock = cint(getattr(return_doc, "return_to_stock", 0))
    legacy_contract_retry = bool(
        (
            existing_return_to_stock == 0
            and existing_fingerprint
            == cstr(validated.get("legacy_request_fingerprint")).strip()
        )
        or (
            existing_return_to_stock == 1
            and existing_fingerprint
            == cstr(validated.get("legacy_stock_request_fingerprint")).strip()
        )
    )
    if (
        existing_fingerprint != validated["request_fingerprint"]
        and not legacy_contract_retry
    ):
        frappe.throw(
            "return_id was already used with a different canonical payload",
            frappe.ValidationError,
        )
    if cstr(getattr(return_doc, "refund_allocation_version", None)).strip() != cstr(
        validated.get("refund_allocation_version")
    ).strip():
        frappe.throw(
            "return_id was already used with a different refund type",
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
    if (
        existing_return_to_stock != cint(validated["return_to_stock"])
        and not legacy_contract_retry
    ):
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
    existing_lines = _return_line_comparison_map(return_doc.get("lines") or [])
    requested_lines = _return_line_comparison_map(validated["lines"])
    if existing_lines != requested_lines:
        frappe.throw(
            "return_id was already used with different return lines",
            frappe.ValidationError,
        )


def _validate_full_return_lines(
    lines: list[dict[str, Any]],
    original_sales_invoice: str,
    fb_order: str | None,
) -> list[dict[str, Any]]:
    expected_lines = _build_full_commercial_return_lines(
        original_sales_invoice,
        fb_order,
    )
    if (
        not expected_lines
        or _return_line_comparison_map(lines)
        != _return_line_comparison_map(expected_lines)
    ):
        frappe.throw(
            "Partial ERP returns are not supported; refund lines must exactly match the full Sales Invoice",
            frappe.ValidationError,
        )
    return expected_lines


def _return_line_comparison_map(lines: Any) -> dict[str, str]:
    comparison: dict[str, str] = {}
    normalized_lines = aggregate_return_lines(
        [
            {
                "original_sales_invoice_item": _line_value(
                    line, "original_sales_invoice_item"
                ),
                "original_fb_order_line_ref": _line_value(
                    line, "original_fb_order_line_ref"
                ),
                "original_resolved_sale": _line_value(
                    line, "original_resolved_sale"
                ),
                "qty_returned": _line_value(line, "qty_returned"),
                "commercial_modifier_snapshot_json": _line_value(
                    line, "commercial_modifier_snapshot_json"
                ),
            }
            for line in (lines or [])
        ]
    )
    for line in normalized_lines:
        canonical = _canonical_return_line(line)
        identity = cstr(
            canonical.get("original_sales_invoice_item")
            or canonical.get("original_resolved_sale")
        ).strip()
        comparison[identity] = canonical["qty_returned"]
    return comparison


def _line_value(line: Any, fieldname: str) -> Any:
    if isinstance(line, Mapping):
        return line.get(fieldname)
    return getattr(line, fieldname, None)


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
        # Inventory is explicitly evaluated, but refunding a prepared item does
        # not restore its ingredients to usable stock. Historical events may
        # retain an older cashier-intent bit; the public contract never repeats
        # that obsolete stock claim.
        "return_to_stock": 0,
        "inventory_evaluation": INVENTORY_EVALUATION_INCLUDED,
        "reversal_stock_entries": [],
    }
    allocation = parse_stored_allocation(getattr(return_doc, "refund_allocation", None))
    if allocation is not None:
        response["refund_allocation"] = allocation.to_wire()
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
