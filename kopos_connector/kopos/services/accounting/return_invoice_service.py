# pyright: reportMissingImports=false

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import frappe

from kopos_connector.kopos.services.accounting.return_settlement_service import (
    get_settlement_cash_adjustment_sen,
)

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
        return_invoice = _make_standard_return_invoice(original_invoice_name)
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
        if hasattr(return_invoice, "custom_fb_idempotency_key"):
            return_invoice.custom_fb_idempotency_key = None
        return_invoice.is_pos = 0
        if hasattr(return_invoice, "set"):
            return_invoice.set("payments", [])
        else:
            return_invoice.payments = []
        _validate_full_standard_return_items(return_doc, return_invoice)
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

        return return_invoice.name
    except Exception:
        _rollback_savepoint(savepoint)
        _log_error("Return sales invoice creation failed")
        raise


def _make_standard_return_invoice(original_invoice_name: str):
    from erpnext.controllers.sales_and_purchase_return import make_return_doc

    return make_return_doc("Sales Invoice", original_invoice_name)


def _validate_full_standard_return_items(return_doc: Any, return_invoice: Any) -> None:
    requested: dict[str, Decimal] = {}
    for line in _value(return_doc, "lines") or []:
        resolved_sale = cstr(_value(line, "original_resolved_sale")).strip()
        if resolved_sale:
            requested[resolved_sale] = requested.get(
                resolved_sale, Decimal("0")
            ) + _quantity_decimal(
                _value(line, "qty_returned"), "requested return quantity"
            )
    returned: dict[str, Decimal] = {}
    for item in _value(return_invoice, "items") or []:
        resolved_sale = cstr(_value(item, "custom_fb_resolved_sale")).strip()
        if resolved_sale:
            returned[resolved_sale] = returned.get(
                resolved_sale, Decimal("0")
            ) + abs(
                _quantity_decimal(_value(item, "qty"), "standard return quantity")
            )
    if not returned or returned != requested:
        frappe.throw(
            "Standard Sales Invoice return does not exactly match the requested full resolved-sale quantities",
            frappe.ValidationError,
        )


def _quantity_decimal(value: Any, label: str) -> Decimal:
    try:
        return Decimal(cstr(value or 0).strip() or "0")
    except (InvalidOperation, ValueError) as error:
        frappe.throw(f"Invalid {label}: {value}", frappe.ValidationError)
        raise ValueError(f"Invalid {label}: {value}") from error


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


def refresh_fb_shift_cash(shift_name: Any) -> None:
    shift = cstr(shift_name).strip()
    if not shift:
        return

    shift_doc = frappe.get_doc("FB Shift", shift)
    sales_invoice_names = _get_shift_sales_invoice_names(shift)
    total_cash_sen = 0

    for invoice_name in sales_invoice_names:
        invoice = _coerce_doc("Sales Invoice", invoice_name)
        if invoice and flt(_value(invoice, "docstatus")) == 1:
            total_cash_sen += _get_cash_payment_total_sen(invoice)

    for return_event in _get_shift_return_events(sales_invoice_names):
        total_cash_sen += get_settlement_cash_adjustment_sen(return_event)

    expected_cash_sen = _money_to_sen(
        _value(shift_doc, "opening_float"), "FB Shift opening_float"
    ) + total_cash_sen
    _set_doc_field(shift_doc, "expected_cash", _sen_to_amount(expected_cash_sen))
    if _value(shift_doc, "counted_cash") is not None:
        counted_cash_sen = _money_to_sen(
            _value(shift_doc, "counted_cash"), "FB Shift counted_cash"
        )
        _set_doc_field(
            shift_doc,
            "cash_variance",
            _sen_to_amount(counted_cash_sen - expected_cash_sen),
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


def _get_shift_return_events(sales_invoice_names: list[str]) -> list[Any]:
    if not sales_invoice_names:
        return []
    rows = frappe.get_all(
        "FB Return Event",
        filters={
            "original_sales_invoice": ["in", sales_invoice_names],
            "docstatus": 1,
            "settlement_status": "Posted",
        },
        fields=["name"],
    )
    return [
        frappe.get_doc("FB Return Event", cstr(_value(row, "name")).strip())
        for row in rows or []
        if cstr(_value(row, "name")).strip()
    ]


def _get_cash_payment_total_sen(invoice: Any) -> int:
    total_sen = 0
    for payment in _value(invoice, "payments") or []:
        if cstr(_value(payment, "mode_of_payment")).strip() == "Cash":
            total_sen += _money_to_sen(
                _value(payment, "amount"), "Sales Invoice cash payment"
            )
    return total_sen


def _money_to_sen(value: Any, label: str) -> int:
    try:
        amount = Decimal(cstr(value or 0).strip() or "0")
    except (InvalidOperation, ValueError) as error:
        frappe.throw(f"Invalid {label}: {value}", frappe.ValidationError)
        raise ValueError(f"Invalid {label}: {value}") from error
    sen = amount * Decimal("100")
    integral_sen = sen.to_integral_value()
    if sen != integral_sen:
        frappe.throw(f"{label} contains fractional sen", frappe.ValidationError)
    return int(integral_sen)


def _sen_to_amount(value_sen: int) -> Decimal:
    return Decimal(value_sen) / Decimal("100")


def _set_doc_field(doc: Any, fieldname: str, value: Any) -> None:
    db_set = getattr(doc, "db_set", None)
    if callable(db_set):
        db_set(fieldname, value, update_modified=False)
        return
    setattr(doc, fieldname, value)
