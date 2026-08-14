"""Manager-facing Inventory Autopilot read models and health checks."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime, now_datetime

from kopos_connector.api.devices import (
    lock_device_for_operational_mutation,
    require_device_context,
)
from kopos_connector.api.catalog import resolve_catalog_pos_profile
from kopos_connector.api.catalog import build_catalog_payload
from kopos_connector.kopos.services.inventory_autopilot.holds import (
    create_hold,
    release_hold,
)
from kopos_connector.kopos.services.inventory_autopilot.legacy_migration import (
    discover_legacy_values,
    migrate_legacy_values,
)
from kopos_connector.kopos.services.inventory_autopilot.exceptions import upsert_inventory_exception
from kopos_connector.kopos.services.inventory_autopilot.document_coordinator import (
    create_and_submit_material_request,
    create_draft_purchase_order,
    outbound_configuration_safe,
)
from kopos_connector.kopos.services.inventory_autopilot.replenishment import ReplenishmentLine


@frappe.whitelist(methods=["GET", "POST"])
def get_autopilot_health(warehouse: str | None = None) -> dict[str, Any]:
    """Return sanitized, warehouse-scoped operational health.

    This route is intentionally separate from device routes. It is a read-only
    monitor surface and never changes a policy, projection, hold, or document.
    """

    resolved_warehouse = cstr(warehouse).strip()
    if not resolved_warehouse:
        frappe.throw(_("Warehouse is required"), frappe.ValidationError)
    if not frappe.has_permission("FB Inventory Policy", ptype="read"):
        frappe.throw(_("Inventory health requires manager permission"), frappe.PermissionError)

    policy = _get_policy(resolved_warehouse)
    now = now_datetime()
    max_age_minutes = int(policy.get("max_source_age_minutes") or 30) if policy else 30
    projection_rows = frappe.db.sql(
        """
        SELECT pl.state, COUNT(*) AS count, MIN(pl.created_at) AS oldest_created_at
        FROM `tabFB Projection Log` pl
        INNER JOIN `tabFB Order` o ON o.name = pl.source_name
        WHERE pl.projection_type = 'Stock Issue'
          AND pl.source_doctype = 'FB Order'
          AND o.booth_warehouse = %s
        GROUP BY pl.state
        """,
        (resolved_warehouse,),
        as_dict=True,
    )
    projection_counts = {
        cstr(row.get("state")): int(row.get("count") or 0)
        for row in projection_rows
    }
    oldest_age_minutes = _age_minutes(
        min(
            (
                get_datetime(row.get("oldest_created_at"))
                for row in projection_rows
                if row.get("oldest_created_at")
            ),
            default=None,
        ),
        now,
    )
    critical_reasons: list[str] = []
    warning_reasons: list[str] = []
    if projection_counts.get("Dead Letter", 0) > 0:
        critical_reasons.append("inventory_projection_dead_letter")
    if oldest_age_minutes is not None and oldest_age_minutes > 60:
        critical_reasons.append("inventory_projection_backlog")
    elif oldest_age_minutes is not None and oldest_age_minutes > 15:
        warning_reasons.append("inventory_projection_delayed")
    duplicate_targets = frappe.db.sql(
        """
        SELECT pl.target_name
        FROM `tabFB Projection Log` pl
        INNER JOIN `tabFB Order` o ON o.name = pl.source_name
        WHERE pl.projection_type = 'Stock Issue'
          AND pl.source_doctype = 'FB Order'
          AND pl.state = 'Succeeded'
          AND pl.target_name IS NOT NULL
          AND pl.target_name != ''
          AND o.booth_warehouse = %s
        GROUP BY pl.target_name
        HAVING COUNT(*) > 1
        LIMIT 1
        """,
        (resolved_warehouse,),
    )
    if duplicate_targets:
        critical_reasons.append("inventory_projection_duplicate_target")
    exceptions = frappe.get_all(
        "FB Inventory Exception",
        filters={"warehouse": resolved_warehouse, "status": "Open", "severity": "Critical"},
        fields=["name", "reason_code", "summary", "last_seen"],
        order_by="last_seen desc",
        limit_page_length=50,
    )
    critical_reasons.extend(cstr(row.get("reason_code")) for row in exceptions)
    scheduler = _scheduler_health()
    scheduler_success = get_datetime(scheduler.get("last_success")) if scheduler.get("last_success") else None
    scheduler_age = _age_minutes(scheduler_success, now)
    if scheduler_age is None or scheduler_age > 90:
        critical_reasons.append("inventory_scheduler_overdue")
    elif scheduler_age > 60:
        warning_reasons.append("inventory_scheduler_delayed")
    device_rows = frappe.db.sql(
        """
        SELECT d.name, d.config_version, d.inventory_config_version, d.inventory_observed_at,
               d.inventory_report_received_at,
               d.inventory_overlay_version, d.inventory_overlay_hash,
               d.inventory_sales_pending, d.inventory_sales_syncing,
               d.inventory_sales_failed, d.inventory_sales_dead_letter,
               d.inventory_commands_pending, d.inventory_commands_failed
        FROM `tabKoPOS Device` d
        INNER JOIN `tabPOS Profile` p ON p.name = d.pos_profile
        WHERE d.enabled = 1 AND p.warehouse = %s
        """,
        (resolved_warehouse,),
        as_dict=True,
    )
    current_devices = dirty_devices = stale_devices = 0
    unacknowledged_overlays = 0
    unacknowledged_overlay_critical = 0
    for row in device_rows:
        received_at = get_datetime(row.get("inventory_report_received_at")) if row.get("inventory_report_received_at") else None
        observed_at = get_datetime(row.get("inventory_observed_at")) if row.get("inventory_observed_at") else None
        effective_at = min((value for value in (received_at, observed_at) if value is not None), default=None)
        is_stale = effective_at is None or _age_minutes(effective_at, now) > max_age_minutes
        is_dirty = any(int(row.get(key) or 0) > 0 for key in (
            "inventory_sales_pending", "inventory_sales_syncing", "inventory_sales_failed",
            "inventory_sales_dead_letter", "inventory_commands_pending", "inventory_commands_failed",
        ))
        overlay_current = _device_overlay_current(row)
        if not overlay_current:
            unacknowledged_overlays += 1
            if is_stale:
                unacknowledged_overlay_critical += 1
        if is_stale:
            stale_devices += 1
        elif is_dirty or not overlay_current:
            dirty_devices += 1
        else:
            current_devices += 1
    if unacknowledged_overlays:
        if unacknowledged_overlay_critical:
            critical_reasons.append("inventory_overlay_unacknowledged")
        else:
            warning_reasons.append("inventory_overlay_pending_acknowledgement")
    if stale_devices:
        warning_reasons.append("inventory_device_stale")
    return {
        "schema_version": "inventory-health-v1",
        "warehouse": resolved_warehouse,
        "as_of": _iso_with_offset(now),
        "automation_state": cstr(policy.get("automation_state")) if policy else "Review First",
        "max_source_age_minutes": max_age_minutes,
        "scheduler": scheduler,
        "projection": {
            "counts_by_state": projection_counts,
            "oldest_age_minutes": oldest_age_minutes,
        },
        "devices": {"current": current_devices, "dirty": dirty_devices, "stale": stale_devices},
        "overlay": {
            "acknowledged": unacknowledged_overlays == 0,
            "unacknowledged_devices": unacknowledged_overlays,
        },
        "exceptions": {
            "open_critical": len(exceptions),
            "critical_reasons": sorted(set(critical_reasons)),
            "warning_reasons": sorted(set(warning_reasons)),
        },
        "last_successful_availability_check": _health_marker("last_availability"),
        "last_successful_plan": _health_marker("last_plan"),
        "draft_purchase_order_safety": "safe" if outbound_configuration_safe()[0] else "unsafe",
    }


@frappe.whitelist(methods=["GET"])
def get_edge_snapshot(
    *,
    device_id: str,
    known_version: str | None = None,
    known_overlay_version: str | None = None,
) -> dict[str, Any]:
    """Return the authenticated device's read-only commercial and stock view.

    This is deliberately a thin adapter over the existing catalog owner. It
    does not create a second snapshot format or grant a device manager access.
    """

    require_device_context(device_id=device_id)
    payload = build_catalog_payload(
        device_id=device_id,
        known_version=known_version,
        known_overlay_version=known_overlay_version,
    )
    _set_health_marker("last_availability")
    return payload


@frappe.whitelist(methods=["GET"])
def get_count_task(*, device_id: str, task_id: str | None = None) -> dict[str, Any]:
    """Return the next assigned blind-count task without exposing expected stock."""

    device = require_device_context(device_id=device_id)
    profile = resolve_catalog_pos_profile(device_id=device_id) or {}
    assigned_users = sorted({
        user
        for user in (
            cstr(getattr(device, "api_user", None)).strip(),
            cstr(getattr(frappe.session, "user", None)).strip(),
            *[
                cstr(getattr(row, "user", None)).strip()
                for row in (getattr(device, "device_users", None) or [])
                if cint(getattr(row, "active", 0))
            ],
        )
        if user
    })
    filters: dict[str, Any] = {"warehouse": cstr(profile.get("warehouse")), "status": "Assigned"}
    if assigned_users:
        filters["assignee"] = ["in", assigned_users]
    if task_id:
        filters["name"] = cstr(task_id).strip()
    if not frappe.db.exists("DocType", "FB Inventory Count Task"):
        return {"status": "ok", "task": None}
    rows = frappe.get_all(
        "FB Inventory Count Task",
        filters=filters,
        fields=["name", "revision", "warehouse", "assignee", "stock_watermark"],
        limit_page_length=1,
    )
    if not rows:
        return {"status": "ok", "task": None}
    task = rows[0]
    task["lines"] = frappe.get_all(
        "FB Inventory Count Task Line",
        filters={"parent": task["name"]},
        fields=["item_id", "uom"],
        order_by="idx asc",
    )
    return {"status": "ok", "task": task}


@frappe.whitelist(methods=["GET", "POST"])
def preflight_legacy_inventory_values(
    *, company: str | None = None, warehouse: str | None = None, apply: bool = False
) -> dict[str, Any]:
    """Discover legacy availability values before outlet cutover.

    Discovery is read-only by default. Applying the explicit migration is a
    manager action and retains the source fields for the retention window.
    """

    if not frappe.has_permission("Item", ptype="read"):
        frappe.throw(_("Legacy inventory preflight requires Item read permission"), frappe.PermissionError)
    values = discover_legacy_values(company=cstr(company).strip() or None)
    if not cint(apply):
        return {"status": "dry_run", "values": values, "unknown_count": sum(1 for value in values if value["availability_mode"].strip().lower() not in {"", "auto", "force_available", "force_unavailable"})}
    resolved_company = cstr(company).strip()
    resolved_warehouse = cstr(warehouse).strip()
    if not resolved_company or not resolved_warehouse:
        frappe.throw(_("Company and warehouse are required to apply legacy migration"), frappe.ValidationError)
    return {"status": "applied", **migrate_legacy_values(company=resolved_company, warehouse=resolved_warehouse, dry_run=False)}


@frappe.whitelist(methods=["POST"])
def create_inventory_material_request(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    if not frappe.has_permission("Material Request", ptype="create"):
        frappe.throw(_("Material Request creation requires manager permission"), frappe.PermissionError)
    value = _parse_json_object(payload, "Material Request payload")
    lines = tuple(
        ReplenishmentLine(
            item=cstr(line.get("item")),
            warehouse=cstr(line.get("warehouse")),
            quantity=line.get("quantity"),
            reason=cstr(line.get("reason") or "inventory_plan"),
        )
        for line in value.get("lines", [])
        if isinstance(line, dict)
    )
    result = create_and_submit_material_request(
        company=cstr(value.get("company")),
        purpose=cstr(value.get("purpose") or "Purchase"),
        required_date=value.get("required_date"),
        lines=lines,
        gates=value.get("gates") if isinstance(value.get("gates"), dict) else {},
    )
    _set_health_marker("last_plan")
    return result


@frappe.whitelist(methods=["POST"])
def create_inventory_draft_purchase_order(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    if not frappe.has_permission("Purchase Order", ptype="create"):
        frappe.throw(_("Draft Purchase Order creation requires manager permission"), frappe.PermissionError)
    value = _parse_json_object(payload, "Draft Purchase Order payload")
    result = create_draft_purchase_order(
        company=cstr(value.get("company")),
        material_request=cstr(value.get("material_request")),
        quotation=cstr(value.get("quotation")),
        plan_hash=cstr(value.get("plan_hash")),
        policy_hash=cstr(value.get("policy_hash")),
        quotation_hash=cstr(value.get("quotation_hash")),
        gates=value.get("gates") if isinstance(value.get("gates"), dict) else {},
    )
    _set_health_marker("last_plan")
    return result


@frappe.whitelist(methods=["POST"])
def create_availability_hold(
    *,
    device_id: str,
    target_type: str,
    target_id: str,
    reason_code: str,
    reason_label: str,
) -> dict[str, Any]:
    device = require_device_context(device_id=device_id)
    with lock_device_for_operational_mutation(device_id=device_id):
        profile = resolve_catalog_pos_profile(device_id=device_id) or {}
        hold_name = create_hold(
            target_type=cstr(target_type).strip(),
            target_id=cstr(target_id).strip(),
            company=cstr(profile.get("company")),
            warehouse=cstr(profile.get("warehouse")),
            source="manual",
            reason_code=cstr(reason_code).strip(),
            reason_label=cstr(reason_label).strip(),
            actor=cstr(getattr(device, "api_user", None)).strip() or frappe.session.user,
            pos_profile=cstr(profile.get("name")),
        )
        frappe.db.commit()
    return {"status": "accepted", "hold_id": hold_name}


@frappe.whitelist(methods=["POST"])
def release_availability_hold(*, device_id: str, hold_id: str) -> dict[str, Any]:
    require_device_context(device_id=device_id)
    with lock_device_for_operational_mutation(device_id=device_id):
        name = release_hold(hold_id)
        frappe.db.commit()
    return {"status": "accepted", "hold_id": name}


@frappe.whitelist(methods=["POST"])
def submit_count_observation(*, device_id: str, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Persist one blind-count observation and at most one draft reconciliation."""

    device = require_device_context(device_id=device_id)
    value = _parse_count_payload(payload)
    profile = resolve_catalog_pos_profile(device_id=device_id) or {}
    warehouse = cstr(profile.get("warehouse")).strip()
    if not warehouse or warehouse != value["warehouse"]:
        frappe.throw(_("Count warehouse does not match the device warehouse"), frappe.ValidationError)
    observation_id = cstr(value["observation_id"]).strip()
    observation_name = "KOPOS-COUNT-" + hashlib.sha256(observation_id.encode("utf-8")).hexdigest()[:24]
    with lock_device_for_operational_mutation(device_id=device_id):
        observation_doctype_exists = frappe.db.exists("DocType", "FB Inventory Count Observation")
        observation_hash = _payload_hash(value)
        existing_observation = frappe.db.get_value(
            "FB Inventory Count Observation", {"observation_id": observation_id}, ["name", "status", "reconciliation", "payload_hash"]
        ) if observation_doctype_exists else None
        if existing_observation:
            existing_hash = cstr(existing_observation[3]).strip()
            if existing_hash and existing_hash != observation_hash:
                frappe.throw(_("Count observation ID was reused with different content"), frappe.ValidationError)
            return {"status": "replayed", "observation_id": observation_id, "reconciliation": cstr(existing_observation[2]) or None}
        _validate_count_assignment(device, value, warehouse)
        watermark_rows = frappe.db.sql(
            "SELECT MAX(modified) FROM `tabStock Ledger Entry` WHERE warehouse = %s",
            (warehouse,),
        )
        current_watermark = cstr(watermark_rows[0][0] if watermark_rows else "").strip()
        conflict = bool(current_watermark and current_watermark != cstr(value["stock_watermark"]).strip())
        observation_doc = None
        if observation_doctype_exists:
            observation_doc = frappe.new_doc("FB Inventory Count Observation")
            observation_doc.observation_id = observation_id
            observation_doc.payload_hash = observation_hash
            observation_doc.task_id = value["task_id"]
            observation_doc.task_revision = value["task_revision"]
            observation_doc.warehouse = warehouse
            observation_doc.actor_id = value["actor_id"]
            observation_doc.stock_watermark = value["stock_watermark"]
            observation_doc.observed_at = value["observed_at"]
            observation_doc.lines_json = json.dumps(value["lines"], sort_keys=True, separators=(",", ":"))
            observation_doc.status = "Conflict" if conflict else "Accepted"
            observation_doc.insert()
        if conflict:
            exception = upsert_inventory_exception(
                reason_code="inventory_count_stale_watermark",
                summary="A blind count was preserved but stock moved before review",
                next_action="Review the physical count and create a new assignment after the stock ledger is stable",
                severity="Warning",
                company=cstr(profile.get("company")),
                warehouse=warehouse,
                source_doctype="FB Inventory Count Observation",
                source_name=observation_id,
            )
            frappe.db.commit()
            return {"status": "conflict", "observation_id": observation_id, "exception": exception, "reconciliation": None}
        existing = frappe.db.exists("Stock Reconciliation", observation_name)
        if existing:
            return {"status": "replayed", "observation_id": observation_id, "reconciliation": existing}
        reconciliation = frappe.new_doc("Stock Reconciliation")
        reconciliation.name = observation_name
        reconciliation.company = cstr(profile.get("company"))
        reconciliation.purpose = "Stock Reconciliation"
        reconciliation.posting_date = get_datetime(value["observed_at"]).date()
        reconciliation.posting_time = get_datetime(value["observed_at"]).time()
        reconciliation.items = []
        for line in value["lines"]:
            reconciliation.append("items", {
                "item_code": cstr(line["item_id"]).strip(),
                "warehouse": warehouse,
                "qty": line["quantity"],
                "uom": cstr(line.get("uom") or "Nos").strip(),
            })
        reconciliation.insert()
        if observation_doc is not None:
            observation_doc.reconciliation = reconciliation.name
            observation_doc.save()
        frappe.db.commit()
    return {"status": "accepted", "observation_id": observation_id, "reconciliation": reconciliation.name}


