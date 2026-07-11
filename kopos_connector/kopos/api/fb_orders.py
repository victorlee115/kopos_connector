from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from typing import Any

frappe = importlib.import_module("frappe")
frappe_utils = importlib.import_module("frappe.utils")

from kopos_connector.kopos.services.orders.sale_datetime import (
    validate_submit_sale_datetime,
    validate_submit_sale_datetime_bounds,
)


def cint(value: Any) -> int:
    return int(frappe_utils.cint(value))


def cstr(value: Any) -> str:
    return str(frappe_utils.cstr(value))


def flt(value: Any) -> float:
    return float(frappe_utils.flt(value))


def now_datetime() -> Any:
    return frappe_utils.now_datetime()


ORDER_PROJECTION_CONFIG = (
    {
        "projection_type": "Sales Invoice",
        "target_field": "sales_invoice",
        "target_doctype": "Sales Invoice",
    },
    {
        "projection_type": "Stock Issue",
        "target_field": "ingredient_stock_entry",
        "target_doctype": "Stock Entry",
    },
)

RETURN_PROJECTION_CONFIG = (
    {
        "projection_type": "Sales Return",
        "target_field": "return_sales_invoice",
        "target_doctype": "Sales Invoice",
    },
    {
        "projection_type": "Stock Reversal",
        "target_field": "return_sales_invoice",
        "target_doctype": "Sales Invoice",
        "enabled_field": "return_to_stock",
    },
)

REMAKE_PROJECTION_CONFIG = (
    {
        "projection_type": "Stock Issue",
        "target_field": "replacement_stock_entry",
        "target_doctype": "Stock Entry",
    },
)

ACCEPTABLE_STOCK_STATUSES = {"Pending", "Posted"}

PROJECTION_SUBSYSTEMS = {
    "Sales Invoice": "sales_invoice",
    "Stock Issue": "stock",
    "Stock Entry": "stock",
    "FB Shift": "shift",
}


def submit_order() -> dict[str, Any]:
    payload = _get_request_payload()
    return submit_order_payload(payload)


def submit_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    validated = _validate_submit_order_payload(payload)
    existing_name = _get_existing_fb_order_name(validated["external_idempotency_key"])
    if existing_name:
        order_doc = frappe.get_doc("FB Order", existing_name)
        return _build_submit_response("duplicate", order_doc)

    try:
        order_doc = _build_fb_order(validated)
        order_doc.insert(ignore_permissions=True)
        order_doc.submit()
    except Exception:
        frappe.db.rollback()
        existing_name = _get_existing_fb_order_name(
            validated["external_idempotency_key"]
        )
        if existing_name:
            order_doc = frappe.get_doc("FB Order", existing_name)
            return _build_submit_response("duplicate", order_doc)
        raise

    return _build_submit_response("ok", order_doc)


def get_order_status(fb_order_name: str) -> dict[str, Any]:
    if not cstr(fb_order_name):
        frappe.throw("fb_order_name is required", frappe.ValidationError)

    order_doc = frappe.get_doc("FB Order", fb_order_name)
    projection_statuses = _get_projection_statuses("FB Order", order_doc.name)
    return {
        "status": "ok",
        "fb_order": order_doc.name,
        "order_id": cstr(order_doc.order_id),
        "sale_datetime": cstr(getattr(order_doc, "sale_datetime", None)) or None,
        "shift_id": cstr(order_doc.shift),
        "staff_id": cstr(order_doc.staff_id),
        "device_id": cstr(order_doc.device_id),
        "event_project": cstr(order_doc.event_project) or None,
        "order_status": cstr(order_doc.status),
        "sales_invoice": cstr(order_doc.sales_invoice) or None,
        "ingredient_stock_entry": cstr(order_doc.ingredient_stock_entry) or None,
        "invoice_status": cstr(order_doc.invoice_status),
        "stock_status": cstr(order_doc.stock_status),
        "projections": projection_statuses,
    }


def retry_failed_projections(fb_order_name: str) -> dict[str, Any]:
    if not cstr(fb_order_name):
        frappe.throw("fb_order_name is required", frappe.ValidationError)

    order_doc = frappe.get_doc("FB Order", fb_order_name)
    failed_logs = frappe.get_all(
        "FB Projection Log",
        filters={
            "source_doctype": "FB Order",
            "source_name": order_doc.name,
            "state": "Failed",
        },
        fields=["name"],
        order_by="creation asc",
    )

    retried = []
    for row in failed_logs:
        retried.append(_retry_projection_log(row.name))

    order_doc.reload()
    return {
        "status": "ok",
        "fb_order": order_doc.name,
        "order_id": cstr(order_doc.order_id),
        "sale_datetime": cstr(getattr(order_doc, "sale_datetime", None)) or None,
        "shift_id": cstr(order_doc.shift),
        "staff_id": cstr(order_doc.staff_id),
        "device_id": cstr(order_doc.device_id),
        "event_project": cstr(order_doc.event_project) or None,
        "order_status": cstr(order_doc.status),
        "sales_invoice": cstr(order_doc.sales_invoice) or None,
        "ingredient_stock_entry": cstr(order_doc.ingredient_stock_entry) or None,
        "invoice_status": cstr(order_doc.invoice_status),
        "stock_status": cstr(order_doc.stock_status),
        "retried": retried,
        "projections": _get_projection_statuses("FB Order", order_doc.name),
    }


def validate_fb_order(doc, method: str | None = None) -> None:
    _validate_fb_order_doc(doc)
    _set_default_order_statuses(doc)


def before_submit_fb_order(doc, method: str | None = None) -> None:
    _validate_fb_order_doc(doc)
    _validate_submit_shift(
        shift_name=cstr(doc.shift),
        device_id=cstr(doc.device_id),
        staff_id=cstr(doc.staff_id),
    )
    doc.status = "Submitted"
    _set_default_order_statuses(doc)


def on_submit_fb_order(doc, method: str | None = None) -> None:
    doc.status = "Submitted"
    _process_projection_bundle(doc, "FB Order", ORDER_PROJECTION_CONFIG)


def validate_fb_return_event(doc, method: str | None = None) -> None:
    _validate_return_event_doc(doc)


