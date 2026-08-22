# pyright: reportMissingImports=false

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import frappe

from kopos_connector.api.devices import privileged_device_api_operation
from kopos_connector.kopos.services.inventory_autopilot.exceptions import (
    resolve_inventory_exception,
    upsert_inventory_exception,
)
from kopos_connector.utils.diagnostics import (
    log_sanitized_error,
    make_savepoint,
    rollback_to_savepoint,
    sanitized_error_message,
)


EXCEPTION_REASON_CODE = "remake_inventory_projection_failed"


def create_remake_stock_entry(fb_remake_event: Any) -> str | None:
    remake_doc = _coerce_doc("FB Remake Event", fb_remake_event)
    if not remake_doc:
        return None

    existing_entry = _get_existing_reference(remake_doc, "replacement_stock_entry")
    if existing_entry:
        _set_source_reference(remake_doc, "inventory_status", "Posted")
        _resolve_exception(remake_doc)
        return existing_entry

    savepoint = make_savepoint("fb_remake_stock")
    try:
        resolved_sale = _load_resolved_sale(remake_doc)
        items_to_issue = _build_issue_items(resolved_sale)
        if not items_to_issue:
            _set_source_reference(remake_doc, "inventory_status", "Not Applicable")
            _resolve_exception(remake_doc)
            return None

        with privileged_device_api_operation("stock_projection"):
            stock_entry = frappe.new_doc("Stock Entry")
            stock_entry.stock_entry_type = "Material Issue"
            stock_entry.purpose = "Material Issue"
            stock_entry.company = _resolve_company(remake_doc)
            posting_dt = _resolve_posting_datetime(remake_doc)
            stock_entry.posting_date = posting_dt.date().isoformat()
            stock_entry.posting_time = posting_dt.time().strftime("%H:%M:%S")
            stock_entry.set_posting_time = 1
            stock_entry.remarks = f"Ingredient issue for remake {remake_doc.name}"
            _set_if_present(
                stock_entry,
                ["fb_remake_event", "custom_fb_remake_event"],
                remake_doc.name,
            )
            _set_if_present(
                stock_entry,
                ["custom_fb_order"],
                _value(remake_doc, "original_order"),
            )
            for item_row in items_to_issue:
                stock_entry.append("items", item_row)
            stock_entry.insert(ignore_permissions=True)
            stock_entry.submit()

        _set_source_reference(remake_doc, "replacement_stock_entry", stock_entry.name)
        _set_source_reference(remake_doc, "inventory_status", "Posted")
        _resolve_exception(remake_doc)
        return stock_entry.name
    except Exception as error:
        rollback_to_savepoint(savepoint, title="Remake stock rollback failed")
        log_sanitized_error("Remake stock entry creation failed", error)
        exception_name = _record_exception(remake_doc, error)
        _set_source_reference(remake_doc, "inventory_status", "Exception")
        if exception_name:
            _set_source_reference(remake_doc, "inventory_exception", exception_name)
        return None


def _load_resolved_sale(remake_doc: Any) -> Any:
    resolved_sale_name = str(_value(remake_doc, "original_resolved_sale") or "").strip()
    if not resolved_sale_name:
        raise ValueError(
            "The frozen sale-time ingredient snapshot is not ready for this order line"
        )
    if not frappe.db.exists("FB Resolved Sale", resolved_sale_name):
        raise ValueError(
            f"Frozen sale-time ingredient snapshot {resolved_sale_name} was not found"
        )
    return frappe.get_doc("FB Resolved Sale", resolved_sale_name)


