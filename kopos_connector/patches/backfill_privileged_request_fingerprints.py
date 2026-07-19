from __future__ import annotations

import hashlib
from typing import Any

import frappe
from frappe.utils import cstr


def execute() -> None:
    _backfill_legacy_guards(
        "FB Order",
        doctype_file="fb_order",
        idempotency_field="external_idempotency_key",
    )
    _backfill_legacy_guards(
        "FB Return Event",
        doctype_file="fb_return_event",
        idempotency_field="return_id",
    )


def _backfill_legacy_guards(
    doctype: str,
    *,
    doctype_file: str,
    idempotency_field: str,
) -> None:
    if not frappe.db.table_exists(doctype):
        return

    reload_doc = getattr(frappe, "reload_doc", None)
    if callable(reload_doc):
        reload_doc("kopos", "doctype", doctype_file)
    if not frappe.db.has_column(doctype, "request_fingerprint"):
        frappe.throw(
            f"{doctype} request fingerprint column is unavailable after reload; backfill was not applied",
            frappe.ValidationError,
        )
    rows = frappe.db.sql(
        f"""
        SELECT name, `{idempotency_field}`
        FROM `tab{doctype}`
        WHERE COALESCE(request_fingerprint, '') = ''
        ORDER BY creation, name
        """,
        as_dict=True,
    )
    for row in rows:
        name = cstr(_row_value(row, "name")).strip()
        idempotency_key = cstr(_row_value(row, idempotency_field)).strip()
        if not name:
            continue
        # The original wire payload cannot be reconstructed safely. Persist a
        # deterministic guard that intentionally cannot equal a new canonical
        # request fingerprint, so legacy retries fail closed without erasing data.
        guard = hashlib.sha256(
            f"legacy-unverifiable\0{doctype}\0{name}\0{idempotency_key}".encode(
                "utf-8"
            )
        ).hexdigest()
        frappe.db.set_value(
            doctype,
            name,
            "request_fingerprint",
            guard,
            update_modified=False,
        )


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, dict):
        return row.get(fieldname)
    return getattr(row, fieldname, None)
