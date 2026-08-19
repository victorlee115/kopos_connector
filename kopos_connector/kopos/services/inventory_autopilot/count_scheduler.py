"""Small, director-configured blind-count scheduler.

The minimum cadence lives on the standard Item Group through two idempotently
installed custom fields.  This service only creates ``FB Inventory Count
Task`` records after a policy has a completed cutover and opening
reconciliation.  The task and its lines are the durable assignment authority;
the tablet only claims and records the physical observation.

Blank or invalid cadence is deliberately not interpreted.  Explicit open
inventory evidence may increase a configured cadence to Daily, but it can
never turn Off on or lower a director's minimum.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from kopos_connector.kopos.services.inventory_autopilot.exceptions import (
    upsert_inventory_exception,
)


BUSINESS_TIMEZONE = ZoneInfo("Asia/Kuala_Lumpur")
COUNT_FREQUENCIES = frozenset({"Daily", "Selected Weekdays", "Weekly", "Off"})
OPEN_TASK_STATUSES = frozenset({"Assigned", "Claimed", "In Progress", "Submitted", "Review"})

# These are explicit, durable exception signals.  The scheduler does not
# infer high use, short life, or batch problems from stock quantities; another
# service must record one of these reason codes first.
CADENCE_ESCALATION_REASONS = frozenset(
    {
        "inventory_count_director_review",
        "inventory_count_stale_watermark",
        "inventory_count_variance",
        "inventory_high_use",
        "inventory_short_life",
        "inventory_projection_failed",
        "inventory_projection_dead_letter",
        "inventory_projection_log_failed",
        "inventory_batch_issue",
        "batch_preparation_configuration",
        "batch_preparation_review_first",
    }
)

_DAY_NAMES = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def normalize_weekdays(value: Any) -> tuple[int, ...]:
    """Return sorted ISO weekday indexes, or an empty tuple for invalid input."""

    raw = cstr(value).strip()
    if not raw:
        return ()
    tokens = [token.strip().lower() for token in raw.split(",")]
    if not tokens or any(not token or token not in _DAY_NAMES for token in tokens):
        return ()
    return tuple(sorted(set(_DAY_NAMES[token] for token in tokens)))


def effective_count_frequency(
    configured: Any,
    evidence_reason_codes: Iterable[Any] = (),
) -> str | None:
    """Apply explicit escalation without weakening the configured minimum."""

    frequency = cstr(configured).strip()
    if frequency not in COUNT_FREQUENCIES:
        return None
    if frequency == "Off":
        return "Off"
    evidence = {
        cstr(reason).strip()
        for reason in evidence_reason_codes
        if cstr(reason).strip()
    }
    if evidence.intersection(CADENCE_ESCALATION_REASONS):
        return "Daily"
    return frequency


def business_local_datetime(value: datetime | date | None = None) -> datetime:
    """Normalize a scheduler timestamp to the protected business timezone."""

    candidate: datetime
    if value is None:
        candidate = now_datetime()
    elif isinstance(value, datetime):
        candidate = value
    elif isinstance(value, date):
        candidate = datetime.combine(value, datetime.min.time())
    else:
        raise TypeError("count scheduler time must be a date or datetime")
    if candidate.tzinfo is None:
        return candidate.replace(tzinfo=BUSINESS_TIMEZONE)
    return candidate.astimezone(BUSINESS_TIMEZONE)


def schedule_period_for(
    frequency: Any,
    weekdays: Any,
    value: datetime | date | None = None,
) -> str | None:
    """Return the due period key for a valid cadence at ``value``."""

    local = business_local_datetime(value)
    normalized = normalize_weekdays(weekdays)
    selected = cstr(frequency).strip()
    if selected == "Daily":
        return local.date().isoformat()
    if selected == "Selected Weekdays":
        return local.date().isoformat() if local.weekday() in normalized else None
    if selected == "Weekly":
        if len(normalized) != 1 or local.weekday() != normalized[0]:
            return None
        iso_year, iso_week, _iso_weekday = local.isocalendar()
        return f"{iso_year:04d}-W{iso_week:02d}"
    return None


def _schedule_is_valid(frequency: str, weekdays: Any) -> bool:
    normalized = normalize_weekdays(weekdays)
    if frequency == "Daily" or frequency == "Off":
        return True
    if frequency == "Selected Weekdays":
        return bool(normalized)
    if frequency == "Weekly":
        return len(normalized) == 1
    return False


def schedule_inventory_count_tasks(
    *,
    at: datetime | date | None = None,
) -> list[dict[str, Any]]:
    """Create at most one scheduled count task per due group and period."""

    if not frappe.db.exists("DocType", "FB Inventory Policy"):
        return []
    task_meta = frappe.get_meta("FB Inventory Count Task")
    if not task_meta.has_field("schedule_key") or not task_meta.has_field("stock_group"):
        return []

    policies = frappe.get_all(
        "FB Inventory Policy",
        filters={"automation_state": ["in", ["Review First", "Active"]]},
        fields=[
            "name",
            "company",
            "warehouse",
            "automation_state",
            "cutover_token",
            "cutover_at",
            "opening_stock_reconciliation",
        ],
        limit_page_length=10_000,
    )
    results: list[dict[str, Any]] = []
    created = False
    for policy in policies:
        try:
            policy_results = _schedule_for_policy(
                policy,
                at=at,
            )
            results.extend(policy_results)
            created = created or any(result.get("status") == "created" for result in policy_results)
        except Exception as error:
            exception = upsert_inventory_exception(
                reason_code="inventory_count_scheduler_failure",
                summary="Inventory count scheduling could not complete safely",
                next_action=f"Review the count schedule and scheduler error: {cstr(error)}",
                severity="Critical",
                company=cstr(policy.get("company")),
                warehouse=cstr(policy.get("warehouse")),
                source_doctype="FB Inventory Policy",
                source_name=cstr(policy.get("name")),
            )
            results.append(
                {
                    "status": "failed",
                    "policy": cstr(policy.get("name")),
                    "exception": exception,
                }
            )
    if created:
        frappe.db.commit()
    return results


def _schedule_for_policy(
    policy: dict[str, Any],
    *,
    at: datetime | date | None,
) -> list[dict[str, Any]]:
    company = cstr(policy.get("company")).strip()
    warehouse = cstr(policy.get("warehouse")).strip()
    policy_name = cstr(policy.get("name")).strip()
    if not company or not warehouse:
        return [_blocked(policy, "inventory_count_configuration", "The inventory policy has no company or warehouse")]
    if not cstr(policy.get("cutover_token")).strip() or not policy.get("cutover_at"):
        return [_blocked(policy, "inventory_count_before_cutover", "Complete the outlet cutover before scheduling routine counts")]
    opening = cstr(policy.get("opening_stock_reconciliation")).strip()
    if not opening or frappe.db.get_value("Stock Reconciliation", opening, "docstatus") != 1:
        return [_blocked(policy, "inventory_count_opening_reconciliation", "Submit the opening Stock Reconciliation before scheduling routine counts")]

    group_meta = frappe.get_meta("Item Group")
    required_group_fields = {"custom_kopos_count_frequency", "custom_kopos_count_weekdays"}
    if not required_group_fields.issubset({field.fieldname for field in group_meta.fields}):
        return [_blocked(policy, "inventory_count_configuration", "Install the director-configured Item Group count schedule fields")]
    item_meta = frappe.get_meta("Item")
    required_item_fields = {
        "item_group", "disabled", "is_stock_item", "stock_uom", "purchase_uom",
        "custom_fb_inventory_excluded",
    }
    if not required_item_fields.issubset({field.fieldname for field in item_meta.fields}):
        return [_blocked(policy, "inventory_count_configuration", "Complete the active Item inventory fields before scheduling counts")]

    groups = frappe.get_all(
        "Item Group",
        # Read every group so a blank or malformed cadence on an active stock
        # group becomes an explicit configuration result instead of being
        # silently omitted by the scheduler query.
        filters={},
        fields=["name", "custom_kopos_count_frequency", "custom_kopos_count_weekdays"],
        limit_page_length=10_000,
    )
    results: list[dict[str, Any]] = []
    for group in groups:
        results.append(
            _schedule_for_group(
                policy=policy,
                group=group,
                at=at,
            )
        )
    return results


def _schedule_for_group(
    *,
    policy: dict[str, Any],
    group: dict[str, Any],
    at: datetime | date | None,
) -> dict[str, Any]:
    company = cstr(policy.get("company")).strip()
    warehouse = cstr(policy.get("warehouse")).strip()
    group_name = cstr(group.get("name")).strip()
    configured = cstr(group.get("custom_kopos_count_frequency")).strip()
    weekdays = group.get("custom_kopos_count_weekdays")
    items = _active_items(group_name)
    if not items:
        return {"status": "no_active_items", "company": company, "warehouse": warehouse, "stock_group": group_name}
    if configured == "Off":
        return {"status": "off", "company": company, "warehouse": warehouse, "stock_group": group_name}
    if not configured:
        return _blocked(
            policy,
            "inventory_count_schedule_unconfigured",
            f"Choose Daily, Selected Weekdays, Weekly, or Off for the {group_name} stock group",
            item_group=group_name,
        )
    if not _schedule_is_valid(configured, weekdays):
        return _blocked(
            policy,
            "inventory_count_schedule_unconfigured",
            f"Configure valid weekdays for the {group_name} count schedule; JiJi will not guess one",
            item_group=group_name,
        )

    count_authority_errors: list[str] = []
    for item in items:
        error = _count_uom_authority_error(item)
        if error:
            count_authority_errors.append(error)
    if count_authority_errors:
        return _blocked(
            policy,
            "inventory_count_missing_uom_conversion",
            f"Complete the exact purchase-unit conversion for {group_name} before assigning a count: "
            + "; ".join(count_authority_errors[:3]),
            item_group=group_name,
        )

    evidence = _cadence_evidence(warehouse=warehouse, item_names=[cstr(item.get("name")) for item in items])
    effective = effective_count_frequency(configured, evidence)
    period = schedule_period_for(effective, weekdays, at)
    if not effective or not period:
        return {
            "status": "not_due",
            "company": company,
            "warehouse": warehouse,
            "stock_group": group_name,
            "frequency": effective or configured,
        }
    watermark = _stock_ledger_watermark(warehouse)
    if not watermark:
        return _blocked(
            policy,
            "inventory_count_stock_watermark_missing",
            f"Wait for a stock-ledger entry before assigning the {group_name} count",
            item_group=group_name,
        )

    schedule_key = _schedule_key(
        company=company,
        warehouse=warehouse,
        stock_group=group_name,
        frequency=effective,
        period=period,
    )
    existing = _task_by_schedule_key(schedule_key)
    if existing:
        return {
            "status": "existing",
            "task": existing,
            "company": company,
            "warehouse": warehouse,
            "stock_group": group_name,
            "frequency": effective,
            "period": period,
        }
    if evidence and _open_task_for_group(company=company, warehouse=warehouse, stock_group=group_name):
        return {
            "status": "existing_open_group_task",
            "company": company,
            "warehouse": warehouse,
            "stock_group": group_name,
            "frequency": effective,
            "period": period,
        }
    try:
        task = _create_count_task(
            company=company,
            warehouse=warehouse,
            stock_group=group_name,
            frequency=effective,
            period=period,
            schedule_key=schedule_key,
            watermark=watermark,
            items=items,
        )
    except Exception as error:
        existing = _task_by_schedule_key(schedule_key)
        if existing:
            return {
                "status": "existing",
                "task": existing,
                "company": company,
                "warehouse": warehouse,
                "stock_group": group_name,
                "frequency": effective,
                "period": period,
            }
        return _blocked(
            policy,
            "inventory_count_scheduler_failure",
            f"Create the {group_name} count task after reviewing the scheduler error: {cstr(error)}",
            item_group=group_name,
        )
    return {
        "status": "created",
        "task": task,
        "company": company,
        "warehouse": warehouse,
        "stock_group": group_name,
        "frequency": effective,
        "period": period,
        "item_count": len(items),
    }


def _active_items(group_name: str) -> list[dict[str, Any]]:
    return frappe.get_all(
        "Item",
        filters={
            "item_group": group_name,
            "disabled": 0,
            "is_stock_item": 1,
            "custom_fb_inventory_excluded": 0,
        },
        fields=["name", "stock_uom", "purchase_uom"],
        order_by="name asc",
        limit_page_length=10_000,
    )


def _count_uom_authority_error(item: dict[str, Any]) -> str | None:
    """Return a plain-language reason when a count cannot be entered safely.

    A pack count is only useful when ERPNext has a saved purchase-unit
    conversion.  The task stores the resolved value once it passes this
    check; the tablet never guesses a pack size or reads a current Item value
    after assignment.
    """

    item_code = cstr(item.get("name")).strip() or "(unknown Item)"
    stock_uom = cstr(item.get("stock_uom")).strip()
    purchase_uom = cstr(item.get("purchase_uom")).strip()
    if not stock_uom:
        return f"{item_code} has no stock UOM"
    if not purchase_uom:
        return f"{item_code} has no purchase UOM"
    if purchase_uom == stock_uom:
        # The standard Item authority is exact when both units are the same;
        # there is no alternate pack to infer in this case.
        item["conversion_factor"] = "1"
        item["stock_uom"] = stock_uom
        item["purchase_uom"] = purchase_uom
        return None
    raw_factor = frappe.db.get_value(
        "UOM Conversion Detail",
        {"parent": item_code, "parenttype": "Item", "uom": purchase_uom},
        "conversion_factor",
    )
    try:
        factor = Decimal(str(raw_factor))
    except (InvalidOperation, TypeError, ValueError):
        factor = Decimal("0")
    if not factor.is_finite() or factor <= 0:
        return f"{item_code} has no positive {purchase_uom} to {stock_uom} conversion"
    normalized = format(factor.normalize(), "f") if factor else "0"
    item["conversion_factor"] = normalized
    item["stock_uom"] = stock_uom
    item["purchase_uom"] = purchase_uom
    return None


def _cadence_evidence(*, warehouse: str, item_names: Iterable[str]) -> tuple[str, ...]:
    if not frappe.db.exists("DocType", "FB Inventory Exception"):
        return ()
    item_set = {cstr(item).strip() for item in item_names if cstr(item).strip()}
    rows = frappe.get_all(
        "FB Inventory Exception",
        filters={"warehouse": warehouse, "status": "Open"},
        fields=["item", "reason_code"],
        limit_page_length=10_000,
    )
    reasons: set[str] = set()
    for row in rows:
        item = cstr(row.get("item")).strip()
        reason = cstr(row.get("reason_code")).strip()
        if reason in CADENCE_ESCALATION_REASONS and (not item or item in item_set):
            reasons.add(reason)
    return tuple(sorted(reasons))


def _stock_ledger_watermark(warehouse: str) -> str:
    rows = frappe.db.sql(
        "SELECT MAX(modified) FROM `tabStock Ledger Entry` WHERE warehouse = %s",
        (warehouse,),
    )
    return cstr(rows[0][0] if rows else "").strip()


def _schedule_key(*, company: str, warehouse: str, stock_group: str, frequency: str, period: str) -> str:
    raw = "\x1f".join((company, warehouse, stock_group, frequency, period))
    return "IC-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _task_by_schedule_key(schedule_key: str) -> str | None:
    return cstr(
        frappe.db.get_value("FB Inventory Count Task", {"schedule_key": schedule_key}, "name")
    ).strip() or None


def _open_task_for_group(*, company: str, warehouse: str, stock_group: str) -> str | None:
    rows = frappe.get_all(
        "FB Inventory Count Task",
        filters={
            "company": company,
            "warehouse": warehouse,
            "stock_group": stock_group,
            "status": ["in", list(OPEN_TASK_STATUSES)],
        },
        fields=["name"],
        limit_page_length=1,
    )
    return cstr(rows[0].get("name")).strip() if rows else None


def _create_count_task(
    *,
    company: str,
    warehouse: str,
    stock_group: str,
    frequency: str,
    period: str,
    schedule_key: str,
    watermark: str,
    items: list[dict[str, Any]],
) -> str:
    document = frappe.new_doc("FB Inventory Count Task")
    document.company = company
    document.warehouse = warehouse
    document.stock_group = stock_group
    document.schedule_frequency = frequency
    document.schedule_period = period
    document.schedule_key = schedule_key
    document.revision = 1
    document.stock_watermark = watermark
    document.status = "Assigned"
    for item in items:
        document.append(
            "lines",
            {
                "item_id": cstr(item.get("name")).strip(),
                # ``uom`` remains the loose/stock UOM compatibility field;
                # the explicit fields below are the frozen count authority.
                "uom": cstr(item.get("stock_uom")).strip(),
                "stock_uom": cstr(item.get("stock_uom")).strip(),
                "purchase_uom": cstr(item.get("purchase_uom")).strip(),
                "conversion_factor": cstr(item.get("conversion_factor")).strip(),
            },
        )
    document.insert(ignore_permissions=True)
    return cstr(document.name).strip()


def _blocked(
    policy: dict[str, Any],
    reason_code: str,
    next_action: str,
    *,
    item_group: str | None = None,
) -> dict[str, Any]:
    exception = upsert_inventory_exception(
        reason_code=reason_code,
        summary="Inventory count scheduling is waiting for required evidence",
        next_action=next_action,
        severity="Warning",
        company=cstr(policy.get("company")),
        warehouse=cstr(policy.get("warehouse")),
        source_doctype="Item Group" if item_group else "FB Inventory Policy",
        source_name=item_group or cstr(policy.get("name")),
    )
    return {
        "status": "blocked",
        "reason_code": reason_code,
        "exception": exception,
        "company": cstr(policy.get("company")),
        "warehouse": cstr(policy.get("warehouse")),
        "stock_group": item_group,
    }
