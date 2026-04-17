from __future__ import annotations

from typing import Any

import frappe


def create_remake_stock_entry(fb_remake_event: Any) -> str | None:
    remake_doc = _coerce_doc("FB Remake Event", fb_remake_event)
    if not remake_doc:
        return None

    existing_entry = _get_existing_reference(remake_doc, "replacement_stock_entry")
    if existing_entry:
        return existing_entry

    original_resolved_sale = _value(remake_doc, "original_resolved_sale")
    if not original_resolved_sale:
        return None

    resolved_sale = frappe.get_doc("FB Resolved Sale", original_resolved_sale)
    items_to_issue = []

    for component in _value(resolved_sale, "resolved_components") or []:
        if not int(_value(component, "affects_stock") or 0):
            continue

        item_code = _value(component, "item")
        qty = flt(_value(component, "stock_qty") or _value(component, "qty"))

        if not item_code or qty <= 0:
            continue

        items_to_issue.append(
            {
                "item_code": item_code,
                "s_warehouse": _value(component, "warehouse")
                or _value(resolved_sale, "booth_warehouse"),
                "qty": qty,
                "stock_uom": _value(component, "stock_uom") or _value(component, "uom"),
                "basic_rate": 0,
            }
        )

    if not items_to_issue:
        return None

    savepoint = _make_savepoint("fb_remake_stock")

    try:
        stock_entry = frappe.new_doc("Stock Entry")
        stock_entry.stock_entry_type = "Material Issue"
        stock_entry.purpose = "Material Issue"
        stock_entry.company = _resolve_company(remake_doc)
        posting_dt = _resolve_posting_datetime(remake_doc)
        stock_entry.posting_date = posting_dt.date().isoformat()
        stock_entry.posting_time = posting_dt.time().strftime("%H:%M:%S")
        stock_entry.set_posting_time = 1
        stock_entry.remarks = f"Remake for {original_resolved_sale}"

        _set_if_present(
            stock_entry,
            ["fb_remake_event", "custom_fb_remake_event"],
            remake_doc.name,
        )

        for item_row in items_to_issue:
            stock_entry.append("items", item_row)

        stock_entry.insert(ignore_permissions=True)
        stock_entry.submit()

        _set_source_reference(remake_doc, "replacement_stock_entry", stock_entry.name)

        return stock_entry.name
    except Exception:
        _rollback_savepoint(savepoint)
        _log_error("Remake stock entry creation failed")
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
        doc.db_set(fieldname, value, update_modified=True)
    except Exception:
        setattr(doc, fieldname, value)
        doc.save(ignore_permissions=True)


def _resolve_company(remake_doc: Any) -> str:
    original_order = _value(remake_doc, "original_order")
    if original_order:
        order_doc = frappe.get_doc("FB Order", original_order)
        company = _value(order_doc, "company")
        if company:
            return company
    return frappe.defaults.get_defaults().get("company", "")


def _resolve_posting_datetime(doc: Any):
    created_at = _value(doc, "modified") or _value(doc, "creation")
    if created_at:
        return frappe.utils.get_datetime(created_at)
    return frappe.utils.now_datetime()


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


def _set_if_present(doc: Any, fieldnames: list[str], value: Any) -> None:
    if value in (None, ""):
        return

    meta = frappe.get_meta(doc.doctype)
    for fieldname in fieldnames:
        if meta.has_field(fieldname):
            setattr(doc, fieldname, value)
            return


def flt(value: Any) -> float:
    return float(frappe.utils.flt(value))