def on_submit_fb_return_event(doc, method: str | None = None) -> None:
    doc.status = "Submitted"
    _process_projection_bundle(doc, "FB Return Event", RETURN_PROJECTION_CONFIG)


def validate_fb_remake_event(doc, method: str | None = None) -> None:
    _validate_remake_event_doc(doc)


def on_submit_fb_remake_event(doc, method: str | None = None) -> None:
    doc.status = "Submitted"
    _process_projection_bundle(doc, "FB Remake Event", REMAKE_PROJECTION_CONFIG)


def _get_request_payload() -> dict[str, Any]:
    if not getattr(frappe, "request", None):
        return _coerce_mapping(getattr(frappe.local, "form_dict", None))

    body = frappe.request.get_data(as_text=True) or ""
    if body:
        payload: Any = None
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            frappe.throw(f"invalid JSON payload: {exc.msg}", frappe.ValidationError)
        return _coerce_mapping(payload)

    return _coerce_mapping(getattr(frappe.local, "form_dict", None))


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    frappe.throw("request payload must be a JSON object", frappe.ValidationError)
    return {}


def _validate_submit_order_payload(payload: dict[str, Any]) -> dict[str, Any]:
    order_value = payload.get("order")
    order_payload: dict[str, Any] = (
        dict(order_value) if isinstance(order_value, Mapping) else {}
    )
    order_id = cstr(payload.get("order_id") or order_payload.get("id"))
    idempotency_key = cstr(payload.get("idempotency_key"))
    source = cstr(payload.get("source") or "API")
    device_id = cstr(payload.get("device_id"))
    shift = cstr(payload.get("shift") or payload.get("shift_id"))
    staff_id = cstr(payload.get("staff_id"))
    booth_warehouse = cstr(payload.get("booth_warehouse") or payload.get("warehouse"))
    company = cstr(payload.get("company"))
    currency = cstr(payload.get("currency"))
    customer = cstr(payload.get("customer")) or None
    event_project = cstr(payload.get("event_project")) or None
    notes = cstr(payload.get("notes") or order_payload.get("notes")) or None
    sale_datetime_value = order_payload.get("created_at")
    items = (
        payload.get("items")
        if isinstance(payload.get("items"), list)
        else order_payload.get("items")
    )
    payments = (
        payload.get("payments")
        if isinstance(payload.get("payments"), list)
        else order_payload.get("payments")
    )

    if not order_id:
        frappe.throw("order_id is required", frappe.ValidationError)
    if not idempotency_key:
        frappe.throw("idempotency_key is required", frappe.ValidationError)
    if not device_id:
        frappe.throw("device_id is required", frappe.ValidationError)
    if not shift:
        frappe.throw("shift is required", frappe.ValidationError)
    if not staff_id:
        frappe.throw("staff_id is required", frappe.ValidationError)
    if not booth_warehouse:
        frappe.throw("booth_warehouse is required", frappe.ValidationError)
    if not company:
        frappe.throw("company is required", frappe.ValidationError)
    if not currency:
        frappe.throw("currency is required", frappe.ValidationError)
    if not isinstance(items, list) or not items:
        frappe.throw("items must contain at least one row", frappe.ValidationError)
    if not isinstance(payments, list) or not payments:
        frappe.throw("payments must contain at least one row", frappe.ValidationError)

    sale_datetime = validate_submit_sale_datetime(sale_datetime_value)

    item_rows = items if isinstance(items, list) else []
    payment_rows = payments if isinstance(payments, list) else []

    validated_items = [
        _validate_order_item(row, index) for index, row in enumerate(item_rows, start=1)
    ]
    validated_payments = [
        _validate_order_payment(row, index)
        for index, row in enumerate(payment_rows, start=1)
    ]

    net_total = sum(flt(row["line_total"]) for row in validated_items)
    tax_total = flt(payload.get("tax_total") or order_payload.get("tax_amount"))
    rounding_adjustment = flt(
        payload.get("rounding_adjustment")
        or payload.get("rounding_adj")
        or order_payload.get("rounding_adjustment")
        or order_payload.get("rounding_adj")
    )
    grand_total = flt(payload.get("grand_total") or order_payload.get("total"))
    expected_total = net_total + tax_total + rounding_adjustment
    if grand_total <= 0:
        grand_total = expected_total
    if abs(expected_total - grand_total) > 0.0001:
        frappe.throw(
            "grand_total must equal net_total plus tax_total",
            frappe.ValidationError,
        )

    paid_total = sum(flt(row["amount"]) for row in validated_payments)
    if abs(paid_total - grand_total) > 0.0001:
        frappe.throw("payments total must equal grand_total", frappe.ValidationError)

    shift_name = _resolve_fb_shift_name(shift)
    if not shift_name:
        frappe.throw(f"shift {shift} was not found", frappe.ValidationError)
    assert shift_name is not None
    shift_doc = _validate_submit_shift(
        shift_name=shift_name,
        device_id=device_id,
        staff_id=staff_id,
    )
    validate_submit_sale_datetime_bounds(
        sale_datetime,
        shift_name=shift_name,
        shift_opened_at=getattr(shift_doc, "opened_at", None),
    )
    _require_doc("Warehouse", booth_warehouse, "booth_warehouse")
    _require_doc("Company", company, "company")
    _require_doc("User", staff_id, "staff_id")
    if customer:
        _require_doc("Customer", customer, "customer")
    if event_project:
        _require_doc("Project", event_project, "event_project")

    return {
        "order_id": order_id,
        "external_idempotency_key": idempotency_key,
        "source": source,
        "device_id": device_id,
        "shift": shift_name,
        "staff_id": staff_id,
        "booth_warehouse": booth_warehouse,
        "company": company,
        "currency": currency,
        "customer": customer,
        "event_project": event_project,
        "notes": notes,
        "sale_datetime": sale_datetime,
        "net_total": net_total,
        "tax_total": tax_total,
        "rounding_adjustment": rounding_adjustment,
        "grand_total": grand_total,
        "items": validated_items,
        "payments": validated_payments,
    }