@frappe.whitelist(methods=["POST"])
def report_device_inventory_state(*, device_id: str, payload: str | dict[str, Any]) -> dict[str, Any]:
    device = require_device_context(device_id=device_id)
    report = _parse_report_payload(payload)
    if cstr(report.get("device_id")).strip() != cstr(device_id).strip():
        frappe.throw(_("Inventory report device_id does not match the authenticated device"), frappe.ValidationError)
    revision = report["report_revision"]
    payload_hash = _payload_hash(report)
    with lock_device_for_operational_mutation(device_id=device_id):
        current_revision = cint(getattr(device, "inventory_report_revision", 0))
        current_hash = cstr(getattr(device, "inventory_report_payload_hash", None)).strip()
        if revision < current_revision:
            frappe.throw(_("Device inventory report revision regressed"), frappe.ValidationError)
        if revision == current_revision and current_hash and current_hash != payload_hash:
            frappe.throw(_("Device inventory report revision was reused with different content"), frappe.ValidationError)
        if revision == current_revision and current_hash == payload_hash:
            return {"status": "replayed", "device_id": device_id, "report_revision": revision}
        if cint(report["config_version"]) != cint(getattr(device, "config_version", 0)):
            frappe.throw(_("Device configuration changed; refresh before reporting inventory state"), frappe.ValidationError)
        device_values = {
            "inventory_report_schema_version": report["schema_version"],
            "inventory_report_revision": revision,
            "inventory_observed_at": report["observed_at"],
            "inventory_report_received_at": now_datetime(),
            "inventory_config_version": report["config_version"],
            "inventory_overlay_version": report["overlay_version"],
            "inventory_overlay_hash": report["overlay_hash"],
            "inventory_sales_pending": report["sales_outbox"]["pending"],
            "inventory_sales_syncing": report["sales_outbox"]["syncing"],
            "inventory_sales_failed": report["sales_outbox"]["failed"],
            "inventory_sales_dead_letter": report["sales_outbox"]["dead_letter"],
            "inventory_oldest_unsaved_sale_at": report["oldest_unsaved_sale_timestamp"],
            "inventory_commands_pending": report["inventory_outbox"]["pending"],
            "inventory_commands_failed": report["inventory_outbox"]["failed"],
            "inventory_report_payload_hash": payload_hash,
        }
        frappe.db.set_value("KoPOS Device", device.name, device_values, update_modified=False)
        frappe.db.commit()
    return {"status": "accepted", "device_id": device_id, "report_revision": revision}


