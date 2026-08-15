from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import frappe
from frappe.utils import cstr, get_datetime, now_datetime

from kopos_connector.kopos.services.inventory_autopilot.availability_capacity import (
    CapacityResult,
    target_capacity,
)


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
    evaluated_at: Any = None,
    idempotency_key: str | None = None,
) -> str:
    actor_name = cstr(actor or frappe.session.user).strip()
    key = cstr(idempotency_key).strip() or _idempotency_key(
        target_type, target_id, company, warehouse, source, reason_code, actor_name
    )
    existing = frappe.db.get_value("FB Availability Hold", {"idempotency_key": key}, "name")
    if existing:
        existing_doc = frappe.get_doc("FB Availability Hold", existing)
        if cstr(existing_doc.status).strip() == "Active" and source == "automation":
            # An automation hold is refreshed only by a new trustworthy
            # calculation.  A manager override remains a timed selling window;
            # it must not be silently cleared before its 30 minutes end.
            existing_doc.last_evaluated_at = evaluated_at or now_datetime()
            if existing_doc.expires_at and get_datetime(existing_doc.expires_at) <= now_datetime():
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
            "last_evaluated_at": evaluated_at or (now_datetime() if source == "automation" else None),
            "idempotency_key": key,
            "active_from": now_datetime(),
            "status": "Active",
        }
    )
    # Device commands are authorized and outlet-scoped by the whitelisted API
    # before reaching this domain owner.  The device role deliberately has no
    # direct DocType permission, so persistence must use the trusted service
    # boundary rather than granting broad document access.
    document.insert(ignore_permissions=True)
    return cstr(document.name)


def release_hold(hold_name: str, *, actor: str | None = None) -> str:
    name = cstr(hold_name).strip()
    document = frappe.get_doc("FB Availability Hold", name)
    if document.status == "Released":
        return name
    document.status = "Released"
    document.save(ignore_permissions=True)
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
        fields=["name", "source", "reason_code", "reason_label", "expires_at", "active_from", "last_evaluated_at", "manager_override_at", "company", "pos_profile"],
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
        source = cstr(row.get("source")).strip()
        # A disconnected device must never turn a durable manual/safety hold
        # into a saleable target.  Automation holds also remain held after an
        # unexpected expiry; they are marked overdue so the overlay can open a
        # manager exception rather than silently restoring selling.
        if source in {"manual", "safety", "quality", "equipment"}:
            # These are human or safety authorities.  Device connectivity
            # cannot release or degrade them.
            row["stale"] = False
            active.append(row)
            continue
        if override_at:
            row["stale"] = False
            active.append(row)
            continue
        row["stale"] = _automation_hold_is_stale(row, now=now)
        active.append(row)
    return active


def restore_automation_holds(*, warehouse: str | None = None) -> int:
    """Release only matching automation holds after reliable capacity recovers.

    A mode change away from ``Auto Pause & Restore`` also releases the old
    automation hold.  Manual, safety, quality, and equipment holds are never
    touched here.
    """

    released = 0
    filters: dict[str, Any] = {
        "source": "automation",
        "status": "Active",
    }
    if cstr(warehouse).strip():
        filters["warehouse"] = cstr(warehouse).strip()
    rows = frappe.get_all(
        "FB Availability Hold",
        filters=filters,
        fields=["name", "target_type", "target_id", "warehouse", "company"],
        limit_page_length=10_000,
    )
    for row in rows:
        company = cstr(row.get("company")).strip()
        target_type = cstr(row.get("target_type")).strip()
        target_id = cstr(row.get("target_id")).strip()
        target_warehouse = cstr(row.get("warehouse")).strip()
        mode = cstr(
            frappe.db.get_value(
                "FB Inventory Availability Rule",
                {
                    "target_type": target_type,
                    "target_id": target_id,
                    "company": company,
                    "warehouse": target_warehouse,
                },
                "mode",
            )
        ).strip()
        if mode != "Auto Pause & Restore":
            release_hold(cstr(row.get("name")))
            released += 1
            continue
        result = target_capacity(
            target_type=target_type,
            target_id=target_id,
            company=company,
            warehouse=target_warehouse,
        )
        if result.reliable and result.capacity is not None and result.capacity >= Decimal("1"):
            release_hold(cstr(row.get("name")))
            released += 1
    if released:
        frappe.db.commit()
    return released


def create_reliable_automation_holds(*, warehouse: str | None = None) -> int:
    """Evaluate explicit availability modes from the shared capacity result."""

    if not frappe.db.exists("DocType", "FB Inventory Availability Rule"):
        return 0
    filters: dict[str, Any] = {}
    if cstr(warehouse).strip():
        filters["warehouse"] = cstr(warehouse).strip()
    rows = frappe.get_all(
        "FB Inventory Availability Rule",
        filters=filters,
        fields=["name", "target_type", "target_id", "company", "warehouse", "mode"],
        limit_page_length=10_000,
    )
    created = 0
    for row in rows:
        target_type = cstr(row.get("target_type")).strip()
        target_id = cstr(row.get("target_id")).strip()
        company = cstr(row.get("company")).strip()
        target_warehouse = cstr(row.get("warehouse")).strip()
        if target_type not in {"Item", "Modifier"}:
            continue
        result = target_capacity(
            target_type=target_type,
            target_id=target_id,
            company=company,
            warehouse=target_warehouse,
        )
        _evaluate_manager_exception(row, result)
        if cstr(row.get("mode")).strip() != "Auto Pause & Restore":
            continue
        if not result.reliable or result.capacity != Decimal("0"):
            continue
        create_hold(
            target_type=target_type,
            target_id=target_id,
            company=company,
            warehouse=target_warehouse,
            source="automation",
            reason_code="automation_zero_capacity",
            reason_label="Selling paused because reliable recipe stock evidence shows zero usable capacity",
            originating_doctype="FB Inventory Availability Rule",
            originating_name=cstr(row.get("name")),
            evaluated_at=now_datetime(),
            idempotency_key=f"automation-zero-capacity:{company}:{target_warehouse}:{target_type}:{target_id}",
        )
        created += 1
    if created:
        frappe.db.commit()
    return created


