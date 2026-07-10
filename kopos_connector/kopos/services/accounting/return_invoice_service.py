# pyright: reportMissingImports=false

from __future__ import annotations

from typing import Any

import frappe

from kopos_connector.utils.diagnostics import (
    log_sanitized_error,
    make_savepoint,
    rollback_to_savepoint,
)


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
        return_invoice.is_pos = 0
        if hasattr(return_invoice, "set"):
            return_invoice.set("payments", [])
        else:
            return_invoice.payments = []
        _append_return_items(return_doc, original_invoice, return_invoice)
        if hasattr(return_invoice, "set_missing_values"):
            return_invoice.set_missing_values()
        if hasattr(return_invoice, "calculate_taxes_and_totals"):
            return_invoice.calculate_taxes_and_totals()
        return_invoice.update_stock = 0
        if hasattr(return_invoice, "paid_amount"):
            return_invoice.paid_amount = 0
        if hasattr(return_invoice, "change_amount"):
            return_invoice.change_amount = 0

        return_invoice.insert(ignore_permissions=True)
        return_invoice.submit()

        _set_source_reference(return_doc, "return_sales_invoice", return_invoice.name)
        refresh_fb_shift_cash(_value(original_invoice, "custom_fb_shift"))

        return return_invoice.name
    except Exception:
        _rollback_savepoint(savepoint)
        _log_error("Return sales invoice creation failed")
        raise


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
    return make_savepoint(prefix)


def _rollback_savepoint(savepoint: str) -> None:
    rollback_to_savepoint(savepoint, title="Return sales invoice rollback failed")


def _log_error(title: str) -> None:
    log_sanitized_error(title)


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
            continue
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


def refresh_fb_shift_cash(shift_name: Any) -> None:
    shift = cstr(shift_name).strip()
    if not shift:
        return

    shift_doc = frappe.get_doc("FB Shift", shift)
    sales_invoice_names = _get_shift_sales_invoice_names(shift)
    total_cash = 0.0

    for invoice_name in sales_invoice_names:
        invoice = _coerce_doc("Sales Invoice", invoice_name)
        if invoice and flt(_value(invoice, "docstatus")) == 1:
            total_cash += _get_cash_payment_total(invoice)

    for return_invoice_name in _get_shift_return_invoice_names(sales_invoice_names):
        return_invoice = _coerce_doc("Sales Invoice", return_invoice_name)
        if return_invoice and flt(_value(return_invoice, "docstatus")) == 1:
            total_cash += _get_return_cash_adjustment(return_invoice)

    expected_cash = flt(_value(shift_doc, "opening_float")) + total_cash
    _set_doc_field(shift_doc, "expected_cash", expected_cash)
    if _value(shift_doc, "counted_cash") is not None:
        _set_doc_field(
            shift_doc,
            "cash_variance",
            flt(_value(shift_doc, "counted_cash")) - expected_cash,
        )


def _get_shift_sales_invoice_names(shift: str) -> list[str]:
    rows = frappe.get_all(
        "FB Order",
        filters={"shift": shift, "status": "Submitted"},
        fields=["sales_invoice"],
    )
    invoice_names: list[str] = []
    for row in rows or []:
        invoice_name = cstr(_value(row, "sales_invoice")).strip()
        if invoice_name:
            invoice_names.append(invoice_name)
    return invoice_names


def _get_shift_return_invoice_names(sales_invoice_names: list[str]) -> list[str]:
    if not sales_invoice_names:
        return []
    rows = frappe.get_all(
        "Sales Invoice",
        filters={
            "return_against": ["in", sales_invoice_names],
            "is_return": 1,
            "docstatus": 1,
        },
        fields=["name"],
    )
    return [
        cstr(_value(row, "name")).strip()
        for row in rows or []
        if cstr(_value(row, "name")).strip()
    ]


def _get_cash_payment_total(invoice: Any) -> float:
    total = 0.0
    for payment in _value(invoice, "payments") or []:
        if cstr(_value(payment, "mode_of_payment")).strip() == "Cash":
            total += flt(_value(payment, "amount"))
    return total


def _get_return_cash_adjustment(return_invoice: Any) -> float:
    return_payments = list(_value(return_invoice, "payments") or [])
    if return_payments:
        return _get_cash_payment_total(return_invoice)

    original_invoice_name = cstr(_value(return_invoice, "return_against")).strip()
    original_invoice = _coerce_doc("Sales Invoice", original_invoice_name)
    if not original_invoice:
        return 0.0

    original_payments = list(_value(original_invoice, "payments") or [])
    total_paid = sum(abs(flt(_value(row, "amount"))) for row in original_payments)
    if total_paid <= 0:
        return 0.0
    cash_paid = sum(
        abs(flt(_value(row, "amount")))
        for row in original_payments
        if cstr(_value(row, "mode_of_payment")).strip() == "Cash"
    )
    return flt(_value(return_invoice, "grand_total")) * (cash_paid / total_paid)


def _set_doc_field(doc: Any, fieldname: str, value: Any) -> None:
    db_set = getattr(doc, "db_set", None)
    if callable(db_set):
        db_set(fieldname, value, update_modified=False)
        return
    setattr(doc, fieldname, value)