def _validate_order_item(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        frappe.throw(f"items[{index}] must be an object", frappe.ValidationError)

    line_id = cstr(
        value.get("line_id") or value.get("backend_line_uuid") or f"LINE-{index}"
    )
    item_code = cstr(value.get("item") or value.get("item_code"))
    qty = flt(value.get("qty"))
    uom = cstr(value.get("uom"))
    unit_price = flt(value.get("unit_price") or value.get("rate"))
    modifier_total = flt(value.get("modifier_total"))
    discount_amount = flt(value.get("discount_amount"))
    line_total = flt(value.get("line_total") or value.get("amount"))
    remarks = cstr(value.get("remarks")) or None
    recipe = cstr(value.get("recipe")) or None
    recipe_version = cint(value.get("recipe_version")) or None
    backend_line_uuid = cstr(value.get("backend_line_uuid")) or None
    modifiers = value.get("selected_modifiers") or value.get("modifiers") or []

    if not item_code:
        frappe.throw(f"items[{index}].item_code is required", frappe.ValidationError)
    if qty <= 0:
        frappe.throw(
            f"items[{index}].qty must be greater than 0", frappe.ValidationError
        )
    if unit_price < 0:
        frappe.throw(
            f"items[{index}].unit_price must be 0 or greater",
            frappe.ValidationError,
        )
    if modifier_total < 0:
        frappe.throw(
            f"items[{index}].modifier_total must be 0 or greater",
            frappe.ValidationError,
        )
    if discount_amount < 0:
        frappe.throw(
            f"items[{index}].discount_amount must be 0 or greater",
            frappe.ValidationError,
        )
    if line_total <= 0:
        frappe.throw(
            f"items[{index}].line_total must be greater than 0",
            frappe.ValidationError,
        )

    item_doc = frappe.get_doc("Item", item_code)
    resolved_uom = uom or cstr(getattr(item_doc, "stock_uom", None))
    if not resolved_uom:
        frappe.throw(f"items[{index}].uom is required", frappe.ValidationError)
    if not frappe.db.exists("UOM", resolved_uom):
        frappe.throw(f"UOM {resolved_uom} was not found", frappe.ValidationError)
    if recipe and not frappe.db.exists("FB Recipe", recipe):
        frappe.throw(f"FB Recipe {recipe} was not found", frappe.ValidationError)

    validated_modifiers = [
        _validate_selected_modifier(row, index, modifier_index)
        for modifier_index, row in enumerate(modifiers, start=1)
    ]
    resolved_modifier_total = sum(
        flt(row["price_adjustment"]) for row in validated_modifiers
    )
    if abs(modifier_total - resolved_modifier_total) > 0.0001:
        frappe.throw(
            f"items[{index}].modifier_total must equal summed FB modifier price adjustments",
            frappe.ValidationError,
        )

    expected_total = (unit_price + modifier_total) * qty - discount_amount
    if abs(expected_total - line_total) > 0.0001:
        frappe.throw(
            f"items[{index}].line_total does not match qty, pricing, and discount",
            frappe.ValidationError,
        )

    return {
        "line_id": line_id,
        "backend_line_uuid": backend_line_uuid,
        "item": item_doc.name,
        "item_name_snapshot": cstr(getattr(item_doc, "item_name", None))
        or item_doc.name,
        "qty": qty,
        "uom": resolved_uom,
        "unit_price": unit_price,
        "modifier_total": resolved_modifier_total,
        "discount_amount": discount_amount,
        "line_total": line_total,
        "recipe": recipe,
        "recipe_version": recipe_version,
        "is_recipe_managed": 1 if recipe else 0,
        "remarks": remarks,
        "selected_modifiers": validated_modifiers,
    }


def _validate_selected_modifier(
    value: Any, item_index: int, modifier_index: int
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        frappe.throw(
            f"items[{item_index}].selected_modifiers[{modifier_index}] must be an object",
            frappe.ValidationError,
        )

    modifier_group = cstr(value.get("modifier_group"))
    modifier = cstr(value.get("modifier"))
    price_adjustment = flt(value.get("price_adjustment"))
    instruction_text = cstr(value.get("instruction_text")) or None
    sort_order = cint(value.get("sort_order"))
    affects_stock = cint(value.get("affects_stock"))
    affects_recipe = cint(value.get("affects_recipe"))

    if not modifier_group:
        frappe.throw(
            f"items[{item_index}].selected_modifiers[{modifier_index}].modifier_group is required",
            frappe.ValidationError,
        )
    if not modifier:
        frappe.throw(
            f"items[{item_index}].selected_modifiers[{modifier_index}].modifier is required",
            frappe.ValidationError,
        )

    field_prefix = f"items[{item_index}].selected_modifiers[{modifier_index}]"
    _get_required_fb_modifier_doc(
        "FB Modifier Group",
        modifier_group,
        f"{field_prefix}.modifier_group",
    )
    modifier_doc = _get_required_fb_modifier_doc(
        "FB Modifier",
        modifier,
        f"{field_prefix}.modifier",
    )
    if cstr(getattr(modifier_doc, "modifier_group", None)) != modifier_group:
        frappe.throw(
            f"{field_prefix}.modifier {modifier} does not belong to FB Modifier Group {modifier_group}",
            frappe.ValidationError,
        )

    return {
        "modifier_group": modifier_group,
        "modifier": modifier,
        "price_adjustment": flt(getattr(modifier_doc, "price_adjustment", 0)),
        "instruction_text": instruction_text
        or cstr(getattr(modifier_doc, "instruction_text", None))
        or None,
        "sort_order": sort_order or cint(getattr(modifier_doc, "display_order", 0)),
        "affects_stock": 1 if cint(getattr(modifier_doc, "affects_stock", 0)) else 0,
        "affects_recipe": 1 if cint(getattr(modifier_doc, "affects_recipe", 0)) else 0,
    }


def _validate_order_payment(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        frappe.throw(f"payments[{index}] must be an object", frappe.ValidationError)

    payment_method = _resolve_mode_of_payment_name(
        cstr(value.get("payment_method") or value.get("method"))
    )
    amount = flt(value.get("amount"))
    tendered_amount = flt(value.get("tendered_amount"))
    change_amount = flt(value.get("change_amount"))
    payment_channel_code = cstr(value.get("payment_channel_code")) or None
    reference_no = cstr(value.get("reference_no")) or None
    external_transaction_id = cstr(value.get("external_transaction_id")) or None

    if not payment_method:
        frappe.throw(
            f"payments[{index}].payment_method is required", frappe.ValidationError
        )
    if amount <= 0:
        frappe.throw(
            f"payments[{index}].amount must be greater than 0", frappe.ValidationError
        )
    if tendered_amount < 0:
        frappe.throw(
            f"payments[{index}].tendered_amount must be 0 or greater",
            frappe.ValidationError,
        )
    if change_amount < 0:
        frappe.throw(
            f"payments[{index}].change_amount must be 0 or greater",
            frappe.ValidationError,
        )

    _require_doc("Mode of Payment", payment_method, "payment_method")

    return {
        "payment_method": payment_method,
        "payment_channel_code": payment_channel_code,
        "amount": amount,
        "tendered_amount": tendered_amount,
        "change_amount": change_amount,
        "reference_no": reference_no,
        "external_transaction_id": external_transaction_id,
    }


PAYMENT_METHOD_ALIASES = {
    "cash": {"cash"},
    "qr": {"duitnow qr"},
    "card": {"card", "credit card", "debit card"},
    "voucher": {"voucher", "coupon", "gift voucher"},
}

PAYMENT_METHOD_CANONICAL_NAMES = {
    "cash": "Cash",
    "qr": "DuitNow QR",
    "card": "Card",
    "voucher": "Voucher",
}


def _normalize_token(value: Any) -> str:
    return " ".join(
        cstr(value).strip().lower().replace("_", " ").replace("-", " ").split()
    )


def _resolve_mode_of_payment_name(requested_mode: str) -> str:
    requested = cstr(requested_mode).strip()
    if not requested:
        return requested

    normalized_requested = _normalize_token(requested)
    if normalized_requested == "duitnow qr" and frappe.db.exists(
        "Mode of Payment", "DuitNow QR"
    ):
        return "DuitNow QR"

    if normalized_requested in {
        "qr",
        "maybank qr",
        "duitnow",
        "e wallet",
        "ewallet",
        "wallet",
    }:
        frappe.throw(
            "payment_method must be DuitNow QR",
            frappe.ValidationError,
        )

    aliases = PAYMENT_METHOD_ALIASES.get(normalized_requested, {normalized_requested})

    canonical_name = PAYMENT_METHOD_CANONICAL_NAMES.get(normalized_requested)
    if canonical_name and frappe.db.exists("Mode of Payment", canonical_name):
        return canonical_name

    if frappe.db.exists("Mode of Payment", requested):
        return requested

    available_modes = frappe.get_all("Mode of Payment", pluck="name") or []
    for mode in available_modes:
        if _normalize_token(mode) == normalized_requested:
            return cstr(mode)
    for mode in available_modes:
        if _normalize_token(mode) in aliases:
            return cstr(mode)

    return requested


def _build_fb_order(validated: dict[str, Any]):
    order_doc = frappe.new_doc("FB Order")
    order_doc.order_id = validated["order_id"]
    order_doc.external_idempotency_key = validated["external_idempotency_key"]
    order_doc.source = validated["source"]
    order_doc.sale_datetime = validated["sale_datetime"]
    order_doc.device_id = validated["device_id"]
    order_doc.shift = validated["shift"]
    order_doc.staff_id = validated["staff_id"]
    order_doc.event_project = validated["event_project"]
    order_doc.booth_warehouse = validated["booth_warehouse"]
    order_doc.company = validated["company"]
    order_doc.currency = validated["currency"]
    order_doc.customer = validated["customer"]
    order_doc.status = "Draft"
    order_doc.invoice_status = "Pending"
    order_doc.stock_status = "Pending"
    order_doc.net_total = validated["net_total"]
    order_doc.tax_total = validated["tax_total"]
    if hasattr(order_doc, "rounding_adjustment"):
        order_doc.rounding_adjustment = validated["rounding_adjustment"]
    order_doc.grand_total = validated["grand_total"]
    order_doc.notes = validated["notes"]

    for item in validated["items"]:
        row = order_doc.append(
            "items",
            {
                "line_id": item["line_id"],
                "backend_line_uuid": item["backend_line_uuid"],
                "item": item["item"],
                "item_name_snapshot": item["item_name_snapshot"],
                "qty": item["qty"],
                "uom": item["uom"],
                "unit_price": item["unit_price"],
                "modifier_total": item["modifier_total"],
                "discount_amount": item["discount_amount"],
                "line_total": item["line_total"],
                "recipe": item["recipe"],
                "recipe_version": item["recipe_version"],
                "is_recipe_managed": item["is_recipe_managed"],
                "remarks": item["remarks"],
            },
        )
        _set_selected_modifiers_payload(row, item["selected_modifiers"])

    for payment in validated["payments"]:
        order_doc.append("payments", payment)

    return order_doc


def _set_selected_modifiers_payload(line, modifiers: list[dict[str, Any]]) -> None:
    setattr(
        line,
        "_selected_modifiers_payload",
        [frappe._dict(modifier) for modifier in modifiers],
    )


def _get_existing_fb_order_name(idempotency_key: str) -> str | None:
    if not idempotency_key:
        return None
    existing = frappe.db.get_value(
        "FB Order",
        {"external_idempotency_key": idempotency_key},
        "name",
    )
    return cstr(existing) or None


def _build_submit_response(result_status: str, order_doc) -> dict[str, Any]:
    projections = _get_projection_statuses("FB Order", order_doc.name)
    projection_status = _get_submit_projection_status(order_doc, projections)
    response_status = (
        "partial_failure" if projection_status["diagnostics"] else result_status
    )
    first_diagnostic = (
        projection_status["diagnostics"][0]
        if projection_status["diagnostics"]
        else None
    )
    return {
        "status": response_status,
        "partial_failure": bool(projection_status["diagnostics"]),
        "fb_order": order_doc.name,
        "order_id": cstr(order_doc.order_id),
        "idempotency_key": cstr(order_doc.external_idempotency_key),
        "sale_datetime": cstr(getattr(order_doc, "sale_datetime", None)) or None,
        "sales_invoice": cstr(order_doc.sales_invoice) or None,
        "ingredient_stock_entry": cstr(order_doc.ingredient_stock_entry) or None,
        "order_status": cstr(order_doc.status),
        "invoice_status": cstr(order_doc.invoice_status),
        "stock_status": cstr(order_doc.stock_status),
        "projection_status": projection_status["projection_status"],
        "failed_subsystem": first_diagnostic["failed_subsystem"]
        if first_diagnostic
        else None,
        "diagnostics": projection_status["diagnostics"],
        "message": first_diagnostic["error_message"] if first_diagnostic else None,
        "projections": projections,
    }


def _get_submit_projection_status(
    order_doc,
    projections: list[dict[str, Any]],
) -> dict[str, Any]:
    diagnostics: list[dict[str, Any]] = []
    fb_order = cstr(order_doc.name)
    idempotency_key = cstr(getattr(order_doc, "external_idempotency_key", None))
    order_status = cstr(getattr(order_doc, "status", None))
    invoice_status = cstr(getattr(order_doc, "invoice_status", None))
    stock_status = cstr(getattr(order_doc, "stock_status", None))

    if order_status != "Submitted":
        diagnostics.append(
            _build_projection_diagnostic(
                fb_order=fb_order,
                idempotency_key=idempotency_key,
                projection_status="failed",
                failed_subsystem="fb_order",
                error_message=f"FB Order status is {order_status or 'missing'}; expected Submitted",
            )
        )

    if invoice_status != "Posted":
        diagnostics.append(
            _build_projection_diagnostic(
                fb_order=fb_order,
                idempotency_key=idempotency_key,
                projection_status="failed",
                failed_subsystem="sales_invoice",
                error_message=f"Sales Invoice projection status is {invoice_status or 'missing'}; expected Posted",
            )
        )

    if stock_status not in ACCEPTABLE_STOCK_STATUSES:
        diagnostics.append(
            _build_projection_diagnostic(
                fb_order=fb_order,
                idempotency_key=idempotency_key,
                projection_status="failed",
                failed_subsystem="stock",
                error_message=f"Stock projection status is {stock_status or 'missing'}; expected Pending or Posted",
            )
        )

    for projection in projections:
        if cstr(projection.get("state")) != "Failed":
            continue
        projection_type = cstr(projection.get("projection_type"))
        failed_subsystem = PROJECTION_SUBSYSTEMS.get(
            projection_type,
            projection_type.lower().replace(" ", "_") or "projection",
        )
        diagnostics.append(
            _build_projection_diagnostic(
                fb_order=fb_order,
                idempotency_key=idempotency_key,
                projection_status="failed",
                failed_subsystem=failed_subsystem,
                error_message=cstr(projection.get("last_error"))
                or f"{projection_type or 'Projection'} failed",
            )
        )

    return {
        "projection_status": "failed" if diagnostics else "posted",
        "diagnostics": _dedupe_projection_diagnostics(diagnostics),
    }


def _build_projection_diagnostic(
    *,
    fb_order: str,
    idempotency_key: str,
    projection_status: str,
    failed_subsystem: str,
    error_message: str,
) -> dict[str, Any]:
    return {
        "fb_order": fb_order,
        "projection_status": projection_status,
        "failed_subsystem": failed_subsystem,
        "error_message": error_message,
        "idempotency_key": idempotency_key,
    }


def _dedupe_projection_diagnostics(
    diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    deduped = []
    seen = set()
    for diagnostic in diagnostics:
        key = (
            diagnostic["fb_order"],
            diagnostic["failed_subsystem"],
            diagnostic["error_message"],
            diagnostic["idempotency_key"],
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(diagnostic)
    return deduped


def _validate_fb_order_doc(doc) -> None:
    if not cstr(getattr(doc, "order_id", None)):
        frappe.throw("FB Order requires order_id", frappe.ValidationError)
    if not cstr(getattr(doc, "external_idempotency_key", None)):
        frappe.throw(
            "FB Order requires external_idempotency_key", frappe.ValidationError
        )
    if not cstr(getattr(doc, "device_id", None)):
        frappe.throw("FB Order requires device_id", frappe.ValidationError)
    if not cstr(getattr(doc, "shift", None)):
        frappe.throw("FB Order requires shift", frappe.ValidationError)
    if not cstr(getattr(doc, "staff_id", None)):
        frappe.throw("FB Order requires staff_id", frappe.ValidationError)
    if not cstr(getattr(doc, "booth_warehouse", None)):
        frappe.throw("FB Order requires booth_warehouse", frappe.ValidationError)
    if not cstr(getattr(doc, "company", None)):
        frappe.throw("FB Order requires company", frappe.ValidationError)
    if not cstr(getattr(doc, "currency", None)):
        frappe.throw("FB Order requires currency", frappe.ValidationError)
    if not doc.get("items"):
        frappe.throw("FB Order requires at least one item", frappe.ValidationError)
    if not doc.get("payments"):
        frappe.throw("FB Order requires at least one payment", frappe.ValidationError)

    _require_doc("FB Shift", cstr(doc.shift), "shift")
    _require_doc("Warehouse", cstr(doc.booth_warehouse), "booth_warehouse")
    _require_doc("Company", cstr(doc.company), "company")
    _require_doc("User", cstr(doc.staff_id), "staff_id")
    if cstr(getattr(doc, "customer", None)):
        _require_doc("Customer", cstr(doc.customer), "customer")
    if cstr(getattr(doc, "event_project", None)):
        _require_doc("Project", cstr(doc.event_project), "event_project")

    if doc.name:
        duplicate_name = frappe.db.get_value(
            "FB Order",
            {
                "external_idempotency_key": cstr(doc.external_idempotency_key),
                "name": ["!=", doc.name],
            },
            "name",
        )
    else:
        duplicate_name = _get_existing_fb_order_name(cstr(doc.external_idempotency_key))
    if duplicate_name:
        frappe.throw(
            f"FB Order already exists for idempotency key {doc.external_idempotency_key}",
            frappe.ValidationError,
        )

    line_total_sum = 0.0
    for index, row in enumerate(doc.get("items") or [], start=1):
        if not cstr(getattr(row, "line_id", None)):
            frappe.throw(
                f"FB Order item {index} requires line_id", frappe.ValidationError
            )
        if not cstr(getattr(row, "item", None)):
            frappe.throw(f"FB Order item {index} requires item", frappe.ValidationError)
        if flt(getattr(row, "qty", None)) <= 0:
            frappe.throw(
                f"FB Order item {index} requires qty greater than 0",
                frappe.ValidationError,
            )
        if flt(getattr(row, "line_total", None)) <= 0:
            frappe.throw(
                f"FB Order item {index} requires line_total greater than 0",
                frappe.ValidationError,
            )
        line_total_sum += flt(row.line_total)

    payment_total = 0.0
    for index, row in enumerate(doc.get("payments") or [], start=1):
        if not cstr(getattr(row, "payment_method", None)):
            frappe.throw(
                f"FB Order payment {index} requires payment_method",
                frappe.ValidationError,
            )
        if flt(getattr(row, "amount", None)) <= 0:
            frappe.throw(
                f"FB Order payment {index} requires amount greater than 0",
                frappe.ValidationError,
            )
        payment_total += flt(row.amount)

    if abs(line_total_sum - flt(doc.net_total)) > 0.0001:
        frappe.throw(
            "FB Order net_total must equal summed line totals", frappe.ValidationError
        )
    if (
        abs(
            flt(doc.net_total)
            + flt(doc.tax_total)
            + flt(getattr(doc, "rounding_adjustment", 0) or 0)
            - flt(doc.grand_total)
        )
        > 0.0001
    ):
        frappe.throw(
            "FB Order grand_total must equal net_total plus tax_total plus rounding_adjustment",
            frappe.ValidationError,
        )
    if abs(payment_total - flt(doc.grand_total)) > 0.0001:
        frappe.throw(
            "FB Order payments total must equal grand_total",
            frappe.ValidationError,
        )


def _set_default_order_statuses(doc) -> None:
    if not cstr(getattr(doc, "status", None)):
        doc.status = "Draft"
    if not cstr(getattr(doc, "invoice_status", None)):
        doc.invoice_status = "Pending"
    if not cstr(getattr(doc, "stock_status", None)):
        doc.stock_status = "Pending"


def _validate_return_event_doc(doc) -> None:
    if not cstr(getattr(doc, "return_id", None)):
        frappe.throw("FB Return Event requires return_id", frappe.ValidationError)
    if not cstr(getattr(doc, "status", None)):
        doc.status = "Draft"
    if not cstr(getattr(doc, "fb_order", None)) and not cstr(
        getattr(doc, "original_sales_invoice", None)
    ):
        frappe.throw(
            "FB Return Event requires fb_order or original_sales_invoice",
            frappe.ValidationError,
        )
    if cstr(getattr(doc, "fb_order", None)):
        _require_doc("FB Order", cstr(doc.fb_order), "fb_order")
    if cstr(getattr(doc, "original_sales_invoice", None)):
        _require_doc(
            "Sales Invoice",
            cstr(doc.original_sales_invoice),
            "original_sales_invoice",
        )


def _validate_remake_event_doc(doc) -> None:
    if not cstr(getattr(doc, "remake_id", None)):
        frappe.throw("FB Remake Event requires remake_id", frappe.ValidationError)
    if not cstr(getattr(doc, "status", None)):
        doc.status = "Draft"
    if not cstr(getattr(doc, "original_order", None)):
        frappe.throw("FB Remake Event requires original_order", frappe.ValidationError)
    _require_doc("FB Order", cstr(doc.original_order), "original_order")
    if cstr(getattr(doc, "original_order_line", None)):
        _require_doc(
            "FB Order Line", cstr(doc.original_order_line), "original_order_line"
        )
    if cstr(getattr(doc, "original_resolved_sale", None)):
        _require_doc(
            "FB Resolved Sale",
            cstr(doc.original_resolved_sale),
            "original_resolved_sale",
        )


def _process_projection_bundle(
    doc, source_doctype: str, config: tuple[dict[str, str], ...]
) -> None:
    results = []
    for entry in config:
        if entry.get("enabled_field") and not cint(
            getattr(doc, entry["enabled_field"], 0)
        ):
            continue
        results.append(_sync_projection(doc, source_doctype, entry))

    if source_doctype == "FB Order":
        doc.invoice_status = _resolve_order_status(results, "Sales Invoice")
        doc.stock_status = _resolve_order_status(results, "Stock Issue")
        doc.db_set("status", "Submitted", update_modified=False)
        doc.db_set("invoice_status", doc.invoice_status, update_modified=False)
        doc.db_set("stock_status", doc.stock_status, update_modified=False)


def _sync_projection(
    doc, source_doctype: str, config: dict[str, str]
) -> dict[str, Any]:
    log = _get_or_create_projection_log(doc, source_doctype, config)
    target_name = cstr(getattr(doc, config["target_field"], None))
    target_exists = bool(
        target_name and frappe.db.exists(config["target_doctype"], target_name)
    )
    state = "Succeeded" if target_exists else "Pending"
    last_error = (
        None if target_exists else cstr(getattr(log, "last_error", None)) or None
    )
    _update_projection_log(
        log, state, target_name if target_exists else None, last_error
    )
    return {
        "projection_type": config["projection_type"],
        "state": state,
        "target_name": target_name if target_exists else None,
        "log": log.name,
    }


def _get_or_create_projection_log(doc, source_doctype: str, config: dict[str, str]):
    existing_name = frappe.db.get_value(
        "FB Projection Log",
        {
            "source_doctype": source_doctype,
            "source_name": doc.name,
            "projection_type": config["projection_type"],
        },
        "name",
    )
    if existing_name:
        return frappe.get_doc("FB Projection Log", existing_name)

    log = frappe.new_doc("FB Projection Log")
    log.projection_id = _make_projection_id(
        source_doctype, doc.name, config["projection_type"]
    )
    log.source_doctype = source_doctype
    log.source_name = doc.name
    log.source_event_type = "submit"
    log.projection_type = config["projection_type"]
    log.idempotency_key = _make_projection_id(
        source_doctype, doc.name, config["projection_type"]
    )
    log.payload_hash = _build_payload_hash(doc, config["projection_type"])
    log.target_doctype = config["target_doctype"]
    log.state = "Pending"
    log.retry_count = 0
    log.created_at = now_datetime()
    log.last_attempt_at = now_datetime()
    log.insert(ignore_permissions=True)
    return log


def _update_projection_log(
    log, state: str, target_name: str | None, last_error: str | None
) -> None:
    log.state = state
    log.target_name = target_name
    log.last_error = last_error
    log.last_attempt_at = now_datetime()
    log.save(ignore_permissions=True)


def _retry_projection_log(log_name: str) -> dict[str, Any]:
    log = frappe.get_doc("FB Projection Log", log_name)
    source_doc = frappe.get_doc(cstr(log.source_doctype), cstr(log.source_name))
    config = _get_projection_config(cstr(log.source_doctype), cstr(log.projection_type))
    if config is None:
        frappe.throw(
            f"No projection handler is configured for {log.source_doctype} {log.projection_type}",
            frappe.ValidationError,
        )
        raise AssertionError("unreachable")

    log.retry_count = cint(log.retry_count) + 1
    log.last_attempt_at = now_datetime()
    log.save(ignore_permissions=True)

    result = _retry_projection_target(source_doc, cstr(log.source_doctype), config, log)
    reload_doc = getattr(source_doc, "reload", None)
    if callable(reload_doc):
        reload_doc()
    if cstr(log.source_doctype) == "FB Order":
        source_doc.invoice_status = _derive_projection_field_status(
            source_doc, "Sales Invoice"
        )
        source_doc.stock_status = _derive_projection_field_status(
            source_doc, "Stock Issue"
        )
        source_doc.db_set(
            "invoice_status", source_doc.invoice_status, update_modified=False
        )
        source_doc.db_set(
            "stock_status", source_doc.stock_status, update_modified=False
        )

    return {
        "projection_log": log.name,
        "projection_type": result["projection_type"],
        "state": result["state"],
        "target_name": result["target_name"],
    }


def _retry_projection_target(
    source_doc,
    source_doctype: str,
    config: dict[str, str],
    log,
) -> dict[str, Any]:
    projection_type = config["projection_type"]
    try:
        target_name = _run_projection_handler(source_doc, source_doctype, projection_type)
    except Exception as error:
        _update_projection_log(log, "Failed", None, str(error))
        return {
            "projection_type": projection_type,
            "state": "Failed",
            "target_name": None,
            "log": log.name,
        }

    if target_name:
        _update_projection_log(log, "Succeeded", target_name, None)
        return {
            "projection_type": projection_type,
            "state": "Succeeded",
            "target_name": target_name,
            "log": log.name,
        }

    if _projection_can_remain_pending(source_doc, source_doctype, projection_type):
        _update_projection_log(log, "Pending", None, None)
        return {
            "projection_type": projection_type,
            "state": "Pending",
            "target_name": None,
            "log": log.name,
        }

    error_message = f"{projection_type} projection retry did not create a target document"
    _update_projection_log(log, "Failed", None, error_message)
    return {
        "projection_type": projection_type,
        "state": "Failed",
        "target_name": None,
        "log": log.name,
    }


def _run_projection_handler(
    source_doc,
    source_doctype: str,
    projection_type: str,
) -> str | None:
    if source_doctype == "FB Order" and projection_type == "Sales Invoice":
        return _create_sales_invoice_projection(source_doc)
    if source_doctype == "FB Order" and projection_type == "Stock Issue":
        resolved_sales = _get_order_resolved_sales(source_doc)
        if not _order_requires_stock_projection(source_doc, resolved_sales):
            return None
        return _create_stock_issue_projection(source_doc, resolved_sales)
    if source_doctype == "FB Order" and projection_type == "FB Shift":
        _refresh_order_shift_projection(source_doc)
        return cstr(getattr(source_doc, "shift", None)) or None
    return _get_existing_projection_target(source_doc, projection_type)


def _create_sales_invoice_projection(source_doc) -> str | None:
    service_module = importlib.import_module(
        "kopos_connector.kopos.services.accounting.sales_invoice_service"
    )

    return cstr(service_module.create_sales_invoice(source_doc)) or None


def _create_stock_issue_projection(source_doc, resolved_sales: list[Any]) -> str | None:
    service_module = importlib.import_module(
        "kopos_connector.kopos.services.inventory.stock_issue_service"
    )

    return cstr(
        service_module.create_ingredient_stock_entry(source_doc, resolved_sales)
    ) or None


def _get_order_resolved_sales(source_doc) -> list[Any]:
    get_resolved_sales = getattr(source_doc, "get_resolved_sales", None)
    if callable(get_resolved_sales):
        resolved_sales = get_resolved_sales()
        if isinstance(resolved_sales, list):
            return resolved_sales
        return []

    resolved_sales = []
    for line in list(getattr(source_doc, "items", None) or []):
        resolved_sale_name = cstr(getattr(line, "resolved_sale", None))
        if not resolved_sale_name:
            continue
        resolved_sales.append(frappe.get_doc("FB Resolved Sale", resolved_sale_name))
    return resolved_sales


def _order_requires_stock_projection(source_doc, resolved_sales: list[Any]) -> bool:
    requires_stock_projection = getattr(source_doc, "requires_stock_projection", None)
    if callable(requires_stock_projection):
        return bool(requires_stock_projection(resolved_sales))
    return any(
        _resolved_sale_affects_stock(resolved_sale) for resolved_sale in resolved_sales
    )


def _resolved_sale_affects_stock(resolved_sale: Any) -> bool:
    for component in list(getattr(resolved_sale, "resolved_components", None) or []):
        if not cint(getattr(component, "affects_stock", 0)):
            continue
        item = cstr(getattr(component, "item", None))
        warehouse = cstr(
            getattr(component, "warehouse", None)
            or getattr(resolved_sale, "booth_warehouse", None)
        )
        qty = flt(
            getattr(component, "stock_qty", None) or getattr(component, "qty", None)
        )
        if item and warehouse and qty > 0:
            return True
    return False


def _refresh_order_shift_projection(source_doc) -> None:
    update_shift_expected_cash = getattr(source_doc, "update_shift_expected_cash", None)
    if callable(update_shift_expected_cash):
        update_shift_expected_cash()
        return
    shift = cstr(getattr(source_doc, "shift", None))
    if not shift:
        frappe.throw("FB Order has no shift to refresh", frappe.ValidationError)
    frappe.get_doc("FB Shift", shift)


def _get_existing_projection_target(source_doc, projection_type: str) -> str | None:
    config = _get_projection_config(
        cstr(getattr(source_doc, "doctype", None)), projection_type
    )
    if not config:
        return None
    target_name = cstr(getattr(source_doc, config["target_field"], None))
    if target_name and frappe.db.exists(config["target_doctype"], target_name):
        return target_name
    return None


def _projection_can_remain_pending(
    source_doc,
    source_doctype: str,
    projection_type: str,
) -> bool:
    if source_doctype != "FB Order" or projection_type != "Stock Issue":
        return False
    return not _order_requires_stock_projection(
        source_doc, _get_order_resolved_sales(source_doc)
    )


def _get_projection_config(
    source_doctype: str, projection_type: str
) -> dict[str, str] | None:
    config_map = {
        "FB Order": ORDER_PROJECTION_CONFIG,
        "FB Return Event": RETURN_PROJECTION_CONFIG,
        "FB Remake Event": REMAKE_PROJECTION_CONFIG,
    }
    for entry in config_map.get(source_doctype, ()):
        if entry["projection_type"] == projection_type:
            return entry
    return None


def _get_projection_statuses(
    source_doctype: str, source_name: str
) -> list[dict[str, Any]]:
    rows = frappe.get_all(
        "FB Projection Log",
        filters={"source_doctype": source_doctype, "source_name": source_name},
        fields=[
            "name",
            "projection_type",
            "state",
            "target_doctype",
            "target_name",
            "idempotency_key",
            "retry_count",
            "last_error",
            "last_attempt_at",
        ],
        order_by="creation asc",
    )
    return [
        {
            "projection_log": cstr(row.name),
            "projection_type": cstr(row.projection_type),
            "state": cstr(row.state),
            "target_doctype": cstr(row.target_doctype) or None,
            "target_name": cstr(row.target_name) or None,
            "idempotency_key": cstr(row.idempotency_key) or None,
            "retry_count": cint(row.retry_count),
            "last_error": cstr(row.last_error) or None,
            "last_attempt_at": row.last_attempt_at.isoformat()
            if getattr(row, "last_attempt_at", None)
            else None,
        }
        for row in rows
    ]


def _resolve_order_status(results: list[dict[str, Any]], projection_type: str) -> str:
    relevant = [row for row in results if row["projection_type"] == projection_type]
    if not relevant:
        return "Pending"
    states = {row["state"] for row in relevant}
    if "Failed" in states:
        return "Failed"
    if states == {"Succeeded"}:
        return "Posted"
    return "Pending"


def _derive_projection_field_status(doc, projection_type: str) -> str:
    rows = frappe.get_all(
        "FB Projection Log",
        filters={
            "source_doctype": "FB Order",
            "source_name": doc.name,
            "projection_type": projection_type,
        },
        fields=["state"],
    )
    states = {cstr(row.state) for row in rows}
    if "Failed" in states:
        return "Failed"
    if states == {"Succeeded"} and states:
        return "Posted"
    return "Pending"


def _make_projection_id(
    source_doctype: str, source_name: str, projection_type: str
) -> str:
    source_prefix = source_doctype.upper().replace(" ", "-")
    projection_prefix = projection_type.upper().replace(" ", "-")
    return f"{source_prefix}-{source_name}-{projection_prefix}"


def _build_payload_hash(doc, projection_type: str) -> str:
    payload = json.dumps(
        {
            "source_doctype": doc.doctype,
            "source_name": doc.name,
            "projection_type": projection_type,
            "status": cstr(getattr(doc, "status", None)),
        },
        sort_keys=True,
        default=str,
    )
    return frappe.generate_hash(payload, 16)


def _require_doc(doctype: str, name: str, field_label: str) -> None:
    if not frappe.db.exists(doctype, name):
        frappe.throw(f"{field_label} {name} was not found", frappe.ValidationError)


def _get_required_fb_modifier_doc(doctype: str, name: str, field_label: str):
    identifier = cstr(name)
    if _looks_like_legacy_kopos_modifier_identifier(identifier):
        frappe.throw(
            f"{field_label} {identifier} is a legacy KoPOS modifier id; submit FB-only modifier ids",
            frappe.ValidationError,
        )
    if not frappe.db.exists(doctype, identifier):
        frappe.throw(
            f"{field_label} {identifier} was not found in {doctype}; submit FB-only modifier ids because legacy KoPOS modifier ids are not supported",
            frappe.ValidationError,
        )
    return frappe.get_cached_doc(doctype, identifier)


def _looks_like_legacy_kopos_modifier_identifier(value: str) -> bool:
    normalized_value = cstr(value).strip().upper().replace("_", "-")
    return normalized_value.startswith("KOPOS-")


def _resolve_fb_shift_name(value: str) -> str | None:
    if frappe.db.exists("FB Shift", value):
        return value
    return frappe.db.get_value("FB Shift", {"shift_code": value}, "name")


def _validate_submit_shift(*, shift_name: str, device_id: str, staff_id: str) -> Any:
    shift_doc = frappe.get_doc("FB Shift", shift_name)
    shift_device_id = cstr(getattr(shift_doc, "device_id", None))
    shift_staff_id = cstr(getattr(shift_doc, "staff_id", None))
    shift_status = cstr(getattr(shift_doc, "status", None))

    if shift_device_id != device_id:
        frappe.throw(
            f"shift {shift_name} does not belong to device {device_id}",
            frappe.ValidationError,
        )
    if shift_staff_id != staff_id:
        frappe.throw(
            f"shift {shift_name} does not belong to staff {staff_id}",
            frappe.ValidationError,
        )
    if shift_status != "Open":
        frappe.throw(
            f"shift {shift_name} is {shift_status or 'missing'}; new FB Orders require an Open FB Shift",
            frappe.ValidationError,
        )
    return shift_doc
