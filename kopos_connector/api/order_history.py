# pyright: reportMissingImports=false

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime, getdate, nowdate

from kopos_connector.api.devices import (
    elevate_device_api_user,
    get_authenticated_device_doc,
    get_device_doc,
)


DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MONEY_QUANTUM = Decimal("0.01")


def get_order_history_payload(
    *,
    device_id: str | None = None,
    since_date: str | date | datetime | None = None,
    cursor: str | int | None = None,
    limit: str | int | None = None,
) -> dict[str, Any]:
    """Return current-shift submitted POS Invoice history for one KoPOS device."""
    device_doc = resolve_history_device(device_id=device_id)
    device_id_value = cstr(getattr(device_doc, "device_id", None)).strip()
    if not device_id_value:
        frappe.throw(_("KoPOS Device has no device_id configured"), frappe.ValidationError)
    if not cint(getattr(device_doc, "enabled", 0)):
        frappe.throw(
            _("KoPOS Device {0} is disabled").format(device_id_value),
            frappe.ValidationError,
        )

    pos_profile_name = cstr(getattr(device_doc, "pos_profile", None)).strip()
    if not pos_profile_name:
        frappe.throw(
            _("KoPOS Device {0} has no POS Profile configured").format(
                device_id_value
            ),
            frappe.ValidationError,
        )

    pos_profile = frappe.get_cached_doc("POS Profile", pos_profile_name)
    company = cstr(getattr(pos_profile, "company", None)).strip()
    if not company:
        frappe.throw(
            _("POS Profile {0} has no company configured").format(pos_profile_name),
            frappe.ValidationError,
        )

    resolved_limit = normalize_limit(limit)
    offset = normalize_cursor(cursor)
    effective_since_date, effective_since_datetime = resolve_since_date(
        device_id=device_id_value,
        pos_profile=pos_profile_name,
        requested_since_date=since_date,
    )

    with elevate_device_api_user():
        invoice_rows = query_invoice_rows(
            company=company,
            pos_profile=pos_profile_name,
            since_date=effective_since_date,
            since_datetime=effective_since_datetime,
            limit=resolved_limit + 1,
            offset=offset,
            device_id=device_id_value,
        )

        page_rows = invoice_rows[:resolved_limit]
        invoice_names = [cstr(row.get("name")).strip() for row in page_rows]
        items_by_parent = query_child_rows_by_parent(
            "POS Invoice Item",
            invoice_names,
            get_invoice_item_fields(),
            order_by="parent asc, idx asc",
        )
        payments_by_parent = query_child_rows_by_parent(
            "Sales Invoice Payment",
            invoice_names,
            get_invoice_payment_fields(),
            order_by="parent asc, idx asc",
        )
        refund_rows = query_refund_rows(
            company=company,
            pos_profile=pos_profile_name,
            invoice_names=invoice_names,
        )
        refund_names = [cstr(row.get("name")).strip() for row in refund_rows]
        refund_items_by_parent = query_child_rows_by_parent(
            "POS Invoice Item",
            refund_names,
            get_invoice_item_fields(),
            order_by="parent asc, idx asc",
        )
        refund_payments_by_parent = query_child_rows_by_parent(
            "Sales Invoice Payment",
            refund_names,
            get_invoice_payment_fields(),
            order_by="parent asc, idx asc",
        )

    return {
        "status": "ok",
        "device_id": device_id_value,
        "company": company,
        "pos_profile": pos_profile_name,
        "since_date": effective_since_date,
        "since_datetime": format_datetime(effective_since_datetime),
        "limit": resolved_limit,
        "next_cursor": str(offset + resolved_limit)
        if len(invoice_rows) > resolved_limit
        else None,
        "orders": [
            serialize_invoice_row(
                row,
                items=items_by_parent.get(cstr(row.get("name")).strip(), []),
                payments=payments_by_parent.get(cstr(row.get("name")).strip(), []),
            )
            for row in page_rows
        ],
        "refunds": [
            serialize_refund_row(
                row,
                items=refund_items_by_parent.get(cstr(row.get("name")).strip(), []),
                payments=refund_payments_by_parent.get(cstr(row.get("name")).strip(), []),
            )
            for row in refund_rows
        ],
    }


