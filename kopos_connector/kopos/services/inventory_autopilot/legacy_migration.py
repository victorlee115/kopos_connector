from __future__ import annotations

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
    filters: dict[str, Any] = {}
    if company:
        filters["company"] = company
    fields = ["name", "item_code", "company", *LEGACY_FIELDS]
    rows = frappe.get_all("Item", filters=filters, fields=fields, limit_page_length=10_000)
    return [
        {
            "item": cstr(row.get("item_code") or row.get("name")),
            "company": cstr(row.get("company")),
            "availability_mode": cstr(row.get("custom_kopos_availability_mode")),
            "track_stock": row.get("custom_kopos_track_stock"),
            "min_qty": row.get("custom_kopos_min_qty"),
        }
        for row in rows
    ]


def migrate_legacy_values(*, warehouse: str, company: str, dry_run: bool = True) -> dict[str, Any]:
    report = {"warehouse": warehouse, "company": company, "dry_run": dry_run, "migrated": [], "blocked": []}
    for value in discover_legacy_values(company=company):
        mode = value["availability_mode"].strip().lower()
        if mode not in {"", "auto", "force_available", "force_unavailable"}:
            report["blocked"].append({**value, "reason": "unknown_availability_mode"})
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
