from __future__ import annotations

import hashlib
from typing import Any

import frappe
from frappe.utils import cstr


def execute() -> None:
    doctype = "Maybank QR Transaction"
    reload_doc = getattr(frappe, "reload_doc", None)
    if callable(reload_doc):
        reload_doc("kopos", "doctype", "maybank_qr_transaction")
    if not frappe.db.table_exists(doctype):
        return

    required_columns = {
        "provider",
        "currency",
        "business_date",
        "request_fingerprint",
    }
    if not all(frappe.db.has_column(doctype, field) for field in required_columns):
        frappe.throw(
            "Maybank QR Transaction authenticity columns are unavailable after reload; backfill was not applied",
            frappe.ValidationError,
        )

    rows = frappe.get_all(
        doctype,
        fields=[
            "name",
            "device_id",
            "idempotency_key",
            "request_fingerprint",
        ],
        order_by="creation asc, name asc",
        limit_page_length=0,
    )
    duplicate_scopes = find_duplicate_idempotency_scopes(rows)
    if duplicate_scopes:
        evidence = "; ".join(
            "device={0} idempotency_key={1} records={2}".format(
                scope["device_id"],
                scope["idempotency_key"],
                ",".join(scope["names"]),
            )
            for scope in duplicate_scopes
        )
        frappe.log_error(
            title="KoPOS Maybank duplicate idempotency migration blocked",
            message=(
                "Migration made no changes. Preserve and reconcile every listed "
                f"Maybank QR Transaction before retrying: {evidence}"
            ),
        )
        frappe.throw(
            "Maybank QR Transaction migration blocked by duplicate "
            "(device_id, idempotency_key) evidence; no records were changed",
            frappe.ValidationError,
        )

    # This DocType is Maybank-only and the provider supports MYR transactions only.
    # Preserve all financial/evidence fields; backfill only immutable provenance.
    frappe.db.sql(
        """
        UPDATE `tabMaybank QR Transaction`
        SET provider = 'maybank_qr'
        WHERE COALESCE(provider, '') = ''
        """
    )
    frappe.db.sql(
        """
        UPDATE `tabMaybank QR Transaction`
        SET currency = 'MYR'
        WHERE COALESCE(currency, '') = ''
        """
    )
    frappe.db.sql(
        """
        UPDATE `tabMaybank QR Transaction`
        SET business_date = DATE(created_at)
        WHERE business_date IS NULL AND created_at IS NOT NULL
        """
    )

    used_fingerprints = {
        cstr(_row_value(row, "request_fingerprint")).strip()
        for row in rows
        if cstr(_row_value(row, "request_fingerprint")).strip()
    }
    for row in rows:
        if cstr(_row_value(row, "request_fingerprint")).strip():
            continue

        device_id = cstr(_row_value(row, "device_id")).strip()
        idempotency_key = cstr(_row_value(row, "idempotency_key")).strip()
        if not device_id or not idempotency_key:
            continue

        canonical = _fingerprint(f"{device_id}\0{idempotency_key}")
        fingerprint = canonical
        if fingerprint in used_fingerprints:
            fingerprint = _fingerprint(
                f"{canonical}\0legacy-duplicate\0{_row_value(row, 'name')}"
            )
        used_fingerprints.add(fingerprint)
        frappe.db.set_value(
            doctype,
            _row_value(row, "name"),
            "request_fingerprint",
            fingerprint,
            update_modified=False,
        )


def find_duplicate_idempotency_scopes(
    rows: list[Any],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[str]] = {}
    for row in rows or []:
        device_id = cstr(_row_value(row, "device_id")).strip()
        idempotency_key = cstr(_row_value(row, "idempotency_key")).strip()
        name = cstr(_row_value(row, "name")).strip()
        if not device_id or not idempotency_key or not name:
            continue
        grouped.setdefault((device_id, idempotency_key), []).append(name)

    return [
        {
            "device_id": device_id,
            "idempotency_key": idempotency_key,
            "names": sorted(names),
        }
        for (device_id, idempotency_key), names in sorted(grouped.items())
        if len(names) > 1
    ]


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, dict):
        return row.get(fieldname)
    return getattr(row, fieldname, None)
