from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import cstr, now_datetime

from kopos_connector.kopos.services.inventory_autopilot.exceptions import (
    resolve_inventory_exception,
    upsert_inventory_exception,
)
from kopos_connector.kopos.services.inventory_autopilot.holds import (
    create_reliable_automation_holds,
    record_stale_automation_hold_exceptions,
    restore_automation_holds,
)


DEBOUNCE_SECONDS = 60
DEBOUNCE_KEY_PREFIX = "kopos:inventory-availability:v1"
HEALTH_MARKER_PREFIX = "kopos:inventory-autopilot:health:last_availability"
COMPARE_AND_DELETE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def on_stock_document_submit(document: Any, _method: str | None = None) -> None:
    """Schedule fixed warehouse rechecks without delaying a standard posting."""

    try:
        for warehouse in _affected_warehouses(document):
            schedule_warehouse_availability_recheck(warehouse)
    except Exception:
        frappe.log_error(
            title="Inventory availability scheduling failed",
            message="A submitted stock document could not schedule its warehouse availability refresh.",
        )


def schedule_warehouse_availability_recheck(warehouse: str) -> bool:
    resolved_warehouse = cstr(warehouse).strip()
    if not resolved_warehouse:
        return False
    redis_client = _redis_client()
    if redis_client is None:
        return False
    key = f"{DEBOUNCE_KEY_PREFIX}:{hashlib.sha256(resolved_warehouse.encode('utf-8')).hexdigest()}"
    token = uuid4().hex
    if not redis_client.set(key, token, ex=DEBOUNCE_SECONDS, nx=True):
        return False
    try:
        frappe.enqueue(
            "kopos_connector.kopos.services.inventory_autopilot.availability_events.reevaluate_warehouse_availability",
            queue="short",
            enqueue_after_commit=True,
            job_id=(
                "kopos-availability-"
                f"{hashlib.sha256(resolved_warehouse.encode('utf-8')).hexdigest()}-{token}"
            ),
            timeout=5 * 60,
            warehouse=resolved_warehouse,
        )
    except Exception:
        redis_client.eval(COMPARE_AND_DELETE_LUA, 1, key, token)
        raise
    return True


def reevaluate_warehouse_availability(warehouse: str) -> dict[str, int | str]:
    resolved_warehouse = cstr(warehouse).strip()
    if not resolved_warehouse:
        raise ValueError("Warehouse is required for availability re-evaluation")
    try:
        created = create_reliable_automation_holds(warehouse=resolved_warehouse)
        released = restore_automation_holds(warehouse=resolved_warehouse)
        stale = record_stale_automation_hold_exceptions(warehouse=resolved_warehouse)
        company = cstr(
            frappe.db.get_value(
                "FB Inventory Policy",
                {"warehouse": resolved_warehouse},
                "company",
            )
        ).strip()
        resolve_inventory_exception(
            reason_code="availability_recalculation_failed",
            company=company or None,
            warehouse=resolved_warehouse,
        )
        _set_availability_health_marker(resolved_warehouse)
        frappe.db.commit()
        return {
            "warehouse": resolved_warehouse,
            "created_holds": created,
            "released_holds": released,
            "stale_exceptions": stale,
        }
    except Exception:
        frappe.db.rollback()
        company = cstr(
            frappe.db.get_value(
                "FB Inventory Policy",
                {"warehouse": resolved_warehouse},
                "company",
            )
        ).strip()
        upsert_inventory_exception(
            reason_code="availability_recalculation_failed",
            summary="Stock availability could not be refreshed",
            next_action="Review the submitted stock document and retry the warehouse stock check",
            severity="Critical",
            company=company or None,
            warehouse=resolved_warehouse,
        )
        frappe.db.commit()
        raise


def recover_availability_hourly() -> dict[str, int]:
    """Recheck every configured warehouse once per hour.

    Stock-document events are the fast path; this bounded recovery pass is
    the safety net for missed events, Redis expiry, worker restarts, and
    devices that reconnect after a quiet period.  It reuses the same
    warehouse owner and 60-second debounce path rather than creating a second
    availability implementation.
    """

    if not frappe.db.exists("DocType", "FB Inventory Policy"):
        return {"warehouses": 0, "succeeded": 0, "failed": 0}
    rows = frappe.get_all(
        "FB Inventory Policy",
        filters={"warehouse": ["is", "set"]},
        fields=["warehouse"],
        order_by="warehouse asc",
        limit_page_length=10_000,
    )
    warehouses = sorted({cstr(row.get("warehouse")).strip() for row in rows if cstr(row.get("warehouse")).strip()})
    scheduled = failed = 0
    for warehouse in warehouses:
        try:
            if schedule_warehouse_availability_recheck(warehouse):
                scheduled += 1
            else:
                failed += 1
        except Exception:
            # Continue so one broken outlet cannot starve all other outlets'
            # hourly recovery.  The queue boundary records its own failure
            # diagnostics when enqueueing or Redis is unavailable.
            failed += 1
    return {"warehouses": len(warehouses), "scheduled": scheduled, "failed": failed}


def _set_availability_health_marker(warehouse: str) -> None:
    resolved = cstr(warehouse).strip()
    if not resolved:
        return
    setter = getattr(frappe.cache(), "set_value", None)
    if not callable(setter):
        return
    key = f"{HEALTH_MARKER_PREFIX}:{hashlib.sha256(resolved.encode('utf-8')).hexdigest()[:24]}"
    current = now_datetime()
    if getattr(current, "tzinfo", None) is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
    setter(key, current.isoformat(), expires_in_sec=7 * 24 * 60 * 60)


def _affected_warehouses(document: Any) -> list[str]:
    doctype = cstr(getattr(document, "doctype", None)).strip()
    if doctype == "Stock Entry" and cstr(getattr(document, "custom_fb_order", None)).strip():
        return []
    warehouses: set[str] = set()
    for row in list(getattr(document, "items", None) or []):
        fieldnames = (
            ("warehouse", "accepted_warehouse")
            if doctype == "Purchase Receipt"
            else ("s_warehouse", "t_warehouse")
            if doctype == "Stock Entry"
            else ("warehouse",)
        )
        for fieldname in fieldnames:
            value = cstr(getattr(row, fieldname, None)).strip()
            if value:
                warehouses.add(value)
    return sorted(warehouses)


def _redis_client() -> Any | None:
    cache = frappe.cache()
    redis_client = getattr(cache, "redis_client", None)
    if callable(redis_client):
        redis_client = redis_client()
    if redis_client is None or not hasattr(redis_client, "set") or not hasattr(redis_client, "eval"):
        return None
    return redis_client