def resolve_history_device(device_id: str | None = None) -> Any:
    resolved_device_id = cstr(device_id).strip()
    if resolved_device_id:
        return get_device_doc(device_id=resolved_device_id)
    return get_authenticated_device_doc()


def normalize_limit(limit: str | int | None) -> int:
    requested_limit = cint(limit) if limit is not None else DEFAULT_LIMIT
    if requested_limit <= 0:
        return DEFAULT_LIMIT
    return min(requested_limit, MAX_LIMIT)


def normalize_cursor(cursor: str | int | None) -> int:
    if cursor in (None, ""):
        return 0
    offset = cint(cursor)
    if offset < 0:
        frappe.throw(_("cursor must be 0 or greater"), frappe.ValidationError)
    return offset


def resolve_since_date(
    *,
    device_id: str,
    pos_profile: str,
    requested_since_date: str | date | datetime | None,
) -> tuple[str, datetime | None]:
    shift_since_datetime = get_current_shift_since_datetime(
        device_id=device_id,
        pos_profile=pos_profile,
    )
    shift_since_date = (
        shift_since_datetime.date()
        if shift_since_datetime
        else get_current_shift_since_date(
            device_id=device_id,
            pos_profile=pos_profile,
        )
    )
    requested_date = parse_date_value(requested_since_date)
    if requested_date and shift_since_date:
        return max(requested_date, shift_since_date).isoformat(), shift_since_datetime
    if shift_since_date:
        return shift_since_date.isoformat(), shift_since_datetime
    if requested_date:
        return requested_date.isoformat(), shift_since_datetime
    return cstr(nowdate()), shift_since_datetime


def get_current_shift_since_datetime(
    device_id: str, pos_profile: str
) -> datetime | None:
    filters: dict[str, Any] = {
        "pos_profile": pos_profile,
        "docstatus": 1,
        "status": "Open",
        "custom_kopos_device_id": device_id,
    }
    entries = frappe.get_all(
        "POS Opening Entry",
        filters=filters,
        fields=["name", "period_start_date", "posting_date", "creation"],
        order_by="period_start_date desc, creation desc",
        limit_page_length=1,
    )
    if not entries:
        return None
    entry = dict(entries[0])
    creation = entry.get("creation")
    return get_datetime(creation) if creation else None


def get_current_shift_since_date(device_id: str, pos_profile: str) -> date | None:
    filters: dict[str, Any] = {
        "pos_profile": pos_profile,
        "docstatus": 1,
        "status": "Open",
        "custom_kopos_device_id": device_id,
    }
    entries = frappe.get_all(
        "POS Opening Entry",
        filters=filters,
        fields=["name", "period_start_date", "posting_date"],
        order_by="period_start_date desc, creation desc",
        limit_page_length=1,
    )
    if not entries:
        return None
    entry = dict(entries[0])
    return parse_date_value(entry.get("period_start_date")) or parse_date_value(
        entry.get("posting_date")
    )


def query_invoice_rows(
    *,
    company: str,
    pos_profile: str,
    since_date: str,
    since_datetime: datetime | None,
    limit: int,
    offset: int,
    device_id: str | None = None,
) -> list[dict[str, Any]]:
    filters: dict[str, Any] = {
        "docstatus": 1,
        "is_return": 0,
        "company": company,
        "pos_profile": pos_profile,
        "posting_date": [">=", since_date],
    }
    if device_id:
        filters["custom_kopos_device_id"] = device_id

    rows = [
        dict(row)
        for row in frappe.get_all(
            "POS Invoice",
            filters=filters,
            fields=get_invoice_fields(),
            order_by="posting_date desc, posting_time desc, creation desc, name desc",
            limit_start=offset,
            limit_page_length=limit,
        )
    ]

    if since_datetime:
        rows = [
            row
            for row in rows
            if get_datetime(row.get("creation")) >= since_datetime
        ]

    return rows


