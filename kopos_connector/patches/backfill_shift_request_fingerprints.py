from __future__ import annotations

import hashlib
from typing import Any

import frappe
from frappe.utils import cstr


REQUIRED_COLUMNS = {
    "open_idempotency_key",
    "open_request_fingerprint",
    "close_idempotency_key",
    "close_request_fingerprint",
}


def execute() -> None:
    """Fail closed for shift requests whose original wire payload is unavailable."""
    reload_doc = getattr(frappe, "reload_doc", None)
    if callable(reload_doc):
        reload_doc("kopos", "doctype", "fb_shift")

    table_exists = getattr(frappe.db, "table_exists", None)
    if callable(table_exists) and not table_exists("FB Shift"):
        frappe.throw(
            "FB Shift schema is unavailable after reload; shift idempotency backfill was not applied",
            frappe.ValidationError,
        )
    if not all(frappe.db.has_column("FB Shift", field) for field in REQUIRED_COLUMNS):
        frappe.throw(
            "FB Shift idempotency columns are unavailable after reload; backfill was not applied",
            frappe.ValidationError,
        )

    rows = frappe.db.sql(
        """
        SELECT
            name, status, closed_at,
            open_idempotency_key, open_request_fingerprint,
            close_idempotency_key, close_request_fingerprint
        FROM `tabFB Shift`
        ORDER BY creation, name
        """,
        as_dict=True,
    )
    for row in rows or []:
        name = cstr(_row_value(row, "name")).strip()
        if not name:
            continue
        updates: dict[str, str] = {}
        if not cstr(_row_value(row, "open_idempotency_key")).strip():
            updates["open_idempotency_key"] = _legacy_key(name, "open")
        if not cstr(_row_value(row, "open_request_fingerprint")).strip():
            updates["open_request_fingerprint"] = _legacy_guard(name, "open")

        status = cstr(_row_value(row, "status")).strip()
        was_closed = bool(cstr(_row_value(row, "closed_at")).strip()) or status in {
            "Closing",
            "Closed",
            "Cancelled",
        }
        if was_closed:
            if not cstr(_row_value(row, "close_idempotency_key")).strip():
                updates["close_idempotency_key"] = _legacy_key(name, "close")
            if not cstr(_row_value(row, "close_request_fingerprint")).strip():
                updates["close_request_fingerprint"] = _legacy_guard(name, "close")

        if updates:
            frappe.db.set_value(
                "FB Shift",
                name,
                updates,
                update_modified=False,
            )


def _legacy_guard(name: str, operation: str) -> str:
    return hashlib.sha256(
        f"legacy-unverifiable\0FB Shift\0{name}\0{operation}".encode("utf-8")
    ).hexdigest()


def _legacy_key(name: str, operation: str) -> str:
    return hashlib.sha256(
        f"legacy-unverifiable-key\0FB Shift\0{name}\0{operation}".encode("utf-8")
    ).hexdigest()


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, dict):
        return row.get(fieldname)
    return getattr(row, fieldname, None)
