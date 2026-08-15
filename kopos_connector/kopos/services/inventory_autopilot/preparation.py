"""Small, standard-document-backed batch preparation scheduler.

Prepared components are configured on the standard ERPNext BOM.  When a
director explicitly enables automatic preparation for a BOM, this service
creates one Draft Work Order per outlet when usable stock falls below the
configured ready level.  The existing POS task read model then surfaces that
Work Order to the outlet tablet.  Work Orders and Manufacture Stock Entries
remain the only authorities for the physical operation.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe
from frappe.utils import cstr

from kopos_connector.kopos.services.inventory_autopilot.exceptions import (
    upsert_inventory_exception,
)


def schedule_preparation_tasks() -> list[dict[str, Any]]:
    """Create safe, idempotent Work Orders for explicitly enabled BOMs.

    ``Review First`` never creates a Work Order automatically; it creates one
    understandable exception instead.  ``Active`` may create a Draft Work
    Order, which still needs the normal POS start and completion actions.
    Missing warehouse or BOM evidence blocks only that outlet/BOM.
    """

    if not frappe.db.exists("DocType", "BOM") or not frappe.db.exists("DocType", "Work Order"):
        return []
    bom_meta = frappe.get_meta("BOM")
    required_fields = {
        "custom_kopos_autoprep_enabled",
        "custom_kopos_batch_qty",
        "custom_kopos_min_ready_qty",
        "custom_kopos_preparation_lead_minutes",
        "custom_kopos_preparation_fingerprint",
    }
    if not required_fields.issubset({field.fieldname for field in bom_meta.fields}):
        # An upgrade that has not installed the optional fields must not
        # create a guessed batch.  The normal migration/preflight will surface
        # the missing field set.
        return []

    policies = frappe.get_all(
        "FB Inventory Policy",
        filters={"automation_state": ["in", ["Review First", "Active"]]},
        fields=["name", "company", "warehouse", "automation_state"],
        limit_page_length=10_000,
    )
    results: list[dict[str, Any]] = []
    for policy in policies:
        results.extend(_schedule_for_policy(policy))
    if results:
        frappe.db.commit()
    return results


def preparation_thresholds(
    *,
    bom_quantity: Any,
    configured_batch_qty: Any = None,
    configured_min_ready_qty: Any = None,
) -> tuple[Decimal, Decimal]:
    """Return positive batch and ready quantities using BOM quantity defaults."""

    fallback = _decimal(bom_quantity)
    batch_qty = _positive_or_default(configured_batch_qty, fallback)
    min_ready = _positive_or_default(configured_min_ready_qty, batch_qty)
    return batch_qty, min_ready


def _schedule_for_policy(policy: dict[str, Any]) -> list[dict[str, Any]]:
    company = cstr(policy.get("company")).strip()
    warehouse = cstr(policy.get("warehouse")).strip()
    if not company or not warehouse:
        return []
    rows = frappe.get_all(
        "BOM",
        filters={
            "company": company,
            "docstatus": 1,
            "is_active": 1,
            "custom_kopos_autoprep_enabled": 1,
        },
        fields=[
            "name",
            "item",
            "quantity",
            "custom_kopos_batch_qty",
            "custom_kopos_min_ready_qty",
            "custom_kopos_preparation_lead_minutes",
        ],
        limit_page_length=10_000,
    )
    results: list[dict[str, Any]] = []
    for bom in rows:
        item = cstr(bom.get("item")).strip()
        bom_name = cstr(bom.get("name")).strip()
        if not item or not bom_name:
            continue
        try:
            batch_qty, min_ready_qty = preparation_thresholds(
                bom_quantity=bom.get("quantity"),
                configured_batch_qty=bom.get("custom_kopos_batch_qty"),
                configured_min_ready_qty=bom.get("custom_kopos_min_ready_qty"),
            )
        except ValueError as error:
            _block_bom(policy, bom_name, item, f"Fix the prepared batch quantities: {error}")
            continue
        actual_qty = _actual_qty(item, warehouse)
        if actual_qty >= min_ready_qty:
            continue
        fingerprint = _fingerprint(company, warehouse, bom_name, batch_qty, min_ready_qty)
        existing = frappe.db.get_value(
            "Work Order",
            {"custom_kopos_preparation_fingerprint": fingerprint},
            "name",
        )
        if existing:
            results.append({"status": "existing", "work_order": cstr(existing), "fingerprint": fingerprint})
            continue
        if cstr(policy.get("automation_state")).strip() != "Active":
            exception = upsert_inventory_exception(
                reason_code="batch_preparation_review_first",
                summary=f"{item} is below its ready level and needs a batch preparation review",
                next_action="Review the standard BOM and create or start a Work Order when the outlet is ready",
                severity="Warning",
                company=company,
                warehouse=warehouse,
                item=item,
                source_doctype="BOM",
                source_name=bom_name,
            )
            results.append({"status": "review_first", "exception": exception, "fingerprint": fingerprint})
            continue
        try:
            work_order = _create_work_order(
                company=company,
                warehouse=warehouse,
                bom_name=bom_name,
                item=item,
                quantity=batch_qty,
                fingerprint=fingerprint,
            )
        except Exception as error:
            _block_bom(policy, bom_name, item, f"Create the prepared batch Work Order: {cstr(error)}")
            continue
        results.append({"status": "created", "work_order": work_order, "fingerprint": fingerprint})
    return results


def _create_work_order(
    *, company: str, warehouse: str, bom_name: str, item: str, quantity: Decimal, fingerprint: str
) -> str:
    document = frappe.new_doc("Work Order")
    document.company = company
    document.production_item = item
    document.bom_no = bom_name
    document.qty = quantity
    document.fg_warehouse = warehouse
    if frappe.get_meta("Work Order").has_field("wip_warehouse"):
        wip = frappe.db.get_value("Company", company, "default_wip_warehouse")
        if wip:
            document.wip_warehouse = wip
    document.custom_kopos_preparation_fingerprint = fingerprint
    document.insert(ignore_permissions=True)
    return cstr(document.name)


def _block_bom(policy: dict[str, Any], bom_name: str, item: str, action: str) -> None:
    upsert_inventory_exception(
        reason_code="batch_preparation_configuration",
        summary=f"Prepared batch {item} cannot be scheduled safely",
        next_action=action,
        severity="Warning",
        company=cstr(policy.get("company")),
        warehouse=cstr(policy.get("warehouse")),
        item=item,
        source_doctype="BOM",
        source_name=bom_name,
    )


def _actual_qty(item: str, warehouse: str) -> Decimal:
    raw = frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty")
    return _decimal(raw or 0)


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value if value not in (None, "") else "0"))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("quantity must be a finite decimal") from error
    if not result.is_finite() or result < 0:
        raise ValueError("quantity must be a non-negative finite decimal")
    return result


def _positive_or_default(value: Any, fallback: Decimal) -> Decimal:
    result = _decimal(value)
    return result if result > 0 else fallback


def _fingerprint(*values: Any) -> str:
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
