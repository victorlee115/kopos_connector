from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe
from frappe.utils import cstr, get_datetime, now_datetime


SOURCE_PRECEDENCE = {"manual": 5, "safety": 5, "quality": 5, "equipment": 5, "automation": 3}


def create_hold(
    *,
    target_type: str,
    target_id: str,
    company: str,
    warehouse: str,
    source: str,
    reason_code: str,
    reason_label: str,
    actor: str | None = None,
    pos_profile: str | None = None,
    originating_doctype: str | None = None,
    originating_name: str | None = None,
    expires_at: Any = None,
    idempotency_key: str | None = None,
) -> str:
    actor_name = cstr(actor or frappe.session.user).strip()
    key = cstr(idempotency_key).strip() or _idempotency_key(
        target_type, target_id, company, warehouse, source, reason_code, actor_name
    )
    existing = frappe.db.get_value("FB Availability Hold", {"idempotency_key": key}, "name")
    if existing:
        existing_doc = frappe.get_doc("FB Availability Hold", existing)
        if cstr(existing_doc.status).strip() == "Active" and existing_doc.expires_at and get_datetime(existing_doc.expires_at) <= now_datetime():
            existing_doc.expires_at = None
            existing_doc.save(ignore_permissions=True)
        return cstr(existing)
    document = frappe.get_doc(
        {
            "doctype": "FB Availability Hold",
            "target_type": target_type,
            "target_id": target_id,
            "company": company,
            "warehouse": warehouse,
            "source": source,
            "reason_code": reason_code,
            "reason_label": reason_label,
            "actor": actor_name,
            "pos_profile": pos_profile,
            "originating_doctype": originating_doctype,
            "originating_name": originating_name,
            "expires_at": expires_at,
            "idempotency_key": key,
            "active_from": now_datetime(),
            "status": "Active",
        }
    )
    document.insert()
    return cstr(document.name)


def release_hold(hold_name: str, *, actor: str | None = None) -> str:
    name = cstr(hold_name).strip()
    document = frappe.get_doc("FB Availability Hold", name)
    if document.status == "Released":
        return name
    document.status = "Released"
    document.save()
    return name


def manager_override_automation_hold(hold_name: str, *, actor: str, reason: str, minutes: int = 30) -> str:
    """Temporarily let a manager sell through an automation hold.

    The hold stays as the durable authority and expires back into the normal
    automation calculation. It is not marked Released, so a later reliable
    zero-capacity run can safely re-apply it.
    """

    name = cstr(hold_name).strip()
    if minutes != 30:
        raise ValueError("manager override duration is fixed at 30 minutes")
    if not cstr(reason).strip():
        frappe.throw("A manager override reason is required", frappe.ValidationError)
    document = frappe.get_doc("FB Availability Hold", name)
    if cstr(document.source).strip() != "automation" or cstr(document.status).strip() != "Active":
        frappe.throw("Only an active automation stock hold can receive a manager override", frappe.ValidationError)
    document.expires_at = now_datetime() + timedelta(minutes=minutes)
    document.actor = cstr(actor).strip() or document.actor
    if frappe.get_meta("FB Availability Hold").has_field("manager_override_reason"):
        document.manager_override_reason = cstr(reason).strip()
    if frappe.get_meta("FB Availability Hold").has_field("manager_override_at"):
        document.manager_override_at = now_datetime()
    document.save(ignore_permissions=True)
    return name


def active_holds(*, target_type: str, target_id: str, warehouse: str) -> list[dict[str, Any]]:
    rows = frappe.get_all(
        "FB Availability Hold",
        filters={"target_type": target_type, "target_id": target_id, "warehouse": warehouse, "status": "Active"},
        fields=["name", "source", "reason_code", "reason_label", "expires_at", "active_from", "manager_override_at"],
        order_by="active_from asc",
    )
    now = now_datetime()
    active: list[dict[str, Any]] = []
    for row in rows:
        expires_at = get_datetime(row["expires_at"]) if row.get("expires_at") else None
        override_at = get_datetime(row["manager_override_at"]) if row.get("manager_override_at") else None
        if override_at and expires_at and expires_at > now:
            # A manager override is a temporary selling window, not a release.
            continue
        if override_at:
            active.append(row)
            continue
        if not expires_at or expires_at > now:
            active.append(row)
    return active


def restore_automation_holds() -> int:
    """Release only automation holds when a trustworthy Bin has capacity."""

    released = 0
    rows = frappe.get_all(
        "FB Availability Hold",
        filters={"source": "automation", "status": "Active", "target_type": "Item"},
        fields=["name", "target_id", "warehouse"],
        limit_page_length=10_000,
    )
    for row in rows:
        actual_qty = frappe.db.get_value(
            "Bin",
            {"item_code": row.get("target_id"), "warehouse": row.get("warehouse")},
            "actual_qty",
        )
        try:
            capacity = Decimal(str(actual_qty)) if actual_qty is not None else Decimal("0")
        except (InvalidOperation, ValueError):
            capacity = Decimal("0")
        if capacity >= Decimal("1"):
            release_hold(cstr(row.get("name")))
            released += 1
    if released:
        frappe.db.commit()
    return released


def create_reliable_automation_holds() -> int:
    """Create zero-capacity holds only after a current reliable plan exists."""

    if not frappe.db.exists("DocType", "FB Inventory Availability Rule"):
        return 0
    rows = frappe.get_all(
        "FB Inventory Availability Rule",
        filters={"mode": "Auto Pause & Restore"},
        fields=["target_type", "target_id", "company", "warehouse"],
        limit_page_length=10_000,
    )
    created = 0
    for row in rows:
        if cstr(row.get("target_type")) != "Item" or not _reliable_warehouse(cstr(row.get("warehouse"))):
            continue
        qty = frappe.db.get_value("Bin", {"item_code": row.get("target_id"), "warehouse": row.get("warehouse")}, "actual_qty")
        try:
            is_zero = Decimal(str(qty or 0)) <= Decimal("0")
        except (InvalidOperation, ValueError):
            is_zero = True
        if not is_zero:
            continue
        create_hold(
            target_type="Item",
            target_id=cstr(row.get("target_id")),
            company=cstr(row.get("company")),
            warehouse=cstr(row.get("warehouse")),
            source="automation",
            reason_code="automation_zero_capacity",
            reason_label="Selling paused because reliable stock evidence shows zero usable capacity",
            originating_doctype="FB Inventory Availability Rule",
            originating_name=cstr(row.get("name")),
            idempotency_key=f"automation-zero-capacity:{row.get('company')}:{row.get('warehouse')}:{row.get('target_id')}",
        )
        created += 1
    if created:
        frappe.db.commit()
    return created


def _reliable_warehouse(warehouse: str) -> bool:
    if not warehouse or not frappe.db.exists("DocType", "FB Inventory Plan"):
        return False
    if frappe.db.exists("FB Inventory Exception", {"warehouse": warehouse, "status": "Open", "severity": "Critical"}):
        return False
    return bool(frappe.db.exists("FB Inventory Plan", {"warehouse": warehouse, "forecast_state": "Reliable", "status": ("in", ["Ready", "Executed"])}))


def choose_availability(*, commercially_enabled: bool, holds: list[dict[str, Any]], warning: bool) -> str:
    if not commercially_enabled:
        return "held"
    if any(SOURCE_PRECEDENCE.get(cstr(hold.get("source")), 0) >= 5 for hold in holds):
        return "held"
    if any(cstr(hold.get("source")) == "automation" for hold in holds):
        return "held"
    return "warning" if warning else "available"


def _idempotency_key(*values: str) -> str:
    return hashlib.sha256(
        json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