def _parse_report_payload(payload: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as error:
            frappe.throw(_("Inventory report is not valid JSON"), frappe.ValidationError)
            raise AssertionError("frappe.throw must raise") from error
    else:
        value = payload
    if not isinstance(value, dict):
        frappe.throw(_("Inventory report must be an object"), frappe.ValidationError)
    required = {"schema_version", "device_id", "config_version", "report_revision", "observed_at", "overlay_version", "overlay_hash", "sales_outbox", "inventory_outbox", "oldest_unsaved_sale_timestamp"}
    if set(value) != required:
        frappe.throw(_("Inventory report fields are incomplete or unexpected"), frappe.ValidationError)
    if cstr(value.get("schema_version")) != "inventory-device-state-v1":
        frappe.throw(_("Unsupported inventory report schema version"), frappe.ValidationError)
    if isinstance(value["report_revision"], bool) or not isinstance(value["report_revision"], int) or value["report_revision"] < 1:
        frappe.throw(_("Inventory report revision must be a positive integer"), frappe.ValidationError)
    if isinstance(value["config_version"], bool) or not isinstance(value["config_version"], int) or value["config_version"] < 0:
        frappe.throw(_("Inventory report config_version must be a non-negative integer"), frappe.ValidationError)
    for fieldname in ("overlay_version", "overlay_hash"):
        if not isinstance(value[fieldname], str) or not value[fieldname].strip():
            frappe.throw(_("Inventory report {0} is required").format(fieldname), frappe.ValidationError)
    try:
        datetime.fromisoformat(cstr(value["observed_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        frappe.throw(_("Inventory report observed_at is invalid"), frappe.ValidationError)
    oldest_sale = value.get("oldest_unsaved_sale_timestamp")
    if oldest_sale is not None:
        try:
            datetime.fromisoformat(cstr(oldest_sale).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            frappe.throw(_("Inventory report oldest_unsaved_sale_timestamp is invalid"), frappe.ValidationError)
    expected_queue_keys = {
        "sales_outbox": {"pending", "syncing", "failed", "dead_letter"},
        "inventory_outbox": {"pending", "failed"},
    }
    for queue_name in ("sales_outbox", "inventory_outbox"):
        queue = value[queue_name]
        if not isinstance(queue, dict) or set(queue) != expected_queue_keys[queue_name]:
            frappe.throw(_("{0} outbox fields are invalid").format(queue_name), frappe.ValidationError)
        if any(not isinstance(queue[key], int) or isinstance(queue[key], bool) or queue[key] < 0 for key in expected_queue_keys[queue_name]):
            frappe.throw(_("{0} outbox counts are invalid").format(queue_name), frappe.ValidationError)
    if cstr(value.get("device_id")).strip() == "":
        frappe.throw(_("Inventory report device_id is required"), frappe.ValidationError)
    return value


def _parse_count_payload(payload: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as error:
            frappe.throw(_("Count observation is not valid JSON"), frappe.ValidationError)
            raise AssertionError("frappe.throw must raise") from error
    else:
        value = payload
    required = {"observation_id", "task_id", "task_revision", "warehouse", "actor_id", "stock_watermark", "observed_at", "lines"}
    if not isinstance(value, dict) or set(value) != required:
        frappe.throw(_("Count observation fields are incomplete or unexpected"), frappe.ValidationError)
    if isinstance(value["task_revision"], bool) or not isinstance(value["task_revision"], int) or value["task_revision"] < 1:
        frappe.throw(_("Count task revision must be positive"), frappe.ValidationError)
    if not isinstance(value["lines"], list) or not value["lines"]:
        frappe.throw(_("Count observation must contain lines"), frappe.ValidationError)
    for fieldname in ("observation_id", "task_id", "warehouse", "actor_id", "stock_watermark", "observed_at"):
        if not isinstance(value[fieldname], str) or not value[fieldname].strip():
            frappe.throw(_("Count observation {0} is required").format(fieldname), frappe.ValidationError)
    try:
        datetime.fromisoformat(cstr(value["observed_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        frappe.throw(_("Count observation observed_at is invalid"), frappe.ValidationError)
    for line in value["lines"]:
        if not isinstance(line, dict) or set(line) != {"item_id", "quantity", "uom"} or not cstr(line.get("item_id")).strip():
            frappe.throw(_("Count line is malformed"), frappe.ValidationError)
        if isinstance(line.get("quantity"), bool) or not isinstance(line.get("quantity"), (int, float, str)):
            frappe.throw(_("Count quantity is invalid"), frappe.ValidationError)
        try:
            quantity = Decimal(str(line["quantity"]))
        except (InvalidOperation, ValueError, TypeError):
            frappe.throw(_("Count quantity is invalid"), frappe.ValidationError)
            raise AssertionError("frappe.throw must raise")
        if not quantity.is_finite() or quantity < 0:
            frappe.throw(_("Count quantity is invalid"), frappe.ValidationError)
    return value


def _validate_count_assignment(device: Any, value: dict[str, Any], warehouse: str) -> None:
    if not frappe.db.exists("DocType", "FB Inventory Count Task"):
        frappe.throw(_("Inventory count tasks are not installed"), frappe.ValidationError)
    task = frappe.get_doc("FB Inventory Count Task", value["task_id"])
    if cstr(task.status).strip() != "Assigned":
        frappe.throw(_("This inventory count is no longer assigned"), frappe.ValidationError)
    if cint(task.revision) != value["task_revision"] or cstr(task.warehouse).strip() != warehouse:
        frappe.throw(_("Inventory count assignment revision or warehouse is stale"), frappe.ValidationError)
    allowed_actors = {
        cstr(getattr(device, "api_user", None)).strip(),
        cstr(getattr(frappe.session, "user", None)).strip(),
        *{
            cstr(getattr(row, "user", None)).strip()
            for row in (getattr(device, "device_users", None) or [])
            if cint(getattr(row, "active", 0))
        },
    }
    if cstr(task.assignee).strip() not in allowed_actors or cstr(value["actor_id"]).strip() not in allowed_actors:
        frappe.throw(_("Inventory count actor is not the assigned user"), frappe.PermissionError)
    expected_lines = {
        (cstr(line.item_id).strip(), cstr(line.uom).strip())
        for line in (task.lines or [])
    }
    observed_lines = [
        (cstr(line.get("item_id")).strip(), cstr(line.get("uom") or "Nos").strip())
        for line in value["lines"]
    ]
    if len(observed_lines) != len(set(observed_lines)) or set(observed_lines) != expected_lines:
        frappe.throw(_("Inventory count lines do not match the assigned task"), frappe.ValidationError)


def _parse_json_object(payload: str | dict[str, Any], label: str) -> dict[str, Any]:
    value: Any = payload
    if isinstance(payload, str):
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as error:
            frappe.throw(_("{0} is not valid JSON").format(label), frappe.ValidationError)
            raise AssertionError("frappe.throw must raise") from error
    if not isinstance(value, dict):
        frappe.throw(_("{0} must be an object").format(label), frappe.ValidationError)
    return value


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _get_policy(warehouse: str) -> dict[str, Any] | None:
    names = frappe.get_all(
        "FB Inventory Policy",
        filters={"warehouse": warehouse},
        fields=["name", "automation_state", "max_source_age_minutes"],
        limit_page_length=1,
    )
    return names[0] if names else None


def _age_minutes(value: Any, now: Any) -> int | None:
    if value is None:
        return None
    return max(0, int((now - value).total_seconds() // 60))


def _scheduler_health() -> dict[str, Any]:
    cache = frappe.cache()
    getter = getattr(cache, "get_value", None)
    values = {}
    for marker in ("last_start", "last_success", "last_failure"):
        values[marker] = getter(f"kopos:inventory-autopilot:scheduler:{marker}") if callable(getter) else None
    return {"expected": "hourly", **values}


def _device_overlay_current(row: dict[str, Any]) -> bool:
    """Compare the device acknowledgement with the current profile overlay."""

    device_name = cstr(row.get("name")).strip()
    acknowledged_version = cstr(row.get("inventory_overlay_version")).strip()
    acknowledged_hash = cstr(row.get("inventory_overlay_hash")).strip()
    if not device_name or not acknowledged_version or not acknowledged_hash:
        return False
    try:
        payload = build_catalog_payload(device_id=device_name)
        overlay = payload.get("inventory_overlay")
        current_version = cstr(overlay.get("version")) if isinstance(overlay, dict) else ""
        return bool(current_version) and current_version == acknowledged_version == acknowledged_hash
    except Exception:
        # Health must remain a sanitized read model. A catalog failure is an
        # unacknowledged overlay, not an API traceback or a false green state.
        return False


def _set_health_marker(marker: str) -> None:
    cache = frappe.cache()
    setter = getattr(cache, "set_value", None)
    if callable(setter):
        setter(f"kopos:inventory-autopilot:health:{marker}", _iso_with_offset(now_datetime()), expires_in_sec=7 * 24 * 60 * 60)


def _health_marker(marker: str) -> str | None:
    cache = frappe.cache()
    getter = getattr(cache, "get_value", None)
    if not callable(getter):
        return None
    value = getter(f"kopos:inventory-autopilot:health:{marker}")
    return cstr(value).strip() or None


def _iso_with_offset(value: Any) -> str:
    current = value
    if getattr(current, "tzinfo", None) is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
    return current.isoformat()
