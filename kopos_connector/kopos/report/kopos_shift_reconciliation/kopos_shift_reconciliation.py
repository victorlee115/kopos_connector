# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt
# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from kopos_connector.api.devices import get_session_roles


SUPPORT_REPORT_ROLES = {"System Manager", "KoPOS Manager", "POS Manager"}
SHIFT_LIMIT = 100
ORDER_LIMIT = 500


def execute(filters=None):
    """Return shift/order/invoice reconciliation rows for support review."""
    require_support_report_access()
    return get_columns(), get_data(filters or {})


def require_support_report_access(user: str | None = None) -> None:
    roles = get_session_roles(user=user)
    if roles.isdisjoint(SUPPORT_REPORT_ROLES):
        frappe.throw(
            _("Only System Manager, KoPOS Manager, or POS Manager can view shift reconciliation"),
            frappe.PermissionError,
        )


def get_columns() -> list[dict[str, object]]:
    return [
        {"label": _("FB Shift"), "fieldname": "fb_shift", "fieldtype": "Link", "options": "FB Shift", "width": 180},
        {"label": _("Shift Code"), "fieldname": "shift_code", "fieldtype": "Data", "width": 150},
        {"label": _("Shift Status"), "fieldname": "shift_status", "fieldtype": "Data", "width": 120},
        {"label": _("FB Order"), "fieldname": "fb_order", "fieldtype": "Link", "options": "FB Order", "width": 180},
        {"label": _("Sales Invoice"), "fieldname": "sales_invoice", "fieldtype": "Link", "options": "Sales Invoice", "width": 190},
        {"label": _("Idempotency Key"), "fieldname": "idempotency_key", "fieldtype": "Data", "width": 220},
        {"label": _("Order Total"), "fieldname": "order_total", "fieldtype": "Currency", "width": 120},
        {"label": _("Invoice Total"), "fieldname": "invoice_total", "fieldtype": "Currency", "width": 120},
        {"label": _("Payments"), "fieldname": "payments", "fieldtype": "Data", "width": 240},
        {"label": _("Refund/Void State"), "fieldname": "refund_void_state", "fieldtype": "Data", "width": 260},
        {"label": _("Projection Status"), "fieldname": "projection_status", "fieldtype": "Data", "width": 240},
        {"label": _("Cash Variance"), "fieldname": "cash_variance", "fieldtype": "Currency", "width": 120},
        {"label": _("Variance Summary"), "fieldname": "variance_summary", "fieldtype": "Data", "width": 280},
        {"label": _("Next Action"), "fieldname": "next_action", "fieldtype": "Data", "width": 360},
    ]


def get_data(filters: Mapping[str, object]) -> list[dict[str, object]]:
    shifts = _get_shift_rows(filters)
    if not shifts:
        return []

    shift_names = [cstr(row.get("name")).strip() for row in shifts if row.get("name")]
    orders = _get_order_rows(shift_names)
    order_names = [cstr(row.get("name")).strip() for row in orders if row.get("name")]
    invoices_by_order = _collect_invoices_by_order(order_names)
    returns_by_order = _collect_returns_by_order(order_names)
    projections_by_order = _collect_projection_rows_by_order(order_names)
    orders_by_shift: dict[str, list[dict[str, Any]]] = {}
    for order in orders:
        orders_by_shift.setdefault(cstr(order.get("shift")).strip(), []).append(order)

    data: list[dict[str, object]] = []
    for shift in shifts:
        shift_name = cstr(shift.get("name")).strip()
        shift_orders = orders_by_shift.get(shift_name, [])
        if not shift_orders:
            data.append(_build_shift_only_row(shift))
            continue
        for order in shift_orders:
            data.append(
                _build_order_reconciliation_row(
                    shift,
                    order,
                    invoices_by_order.get(cstr(order.get("name")).strip(), []),
                    returns_by_order.get(cstr(order.get("name")).strip(), []),
                    projections_by_order.get(cstr(order.get("name")).strip(), []),
                )
            )
    return data


def _get_shift_rows(filters: Mapping[str, object]) -> list[dict[str, Any]]:
    query_filters: dict[str, Any] = {}
    shift_name = cstr(filters.get("fb_shift") or filters.get("shift")).strip()
    status = cstr(filters.get("status") or filters.get("shift_status")).strip()
    if shift_name:
        query_filters["name"] = shift_name
    if status:
        query_filters["status"] = status
    return _get_rows(
        "FB Shift",
        filters=query_filters,
        fields=[
            "name",
            "shift_code",
            "device_id",
            "status",
            "opening_float",
            "expected_cash",
            "counted_cash",
            "cash_variance",
            "warehouse",
            "company",
            "creation",
        ],
        order_by="creation desc, name desc",
        limit_page_length=SHIFT_LIMIT,
    )


