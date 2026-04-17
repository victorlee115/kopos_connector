from __future__ import annotations

from typing import Any

import frappe


def create_reversal_stock_entry(fb_return_event: Any) -> str | None:
    return_doc = _coerce_doc("FB Return Event", fb_return_event)
    if not return_doc:
        return None

    if not int(_value(return_doc, "return_to_stock") or 0):
        return None

    existing_entry = _get_existing_reversal_entry(return_doc)
    if existing_entry:
        return existing_entry

    lines = _value(return_doc, "lines") or []
    if not lines:
        return None

    items_to_receive = []
    for line in lines:
        resolved_sale_name = _value(line, "original_resolved_sale")
        qty_returned = flt(_value(line, "qty_returned"))
        if not resolved_sale_name or qty_returned <= 0:
            continue

        resolved_sale = frappe.get_doc("FB Resolved Sale", resolved_sale_name)
        warehouse = _value(resolved_sale, "booth_warehouse")

        for component in _value(resolved_sale, "resolved_components") or []:
            if not int(_value(component, "affects_stock") or 0):
                continue

            item_code = _value(component, "item")
            component_qty = flt(
                _value(component, "stock_qty") or _value(component, "qty")
            )
            total_qty = resolved_sale.qty
            if total_qty <= 0:
                continue

            qty_per_unit = component_qty / total_qty
            return_qty = qty_per_unit * qty_returned

            items_to_receive.append(
                {
                    "item_code": item_code,
                    "t_warehouse": warehouse,
                    "qty": return_qty,
                    "stock_uom": _value(component, "stock_uom")
                    or _value(component, "uom"),
                    "basic_rate": 0,
                }
            )

    if not items_to_receive:
        return None

    savepoint = _make_savepoint("fb_reversal_stock")

    try:
        stock_entry = frappe.new_doc("Stock Entry")
        stock_entry.stock_entry_type = "Material Receipt"
        stock_entry.purpose = "Material Receipt"
        stock_entry.company = _resolve_company(return_doc)
        posting_dt = _resolve_posting_datetime(return_doc)
        stock_entry.posting_date = posting_dt.date().isoformat()
        stock_entry.posting_time = posting_dt.time().strftime("%H:%M:%S")
        stock_entry.set_posting_time = 1

        _set_if_present(
            stock_entry,
            ["fb_return_event", "custom_fb_return_event"],
            return_doc.name,
        )

        for item_row in items_to_receive:
            stock_entry.append("items", item_row)

        stock_entry.insert(ignore_permissions=True)
        stock_entry.submit()

        for line in lines:
            line.reversal_stock_entry = stock_entry.name
            line.db_set("reversal_stock_entry", stock_entry.name, update_modified=False)

        return stock_entry.name
    except Exception:
        _rollback_savepoint(savepoint)
        _log_error("Reversal stock entry creation failed")
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


def _get_existing_reversal_entry(return_doc: Any) -> str | None:
    lines = _value(return_doc, "lines") or []
    for line in lines:
        entry = _value(line, "reversal_stock_entry")
        if entry:
            return str(entry)
    return None


def _resolve_company(return_doc: Any) -> str:
    fb_order = _value(return_doc, "fb_order")
    if fb_order:
        order_doc = frappe.get_doc("FB Order", fb_order)
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
