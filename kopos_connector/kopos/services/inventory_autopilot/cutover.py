"""Small, fail-closed checks for an outlet's inventory cutover action.

The API owns the Frappe reads and the policy mutation.  This module owns only
the repeatable prerequisite rules so they can be tested without a live site.
It deliberately does not activate automation or invent missing configuration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo


MONITOR_DESTINATION_KEYS = (
    "kopos_inventory_alert_destination",
    "kopos_inventory_monitor_destination",
    "monitor_destination",
    "alert_destination",
)

_SALES_QUEUE_FIELDS = (
    "inventory_sales_pending",
    "inventory_sales_syncing",
    "inventory_sales_failed",
    "inventory_sales_dead_letter",
)

_INVENTORY_QUEUE_FIELDS = (
    "inventory_commands_pending",
    "inventory_commands_syncing",
    "inventory_commands_failed",
    "inventory_commands_dead_letter",
)

BUSINESS_TIMEZONE = ZoneInfo("Asia/Kuala_Lumpur")


def opening_reconciliation_failure(
    reconciliation: Any,
    *,
    company: str,
    warehouse: str,
) -> str | None:
    """Return one actionable reason when opening stock is not submitted/safe."""

    if int(getattr(reconciliation, "docstatus", 0) or 0) != 1:
        return "opening_stock_reconciliation_not_submitted"
    if _text(getattr(reconciliation, "company", None)) != _text(company):
        return "opening_stock_reconciliation_company_mismatch"
    rows = list(getattr(reconciliation, "items", None) or ())
    if not rows:
        return "opening_stock_reconciliation_has_no_items"
    for row in rows:
        item = _text(getattr(row, "item_code", None) or getattr(row, "item", None))
        row_warehouse = _text(getattr(row, "warehouse", None))
        if not item or not row_warehouse:
            return "opening_stock_reconciliation_line_incomplete"
        if row_warehouse != _text(warehouse):
            return "opening_stock_reconciliation_warehouse_mismatch"
    return None


def device_activation_failures(
    rows: list[Mapping[str, Any]],
    *,
    max_source_age_minutes: int,
    now: datetime,
    overlay_is_current: Callable[[Mapping[str, Any]], bool],
) -> tuple[str, ...]:
    """Check all enabled selling devices without exposing financial state."""

    if not rows:
        return ("no_enabled_inventory_device",)
    failures: list[str] = []
    for row in rows:
        name = _text(row.get("name")) or "unknown"
        received = _datetime(row.get("inventory_report_received_at"))
        observed = _datetime(row.get("inventory_observed_at"))
        effective = min(
            (value for value in (received, observed) if value),
            key=_datetime_sort_key,
            default=None,
        )
        if effective is None:
            failures.append(f"device_report_missing:{name}")
        elif _age_minutes(effective, now) > max(1, int(max_source_age_minutes)):
            failures.append(f"device_report_stale:{name}")
        if _text(row.get("config_version")) != _text(row.get("inventory_config_version")):
            failures.append(f"device_config_not_current:{name}")
        if not _text(row.get("inventory_catalog_version")):
            failures.append(f"device_catalog_not_acknowledged:{name}")
        if not _text(row.get("inventory_overlay_version")) or not _text(row.get("inventory_overlay_hash")):
            failures.append(f"device_overlay_not_acknowledged:{name}")
        try:
            overlay_current = bool(overlay_is_current(row))
        except Exception:
            overlay_current = False
        if not overlay_current:
            failures.append(f"device_overlay_not_current:{name}")
        if any(int(row.get(fieldname) or 0) > 0 for fieldname in _SALES_QUEUE_FIELDS):
            failures.append(f"device_sales_queue_not_clean:{name}")
        if any(int(row.get(fieldname) or 0) > 0 for fieldname in _INVENTORY_QUEUE_FIELDS):
            failures.append(f"device_inventory_queue_not_clean:{name}")
    return tuple(dict.fromkeys(failures))


def monitoring_owner_failures(
    config: Mapping[str, Any] | Any,
    *,
    automation_identity_ready: bool,
    purchase_review_owner: str | None,
    require_monitor_destination: bool = True,
    require_automation_identity: bool = True,
    require_purchase_review_owner: bool = True,
) -> tuple[str, ...]:
    """Check configured owners for the capability being enabled.

    Core outlet cutover only records the identity and leaves the policy in
    ``Review First``.  Its caller can therefore inspect these prerequisites
    without blocking core inventory activation; Material Request and Draft PO
    gates pass the corresponding ``require_*`` flags when they are enabled.
    """

    failures: list[str] = []
    if require_monitor_destination and not any(_config_value(config, key) for key in MONITOR_DESTINATION_KEYS):
        failures.append("monitor_destination_not_configured")
    if require_automation_identity and not automation_identity_ready:
        failures.append("inventory_automation_user_not_configured")
    if require_purchase_review_owner and not _text(purchase_review_owner):
        failures.append("inventory_purchase_review_owner_not_configured")
    return tuple(failures)


def _config_value(config: Mapping[str, Any] | Any, key: str) -> str:
    if isinstance(config, Mapping):
        return _text(config.get(key))
    return _text(getattr(config, key, None))


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _age_minutes(value: datetime, now: datetime) -> int:
    value_utc = _as_utc_naive(value)
    now_utc = _as_utc_naive(now)
    return max(0, int((now_utc - value_utc).total_seconds() // 60))


def _as_utc_naive(value: datetime) -> datetime:
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=BUSINESS_TIMEZONE)
    return current.astimezone(timezone.utc).replace(tzinfo=None)


def _datetime_sort_key(value: datetime) -> float:
    """Compare ERP-naive and device-offset timestamps as the same instant."""

    return _as_utc_naive(value).replace(tzinfo=timezone.utc).timestamp()
