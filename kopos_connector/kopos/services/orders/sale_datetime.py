# pyright: reportMissingImports=false

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe


SALE_DATETIME_CLOCK_SKEW = timedelta(minutes=5)


def normalize_site_datetime(value: Any, *, fieldname: str) -> datetime:
    """Normalize an input datetime to a naive Frappe site-local datetime.

    Frappe stores Datetime values without timezone information. Aware timestamps
    therefore have to be converted to the configured site timezone before the
    timezone is removed. Naive timestamps are already interpreted as site-local
    for compatibility with Frappe clients and historical records.
    """

    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{fieldname} is required")

    try:
        parsed = frappe.utils.get_datetime(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{fieldname} must be a valid datetime") from error

    if not isinstance(parsed, datetime):
        raise ValueError(f"{fieldname} must be a valid datetime")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed

    timezone_name = str(frappe.utils.get_system_timezone() or "UTC").strip() or "UTC"
    try:
        site_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(
            f"Frappe site timezone {timezone_name} is not a valid IANA timezone"
        ) from error

    return parsed.astimezone(site_timezone).replace(tzinfo=None)


def validate_submit_sale_datetime(value: Any) -> datetime:
    """Validate the canonical order.created_at submit field."""

    try:
        return normalize_site_datetime(value, fieldname="order.created_at")
    except ValueError as error:
        frappe.throw(str(error), frappe.ValidationError)
    raise AssertionError("frappe.throw did not raise for invalid order.created_at")


def validate_submit_sale_datetime_bounds(
    sale_datetime: datetime,
    *,
    shift_name: str,
    shift_opened_at: Any = None,
) -> None:
    """Reject untrusted tablet times without altering legitimate offline sales."""

    current_datetime = normalize_site_datetime(
        frappe.utils.now_datetime(),
        fieldname="current site datetime",
    )
    if sale_datetime > current_datetime + SALE_DATETIME_CLOCK_SKEW:
        frappe.throw(
            "order.created_at cannot be more than 5 minutes in the future relative to the Frappe site time",
            frappe.ValidationError,
        )

    if shift_opened_at is None or (
        isinstance(shift_opened_at, str) and not shift_opened_at.strip()
    ):
        return

    try:
        opened_at = normalize_site_datetime(
            shift_opened_at,
            fieldname=f"FB Shift {shift_name} opened_at",
        )
    except ValueError as error:
        frappe.throw(str(error), frappe.ValidationError)
        raise AssertionError(
            f"frappe.throw did not raise for invalid FB Shift {shift_name} opened_at"
        )

    if sale_datetime < opened_at - SALE_DATETIME_CLOCK_SKEW:
        frappe.throw(
            f"order.created_at cannot be more than 5 minutes before FB Shift {shift_name} opened_at",
            frappe.ValidationError,
        )


def resolve_order_sale_datetime(order_doc: Any) -> datetime:
    """Resolve posting time, supporting FB Orders created before sale_datetime."""

    order_name = str(_value(order_doc, "name") or "unknown")
    for fieldname in ("sale_datetime", "creation", "modified"):
        value = _value(order_doc, fieldname)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        return normalize_site_datetime(
            value,
            fieldname=f"FB Order {order_name} {fieldname}",
        )

    return normalize_site_datetime(
        frappe.utils.now_datetime(),
        fieldname=f"FB Order {order_name} posting datetime",
    )


def _value(doc: Any, fieldname: str) -> Any:
    getter = getattr(doc, "get", None)
    if callable(getter):
        value = getter(fieldname)
        if value is not None or (isinstance(doc, dict) and fieldname in doc):
            return value
    if hasattr(doc, fieldname):
        return getattr(doc, fieldname)
    return None