def _evaluate_manager_exception(row: dict[str, Any], result: CapacityResult) -> None:
    """Create/resolve the one Ask Manager exception without overlay writes."""

    from kopos_connector.kopos.services.inventory_autopilot.exceptions import (
        resolve_inventory_exception,
        upsert_inventory_exception,
    )

    if cstr(row.get("mode")).strip() != "Ask Manager":
        resolve_inventory_exception(
            reason_code="availability_manager_action_required",
            company=cstr(row.get("company")),
            warehouse=cstr(row.get("warehouse")),
            item=cstr(row.get("target_id")) if cstr(row.get("target_type")) == "Item" else None,
            source_doctype="FB Inventory Availability Rule",
            source_name=cstr(row.get("name")),
        )
        return
    if result.reliable and result.capacity == Decimal("0"):
        upsert_inventory_exception(
            reason_code="availability_manager_action_required",
            summary="Stock evidence shows this target has no reliable sellable capacity",
            next_action="Review the stock check and pause or restore the target from the manager POS flow",
            severity="Warning",
            company=cstr(row.get("company")),
            warehouse=cstr(row.get("warehouse")),
            item=cstr(row.get("target_id")) if cstr(row.get("target_type")) == "Item" else None,
            source_doctype="FB Inventory Availability Rule",
            source_name=cstr(row.get("name")),
        )
    elif result.reliable and result.capacity is not None and result.capacity >= Decimal("1"):
        resolve_inventory_exception(
            reason_code="availability_manager_action_required",
            company=cstr(row.get("company")),
            warehouse=cstr(row.get("warehouse")),
            item=cstr(row.get("target_id")) if cstr(row.get("target_type")) == "Item" else None,
            source_doctype="FB Inventory Availability Rule",
            source_name=cstr(row.get("name")),
        )


def record_stale_automation_hold_exceptions(*, warehouse: str | None = None) -> int:
    """Create one manager exception for automation holds without fresh evidence."""

    from kopos_connector.kopos.services.inventory_autopilot.exceptions import (
        resolve_inventory_exception,
        upsert_inventory_exception,
    )

    filters: dict[str, Any] = {"source": "automation", "status": "Active"}
    if cstr(warehouse).strip():
        filters["warehouse"] = cstr(warehouse).strip()
    rows = frappe.get_all(
        "FB Availability Hold",
        filters=filters,
        fields=["name", "company", "warehouse", "target_type", "target_id", "active_from", "last_evaluated_at", "expires_at", "manager_override_at"],
        limit_page_length=10_000,
    )
    now = now_datetime()
    created = 0
    for row in rows:
        identity = {
            "reason_code": "availability_hold_stale",
            "company": cstr(row.get("company")),
            "warehouse": cstr(row.get("warehouse")),
            "item": cstr(row.get("target_id")) if cstr(row.get("target_type")) == "Item" else None,
            "source_doctype": "FB Availability Hold",
            "source_name": cstr(row.get("name")),
        }
        override_at = get_datetime(row.get("manager_override_at")) if row.get("manager_override_at") else None
        if override_at and row.get("expires_at") and get_datetime(row.get("expires_at")) > now:
            resolve_inventory_exception(**identity)
            continue
        if not _automation_hold_is_stale(row, now=now):
            resolve_inventory_exception(**identity)
            continue
        upsert_inventory_exception(
            **identity,
            summary="An automation stock hold is overdue for a trustworthy stock check",
            next_action="Run a manager stock check and refresh the device overlay before restoring selling",
            severity="Warning",
        )
        created += 1
    return created


def warehouse_has_reliable_forecast(warehouse: str) -> bool:
    """Return whether preparation may add measured forecast demand.

    Availability holds deliberately do not call this helper: current recipe
    stock evidence is sufficient for pause/restore.  Batch preparation uses it
    only to decide whether to add forecast demand to its configured minimum.
    """

    if not warehouse or not frappe.db.exists("DocType", "FB Inventory Plan"):
        return False
    if frappe.db.exists(
        "FB Inventory Exception",
        {"warehouse": warehouse, "status": "Open", "severity": "Critical"},
    ):
        return False
    return bool(
        frappe.db.exists(
            "FB Inventory Plan",
            {
                "warehouse": warehouse,
                "forecast_state": "Reliable",
                "status": ("in", ["Ready", "Executed"]),
            },
        )
    )


def _automation_hold_is_stale(row: dict[str, Any], *, now: Any) -> bool:
    warehouse = cstr(row.get("warehouse")).strip()
    company = cstr(row.get("company")).strip()
    if not warehouse:
        return True
    max_age = frappe.db.get_value(
        "FB Inventory Policy",
        {"company": company, "warehouse": warehouse},
        "max_source_age_minutes",
    )
    try:
        minutes = int(max_age or 30)
    except (TypeError, ValueError):
        minutes = 30
    minutes = max(minutes, 1)
    evidence = row.get("last_evaluated_at") or row.get("active_from")
    if not evidence:
        return True
    try:
        return now - get_datetime(evidence) > timedelta(minutes=minutes)
    except (TypeError, ValueError):
        return True


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