def _get_order_rows(shift_names: list[str]) -> list[dict[str, Any]]:
    if not shift_names:
        return []
    return _get_rows(
        "FB Order",
        filters={"shift": ["in", shift_names]},
        fields=[
            "name",
            "order_id",
            "external_idempotency_key",
            "shift",
            "status",
            "invoice_status",
            "stock_status",
            "sales_invoice",
            "grand_total",
            "currency",
            "docstatus",
            "creation",
        ],
        order_by="creation asc, name asc",
        limit_page_length=ORDER_LIMIT,
    )


def _collect_invoices_by_order(order_names: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not order_names:
        return {}
    rows = _get_rows(
        "Sales Invoice",
        filters={"custom_fb_order": ["in", order_names]},
        fields=[
            "name",
            "docstatus",
            "status",
            "is_return",
            "return_against",
            "grand_total",
            "paid_amount",
            "custom_fb_order",
            "custom_fb_shift",
            "custom_fb_idempotency_key",
        ],
        order_by="creation asc, name asc",
        limit_page_length=ORDER_LIMIT,
    )
    invoices_by_order: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        order_name = cstr(row.get("custom_fb_order")).strip()
        if not order_name:
            continue
        row["payments"] = _payment_summary(cstr(row.get("name")).strip())
        invoices_by_order.setdefault(order_name, []).append(row)
    return invoices_by_order


def _collect_returns_by_order(order_names: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not order_names:
        return {}
    rows = _get_rows(
        "FB Return Event",
        filters={"fb_order": ["in", order_names]},
        fields=[
            "name",
            "return_id",
            "fb_order",
            "original_sales_invoice",
            "return_sales_invoice",
            "return_to_stock",
            "status",
            "docstatus",
        ],
        order_by="creation asc, name asc",
        limit_page_length=ORDER_LIMIT,
    )
    returns_by_order: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        order_name = cstr(row.get("fb_order")).strip()
        if order_name:
            returns_by_order.setdefault(order_name, []).append(row)
    return returns_by_order


def _collect_projection_rows_by_order(order_names: list[str]) -> dict[str, list[dict[str, Any]]]:
    if not order_names:
        return {}
    rows = _get_rows(
        "FB Projection Log",
        filters={"source_doctype": "FB Order", "source_name": ["in", order_names]},
        fields=[
            "name",
            "source_name",
            "projection_type",
            "idempotency_key",
            "target_doctype",
            "target_name",
            "state",
            "retry_count",
            "last_error",
        ],
        order_by="creation asc, name asc",
        limit_page_length=ORDER_LIMIT,
    )
    projections_by_order: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        order_name = cstr(row.get("source_name")).strip()
        if order_name:
            projections_by_order.setdefault(order_name, []).append(row)
    return projections_by_order


def _build_shift_only_row(shift: Mapping[str, Any]) -> dict[str, object]:
    return {
        "fb_shift": cstr(shift.get("name")).strip(),
        "shift_code": cstr(shift.get("shift_code")).strip(),
        "shift_status": cstr(shift.get("status")).strip(),
        "fb_order": "",
        "sales_invoice": "",
        "idempotency_key": "",
        "order_total": None,
        "invoice_total": None,
        "payments": _("No submitted orders found for this FB Shift"),
        "refund_void_state": _("No refunds or voids found"),
        "projection_status": _("No order projections found"),
        "cash_variance": _money(shift.get("cash_variance")),
        "variance_summary": _variance_summary(shift),
        "next_action": _("Confirm whether this FB Shift is still expected to have no orders"),
    }


def _build_order_reconciliation_row(
    shift: Mapping[str, Any],
    order: Mapping[str, Any],
    invoices: list[dict[str, Any]],
    returns: list[dict[str, Any]],
    projections: list[dict[str, Any]],
) -> dict[str, object]:
    primary_invoice = _primary_invoice(order, invoices)
    return {
        "fb_shift": cstr(shift.get("name")).strip(),
        "shift_code": cstr(shift.get("shift_code")).strip(),
        "shift_status": cstr(shift.get("status")).strip(),
        "fb_order": cstr(order.get("name")).strip(),
        "sales_invoice": cstr(primary_invoice.get("name") or order.get("sales_invoice")).strip(),
        "idempotency_key": cstr(order.get("external_idempotency_key")).strip(),
        "order_total": _money(order.get("grand_total")),
        "invoice_total": _money(primary_invoice.get("grand_total")),
        "payments": cstr(primary_invoice.get("payments")).strip() or _("No payment rows found"),
        "refund_void_state": _refund_void_summary(invoices, returns, order),
        "projection_status": _projection_summary(projections, order),
        "cash_variance": _money(shift.get("cash_variance")),
        "variance_summary": _variance_summary(shift),
        "next_action": _reconciliation_next_action(primary_invoice, projections, order),
    }


def _primary_invoice(order: Mapping[str, Any], invoices: list[dict[str, Any]]) -> dict[str, Any]:
    expected_name = cstr(order.get("sales_invoice")).strip()
    for invoice in invoices:
        if cstr(invoice.get("name")).strip() == expected_name:
            return invoice
    for invoice in invoices:
        if not cint(invoice.get("is_return") or 0):
            return invoice
    return {}


def _payment_summary(invoice_name: str) -> str:
    if not invoice_name:
        return ""
    try:
        invoice_doc = frappe.get_doc("Sales Invoice", invoice_name)
    except Exception:
        return _("Payment rows unavailable")
    rows = []
    for payment in getattr(invoice_doc, "payments", None) or []:
        mode = cstr(_read(payment, "mode_of_payment")).strip() or _("unknown")
        rows.append(f"{mode}: {_money(_read(payment, 'amount')) or 0:.2f}")
    return "; ".join(rows)


def _refund_void_summary(
    invoices: list[dict[str, Any]],
    returns: list[dict[str, Any]],
    order: Mapping[str, Any],
) -> str:
    submitted_returns = [row for row in invoices if cint(row.get("is_return") or 0) and cint(row.get("docstatus") or 0) == 1]
    cancelled_invoices = [row for row in invoices if cint(row.get("docstatus") or 0) == 2]
    order_cancelled = cstr(order.get("status")).strip() == "Cancelled"
    parts = []
    if submitted_returns or returns:
        parts.append(_("{0} refund/return record(s)").format(len(submitted_returns) or len(returns)))
    if cancelled_invoices or order_cancelled:
        parts.append(_("void/cancelled state present"))
    return "; ".join(parts) if parts else _("No refunds or voids found")


def _projection_summary(projections: list[dict[str, Any]], order: Mapping[str, Any]) -> str:
    parts = []
    if cstr(order.get("invoice_status")).strip():
        parts.append(_("invoice {0}").format(cstr(order.get("invoice_status")).strip()))
    if cstr(order.get("stock_status")).strip():
        parts.append(_("stock {0}").format(cstr(order.get("stock_status")).strip()))
    for row in projections:
        projection_type = cstr(row.get("projection_type")).strip() or _("projection")
        state = cstr(row.get("state")).strip() or _("unknown")
        retry_count = cint(row.get("retry_count") or 0)
        parts.append(_("{0}: {1} ({2} retries)").format(projection_type, state, retry_count))
    return "; ".join(parts) if parts else _("No projection rows found")


def _variance_summary(shift: Mapping[str, Any]) -> str:
    opening_float = _money(shift.get("opening_float")) or 0
    expected_cash = _money(shift.get("expected_cash"))
    counted_cash = _money(shift.get("counted_cash"))
    variance = _money(shift.get("cash_variance"))
    return _("opening {0:.2f}; expected {1}; counted {2}; variance {3}").format(
        opening_float,
        _money_copy(expected_cash),
        _money_copy(counted_cash),
        _money_copy(variance),
    )


def _reconciliation_next_action(
    invoice: Mapping[str, Any],
    projections: list[dict[str, Any]],
    order: Mapping[str, Any],
) -> str:
    failed = [row for row in projections if cstr(row.get("state")).strip() == "Failed"]
    pending = [row for row in projections if cstr(row.get("state")).strip() == "Pending"]
    if failed:
        return _("Open Projection Support Queue and review failed projection before shift close")
    if pending:
        return _("Wait for projection worker or use the existing retry workflow if this remains blocked")
    if not cstr(invoice.get("name") or order.get("sales_invoice")).strip():
        return _("Investigate missing Sales Invoice for this FB Order")
    return _("Reconciled; no support action required")


def _get_rows(
    doctype: str,
    *,
    filters: dict[str, Any] | None = None,
    fields: list[str] | None = None,
    order_by: str | None = None,
    limit_page_length: int | None = None,
) -> list[dict[str, Any]]:
    rows = frappe.get_all(
        doctype,
        filters=filters or {},
        fields=fields or ["name"],
        order_by=order_by,
        limit_page_length=limit_page_length,
    )
    return [dict(row) for row in rows or []]


def _money(value: object) -> float | None:
    if value is None:
        return None
    return round(flt(value), 2)


def _money_copy(value: object) -> str:
    amount = _money(value)
    return _("not counted") if amount is None else f"{amount:.2f}"


def _read(row: object, fieldname: str) -> object:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)