def _build_issue_items(resolved_sale: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for component in list(_value(resolved_sale, "resolved_components") or []):
        if not int(_value(component, "affects_stock") or 0):
            continue
        item_code = str(_value(component, "item") or "").strip()
        warehouse = str(
            _value(component, "warehouse")
            or _value(resolved_sale, "booth_warehouse")
            or ""
        ).strip()
        stock_uom = str(
            _value(component, "stock_uom") or _value(component, "uom") or ""
        ).strip()
        stock_qty_value = _value(component, "stock_qty_decimal")
        if stock_qty_value in (None, ""):
            stock_qty_value = _value(component, "stock_qty")
        if stock_qty_value in (None, ""):
            stock_qty_value = _value(component, "qty")
        qty = _positive_decimal(stock_qty_value)
        if not item_code or not warehouse or not stock_uom:
            raise ValueError(
                "A stock-affecting remake component requires item, warehouse, and stock UOM"
            )
        items.append(
            {
                "item_code": item_code,
                "s_warehouse": warehouse,
                "qty": qty,
                "uom": stock_uom,
                "stock_uom": stock_uom,
                "conversion_factor": 1,
                "basic_rate": 0,
            }
        )
    return items


def _positive_decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Remake component stock quantity is invalid") from error
    if not result.is_finite() or result <= 0:
        raise ValueError("Remake component stock quantity must be greater than zero")
    return result


def _record_exception(remake_doc: Any, error: Exception) -> str | None:
    try:
        return upsert_inventory_exception(
            reason_code=EXCEPTION_REASON_CODE,
            summary="A recorded remake could not consume its frozen ingredient quantities",
            next_action=(
                "Review the remake event and its sale-time recipe evidence, then retry "
                f"the inventory posting. Detail: {sanitized_error_message(error)}"
            ),
            severity="Critical",
            company=_resolve_company(remake_doc),
            warehouse=_resolve_warehouse(remake_doc),
            source_doctype="FB Remake Event",
            source_name=str(_value(remake_doc, "name") or "").strip(),
        )
    except Exception as exception_error:
        log_sanitized_error(
            "Remake inventory exception registration failed", exception_error
        )
        return None


def _resolve_exception(remake_doc: Any) -> None:
    try:
        resolve_inventory_exception(
            reason_code=EXCEPTION_REASON_CODE,
            company=_resolve_company(remake_doc),
            warehouse=_resolve_warehouse(remake_doc),
            source_doctype="FB Remake Event",
            source_name=str(_value(remake_doc, "name") or "").strip(),
        )
        if _value(remake_doc, "inventory_exception"):
            _set_source_reference(remake_doc, "inventory_exception", None)
    except Exception as error:
        log_sanitized_error("Remake inventory exception resolution failed", error)


def _coerce_doc(doctype: str, value: Any):
    if not value:
        return None
    if getattr(value, "doctype", None) == doctype:
        return value
    try:
        return frappe.get_doc(doctype, value)
    except Exception:
        return None


def _value(doc: Any, fieldname: str) -> Any:
    if hasattr(doc, fieldname):
        return getattr(doc, fieldname)
    getter = getattr(doc, "get", None)
    if callable(getter):
        return getter(fieldname)
    return None


def _get_existing_reference(doc: Any, fieldname: str) -> str | None:
    value = _value(doc, fieldname)
    return str(value) if value else None


def _set_source_reference(doc: Any, fieldname: str, value: Any) -> None:
    if not hasattr(doc, fieldname):
        return
    try:
        doc.db_set(fieldname, value, update_modified=False)
    except Exception:
        setattr(doc, fieldname, value)
        doc.save(ignore_permissions=True)


def _resolve_order(remake_doc: Any) -> Any | None:
    original_order = _value(remake_doc, "original_order")
    if not original_order:
        return None
    try:
        return frappe.get_doc("FB Order", original_order)
    except Exception:
        return None


def _resolve_company(remake_doc: Any) -> str:
    order = _resolve_order(remake_doc)
    company = _value(order, "company") if order else None
    return str(company or frappe.defaults.get_defaults().get("company", "")).strip()


def _resolve_warehouse(remake_doc: Any) -> str | None:
    order = _resolve_order(remake_doc)
    warehouse = _value(order, "booth_warehouse") if order else None
    return str(warehouse).strip() if warehouse else None


def _resolve_posting_datetime(doc: Any):
    created_at = _value(doc, "modified") or _value(doc, "creation")
    if created_at:
        return frappe.utils.get_datetime(created_at)
    return frappe.utils.now_datetime()


def _set_if_present(doc: Any, fieldnames: list[str], value: Any) -> None:
    if value in (None, ""):
        return
    meta = frappe.get_meta(doc.doctype)
    for fieldname in fieldnames:
        if meta.has_field(fieldname):
            setattr(doc, fieldname, value)
            return
