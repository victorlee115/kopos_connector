from __future__ import annotations

from typing import Any

import frappe


def create_return_sales_invoice(fb_return_event: Any) -> str | None:
    return_doc = _coerce_doc("FB Return Event", fb_return_event)
    if not return_doc:
        return None

    existing_invoice = _get_existing_reference(return_doc, "return_sales_invoice")
    if existing_invoice:
        return existing_invoice

    original_invoice_name = _value(return_doc, "original_sales_invoice")
    if not original_invoice_name:
        return None

    savepoint = _make_savepoint("fb_return_invoice")

    try:
        original_invoice = frappe.get_doc("Sales Invoice", original_invoice_name)
        return_invoice = frappe.new_doc("Sales Invoice")
        return_invoice.customer = original_invoice.customer
        return_invoice.company = original_invoice.company
        return_invoice.currency = original_invoice.currency
        return_invoice.is_return = 1
        return_invoice.return_against = original_invoice_name
        return_invoice.set_posting_time = 1
        posting_dt = _resolve_posting_datetime(return_doc)
        return_invoice.posting_date = posting_dt.date().isoformat()
        return_invoice.posting_time = posting_dt.time().strftime("%H:%M:%S")

        _set_if_present(
            return_invoice,
            ["fb_return_event", "custom_fb_return_event"],
            return_doc.name,
        )

        _copy_invoice_dimensions(original_invoice, return_invoice)
        _append_return_items(return_doc, original_invoice, return_invoice)
        if hasattr(return_invoice, "set_missing_values"):
            return_invoice.set_missing_values()
        if hasattr(return_invoice, "calculate_taxes_and_totals"):
            return_invoice.calculate_taxes_and_totals()
        return_invoice.update_stock = 0

        return_invoice.insert(ignore_permissions=True)
        return_invoice.submit()

        _set_source_reference(return_doc, "return_sales_invoice", return_invoice.name)

        return return_invoice.name
    except Exception:
        _rollback_savepoint(savepoint)
        _log_error("Return sales invoice creation failed")
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


def flt(value: Any) -> float:
    return float(frappe.utils.flt(value))


def cstr(value: Any) -> str:
    return str(frappe.utils.cstr(value))


def _copy_invoice_dimensions(original_invoice: Any, return_invoice: Any) -> None:
    for fieldname in (
        "is_pos",
        "pos_profile",
        "cost_center",
        "project",
        "remarks",
        "custom_fb_order",
        "custom_fb_shift",
        "custom_fb_device_id",
        "custom_fb_event_project",
        "custom_fb_operational_status",
    ):
        if hasattr(original_invoice, fieldname):
            setattr(return_invoice, fieldname, getattr(original_invoice, fieldname))


def _append_return_items(
    return_doc: Any, original_invoice: Any, return_invoice: Any
) -> None:
    lines = _value(return_doc, "lines") or []
    if not lines:
        return
    for line in lines:
        resolved_sale_name = _value(line, "original_resolved_sale")
        qty_returned = abs(flt(_value(line, "qty_returned")))
        if not resolved_sale_name or qty_returned <= 0:
            continue
        original_row = _find_invoice_item(original_invoice, resolved_sale_name)
        if not original_row:
            frappe.throw(
                f"Original invoice row for resolved sale {resolved_sale_name} was not found"
            )
        return_invoice.append(
            "items",
            {
                "item_code": original_row.item_code,
                "item_name": original_row.item_name,
                "description": original_row.description,
                "qty": -qty_returned,
                "uom": original_row.uom,
                "conversion_factor": original_row.conversion_factor,
                "rate": original_row.rate,
                "amount": -(qty_returned * flt(original_row.rate)),
                "warehouse": getattr(original_row, "warehouse", None),
                "cost_center": getattr(original_row, "cost_center", None),
                "project": getattr(original_row, "project", None),
                "custom_fb_order_line_ref": getattr(
                    original_row, "custom_fb_order_line_ref", None
                ),
                "custom_fb_resolved_sale": resolved_sale_name,
                "custom_fb_recipe_snapshot_json": getattr(
                    original_row, "custom_fb_recipe_snapshot_json", None
                ),
                "custom_fb_resolution_hash": getattr(
                    original_row, "custom_fb_resolution_hash", None
                ),
            },
        )


def _find_invoice_item(original_invoice: Any, resolved_sale_name: str):
    for item in getattr(original_invoice, "items", []) or []:
        if cstr(getattr(item, "custom_fb_resolved_sale", None)) == cstr(
            resolved_sale_name
        ):
            return item
    return None