def query_refund_rows(
    *,
    company: str,
    pos_profile: str,
    invoice_names: list[str],
) -> list[dict[str, Any]]:
    if not invoice_names:
        return []
    return [
        dict(row)
        for row in frappe.get_all(
            "POS Invoice",
            filters={
                "docstatus": 1,
                "is_return": 1,
                "company": company,
                "pos_profile": pos_profile,
                "return_against": ["in", invoice_names],
            },
            fields=get_invoice_fields() + ["return_against"],
            order_by="posting_date desc, posting_time desc, creation desc, name desc",
        )
    ]


def query_child_rows_by_parent(
    doctype: str,
    parent_names: list[str],
    fields: list[str],
    *,
    order_by: str,
) -> dict[str, list[dict[str, Any]]]:
    if not parent_names:
        return {}
    rows = frappe.get_all(
        doctype,
        filters={"parent": ["in", parent_names], "parenttype": "POS Invoice"},
        fields=fields,
        order_by=order_by,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        parent = cstr(row.get("parent")).strip()
        if parent:
            grouped.setdefault(parent, []).append(dict(row))
    return grouped


def serialize_invoice_row(
    row: dict[str, Any],
    *,
    items: list[dict[str, Any]],
    payments: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "name": cstr(row.get("name")),
        "display_number": empty_to_none(row.get("custom_kopos_display_number"))
        or extract_display_number_from_remarks(row.get("remarks")),
        "idempotency_key": empty_to_none(row.get("custom_kopos_idempotency_key")),
        "device_id": empty_to_none(row.get("custom_kopos_device_id")),
        "company": cstr(row.get("company")),
        "pos_profile": cstr(row.get("pos_profile")),
        "customer": empty_to_none(row.get("customer")),
        "currency": empty_to_none(row.get("currency")),
        "posting_date": format_date(row.get("posting_date")),
        "posting_time": format_time(row.get("posting_time")),
        "created_at": format_datetime(row.get("creation")),
        "modified_at": format_datetime(row.get("modified")),
        "net_total": money_string(row.get("net_total")),
        "total_taxes_and_charges": money_string(row.get("total_taxes_and_charges")),
        "discount_amount": money_string(row.get("discount_amount")),
        "grand_total": money_string(row.get("grand_total")),
        "rounded_total": money_string(row.get("rounded_total")),
        "paid_amount": money_string(row.get("paid_amount")),
        "change_amount": money_string(row.get("change_amount")),
        "items": [serialize_item_row(item) for item in items],
        "payments": [serialize_payment_row(payment) for payment in payments],
    }


def serialize_refund_row(
    row: dict[str, Any],
    *,
    items: list[dict[str, Any]],
    payments: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        **serialize_invoice_row(row, items=items, payments=payments),
        "return_against": cstr(row.get("return_against")),
        "refund_reason_code": empty_to_none(row.get("custom_kopos_refund_reason_code")),
        "refund_reason": empty_to_none(row.get("custom_kopos_refund_reason")),
    }


def serialize_item_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "idx": cint(row.get("idx")),
        "item_code": cstr(row.get("item_code")),
        "item_name": cstr(row.get("item_name")),
        "description": empty_to_none(row.get("description")),
        "qty": decimal_string(row.get("qty")),
        "rate": money_string(row.get("rate")),
        "amount": money_string(row.get("amount")),
        "net_amount": money_string(row.get("net_amount")),
        "base_rate": money_string(row.get("base_rate")),
        "base_amount": money_string(row.get("base_amount")),
        "discount_amount": money_string(row.get("discount_amount")),
        "warehouse": empty_to_none(row.get("warehouse")),
        "modifier_total": money_string(row.get("custom_kopos_modifier_total")),
        "modifiers": serialize_modifiers_snapshot(row.get("custom_kopos_modifiers")),
    }


