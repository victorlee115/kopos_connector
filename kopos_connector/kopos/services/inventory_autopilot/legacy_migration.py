from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import cstr

from kopos_connector.kopos.services.inventory_autopilot.holds import create_hold


LEGACY_FIELDS = (
    "custom_kopos_availability_mode",
    "custom_kopos_track_stock",
    "custom_kopos_min_qty",
)


def discover_legacy_values(*, company: str | None = None) -> list[dict[str, Any]]:
    """Return global Item legacy values for an explicitly selected outlet scope.

    ERPNext Item is a global master; it does not have a standard ``company``
    field.  The legacy flags therefore cannot be queried per company.  The
    caller selects the company and warehouse into which those global flags
    should be migrated, and the returned ``company`` is that selected scope --
    never an invented Item attribute.

    A site upgraded from a much older connector can be missing one or more
    legacy Custom Fields.  That is a valid "nothing to migrate" state, so
    query only fields that are actually installed rather than making preflight
    fail before it can produce its report.
    """

    scope_company = cstr(company).strip()
    fields = ["name", "item_code", *_installed_legacy_fields()]
    rows = frappe.get_all("Item", fields=fields, limit_page_length=10_000)
    return [
        {
            "item": cstr(row.get("item_code") or row.get("name")),
            "company": scope_company,
            "availability_mode": cstr(row.get("custom_kopos_availability_mode")),
            "track_stock": row.get("custom_kopos_track_stock"),
            "min_qty": row.get("custom_kopos_min_qty"),
        }
        for row in rows
    ]


def _installed_legacy_fields() -> tuple[str, ...]:
    """Return only legacy Item fields that are present in this site schema."""

    try:
        item_meta = frappe.get_meta("Item")
        return tuple(fieldname for fieldname in LEGACY_FIELDS if item_meta.has_field(fieldname))
    except Exception:
        # Metadata lookup is not a business authority.  Retain the previous
        # query shape if it is temporarily unavailable; a real query error is
        # still surfaced to the director rather than silently applying data.
        return LEGACY_FIELDS


def legacy_input_digest(values: list[dict[str, Any]]) -> str:
    """Bind execution to the exact dry-run rows a director reviewed."""

    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def migrate_legacy_values(*, warehouse: str, company: str, dry_run: bool = True) -> dict[str, Any]:
    values = discover_legacy_values(company=company)
    return _migrate_discovered_values(
        values=values,
        warehouse=warehouse,
        company=company,
        dry_run=dry_run,
    )


def execute_legacy_migration(
    *, warehouse: str, company: str, expected_digest: str
) -> dict[str, Any]:
    """Apply exactly the rows reviewed in the immediately preceding dry-run."""

    values = discover_legacy_values(company=company)
    actual_digest = legacy_input_digest(values)
    if cstr(expected_digest).strip() != actual_digest:
        raise ValueError(
            "input digest does not match the current dry-run rows; run dry-run again"
        )
    return _migrate_discovered_values(
        values=values,
        warehouse=warehouse,
        company=company,
        dry_run=False,
    )


def _migrate_discovered_values(
    *, values: list[dict[str, Any]], warehouse: str, company: str, dry_run: bool
) -> dict[str, Any]:
    unknown = [
        {**value, "reason": "unknown_availability_mode"} for value in values
        if value["availability_mode"].strip().lower() not in {"", "auto", "force_available", "force_unavailable"}
    ]
    report = {
        "status": "dry_run" if dry_run else "applied",
        "warehouse": warehouse,
        "company": company,
        "dry_run": dry_run,
        "input_digest": legacy_input_digest(values),
        "migrated": [],
        "blocked": unknown,
    }
    if unknown:
        report["status"] = "blocked"
    if unknown and not dry_run:
        return report
    for value in values:
        mode = value["availability_mode"].strip().lower()
        if mode not in {"", "auto", "force_available", "force_unavailable"}:
            continue
        report["migrated"].append(value)
        if dry_run or mode in {"", "auto"}:
            continue
        if mode == "force_unavailable":
            create_hold(
                target_type="Item",
                target_id=value["item"],
                company=company,
                warehouse=warehouse,
                source="manual",
                reason_code="legacy_force_unavailable",
                reason_label="Migrated from the legacy unavailable setting",
                idempotency_key=f"legacy-force-unavailable:{company}:{warehouse}:{value['item']}",
            )
        else:
            _create_off_rule(value["item"], company, warehouse)
    if not dry_run:
        frappe.db.commit()
    return report


def _create_off_rule(item: str, company: str, warehouse: str) -> None:
    existing = frappe.db.get_value(
        "FB Inventory Availability Rule",
        {"target_type": "Item", "target_id": item, "company": company, "warehouse": warehouse},
        "name",
    )
    if existing:
        return
    frappe.get_doc(
        {
            "doctype": "FB Inventory Availability Rule",
            "target_type": "Item",
            "target_id": item,
            "company": company,
            "warehouse": warehouse,
            "mode": "Off",
            "source_legacy_field": "custom_kopos_availability_mode",
        }
    ).insert()
