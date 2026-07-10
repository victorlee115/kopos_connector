# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt
# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import frappe
from frappe import _

from kopos_connector.api.devices import get_session_roles
from kopos_connector.smoke import build_smoke_support_report


SUPPORT_REPORT_ROLES = {"System Manager", "KoPOS Manager", "POS Manager"}


def execute(filters=None):
    """Return support-readable smoke/dump proof without sensitive payloads."""
    require_support_report_access()
    expected_keys = _expected_keys_from_filters(filters or {})
    payload = build_smoke_support_report(expected_idempotency_keys=expected_keys)
    return get_columns(), _rows_from_payload(payload)


def require_support_report_access(user: str | None = None) -> None:
    roles = get_session_roles(user=user)
    if roles.isdisjoint(SUPPORT_REPORT_ROLES):
        frappe.throw(
            _("Only System Manager, KoPOS Manager, or POS Manager can view KoPOS support reports"),
            frappe.PermissionError,
        )


def get_columns() -> list[dict[str, object]]:
    return [
        {"label": _("Section"), "fieldname": "section", "fieldtype": "Data", "width": 180},
        {"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 150},
        {"label": _("Summary"), "fieldname": "summary", "fieldtype": "Data", "width": 360},
        {"label": _("Detail"), "fieldname": "detail", "fieldtype": "Data", "width": 420},
        {"label": _("Next Action"), "fieldname": "next_action", "fieldtype": "Data", "width": 360},
    ]


def _expected_keys_from_filters(filters: Mapping[str, object]) -> list[str]:
    value = filters.get("idempotency_key") or filters.get("expected_idempotency_key")
    if isinstance(value, str):
        keys = [part.strip() for part in value.split(",") if part.strip()]
        return keys
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _rows_from_payload(payload: Mapping[str, Any]) -> list[dict[str, object]]:
    summary = _mapping_or_empty(payload.get("summary"))
    proof = _mapping_or_empty(payload.get("proof"))
    projection = _mapping_or_empty(payload.get("projection_status"))
    reconciliation = _mapping_or_empty(payload.get("reconciliation"))
    rows = [
        {
            "section": _("Smoke Support Report"),
            "status": payload.get("status"),
            "summary": _("FB Shift {0}; FB Order {1}; Sales Invoice {2}").format(
                summary.get("fb_shifts", 0),
                summary.get("fb_orders", 0),
                summary.get("sales_invoices", 0),
            ),
            "detail": _("Generated from canonical smoke/dump state"),
            "next_action": payload.get("next_action"),
        },
        {
            "section": _("Idempotency"),
            "status": proof.get("idempotency_status"),
            "summary": _("Duplicate Sales Invoice keys: {0}").format(
                len(proof.get("duplicate_sales_invoice_keys") or [])
            ),
            "detail": _("Expected keys checked: {0}").format(
                ", ".join(proof.get("expected_idempotency_keys") or []) or _("none provided")
            ),
            "next_action": _("Investigate any idempotency key with more than one Sales Invoice"),
        },
        {
            "section": _("Legacy Path Status"),
            "status": proof.get("legacy_path_status"),
            "summary": _("Active legacy path count: {0}").format(
                proof.get("active_legacy_path_count", 0)
            ),
            "detail": _("Report intentionally omits legacy document names from support output"),
            "next_action": _("If count is non-zero, stop release and run contract vocabulary validation"),
        },
        {
            "section": _("Projection Status"),
            "status": proof.get("projection_status"),
            "summary": _("Failed projections: {0}").format(proof.get("failed_projection_count", 0)),
            "detail": _("State counts: {0}").format(projection.get("counts_by_state") or {}),
            "next_action": _("Open Projection Support Queue for failed or pending rows"),
        },
        {
            "section": _("Reconciliation"),
            "status": reconciliation.get("status"),
            "summary": _("Payments {0}; refunds {1}; voids {2}").format(
                reconciliation.get("payment_rows", 0),
                reconciliation.get("return_records", 0),
                reconciliation.get("void_records", 0),
            ),
            "detail": _("Cash variance rows: {0}").format(
                reconciliation.get("cash_variance_rows", 0)
            ),
            "next_action": _("Open Shift Reconciliation to inspect shift/order/invoice links"),
        },
    ]
    return rows


def _mapping_or_empty(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
