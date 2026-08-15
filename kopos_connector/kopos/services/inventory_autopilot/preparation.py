"""Small, standard-document-backed batch preparation scheduler.

Prepared components are configured on the standard ERPNext BOM.  When usable
stock falls below the configured ready level, this service derives a bounded
alert for the assigned outlet device.  It deliberately does not create a
Work Order from the scheduler.  A staff member accepts the alert, at which
point the guided API validates the current BOM, threshold, hold, shelf-life,
and stock evidence and creates exactly one standard Draft Work Order.  Work
Orders and Manufacture Stock Entries remain the only authorities for the
physical operation.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import frappe
from frappe.utils import cstr

from kopos_connector.kopos.services.inventory_autopilot.exceptions import (
    upsert_inventory_exception,
)
from kopos_connector.kopos.services.inventory_autopilot.holds import (
    active_holds,
    warehouse_has_reliable_forecast,
)


def schedule_preparation_tasks() -> list[dict[str, Any]]:
    """Derive safe preparation alerts for explicitly enabled BOMs.

    This hook is intentionally read/exception-only.  It never creates a Work
    Order.  Work Order creation belongs to the atomic ``accept_preparation``
    command after the outlet confirms that the current evidence still
    matches the alert.
    """

    if not frappe.db.exists("DocType", "BOM") or not frappe.db.exists("DocType", "Work Order"):
        return []
    bom_meta = frappe.get_meta("BOM")
    required_fields = {
        "custom_kopos_autoprep_enabled",
        "custom_kopos_batch_qty",
        "custom_kopos_min_ready_qty",
        "custom_kopos_preparation_lead_minutes",
    }
    if (
        not required_fields.issubset({field.fieldname for field in bom_meta.fields})
        or not frappe.get_meta("Work Order").has_field("custom_kopos_preparation_fingerprint")
    ):
        # An upgrade that has not installed the optional fields must not
        # create a guessed batch.  The normal migration/preflight will surface
        # the missing field set.
        return []

    policies = frappe.get_all(
        "FB Inventory Policy",
        filters={"automation_state": ["in", ["Review First", "Active"]]},
        fields=["name", "company", "warehouse", "automation_state", "cutover_at", "cutover_token"],
        limit_page_length=10_000,
    )
    results: list[dict[str, Any]] = []
    for policy in policies:
        results.extend(_schedule_for_policy(policy))
    if results:
        frappe.db.commit()
    return results


def derived_preparation_alerts(*, company: str, warehouse: str) -> list[dict[str, Any]]:
    """Return current preparation alerts for one authenticated outlet.

    This is the device read-model entry point.  It does not write exceptions
    or Work Orders, and therefore is safe to call from a catalog/edge request.
    Configuration and operational failures simply produce no actionable alert;
    the existing manager exception/health surfaces remain responsible for
    explaining those failures.
    """

    resolved_company = cstr(company).strip()
    resolved_warehouse = cstr(warehouse).strip()
    if not resolved_company or not resolved_warehouse:
        return []
    if not frappe.db.exists("DocType", "BOM") or not frappe.db.exists("DocType", "Work Order"):
        return []
    policy = frappe.db.get_value(
        "FB Inventory Policy",
        {"company": resolved_company, "warehouse": resolved_warehouse},
        ["name", "company", "warehouse", "automation_state", "cutover_at", "cutover_token"],
        as_dict=True,
    )
    if not policy:
        return []
    return _preparation_alerts_for_policy(policy, record_exceptions=False)


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


def preparation_trigger_level(*, minimum_ready_qty: Any, daily_demand: Any | None, lead_minutes: Any) -> Decimal:
    """Keep the configured ready level plus measured lead-time demand."""

    minimum_ready = _decimal(minimum_ready_qty)
    if daily_demand in (None, ""):
        return minimum_ready
    demand = _decimal(daily_demand)
    try:
        lead = Decimal(str(lead_minutes if lead_minutes not in (None, "") else "0"))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("preparation lead must be a finite decimal") from error
    if not lead.is_finite() or lead < 0:
        raise ValueError("preparation lead must be a non-negative finite decimal")
    return minimum_ready + (demand * lead / Decimal("1440"))


def preparation_variance_preflight(*, company: str, warehouse: str) -> tuple[str, ...]:
    """Require an explicit variance authority for every enabled prepared BOM.

    There is intentionally no default tolerance.  A director may set the
    ceiling on the BOM, or once for the outlet policy; a missing or malformed
    value blocks cutover before a batch can post unexplained waste.
    """

    if not frappe.db.exists("DocType", "BOM"):
        return ()
    bom_meta = frappe.get_meta("BOM")
    if not bom_meta.has_field("custom_kopos_autoprep_enabled"):
        return ("batch_preparation_configuration_fields_missing",)
    bom_variance_field = "custom_kopos_preparation_variance_percent"
    policy_variance_field = "preparation_variance_percent_ceiling"
    has_bom_variance = bom_meta.has_field(bom_variance_field)
    has_policy_variance = bool(
        frappe.db.exists("DocType", "FB Inventory Policy")
        and frappe.get_meta("FB Inventory Policy").has_field(policy_variance_field)
    )
    bom_fields = [
        "name",
        "item",
        "quantity",
        "modified",
        "custom_kopos_batch_qty",
        "custom_kopos_min_ready_qty",
        "custom_kopos_preparation_lead_minutes",
    ]
    if frappe.get_meta("BOM").has_field("item_name"):
        bom_fields.append("item_name")
    if frappe.get_meta("BOM").has_field("custom_kopos_preparation_instructions"):
        bom_fields.append("custom_kopos_preparation_instructions")
    rows = frappe.get_all(
        "BOM",
        filters={
            "company": company,
            "docstatus": 1,
            "is_active": 1,
            "custom_kopos_autoprep_enabled": 1,
        },
        fields=["name", "item"] + ([bom_variance_field] if has_bom_variance else []),
        limit_page_length=10_000,
    )
    if not rows:
        return ()
    policy_value = None
    if has_policy_variance:
        policy_value = frappe.db.get_value(
            "FB Inventory Policy",
            {"company": company, "warehouse": warehouse},
            policy_variance_field,
        )
    failures: list[str] = []
    for row in rows:
        bom_name = cstr(row.get("name")).strip()
        try:
            bom_value = row.get(bom_variance_field) if has_bom_variance else None
            _select_preparation_variance_ceiling(
                bom_value=bom_value,
                policy_value=policy_value,
                has_bom_field=has_bom_variance,
                has_policy_field=has_policy_variance,
            )
        except ValueError as error:
            failures.append(f"batch_preparation_variance_threshold_invalid:{bom_name}:{error}")
    return tuple(failures)


def record_preparation_variance(
    *, order: Any, stock_entry: Any, actual_yield: Any, waste_qty: Any = None
) -> str | None:
    """Open one warning when a posted batch exceeds its configured ceiling."""

    bom_name = cstr(getattr(order, "bom_no", None)).strip()
    company = cstr(getattr(order, "company", None)).strip()
    warehouse = cstr(getattr(order, "fg_warehouse", None)).strip()
    if not bom_name or not company or not warehouse:
        return None
    bom_value = None
    bom_meta = frappe.get_meta("BOM")
    bom_field = "custom_kopos_preparation_variance_percent"
    if bom_meta.has_field(bom_field):
        bom_value = frappe.db.get_value("BOM", bom_name, bom_field)
    policy_value = None
    has_policy_field = False
    if frappe.db.exists("DocType", "FB Inventory Policy"):
        policy_meta = frappe.get_meta("FB Inventory Policy")
        policy_field = "preparation_variance_percent_ceiling"
        has_policy_field = policy_meta.has_field(policy_field)
        if has_policy_field:
            policy_value = frappe.db.get_value(
                "FB Inventory Policy",
                {"company": company, "warehouse": warehouse},
                policy_field,
            )
    try:
        threshold = _select_preparation_variance_ceiling(
            bom_value=bom_value,
            policy_value=policy_value,
            has_bom_field=bom_meta.has_field(bom_field),
            has_policy_field=has_policy_field,
        )
    except ValueError:
        # Cutover preflight is the authority for requiring a configured
        # ceiling.  A legacy order that predates that gate must not invent a
        # tolerance during completion.
        return None
    if threshold is None:
        return None
    target = _decimal(getattr(order, "qty", None))
    if target <= 0:
        return None
    actual = _decimal(actual_yield)
    waste = _decimal(waste_qty)
    deviation = max(abs(actual - target), waste)
    variance_percent = (deviation / target * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if variance_percent <= threshold:
        return None
    item = cstr(getattr(order, "production_item", None)).strip()
    stock_entry_name = cstr(getattr(stock_entry, "name", None) or stock_entry).strip()
    return upsert_inventory_exception(
        reason_code="batch_preparation_variance",
        summary=(
            f"Prepared batch {item} yield/waste variance is {variance_percent}% "
            f"against the {threshold}% ceiling"
        ),
        next_action="Review the measured batch yield and waste, then correct the BOM or preparation process",
        severity="Warning",
        company=company,
        warehouse=warehouse,
        item=item,
        source_doctype="Stock Entry",
        source_name=stock_entry_name,
    )


def _select_preparation_variance_ceiling(
    *,
    bom_value: Any,
    policy_value: Any,
    has_bom_field: bool,
    has_policy_field: bool,
) -> Decimal | None:
    selected = bom_value if bom_value not in (None, "") else policy_value
    if selected in (None, ""):
        if not has_bom_field and not has_policy_field:
            raise ValueError("variance threshold fields are not installed")
        raise ValueError("a BOM or policy variance ceiling is required")
    try:
        parsed = Decimal(str(selected))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("variance ceiling must be a finite decimal") from error
    if not parsed.is_finite() or parsed < 0 or parsed > 100:
        raise ValueError("variance ceiling must be between 0 and 100")
    return parsed


def _schedule_for_policy(policy: dict[str, Any]) -> list[dict[str, Any]]:
    return _preparation_alerts_for_policy(policy, record_exceptions=True)


def _preparation_alerts_for_policy(
    policy: dict[str, Any], *, record_exceptions: bool
) -> list[dict[str, Any]]:
    """Derive alerts for one outlet policy.

    ``record_exceptions`` is enabled only by the scheduler.  The device read
    model uses the same calculation without turning a GET into a write.
    """

    company = cstr(policy.get("company")).strip()
    warehouse = cstr(policy.get("warehouse")).strip()
    if not company or not warehouse or not cstr(policy.get("cutover_at")).strip() or not cstr(policy.get("cutover_token")).strip():
        return []
    bom_meta = frappe.get_meta("BOM")
    required_bom_fields = {
        "custom_kopos_autoprep_enabled",
        "custom_kopos_batch_qty",
        "custom_kopos_min_ready_qty",
        "custom_kopos_preparation_lead_minutes",
    }
    if not required_bom_fields.issubset({field.fieldname for field in bom_meta.fields}):
        return []
    if not frappe.get_meta("Work Order").has_field("custom_kopos_preparation_fingerprint"):
        return []
    variance_failures = preparation_variance_preflight(company=company, warehouse=warehouse)
    if variance_failures:
        if record_exceptions:
            for failure in variance_failures:
                upsert_inventory_exception(
                    reason_code="batch_preparation_variance_configuration",
                    summary="Prepared batch variance authority is missing or invalid",
                    next_action="Set a director-approved BOM or outlet policy variance ceiling before preparing another batch",
                    severity="Warning",
                    company=company,
                    warehouse=warehouse,
                    source_doctype="FB Inventory Policy",
                    source_name=cstr(policy.get("name")),
                )
        # Keep configuration failures visible in the same bounded POS task
        # feed as normal preparation alerts.  The task deliberately has no
        # BOM/work-order identity, so the client can explain the setup gap but
        # cannot start a physical operation from it.
        setup_fingerprint = _fingerprint(
            company,
            warehouse,
            "setup:" + "|".join(variance_failures),
            Decimal("0"),
            Decimal("0"),
            "setup",
        )
        return [{
            "status": "alert",
            "kind": "preparation",
            "preparation_alert": True,
            "title": "Batch preparation setup needed",
            "document": "Setup required",
            "item_name": "Batch preparation setup needed",
            "warehouse": warehouse,
            "blocked_reason": (
                "Batch preparation setup is incomplete. A Company Director "
                "must set a valid BOM or outlet variance ceiling before "
                "another batch can be prepared."
            ),
            "preparation_instructions": (
                "Ask a Company Director to complete the batch preparation setup."
            ),
            "reason": "|".join(variance_failures),
            # POS requires every guided task to carry a non-empty source
            # revision, even when this task is deliberately non-actionable.
            # Bind the setup notice to the same stable fingerprint used for
            # the preparation alert so it can be cached and safely refreshed.
            "revision": "setup:" + setup_fingerprint,
            "fingerprint": setup_fingerprint,
        }]
    rows = frappe.get_all(
        "BOM",
        filters={
            "company": company,
            "docstatus": 1,
            "is_active": 1,
            "custom_kopos_autoprep_enabled": 1,
        },
        fields=bom_fields,
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
            if record_exceptions:
                _block_bom(policy, bom_name, item, f"Fix the prepared batch quantities: {error}")
            continue
        reliable_forecast = warehouse_has_reliable_forecast(warehouse)
        daily_demand = (
            _recent_daily_consumption(item=item, warehouse=warehouse, cutover_at=policy.get("cutover_at"))
            if reliable_forecast
            else None
        )
        try:
            trigger_qty = preparation_trigger_level(
                minimum_ready_qty=min_ready_qty,
                daily_demand=daily_demand,
                lead_minutes=bom.get("custom_kopos_preparation_lead_minutes"),
            )
        except ValueError as error:
            if record_exceptions:
                _block_bom(policy, bom_name, item, f"Fix the preparation lead: {error}")
            continue
        actual_qty, stock_marker = _actual_qty_and_marker(item, warehouse)
        if actual_qty >= trigger_qty:
            continue
        blocking_sources = _blocking_hold_sources(item=item, warehouse=warehouse)
        if blocking_sources:
            blocked_reason = (
                "Resolve the " + ", ".join(sorted(blocking_sources))
                + " hold before preparing another batch"
            )
            if record_exceptions:
                _block_bom(policy, bom_name, item, blocked_reason)
            results.append(
                _preparation_alert(
                    policy=policy,
                    bom=bom,
                    item=item,
                    batch_qty=batch_qty,
                    min_ready_qty=min_ready_qty,
                    trigger_qty=trigger_qty,
                    actual_qty=actual_qty,
                    stock_marker=stock_marker,
                    blocked_reason=blocked_reason,
                )
            )
            continue
        if not _batch_shelf_life_safe(item=item, batch_qty=batch_qty, daily_demand=daily_demand):
            blocked_reason = (
                "Review batch size or shelf life; measured sell-through cannot use this batch before expiry"
            )
            if record_exceptions:
                _block_bom(policy, bom_name, item, blocked_reason)
            results.append(
                _preparation_alert(
                    policy=policy,
                    bom=bom,
                    item=item,
                    batch_qty=batch_qty,
                    min_ready_qty=min_ready_qty,
                    trigger_qty=trigger_qty,
                    actual_qty=actual_qty,
                    stock_marker=stock_marker,
                    blocked_reason=blocked_reason,
                )
            )
            continue
        fingerprint = _fingerprint(company, warehouse, bom_name, batch_qty, trigger_qty, stock_marker)
        # Review First gates unattended forecast/purchasing actions.  It does
        # not gate routine physical preparation: staff still follow the
        # published BOM and a manager/director can review the resulting
        # standard Work Order through the normal operational flow.  Paused is
        # the only policy state that blocks a staff-guided preparation.
        paused = cstr(policy.get("automation_state")).strip() == "Paused"
        alert = _preparation_alert(
            policy=policy,
            bom=bom,
            item=item,
            batch_qty=batch_qty,
            min_ready_qty=min_ready_qty,
            trigger_qty=trigger_qty,
            actual_qty=actual_qty,
            stock_marker=stock_marker,
            blocked_reason=(
                "Inventory automation is paused for this outlet; resume it before preparing this batch"
                if paused
                else None
            ),
        )
        results.append(alert)
    return results


def _preparation_alert(
    *,
    policy: dict[str, Any],
    bom: dict[str, Any],
    item: str,
    batch_qty: Decimal,
    min_ready_qty: Decimal,
    trigger_qty: Decimal,
    actual_qty: Decimal,
    stock_marker: str,
    blocked_reason: str | None = None,
) -> dict[str, Any]:
    """Return the non-financial, revision-bound alert shown on POS."""

    bom_name = cstr(bom.get("name")).strip()
    company = cstr(policy.get("company")).strip()
    warehouse = cstr(policy.get("warehouse")).strip()
    lead_minutes = bom.get("custom_kopos_preparation_lead_minutes")
    fingerprint = _fingerprint(company, warehouse, bom_name, batch_qty, trigger_qty, stock_marker)
    alert: dict[str, Any] = {
        "status": "alert",
        "kind": "preparation",
        "preparation_alert": True,
        "document": bom_name,
        "bom_no": bom_name,
        "revision": cstr(bom.get("modified")).strip(),
        "fingerprint": fingerprint,
        "item_code": item,
        "item_name": cstr(bom.get("item_name")).strip() or item,
        "qty": str(batch_qty),
        "batch_qty": str(batch_qty),
        "min_ready_qty": str(min_ready_qty),
        "trigger_qty": str(trigger_qty),
        "current_qty": str(actual_qty),
        "stock_marker": stock_marker,
        "lead_minutes": str(lead_minutes if lead_minutes not in (None, "") else "0"),
        "warehouse": warehouse,
        "automation_state": cstr(policy.get("automation_state")).strip(),
        "preparation_instructions": cstr(
            bom.get("custom_kopos_preparation_instructions")
        ).strip()[:2_000],
    }
    if blocked_reason:
        alert["blocked_reason"] = blocked_reason
    return alert


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


def _actual_qty_and_marker(item: str, warehouse: str) -> tuple[Decimal, str]:
    row = frappe.db.get_value(
        "Bin",
        {"item_code": item, "warehouse": warehouse},
        ["actual_qty", "modified"],
        as_dict=True,
    ) or {}
    return _decimal(row.get("actual_qty") or 0), cstr(row.get("modified")).strip() or "no-bin"


def _recent_daily_consumption(*, item: str, warehouse: str, cutover_at: Any) -> Decimal | None:
    if (
        not cutover_at
        or not frappe.db.exists("DocType", "FB Resolved Sale")
        or not frappe.db.exists("DocType", "FB Resolved Component")
    ):
        return None
    try:
        quantity_expression = _resolved_component_quantity_expression("rc")
        rows = frappe.db.sql(
            f"""
            SELECT DATE(rs.creation) AS operating_day, SUM({quantity_expression}) AS quantity
            FROM `tabFB Resolved Sale` rs
            INNER JOIN `tabFB Resolved Component` rc ON rc.parent = rs.name
            WHERE rs.booth_warehouse = %s
              AND rs.creation >= %s
              AND rc.item = %s
              AND rc.affects_stock = 1
              AND {quantity_expression} > 0
            GROUP BY DATE(rs.creation)
            ORDER BY operating_day DESC
            LIMIT 14
            """,
            (warehouse, cutover_at, item),
            as_dict=True,
        ) or []
    except Exception:
        return None
    values = sorted(_decimal(row.get("quantity")) for row in rows if row.get("quantity") not in (None, ""))
    if not values:
        return None
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / Decimal("2")


def _batch_shelf_life_safe(*, item: str, batch_qty: Decimal, daily_demand: Decimal | None) -> bool:
    meta = frappe.get_meta("Item")
    fieldname = next(
        (name for name in ("shelf_life_in_days", "custom_kopos_shelf_life_days") if meta.has_field(name)),
        None,
    )
    if not fieldname:
        return True
    shelf_life = _decimal(frappe.db.get_value("Item", item, fieldname) or 0)
    if shelf_life <= 0:
        return True
    if daily_demand is None or daily_demand <= 0:
        return False
    return batch_qty <= daily_demand * shelf_life


def _blocking_hold_sources(*, item: str, warehouse: str) -> set[str]:
    return {
        cstr(hold.get("source")).strip()
        for hold in active_holds(target_type="Item", target_id=item, warehouse=warehouse)
        if cstr(hold.get("source")).strip() in {"safety", "quality", "equipment"}
    }


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value if value not in (None, "") else "0"))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("quantity must be a finite decimal") from error
    if not result.is_finite() or result < 0:
        raise ValueError("quantity must be a non-negative finite decimal")
    return result


def _resolved_component_quantity_expression(alias: str) -> str:
    field = "stock_qty_decimal" if frappe.get_meta("FB Resolved Component").has_field("stock_qty_decimal") else None
    return f"COALESCE(NULLIF({alias}.{field}, ''), {alias}.stock_qty)" if field else f"{alias}.stock_qty"


def _positive_or_default(value: Any, fallback: Decimal) -> Decimal:
    result = _decimal(value)
    return result if result > 0 else fallback


def _fingerprint(*values: Any) -> str:
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