def serialize_payment_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "idx": cint(row.get("idx")),
        "mode_of_payment": cstr(row.get("mode_of_payment")),
        "amount": money_string(row.get("amount")),
        "account": empty_to_none(row.get("account")),
        "type": empty_to_none(row.get("type")),
        "default": bool(cint(row.get("default"))),
    }


def get_invoice_fields() -> list[str]:
    return [
        "name",
        "company",
        "pos_profile",
        "customer",
        "currency",
        "posting_date",
        "posting_time",
        "creation",
        "modified",
        "custom_kopos_idempotency_key",
        "custom_kopos_device_id",
        "custom_kopos_display_number",
        "net_total",
        "total_taxes_and_charges",
        "discount_amount",
        "grand_total",
        "rounded_total",
        "paid_amount",
        "change_amount",
        "custom_kopos_refund_reason_code",
        "custom_kopos_refund_reason",
        "remarks",
    ]


def get_invoice_item_fields() -> list[str]:
    return [
        "parent",
        "idx",
        "item_code",
        "item_name",
        "description",
        "qty",
        "rate",
        "amount",
        "net_amount",
        "base_rate",
        "base_amount",
        "discount_amount",
        "warehouse",
        "custom_kopos_modifiers",
        "custom_kopos_modifier_total",
        "custom_kopos_has_modifiers",
    ]


def serialize_modifiers_snapshot(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        snapshot = json.loads(cstr(value))
    except (TypeError, ValueError):
        return []
    modifiers = snapshot.get("modifiers") if isinstance(snapshot, dict) else None
    if not isinstance(modifiers, list):
        return []
    rows: list[dict[str, Any]] = []
    for modifier in modifiers:
        if not isinstance(modifier, dict):
            continue
        modifier_id = empty_to_none(modifier.get("id")) or empty_to_none(
            modifier.get("modifier")
        )
        name = empty_to_none(modifier.get("name")) or modifier_id
        if not modifier_id or not name:
            continue
        rows.append(
            {
                "id": modifier_id,
                "name": name,
                "group_id": empty_to_none(modifier.get("group_id"))
                or empty_to_none(modifier.get("modifier_group")),
                "price_adjustment": money_string(
                    modifier.get("price", modifier.get("price_adjustment"))
                ),
            }
        )
    return rows


def extract_display_number_from_remarks(value: Any) -> str | None:
    prefix = "KoPOS display number:"
    for line in cstr(value).splitlines():
        if line.startswith(prefix):
            display_number = line.removeprefix(prefix).strip()
            return display_number if display_number and display_number != "N/A" else None
    return None


def get_invoice_payment_fields() -> list[str]:
    return [
        "parent",
        "idx",
        "mode_of_payment",
        "amount",
        "account",
        "type",
        "default",
    ]


def money_string(value: Any) -> str:
    return decimal_string(value, quantize=MONEY_QUANTUM)


def decimal_string(value: Any, *, quantize: Decimal | None = None) -> str:
    try:
        decimal_value = Decimal(cstr(value) or "0")
    except (InvalidOperation, ValueError):
        decimal_value = Decimal("0")
    if quantize:
        decimal_value = decimal_value.quantize(quantize, rounding=ROUND_HALF_UP)
    return format(decimal_value, "f")


def parse_date_value(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return getdate(value)
    except Exception:
        frappe.throw(_("since_date is invalid"), frappe.ValidationError)
    return None


def format_date(value: Any) -> str | None:
    parsed = parse_date_value(value)
    return parsed.isoformat() if parsed else None


def format_time(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, time):
        return value.strftime("%H:%M:%S")
    return cstr(value)


def format_datetime(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        return get_datetime(value).isoformat()
    except Exception:
        return cstr(value)


def empty_to_none(value: Any) -> str | None:
    text = cstr(value).strip()
    return text or None
