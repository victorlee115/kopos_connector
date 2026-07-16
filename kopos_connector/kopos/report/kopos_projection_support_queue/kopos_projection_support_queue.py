# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt
# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping

import frappe
from frappe import _
from frappe.utils import cint, cstr

from kopos_connector.api.devices import get_session_roles


SUPPORT_REPORT_ROLES = {"System Manager", "KoPOS Manager", "POS Manager"}
DEFAULT_PROJECTION_STATES = ["Pending", "Failed"]


def execute(filters=None):
    """Return pending/failed projection_status rows that need support review."""
    require_support_report_access()
    return get_columns(), get_data(filters or {})


def require_support_report_access(user: str | None = None) -> None:
    roles = get_session_roles(user=user)
    if roles.isdisjoint(SUPPORT_REPORT_ROLES):
        frappe.throw(
            _("Only System Manager, KoPOS Manager, or POS Manager can view projection support queues"),
            frappe.PermissionError,
        )


def get_columns() -> list[dict[str, object]]:
    return [
        {"label": _("Projection Log"), "fieldname": "name", "fieldtype": "Link", "options": "FB Projection Log", "width": 190},
        {"label": _("Projection Status"), "fieldname": "projection_status", "fieldtype": "Data", "width": 140},
        {"label": _("Review State"), "fieldname": "support_review", "fieldtype": "Data", "width": 170},
        {"label": _("Affected Order"), "fieldname": "affected_order", "fieldtype": "Link", "options": "FB Order", "width": 180},
        {"label": _("Source DocType"), "fieldname": "source_doctype", "fieldtype": "Data", "width": 130},
        {"label": _("Source Name"), "fieldname": "source_name", "fieldtype": "Dynamic Link", "options": "source_doctype", "width": 180},
        {"label": _("Projection Type"), "fieldname": "projection_type", "fieldtype": "Data", "width": 150},
        {"label": _("Idempotency Key"), "fieldname": "idempotency_key", "fieldtype": "Data", "width": 220},
        {"label": _("Retry Count"), "fieldname": "retry_count", "fieldtype": "Int", "width": 110},
        {"label": _("Next Retry"), "fieldname": "next_retry_at", "fieldtype": "Datetime", "width": 170},
        {"label": _("Last Attempt"), "fieldname": "last_attempt_at", "fieldtype": "Datetime", "width": 170},
        {"label": _("Manual Recovery Required"), "fieldname": "dead_lettered_at", "fieldtype": "Datetime", "width": 190},
        {"label": _("Target DocType"), "fieldname": "target_doctype", "fieldtype": "Data", "width": 130},
        {"label": _("Target Name"), "fieldname": "target_name", "fieldtype": "Data", "width": 180},
        {"label": _("Failure Reason"), "fieldname": "failure_reason", "fieldtype": "Data", "width": 280},
        {"label": _("Next Action"), "fieldname": "next_action", "fieldtype": "Data", "width": 360},
        {"label": _("Safe Retry"), "fieldname": "safe_retry", "fieldtype": "Data", "width": 260},
        {"label": _("Last Error Summary"), "fieldname": "last_error_summary", "fieldtype": "Data", "width": 320},
    ]


def get_data(filters: Mapping[str, object]) -> list[dict[str, object]]:
    state_filter = _states_from_filters(filters)
    rows = frappe.get_all(
        "FB Projection Log",
        filters={"state": ["in", state_filter]},
        fields=[
            "name",
            "source_doctype",
            "source_name",
            "projection_type",
            "idempotency_key",
            "state",
            "retry_count",
            "next_retry_at",
            "last_attempt_at",
            "dead_lettered_at",
            "target_doctype",
            "target_name",
            "last_error",
        ],
        order_by="last_attempt_at desc, modified desc",
        limit_page_length=500,
    )

    return [_build_support_row(row) for row in rows]


def _build_support_row(row: object) -> dict[str, object]:
    state = cstr(_read(row, "state")).strip()
    source_doctype = cstr(_read(row, "source_doctype")).strip()
    source_name = cstr(_read(row, "source_name")).strip()
    projection_type = cstr(_read(row, "projection_type")).strip()
    error_summary = _summarize_error(_read(row, "last_error"))
    dead_lettered_at = _read(row, "dead_lettered_at")
    return {
        "name": cstr(_read(row, "name")).strip(),
        "projection_status": state,
        "support_review": _support_review_copy(state, bool(dead_lettered_at)),
        "affected_order": source_name if source_doctype == "FB Order" else "",
        "source_doctype": source_doctype,
        "source_name": source_name,
        "projection_type": projection_type,
        "idempotency_key": cstr(_read(row, "idempotency_key")).strip(),
        "retry_count": cint(_read(row, "retry_count") or 0),
        "next_retry_at": _read(row, "next_retry_at"),
        "last_attempt_at": _read(row, "last_attempt_at"),
        "dead_lettered_at": dead_lettered_at,
        "target_doctype": cstr(_read(row, "target_doctype")).strip(),
        "target_name": cstr(_read(row, "target_name")).strip(),
        "failure_reason": _failure_reason(state, error_summary),
        "next_action": _next_action_copy(
            state, projection_type, bool(dead_lettered_at)
        ),
        "safe_retry": _safe_retry_copy(state, bool(dead_lettered_at)),
        "last_error_summary": error_summary,
    }


def _states_from_filters(filters: Mapping[str, object]) -> list[str]:
    requested = filters.get("state") or filters.get("projection_status")
    if isinstance(requested, str):
        state = requested.strip()
        return [state] if state else DEFAULT_PROJECTION_STATES
    if isinstance(requested, list):
        states = [cstr(state).strip() for state in requested if cstr(state).strip()]
        return states or DEFAULT_PROJECTION_STATES
    return DEFAULT_PROJECTION_STATES


def _support_review_copy(state: str, dead_lettered: bool = False) -> str:
    if dead_lettered:
        return _("Manual recovery required")
    if state == "Failed":
        return _("Support review required")
    if state == "Pending":
        return _("Pending projection")
    return _("Review if blocking close")


def _failure_reason(state: str, error_summary: str) -> str:
    if error_summary:
        return error_summary
    if state == "Pending":
        return _("Projection worker has not completed this row yet")
    if state == "Failed":
        return _("Projection failed without a stored error summary")
    return _("No failure reason recorded")


def _next_action_copy(
    state: str, projection_type: str, dead_lettered: bool = False
) -> str:
    if dead_lettered:
        return _(
            "Automatic retries are exhausted; correct the source failure, then use the supported idempotent retry workflow and verify the target document"
        )
    if state == "Failed":
        return _(
            "Review the linked FB Order, Sales Invoice, and projection error; retry only after the source data is corrected"
        )
    if state == "Pending":
        return _(
            "Wait for the projection worker or run the existing retry workflow if this blocks shift close"
        )
    if projection_type:
        return _("Confirm the {0} projection is no longer blocking reconciliation").format(
            projection_type
        )
    return _("Confirm this projection is no longer blocking reconciliation")


def _safe_retry_copy(state: str, dead_lettered: bool = False) -> str:
    if state == "Failed":
        if dead_lettered:
            return _("Supported after review: the handler reuses an existing target or creates exactly one target")
        return _("Automatic retry scheduled; the handler is idempotent and never relabels work without executing it")
    if state == "Pending":
        return _("Not needed yet; row is already waiting for projection")
    return _("No retry guidance required")


def _summarize_error(value: object) -> str:
    text = " ".join(cstr(value).split())
    if len(text) <= 180:
        return text
    return f"{text[:177]}..."


def _read(row: object, fieldname: str) -> object:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)
