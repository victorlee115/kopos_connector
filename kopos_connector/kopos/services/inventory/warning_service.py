from __future__ import annotations

from collections import defaultdict
from importlib import import_module
from typing import Any

frappe = import_module("frappe")
now_datetime = import_module("frappe.utils").now_datetime


def get_available_stock(item_code: str, warehouse: str) -> float:
    qty = frappe.db.get_value(
        "Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
    )
    return float(qty or 0)


def detect_stock_shortfall(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    required_stock_by_bin: dict[tuple[str, str], float] = defaultdict(float)

    for component in components:
        if not int(component.get("affects_stock") or 0):
            continue

        warehouse = component.get("warehouse")
        item_code = component.get("item")
        needed = float(component.get("stock_qty") or component.get("qty") or 0)
        if not warehouse or not item_code or needed <= 0:
            continue

        required_stock_by_bin[(str(item_code), str(warehouse))] += needed

    shortfalls: list[dict[str, Any]] = []
    for (item_code, warehouse), required_qty in required_stock_by_bin.items():
        available_qty = get_available_stock(item_code, warehouse)
        if available_qty + 0.0001 < required_qty:
            shortfalls.append(
                {
                    "item": item_code,
                    "item_code": item_code,
                    "warehouse": warehouse,
                    "available": available_qty,
                    "available_qty": available_qty,
                    "needed": required_qty,
                    "required_qty": required_qty,
                    "shortfall_qty": required_qty - available_qty,
                }
            )
    return shortfalls


def require_advisory_shortfall_policy(
    shortfalls: list[dict[str, Any]],
) -> None:
    """Fail before accounting unless ERP can honor the advisory stock policy.

    KoPOS intentionally keeps selling ordinary F&B ingredients during a
    transient stock-count shortfall.  That policy is only safe when ERPNext is
    configured to accept negative stock for non-serialised, non-batched items.
    Otherwise Sales Invoice projection could succeed while Stock Entry fails,
    leaving the shift impossible to close.  Validate the site policy before the
    FB Order is submitted so the outcome is deterministic.
    """

    if not shortfalls:
        return

    allow_negative_stock = frappe.db.get_single_value(
        "Stock Settings", "allow_negative_stock"
    )
    if not _is_enabled(allow_negative_stock):
        frappe.throw(
            "KoPOS advisory stock shortfall requires Stock Settings > Allow Negative Stock; "
            "the order was rejected before Sales Invoice creation",
            frappe.ValidationError,
        )

    item_codes = sorted(
        {
            str(row.get("item_code") or row.get("item") or "").strip()
            for row in shortfalls
            if str(row.get("item_code") or row.get("item") or "").strip()
        }
    )
    if not item_codes:
        frappe.throw(
            "KoPOS stock shortfall evidence is missing item codes",
            frappe.ValidationError,
        )

    item_rows = frappe.get_all(
        "Item",
        filters={"name": ["in", item_codes]},
        fields=["name", "has_serial_no", "has_batch_no"],
        limit_page_length=len(item_codes),
    )
    restricted_items = sorted(
        str(_value(row, "name"))
        for row in item_rows
        if _is_enabled(_value(row, "has_serial_no"))
        or _is_enabled(_value(row, "has_batch_no"))
    )
    if restricted_items:
        frappe.throw(
            "KoPOS cannot apply advisory negative stock to serialised or batched items: "
            + ", ".join(restricted_items),
            frappe.ValidationError,
        )


def log_stock_shortfall(
    fb_order: Any,
    shortfalls: list[dict[str, Any]],
    timestamp: Any | None = None,
) -> list[str]:
    if not shortfalls:
        return []

    log_names: list[str] = []
    logged_at = timestamp or now_datetime()
    order_reference = _value(fb_order, "order_id") or _value(fb_order, "name")

    for shortfall in shortfalls:
        item_code = str(shortfall.get("item_code") or shortfall.get("item") or "")
        warehouse = str(shortfall.get("warehouse") or "")
        required_qty = float(
            shortfall.get("required_qty") or shortfall.get("needed") or 0
        )
        available_qty = float(
            shortfall.get("available_qty") or shortfall.get("available") or 0
        )

        log_doc = frappe.new_doc("FB Stock Override Log")
        log_doc.override_id = _build_override_id(order_reference, item_code, warehouse)
        log_doc.fb_order = _value(fb_order, "name")
        log_doc.warehouse = warehouse
        log_doc.item = item_code
        log_doc.available_qty_before = available_qty
        log_doc.requested_qty = required_qty
        log_doc.shortfall_qty = float(shortfall.get("shortfall_qty") or 0)
        log_doc.reason_code = "Low Stock"
        log_doc.reason_text = (
            "KoPOS advisory ERP stock shortfall detected during submit"
        )
        log_doc.approved_at = logged_at

        if hasattr(log_doc, "order_reference"):
            log_doc.order_reference = order_reference
        if hasattr(log_doc, "logged_at"):
            log_doc.logged_at = logged_at

        log_doc.insert(ignore_permissions=True)
        log_names.append(log_doc.name)

    return log_names


def _build_override_id(order_reference: Any, item_code: str, warehouse: str) -> str:
    order_token = frappe.scrub(str(order_reference or "fb-order"))[:40] or "fb-order"
    item_token = frappe.scrub(item_code)[:20] or "item"
    warehouse_token = frappe.scrub(warehouse)[:20] or "warehouse"
    unique_suffix = frappe.generate_hash(length=8)
    return f"{order_token}-{item_token}-{warehouse_token}-{unique_suffix}".upper()


def _value(doc: Any, fieldname: str) -> Any:
    if hasattr(doc, fieldname):
        return getattr(doc, fieldname)

    getter = getattr(doc, "get", None)
    if callable(getter):
        return getter(fieldname)

    return None


def _is_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    try:
        return int(value or 0) == 1
    except (TypeError, ValueError):
        return False
