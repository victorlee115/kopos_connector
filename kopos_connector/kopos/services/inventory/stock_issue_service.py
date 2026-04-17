from __future__ import annotations

from collections import defaultdict
from typing import Any

import frappe


def create_ingredient_stock_entry(fb_order: Any, resolved_sales: Any) -> str | None:
    order_doc = _coerce_doc("FB Order", fb_order)
    if not order_doc:
        return None

    existing_entry = _get_existing_reference(order_doc, "ingredient_stock_entry")
    if existing_entry:
        return existing_entry

    resolved_sale_docs = _coerce_resolved_sales(resolved_sales)
    if not resolved_sale_docs:
        return None

    grouped_items = _build_grouped_issue_items(resolved_sale_docs)
    if not grouped_items:
        return None

    savepoint = _make_savepoint("fb_stock_issue")

    try:
        stock_entry = frappe.new_doc("Stock Entry")
        stock_entry.stock_entry_type = "Material Issue"
        stock_entry.purpose = "Material Issue"
        stock_entry.company = _value(order_doc, "company")
        stock_entry.project = _value(order_doc, "event_project") or None
        posting_dt = _resolve_posting_datetime(order_doc)
        stock_entry.posting_date = posting_dt.date().isoformat()
        stock_entry.posting_time = posting_dt.time().strftime("%H:%M:%S")
        stock_entry.set_posting_time = 1
        stock_entry.remarks = _build_stock_entry_remarks(order_doc, resolved_sale_docs)

        _set_if_present(stock_entry, ["custom_fb_order"], order_doc.name)
        _set_if_present(stock_entry, ["custom_fb_shift"], _value(order_doc, "shift"))
        _set_if_present(
            stock_entry,
            ["custom_fb_event_project"],
            _value(order_doc, "event_project"),
        )

        for item_row in grouped_items:
            stock_entry.append("items", item_row)

        stock_entry.insert(ignore_permissions=True)
        stock_entry.submit()

        _set_source_reference(order_doc, "ingredient_stock_entry", stock_entry.name)
        _set_source_reference(order_doc, "stock_status", "Posted")

        for resolved_sale_doc in resolved_sale_docs:
            _set_source_reference(
                resolved_sale_doc, "stock_entry_issue", stock_entry.name
            )

        return stock_entry.name
    except Exception:
        _rollback_savepoint(savepoint)
        _log_error("Ingredient stock issue projection failed")
        return None


def _coerce_doc(doctype: str, value: Any):
    if not value:
        return None
    if getattr(value, "doctype", None) == doctype:
        return value
    try:
        return frappe.get_doc(doctype, value)
    except Exception:
        return None


def _coerce_resolved_sales(resolved_sales: Any) -> list[Any]:
    documents: list[Any] = []
    for value in resolved_sales or []:
        doc = _coerce_doc("FB Resolved Sale", value)
        if doc:
            documents.append(doc)
    return documents


def _value(doc: Any, fieldname: str) -> Any:
    if hasattr(doc, fieldname):
        return getattr(doc, fieldname)
    getter = getattr(doc, "get", None)
    if callable(getter):
        return getter(fieldname)
    return None


def _build_grouped_issue_items(resolved_sale_docs: list[Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(dict)

    for resolved_sale_doc in resolved_sale_docs:
        for component in list(_value(resolved_sale_doc, "resolved_components") or []):
            if not int(_value(component, "affects_stock") or 0):
                continue

            warehouse = _value(component, "warehouse") or _value(
                resolved_sale_doc, "booth_warehouse"
            )
            item_code = _value(component, "item")
            qty = float(_value(component, "stock_qty") or _value(component, "qty") or 0)
            stock_uom = _value(component, "stock_uom") or _value(component, "uom")

            if not warehouse or not item_code or qty <= 0:
                continue

            key = (
                warehouse,
                item_code,
                str(stock_uom or ""),
                resolved_sale_doc.company
                if hasattr(resolved_sale_doc, "company")
                else "",
            )
            current = grouped.get(key)
            if not current:
                grouped[key] = {
                    "item_code": item_code,
                    "s_warehouse": warehouse,
                    "qty": qty,
                    "uom": stock_uom,
                    "stock_uom": stock_uom,
                    "conversion_factor": 1,
                    "description": _build_component_description(
                        resolved_sale_doc, component
                    ),
                }
                continue

            current["qty"] = float(current.get("qty") or 0) + qty

    return list(grouped.values())


def _build_component_description(resolved_sale_doc: Any, component: Any) -> str:
    parts = [
        f"Resolved Sale: {resolved_sale_doc.name}",
        f"Source: {_value(component, 'source_type') or ''}",
        f"Reference: {_value(component, 'source_reference') or ''}",
    ]
    remarks = _value(component, "remarks")
    if remarks:
        parts.append(str(remarks))
    return " | ".join(part for part in parts if part and part.split(": ")[-1] != "")


def _build_stock_entry_remarks(order_doc: Any, resolved_sale_docs: list[Any]) -> str:
    resolved_sale_names = ", ".join(doc.name for doc in resolved_sale_docs)
    parts = [
        f"FB Order: {order_doc.name}",
        f"Shift: {_value(order_doc, 'shift') or ''}",
        f"Device ID: {_value(order_doc, 'device_id') or ''}",
        f"Resolved Sales: {resolved_sale_names}",
    ]
    return "\n".join(part for part in parts if part and part.split(": ")[-1] != "")


def _set_if_present(doc: Any, fieldnames: list[str], value: Any) -> None:
    if value in (None, ""):
        return

    meta = frappe.get_meta(doc.doctype)
    for fieldname in fieldnames:
        if meta.has_field(fieldname):
            setattr(doc, fieldname, value)
            return


def _resolve_posting_datetime(order_doc: Any):
    created_at = _value(order_doc, "modified") or _value(order_doc, "creation")
    if created_at:
        return frappe.utils.get_datetime(created_at)
    return frappe.utils.now_datetime()


def _get_existing_reference(doc: Any, fieldname: str) -> str | None:
    value = _value(doc, fieldname)
    return str(value) if value else None


def _set_source_reference(doc: Any, fieldname: str, value: Any) -> None:
    if not hasattr(doc, fieldname):
        return
    try:
        doc.db_set(fieldname, value, update_modified=True)
    except Exception:
        setattr(doc, fieldname, value)
        doc.save(ignore_permissions=True)


def _make_savepoint(prefix: str) -> str:
    name = f"{prefix}_{frappe.generate_hash(length=8)}"
    try:
        frappe.db.savepoint(name)
    except Exception:
        return ""
    return name


def _rollback_savepoint(savepoint: str) -> None:
    try:
        if savepoint:
            frappe.db.rollback(save_point=savepoint)
        else:
            frappe.db.rollback()
    except Exception:
        pass


def _log_error(title: str) -> None:
    try:
        frappe.log_error(frappe.get_traceback(), title)
    except Exception:
        pass
