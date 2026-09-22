"""Manager-facing Inventory Autopilot read models and health checks."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime, now_datetime

from kopos_connector.api.cafe_units import amount_phrase, difference_phrase
from kopos_connector.pricing.runtime import PromotionRuntimeError
from kopos_connector.api.devices import (
    lock_device_for_operational_mutation,
    require_device_context,
    require_device_operational_scope,
)
from kopos_connector.api.catalog import (
    get_default_pos_profile,
    get_catalog_target_ids,
    get_item_barcodes_map,
    get_item_modifier_groups_map,
    get_item_recipe_snapshots_map,
    get_items,
    resolve_catalog_pos_profile,
)
from kopos_connector.api.catalog import build_catalog_payload
from kopos_connector.api.director_scope import (
    director_pos_profiles,
    require_director_role,
    resolve_director_context,
)
from kopos_connector.kopos.services.inventory_autopilot.holds import (
    create_hold,
    manager_override_automation_hold,
    release_hold,
)
from kopos_connector.kopos.services.inventory_autopilot.legacy_migration import (
    discover_legacy_values,
    execute_legacy_migration,
    legacy_input_digest,
)
from kopos_connector.kopos.services.inventory_autopilot.exceptions import (
    configured_inventory_exception_owner,
    resolve_inventory_exception,
    upsert_inventory_exception,
)
from kopos_connector.kopos.services.inventory_autopilot.document_coordinator import (
    outbound_configuration_safe,
)
from kopos_connector.kopos.services.inventory_autopilot.promotion_economics import (
    PromotionEconomicsError,
    calculate_promotion_economics,
    calculate_actual_cogs_from_stock_entries,
    economics_source_hash,
    normalize_scenarios,
    summarize_actual_promotion_results,
)
from kopos_connector.kopos.services.inventory_autopilot.menu_authoring import (
    csv_template,
    draft_recipe_code,
    summarize_menu_authoring,
    validate_recipe_csv,
)
from kopos_connector.kopos.services.inventory_autopilot.preparation import (
    derived_preparation_alerts,
    preparation_variance_preflight,
    record_preparation_variance,
)
from kopos_connector.kopos.services.inventory_autopilot.overlay import device_overlay_is_current
from kopos_connector.kopos.services.inventory_autopilot.health_monitor import (
    critical_health_reasons,
    health_blocks_rollout,
)
from kopos_connector.kopos.services.inventory_autopilot.automation_identity import (
    AutomationIdentityError,
    inventory_automation_identity,
    automation_identity_is_configured,
    purchase_review_owner,
)
from kopos_connector.kopos.services.inventory_autopilot.cutover import (
    device_activation_failures,
    monitoring_owner_failures,
    opening_reconciliation_failure,
)
from kopos_connector.kopos.services.inventory_autopilot.edge_snapshot import (
    attach_bounded_tasks,
    build_edge_inventory_snapshot,
)
from kopos_connector.kopos.services.inventory_autopilot.recipe_compiler import (
    RecipeCompilerError,
    compile_recipe_components,
)
from kopos_connector.kopos.services.inventory_autopilot.availability_capacity import (
    resolve_made_item_waste,
)
from kopos_connector.kopos.services.inventory_autopilot.staff_access import (
    find_staff_access_for_device,
    outlet_erp_role_failures,
    resolve_staff_access_for_device,
)
from kopos_connector.utils.diagnostics import log_sanitized_error
from kopos_connector.utils.manager_approval import (
    canonical_context_hash,
    verify_manager_approval_token,
)


# recover_inventory_projections stamps the scheduler markers and runs on the
# "*/5 * * * *" cron in hooks.py. Health used to describe it as hourly and only
# complained after 60/90 minutes, so a stalled five-minute worker could miss
# seventeen runs before anyone was told.
INVENTORY_RECOVER_INTERVAL_MINUTES = 5
INVENTORY_SCHEDULER_WARNING_MINUTES = 30
INVENTORY_SCHEDULER_CRITICAL_MINUTES = 60
STOCK_UPDATE_TAKING_LONGER_SECONDS = 60
STOCK_UPDATE_STATUS_LIMIT = 1000

ACTIVE_PROJECTION_STATES = frozenset({"Pending", "Processing", "Failed", "Dead Letter"})
OPEN_COUNT_TASK_STATUSES = frozenset({"Assigned", "Claimed", "In Progress", "Submitted", "Review"})
OPEN_PLAN_STATUSES = frozenset({"Review First", "Ready", "Blocked"})


def _resolve_count_director_review_exception(
    *,
    company: str,
    warehouse: str,
    observation_id: str,
) -> None:
    """Resolve only the exception opened for this exact count observation."""

    resolve_inventory_exception(
        reason_code="inventory_count_director_review",
        company=company,
        warehouse=warehouse,
        source_doctype="FB Inventory Count Observation",
        source_name=observation_id,
    )


@frappe.whitelist(methods=["GET"])
def get_autopilot_health(warehouse: str | None = None) -> dict[str, Any]:
    """Return sanitized, warehouse-scoped operational health.

    This route is intentionally separate from device routes. It is a read-only
    monitor surface and never changes a policy, projection, hold, or document.
    """

    _require_company_director("Stock health")
    resolved_warehouse = cstr(warehouse).strip()
    if not resolved_warehouse:
        frappe.throw(_("Warehouse is required"), frappe.ValidationError)
    policy = _get_policy(resolved_warehouse)
    company = cstr((policy or {}).get("company")).strip() or cstr(
        frappe.db.get_value("Warehouse", resolved_warehouse, "company")
    ).strip()
    if not company:
        frappe.throw(_("This outlet is not linked to a café"), frappe.ValidationError)
    resolve_director_context(company=company, warehouse=resolved_warehouse)
    now = now_datetime()
    max_age_minutes = int(policy.get("max_source_age_minutes") or 30) if policy else 30
    projection_rows = frappe.db.sql(
        """
        SELECT pl.state, COUNT(*) AS count, MIN(pl.created_at) AS oldest_created_at
        FROM `tabFB Projection Log` pl
        LEFT JOIN `tabFB Order` o
          ON pl.source_doctype = 'FB Order' AND o.name = pl.source_name
        LEFT JOIN `tabFB Inventory Policy` ip
          ON pl.source_doctype = 'FB Inventory Policy' AND ip.name = pl.source_name
        WHERE pl.projection_type = 'Stock Issue'
          AND ((pl.source_doctype = 'FB Order' AND o.booth_warehouse = %s)
            OR (pl.source_doctype = 'FB Inventory Policy' AND ip.warehouse = %s))
        GROUP BY pl.state
        """,
        (resolved_warehouse, resolved_warehouse),
        as_dict=True,
    )
    projection_counts = {
        cstr(row.get("state")): int(row.get("count") or 0)
        for row in projection_rows
    }
    oldest_age_minutes = _active_projection_oldest_age(projection_rows, now=now)
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
        filters={"warehouse": resolved_warehouse, "status": "Open"},
        fields=[
            "name", "severity", "reason_code", "summary", "next_action",
            "source_doctype", "source_name", "item", "last_seen",
        ],
        order_by="last_seen desc",
        limit_page_length=50,
    )
    critical_exceptions = [
        row for row in exceptions
        if cstr(row.get("severity")).strip() == "Critical"
    ]
    critical_reasons.extend(
        cstr(row.get("reason_code")).strip()
        for row in critical_exceptions
        if cstr(row.get("reason_code")).strip()
    )
    scheduler = _scheduler_health()
    scheduler_success = get_datetime(scheduler.get("last_success")) if scheduler.get("last_success") else None
    scheduler_age = _age_minutes(scheduler_success, now)
    if scheduler_age is None or scheduler_age > INVENTORY_SCHEDULER_CRITICAL_MINUTES:
        critical_reasons.append("inventory_scheduler_overdue")
    elif scheduler_age > INVENTORY_SCHEDULER_WARNING_MINUTES:
        warning_reasons.append("inventory_scheduler_delayed")
    device_rows = frappe.db.sql(
        """
        SELECT d.name, d.config_version, d.inventory_config_version, d.inventory_observed_at,
               d.inventory_report_received_at,
               d.inventory_catalog_version,
               d.inventory_overlay_version, d.inventory_overlay_hash,
               d.inventory_sales_pending, d.inventory_sales_syncing,
               d.inventory_sales_failed, d.inventory_sales_dead_letter,
               d.inventory_commands_pending, d.inventory_commands_syncing,
               d.inventory_commands_failed, d.inventory_commands_dead_letter
        FROM `tabKoPOS Device` d
        INNER JOIN `tabPOS Profile` p ON p.name = d.pos_profile
        WHERE d.enabled = 1 AND p.warehouse = %s
        LIMIT 500
        """,
        (resolved_warehouse,),
        as_dict=True,
    )
    current_devices = dirty_devices = stale_devices = 0
    inventory_command_counts = {"pending": 0, "syncing": 0, "failed": 0, "dead_letter": 0}
    unacknowledged_overlays = 0
    unacknowledged_overlay_critical = 0
    device_acknowledgements: list[dict[str, Any]] = []
    for row in device_rows:
        received_at = get_datetime(row.get("inventory_report_received_at")) if row.get("inventory_report_received_at") else None
        observed_at = get_datetime(row.get("inventory_observed_at")) if row.get("inventory_observed_at") else None
        effective_at = min(
            (value for value in (received_at, observed_at) if value is not None),
            key=_datetime_sort_key,
            default=None,
        )
        is_stale = effective_at is None or _age_minutes(effective_at, now) > max_age_minutes
        is_dirty = _device_is_dirty(row)
        for count_name, fieldname in (
            ("pending", "inventory_commands_pending"),
            ("syncing", "inventory_commands_syncing"),
            ("failed", "inventory_commands_failed"),
            ("dead_letter", "inventory_commands_dead_letter"),
        ):
            inventory_command_counts[count_name] += int(row.get(fieldname) or 0)
        if int(row.get("inventory_commands_dead_letter") or 0) > 0:
            critical_reasons.append("inventory_command_dead_letter")
        elif int(row.get("inventory_commands_failed") or 0) > 0:
            warning_reasons.append("inventory_command_failed")
        overlay_current = _device_overlay_current(row)
        if not overlay_current:
            unacknowledged_overlays += 1
            overlay_age = _device_overlay_age(row, max_age_minutes=max_age_minutes, now=now)
            if is_stale or (overlay_age is not None and overlay_age > max_age_minutes):
                unacknowledged_overlay_critical += 1
        if is_stale:
            stale_devices += 1
        if is_dirty:
            dirty_devices += 1
        if not is_stale and not is_dirty and overlay_current:
            current_devices += 1
        device_acknowledgements.append({
            "device_id": cstr(row.get("name")),
            "config_current": cstr(row.get("config_version")) == cstr(row.get("inventory_config_version")),
            "catalog_version": cstr(row.get("inventory_catalog_version")),
            "overlay_version": cstr(row.get("inventory_overlay_version")),
            "overlay_hash": cstr(row.get("inventory_overlay_hash")),
            "overlay_current": overlay_current,
            "stale": is_stale,
            "dirty": is_dirty,
            "effective_at": _iso_with_offset(effective_at) if effective_at else None,
        })
    if unacknowledged_overlays:
        if unacknowledged_overlay_critical:
            critical_reasons.append("inventory_overlay_unacknowledged")
        else:
            warning_reasons.append("inventory_overlay_pending_acknowledgement")
    if stale_devices:
        warning_reasons.append("inventory_device_stale")
    # Paused automation is not a healthy steady state: nothing is projecting,
    # planning, or reordering. The external classifier already treats it as a
    # warning, but the desk banner reads these reasons, so without it the page
    # could show "All clear" over a stopped Autopilot.
    if policy and cstr(policy.get("automation_state")).strip() == "Paused":
        warning_reasons.append("inventory_automation_paused")
    runtime_artifact = _runtime_artifact_identity()
    runtime_artifact_status = cstr(runtime_artifact.get("status")).strip()
    if runtime_artifact_status == "unavailable":
        critical_reasons.append("inventory_runtime_artifact_identity_unavailable")
    elif runtime_artifact_status == "unmatched":
        # The installed runtime is not the artifact this site was told to expect.
        critical_reasons.append("inventory_runtime_artifact_identity_unmatched")
    elif runtime_artifact_status != "verified":
        # Hashed but never compared against release evidence. Not a stop, but the
        # desk must not present it as a verified artifact.
        warning_reasons.append("inventory_runtime_artifact_identity_unverified")
    counts = _health_count_summary(resolved_warehouse, now=now)
    count_director_reviews = _health_count_director_reviews(resolved_warehouse, now=now)
    plans = _health_plan_summary(resolved_warehouse, now=now)
    if counts.get("status") != "ok":
        critical_reasons.append("inventory_count_health_unavailable")
    if count_director_reviews.get("status") == "unavailable":
        critical_reasons.append("inventory_count_director_review_unavailable")
    if plans.get("status") != "ok":
        critical_reasons.append("inventory_plan_health_unavailable")
    exception_owner = configured_inventory_exception_owner(
        policy_name=cstr(policy.get("name")).strip() if policy else None,
        company=cstr(policy.get("company")).strip() if policy else None,
        warehouse=resolved_warehouse,
    )
    exception_routing = {
        "status": "configured" if exception_owner else ("missing" if policy else "policy_missing"),
        "message": (
            "Inventory exceptions are routed to the configured Company Director."
            if exception_owner
            else (
                "Create an outlet Inventory Policy before activating inventory."
                if not policy
                else "Set an enabled Company Director as Inventory Exception Owner on this outlet policy."
            )
        ),
    }
    if policy and not exception_owner:
        critical_reasons.append("inventory_exception_owner_not_configured")
    availability_at = _health_marker("last_availability", warehouse=resolved_warehouse)
    plan_at = _health_marker("last_plan", warehouse=resolved_warehouse)
    po_safe, po_reason = outbound_configuration_safe()
    return {
        "schema_version": "inventory-health-v2",
        "warehouse": resolved_warehouse,
        "company": cstr(policy.get("company")).strip() if policy else None,
        "currency": cstr(frappe.db.get_value("Company", cstr(policy.get("company")).strip(), "default_currency")).strip() if policy else None,
        "stock_tracking_started": _stock_tracking_started(policy),
        "as_of": _iso_with_offset(now),
        "automation_state": cstr(policy.get("automation_state")) if policy else "Review First",
        "max_source_age_minutes": max_age_minutes,
        "scheduler": scheduler,
        "projection": {
            "counts_by_state": projection_counts,
            "oldest_age_minutes": oldest_age_minutes,
        },
        "devices": {
            "current": current_devices,
            "dirty": dirty_devices,
            "stale": stale_devices,
            "acknowledgements": device_acknowledgements,
        },
        "inventory_commands": inventory_command_counts,
        "overlay": {
            "acknowledged": unacknowledged_overlays == 0,
            "unacknowledged_devices": unacknowledged_overlays,
        },
        "catalog": {
            "acknowledged": unacknowledged_overlays == 0,
            "unacknowledged_devices": unacknowledged_overlays,
        },
        "counts": counts,
        "count_director_reviews": count_director_reviews,
        "planning": plans,
        "exception_routing": exception_routing,
        "exceptions": {
            "open_critical": len(critical_exceptions),
            "critical_reasons": sorted(set(critical_reasons)),
            "warning_reasons": sorted(set(warning_reasons)),
            "top": [_health_exception_response(row, now=now) for row in exceptions[:10]],
        },
        "last_successful_availability_check": availability_at,
        "last_successful_availability_at": availability_at,
        "last_successful_plan": plan_at,
        "last_successful_plan_at": plan_at,
        "draft_purchase_order_safety": "safe" if po_safe else "unsafe",
        "draft_purchase_order_safety_reason": po_reason,
        "runtime_artifact": runtime_artifact,
    }


AUTOPILOT_WORKFLOWS = frozenset({"buying", "updates", "finance"})
AUTOPILOT_PERIOD_DAYS = frozenset({7, 30, 90})
AUTOPILOT_FINANCE_DETAIL_LIMIT = 100


@frappe.whitelist(methods=["GET"])
def get_autopilot_workflow(
    warehouse: str | None = None,
    workflow: str | None = None,
    period_days: int | str | None = 30,
) -> dict[str, Any]:
    """Return one bounded, read-only workflow for the guided stock desk."""

    _require_company_director("Stock workflow")
    resolved_warehouse = cstr(warehouse).strip()
    resolved_workflow = cstr(workflow).strip().lower()
    if not resolved_warehouse:
        frappe.throw(_("Choose an outlet first"), frappe.ValidationError)
    if resolved_workflow not in AUTOPILOT_WORKFLOWS:
        frappe.throw(_("Choose Ordering, Stock updates, or Cost and value"), frappe.ValidationError)
    policy = _get_policy(resolved_warehouse)
    scoped_company = cstr((policy or {}).get("company")).strip() or cstr(
        frappe.db.get_value("Warehouse", resolved_warehouse, "company")
    ).strip()
    if not scoped_company:
        frappe.throw(_("This outlet is not linked to a café"), frappe.ValidationError)
    resolve_director_context(company=scoped_company, warehouse=resolved_warehouse)
    if not policy:
        return {
            "schema_version": "inventory-workflow-v1",
            "workflow": resolved_workflow,
            "warehouse": resolved_warehouse,
            "company": None,
            "currency": None,
            "as_of": _iso_with_offset(now_datetime()),
            "status": "setup_required",
            "headline": "Stock settings are not finished",
            "next_action": "Ask Support to finish stock setup for this outlet.",
            "items": [],
        }

    company = cstr(policy.get("company")).strip()
    currency = cstr(frappe.db.get_value("Company", company, "default_currency")).strip()
    common = {
        "schema_version": "inventory-workflow-v1",
        "workflow": resolved_workflow,
        "warehouse": resolved_warehouse,
        "company": company,
        "currency": currency,
        "as_of": _iso_with_offset(now_datetime()),
    }
    if resolved_workflow == "buying":
        return {**common, **_autopilot_buying_workflow(company=company, warehouse=resolved_warehouse, currency=currency)}
    if resolved_workflow == "updates":
        return {**common, **_autopilot_updates_workflow(warehouse=resolved_warehouse)}

    try:
        resolved_period = int(period_days or 30)
    except (TypeError, ValueError):
        resolved_period = 0
    if resolved_period not in AUTOPILOT_PERIOD_DAYS:
        frappe.throw(_("Finance period must be 7, 30, or 90 days"), frappe.ValidationError)
    return {
        **common,
        **_autopilot_finance_workflow(
            company=company,
            warehouse=resolved_warehouse,
            period_days=resolved_period,
        ),
    }


def _autopilot_buying_workflow(*, company: str, warehouse: str, currency: str) -> dict[str, Any]:
    rows = frappe.get_all(
        "FB Inventory Plan",
        filters={"company": company, "warehouse": warehouse},
        fields=["name", "status", "planning_date", "forecast_state", "forecast_evidence", "created_documents", "modified"],
        order_by="planning_date desc, modified desc",
        limit_page_length=20,
    )
    plans: list[dict[str, Any]] = []
    remaining_line_budget = 100
    for row in rows or []:
        document = frappe.get_doc("FB Inventory Plan", cstr(row.get("name")))
        lines = []
        for line in (getattr(document, "lines", None) or [])[:remaining_line_budget]:
            item = cstr(getattr(line, "item", None)).strip()
            exact_quantity = cstr(getattr(line, "quantity_decimal", None)).strip() or cstr(getattr(line, "quantity", None)).strip()
            action = cstr(getattr(line, "action", None)).strip()
            unit = cstr(getattr(line, "uom", None)).strip()
            lines.append({
                "item_name": cstr(frappe.db.get_value("Item", item, "item_name")).strip()
                or "Ingredient or supply",
                "action_label": {
                    "Purchase": "Order from supplier",
                    "Manufacture": "Prepare in the café",
                    "Transfer": "Move from another outlet",
                }.get(action, "Check this"),
                "quantity_display": amount_phrase(exact_quantity, unit),
                "reason": _autopilot_human_plan_reason(getattr(line, "reason", None)),
            })
        remaining_line_budget -= len(lines)
        plans.append({
            "planning_date": cstr(row.get("planning_date")),
            "lines": lines,
            "draft_documents": _autopilot_plan_documents(
                row.get("created_documents"),
                company=company,
                warehouse=warehouse,
                currency=currency,
            ),
        })
        if remaining_line_budget <= 0:
            break
    last_plan = _health_marker("last_plan", warehouse=warehouse)
    return {
        "status": "ready" if plans else "empty",
        "headline": f"{len(plans)} suggested order{'s' if len(plans) != 1 else ''} to check" if plans else "No suggested order is waiting",
        "next_action": "Review each suggested amount and the requests or supplier orders beside it. JiJi never sends an order." if plans else (
            f"JiJi last checked what may be needed at {last_plan}. Look again after the next stock update."
            if last_plan else "JiJi has not suggested an order yet. Open Stock updates to see what is happening."
        ),
        "items": plans,
    }


def _autopilot_plan_documents(
    raw: Any, *, company: str, warehouse: str, currency: str
) -> list[dict[str, Any]]:
    try:
        references = json.loads(cstr(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        references = []
    if not isinstance(references, list):
        return []
    result: list[dict[str, Any]] = []
    for reference in references[:20]:
        if not isinstance(reference, dict):
            continue
        doctype = cstr(reference.get("doctype")).strip()
        name = cstr(
            reference.get("name")
            or reference.get("purchase_order")
            or reference.get("material_request")
        ).strip()
        if not name:
            continue
        if doctype in {"", "Purchase Order"}:
            rows = frappe.db.sql(
                """
                SELECT purchase_order.name, purchase_order.supplier,
                       purchase_order.docstatus, purchase_order.grand_total,
                       purchase_order.currency
                FROM `tabPurchase Order` purchase_order
                INNER JOIN `tabPurchase Order Item` line
                  ON line.parent = purchase_order.name
                 AND line.parenttype = 'Purchase Order'
                WHERE purchase_order.name = %s AND purchase_order.company = %s
                  AND purchase_order.docstatus = 0
                  AND COALESCE(NULLIF(line.warehouse, ''), purchase_order.set_warehouse) = %s
                GROUP BY purchase_order.name, purchase_order.supplier,
                         purchase_order.docstatus, purchase_order.grand_total,
                         purchase_order.currency
                LIMIT 1
                """,
                (name, company, warehouse),
                as_dict=True,
            )
            row = (rows or [None])[0]
            if not row:
                continue
            result.append({
                "document_label": "Supplier order",
                "status": "Written — not sent",
                "supplier": cstr(row.get("supplier")),
                "total_sen": _autopilot_money_sen(row.get("grand_total")),
                "currency": cstr(row.get("currency")).strip() or currency,
                "next_action": "Open Supplies to check it. JiJi never sends an order.",
            })
            continue
        if doctype != "Material Request":
            continue
        request_item_meta = frappe.get_meta("Material Request Item")
        source_clause = " OR line.from_warehouse = %s" if request_item_meta.has_field("from_warehouse") else ""
        params: tuple[Any, ...] = (
            (name, company, warehouse, warehouse)
            if source_clause
            else (name, company, warehouse)
        )
        rows = frappe.db.sql(
            f"""
            SELECT request.name, request.material_request_type, request.docstatus
            FROM `tabMaterial Request` request
            INNER JOIN `tabMaterial Request Item` line
              ON line.parent = request.name
             AND line.parenttype = 'Material Request'
            WHERE request.name = %s AND request.company = %s
              AND request.docstatus IN (0, 1)
              AND (line.warehouse = %s{source_clause})
            GROUP BY request.name, request.material_request_type, request.docstatus
            LIMIT 1
            """,
            params,
            as_dict=True,
        )
        row = (rows or [None])[0]
        if not row:
            continue
        request_type = cstr(row.get("material_request_type")).strip()
        submitted = cint(row.get("docstatus")) == 1
        transfer = request_type == "Material Transfer"
        result.append({
            "document_label": "Stock transfer request" if transfer else "Supplier request",
            "status": (
                "Ready for the outlet team"
                if transfer and submitted
                else "Requested — waiting for an order"
                if submitted
                else "Written — waiting for a director"
            ),
            "next_action": (
                "The outlet team can now move this stock."
                if transfer and submitted
                else "A supplier order can now be prepared from this request."
                if submitted
                else "A director checks and confirms this request."
            ),
        })
    return result


def _autopilot_updates_workflow(*, warehouse: str) -> dict[str, Any]:
    rows = frappe.db.sql(
        """
        SELECT pl.name, pl.state, pl.retry_count, pl.created_at, pl.last_attempt_at,
               pl.next_retry_at, pl.source_doctype, pl.source_name,
               pl.target_doctype, pl.target_name
        FROM `tabFB Projection Log` pl
        LEFT JOIN `tabFB Order` o
          ON pl.source_doctype = 'FB Order' AND o.name = pl.source_name
        LEFT JOIN `tabFB Inventory Policy` ip
          ON pl.source_doctype = 'FB Inventory Policy' AND ip.name = pl.source_name
        WHERE pl.projection_type = 'Stock Issue'
          AND ((pl.source_doctype = 'FB Order' AND o.booth_warehouse = %s)
            OR (pl.source_doctype = 'FB Inventory Policy' AND ip.warehouse = %s))
        ORDER BY pl.created_at DESC, pl.name DESC
        LIMIT 20
        """,
        (warehouse, warehouse),
        as_dict=True,
    )
    states = [cstr(row.get("state")) for row in rows or []]
    items = [{
        "checked_at": _autopilot_iso(row.get("last_attempt_at") or row.get("created_at")),
        "state_label": _autopilot_projection_state_label(cstr(row.get("state"))),
        "failure_reason": _autopilot_projection_plain_reason(cstr(row.get("state"))),
        "next_action": _autopilot_projection_next_action(cstr(row.get("state"))),
    } for row in rows or []]
    raw_update_health = _scheduler_health()
    failed = sum(1 for state in states if state in {"Failed", "Dead Letter"})
    return {
        "status": "attention" if failed else ("ready" if items else "empty"),
        "headline": f"{failed} automatic stock update{'s' if failed != 1 else ''} need attention" if failed else (
            "Automatic stock updates are running" if items else "No automatic stock run has completed"
        ),
        "next_action": "Follow the next step beside the update that needs help. Ask Support if it remains stuck." if failed else (
            "JiJi has not finished a stock update yet. Refresh in a few minutes. Nothing is changed by this page." if not items else "Nothing needs to be done by hand. Refresh to see the latest result."
        ),
        "scheduler": {
            "last_success": raw_update_health.get("last_success"),
            "last_failure": raw_update_health.get("last_failure"),
            "expected_interval_minutes": INVENTORY_RECOVER_INTERVAL_MINUTES,
        },
        "items": items,
    }


def _autopilot_finance_workflow(*, company: str, warehouse: str, period_days: int) -> dict[str, Any]:
    bins = frappe.get_all(
        "Bin",
        filters={"warehouse": warehouse},
        fields=["item_code", "actual_qty", "stock_value"],
        order_by="item_code asc",
        limit_page_length=AUTOPILOT_FINANCE_DETAIL_LIMIT + 1,
    )
    balances_truncated = len(bins or []) > AUTOPILOT_FINANCE_DETAIL_LIMIT
    visible_bins = (bins or [])[:AUTOPILOT_FINANCE_DETAIL_LIMIT]
    start_date = (now_datetime() - timedelta(days=period_days)).date().isoformat()
    stock_value_rows = frappe.db.sql(
        """
        SELECT COALESCE(SUM(stock_value), 0) AS stock_value
        FROM `tabBin`
        WHERE warehouse = %s
        """,
        (warehouse,),
        as_dict=True,
    )
    movement_totals = frappe.db.sql(
        """
        SELECT sle.voucher_type, se.purpose,
               CASE WHEN se.custom_kopos_inventory_command_id LIKE 'fb-waste:%%' THEN 1 ELSE 0 END AS is_waste,
               CASE WHEN sle.actual_qty > 0 THEN 1 ELSE 0 END AS has_positive_quantity,
               CASE WHEN sle.stock_value_difference > 0 THEN 1 ELSE 0 END AS has_positive_value,
               COALESCE(SUM(CASE WHEN sle.stock_value_difference > 0 THEN sle.stock_value_difference ELSE 0 END), 0) AS positive_value,
               COALESCE(SUM(ABS(sle.stock_value_difference)), 0) AS absolute_value
        FROM `tabStock Ledger Entry` sle
        LEFT JOIN `tabStock Entry` se
          ON sle.voucher_type = 'Stock Entry' AND se.name = sle.voucher_no
        WHERE sle.company = %s AND sle.warehouse = %s AND sle.posting_date >= %s
          AND sle.is_cancelled = 0
        GROUP BY sle.voucher_type, se.purpose, is_waste, has_positive_quantity, has_positive_value
        """,
        (company, warehouse, start_date),
        as_dict=True,
    )
    movements = frappe.db.sql(
        """
        SELECT sle.name, sle.posting_date, sle.posting_time, sle.item_code, sle.stock_uom,
               sle.actual_qty, sle.qty_after_transaction, sle.stock_value_difference,
               sle.voucher_type, sle.voucher_no,
               se.purpose, se.custom_kopos_inventory_command_id
        FROM `tabStock Ledger Entry` sle
        LEFT JOIN `tabStock Entry` se
          ON sle.voucher_type = 'Stock Entry' AND se.name = sle.voucher_no
        WHERE sle.company = %s AND sle.warehouse = %s AND sle.posting_date >= %s
          AND sle.is_cancelled = 0
        ORDER BY sle.posting_date DESC, sle.posting_time DESC, sle.name DESC
        LIMIT %s
        """,
        (company, warehouse, start_date, AUTOPILOT_FINANCE_DETAIL_LIMIT + 1),
        as_dict=True,
    )
    items_truncated = len(movements or []) > AUTOPILOT_FINANCE_DETAIL_LIMIT
    visible_movements = (movements or [])[:AUTOPILOT_FINANCE_DETAIL_LIMIT]
    item_codes = sorted({
        item
        for row in [*visible_bins, *visible_movements]
        if (item := cstr(row.get("item_code")).strip())
    })
    item_details = {
        cstr(row.get("name")): {
            "name": cstr(row.get("item_name")).strip() or "Ingredient or supply",
            "unit": cstr(row.get("stock_uom")).strip(),
        }
        for row in frappe.get_all(
            "Item",
            filters={"name": ["in", item_codes]},
            fields=["name", "item_name", "stock_uom"],
            limit_page_length=len(item_codes),
        )
    } if item_codes else {}
    balances = [{
        "item_name": item_details.get(item, {}).get("name") or "Ingredient or supply",
        "quantity_display": amount_phrase(
            _autopilot_decimal_text(row.get("actual_qty")),
            cstr(item_details.get(item, {}).get("unit")),
        ),
        "stock_value_sen": _autopilot_money_sen(row.get("stock_value")),
    } for row in visible_bins if (item := cstr(row.get("item_code")).strip())]
    summary = _autopilot_movement_summary(movement_totals or [])
    movement_items = []
    for row in visible_movements:
        kind = _autopilot_movement_kind(row)
        value_sen = _autopilot_money_sen(row.get("stock_value_difference"))
        movement_items.append({
            "posting_date": cstr(row.get("posting_date")),
            "item_name": item_details.get(cstr(row.get("item_code")), {}).get("name")
            or "Ingredient or supply",
            "kind": kind,
            "quantity_display": amount_phrase(
                _autopilot_movement_quantity(row),
                cstr(row.get("stock_uom"))
                or cstr(item_details.get(cstr(row.get("item_code")), {}).get("unit")),
            ),
            "value_change_sen": value_sen,
        })
    total_stock_value_sen = _autopilot_money_sen(
        (stock_value_rows or [{}])[0].get("stock_value")
    )
    return {
        "status": "ready" if balances or movement_items else "empty",
        "headline": "Stock value and changes for finance" if balances or movement_items else "No stock change is recorded for this period",
        "next_action": "Compare the totals with your café records. Ask your bookkeeper if something looks wrong.",
        "period_days": period_days,
        "summary": {"stock_value_sen": total_stock_value_sen, **summary},
        "balances": balances,
        "balances_truncated": balances_truncated,
        "items": movement_items,
        "items_truncated": items_truncated,
    }


def _autopilot_movement_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"received_sen": 0, "used_sen": 0, "transferred_sen": 0, "waste_sen": 0}
    for row in rows:
        kind = _autopilot_movement_kind({
            "voucher_type": row.get("voucher_type"),
            "purpose": row.get("purpose"),
            "custom_kopos_inventory_command_id": "fb-waste:" if cint(row.get("is_waste")) else "",
            "actual_qty": "1" if cint(row.get("has_positive_quantity")) else "0",
            "stock_value_difference": "1" if cint(row.get("has_positive_value")) else "0",
        })
        value_sen = _autopilot_money_sen(
            row.get("positive_value") if kind == "received" else row.get("absolute_value")
        )
        summary[f"{kind if kind != 'transfer' else 'transferred'}_sen"] += value_sen
    return summary


def _autopilot_projection_next_action(state: str) -> str:
    if state == "Dead Letter":
        return "Ask Support for help and give them the help number. Do not delete this saved update."
    if state == "Failed":
        return "JiJi will try again. If it still needs help, ask Support and give them the help number."
    if state in {"Pending", "Processing"}:
        return "JiJi is finishing this update. Refresh in a few minutes."
    return "No action is needed."


def _autopilot_projection_plain_reason(state: str) -> str | None:
    if state == "Dead Letter":
        return "JiJi stopped this stock update so Support can check it safely. Your saved information is safe."
    if state == "Failed":
        return "JiJi could not finish this stock update. Your saved information is safe."
    if state in {"Pending", "Processing"}:
        return "JiJi is still finishing this stock update."
    return None


def _autopilot_projection_state_label(state: str) -> str:
    return {
        "Succeeded": "Finished",
        "Pending": "Waiting",
        "Processing": "In progress",
        "Failed": "Needs help",
        "Dead Letter": "Stopped for safety",
    }.get(state, "Check status")


def _autopilot_human_plan_reason(value: Any) -> str:
    """Return a short business reason, never a stored internal diagnostic."""

    reason = " ".join(cstr(value).split())[:240]
    lowered = reason.lower()
    hidden_words = (
        "api",
        "bom",
        "doctype",
        "exception",
        "hash",
        "item code",
        "json",
        "policy",
        "projection",
        "queue",
        "redis",
        "scheduler",
        "schema",
        "stock entry",
        "uom",
        "warehouse",
        "worker",
    )
    if not reason or any(word in lowered for word in hidden_words):
        return "Based on current stock and expected use."
    return reason


def _autopilot_movement_kind(row: dict[str, Any]) -> str:
    command_id = cstr(row.get("custom_kopos_inventory_command_id")).strip()
    purpose = cstr(row.get("purpose")).strip()
    voucher_type = cstr(row.get("voucher_type")).strip()
    quantity = Decimal(cstr(row.get("actual_qty") or "0"))
    value_change = Decimal(cstr(row.get("stock_value_difference") or "0"))
    if command_id.startswith("fb-waste:"):
        return "waste"
    if purpose == "Material Transfer":
        return "transfer"
    if voucher_type == "Stock Reconciliation" and value_change > 0:
        return "received"
    return "received" if quantity > 0 else "used"


def _autopilot_movement_quantity(row: dict[str, Any]) -> str:
    actual_quantity = _autopilot_decimal_text(row.get("actual_qty"))
    if cstr(row.get("voucher_type")).strip() == "Stock Reconciliation" and actual_quantity == "0":
        return _autopilot_decimal_text(row.get("qty_after_transaction"))
    return actual_quantity


def _autopilot_decimal_text(value: Any) -> str:
    try:
        number = Decimal(cstr(value or "0"))
    except (InvalidOperation, ValueError):
        number = Decimal("0")
    if not number.is_finite():
        number = Decimal("0")
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _autopilot_money_sen(value: Any) -> int:
    try:
        amount = Decimal(cstr(value or "0"))
    except (InvalidOperation, ValueError):
        amount = Decimal("0")
    if not amount.is_finite():
        amount = Decimal("0")
    return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _autopilot_iso(value: Any) -> str | None:
    if not value:
        return None
    try:
        return _iso_with_offset(get_datetime(value))
    except (TypeError, ValueError):
        return None


@frappe.whitelist(methods=["POST"])
def activate_inventory_cutover(
    *,
    policy: str,
    opening_stock_reconciliation: str | None = None,
) -> dict[str, Any]:
    """Support-only compatibility entry point for the detailed operation."""

    if "System Manager" not in set(frappe.get_roles(frappe.session.user)):
        frappe.throw(
            _("Use the Stock page to start stock tracking."),
            frappe.PermissionError,
        )
    return _activate_inventory_cutover(
        policy=policy,
        opening_stock_reconciliation=opening_stock_reconciliation,
    )


def _activate_inventory_cutover(
    *,
    policy: str,
    opening_stock_reconciliation: str | None = None,
) -> dict[str, Any]:
    """Record one outlet cutover identity after director preflight.

    This action is intentionally narrower than enabling automation.  It only
    records the server-generated cutover token/time and leaves the policy in
    its current safe state (normally ``Review First``).  Repeating the same
    request returns the original identity; a different opening reconciliation
    is rejected rather than creating a second cutover.
    """

    _require_company_director("Inventory cutover activation")
    policy_name = cstr(policy).strip()
    if not policy_name:
        frappe.throw(_("Inventory Policy is required"), frappe.ValidationError)

    # Serialize competing director clicks before inspecting the immutable
    # identity.  The unique field is a final database guard, not the primary
    # idempotency mechanism.
    frappe.db.sql(
        "SELECT name FROM `tabFB Inventory Policy` WHERE name = %s FOR UPDATE",
        (policy_name,),
    )
    policy_doc = frappe.get_doc("FB Inventory Policy", policy_name)
    company = cstr(getattr(policy_doc, "company", None)).strip()
    warehouse = cstr(getattr(policy_doc, "warehouse", None)).strip()
    if not company or not warehouse:
        frappe.throw(_("Inventory Policy needs a company and warehouse before cutover"), frappe.ValidationError)
    resolve_director_context(company=company, warehouse=warehouse)
    requested_reconciliation = cstr(opening_stock_reconciliation).strip()
    stored_token = cstr(getattr(policy_doc, "cutover_token", None)).strip()
    stored_reconciliation = cstr(getattr(policy_doc, "opening_stock_reconciliation", None)).strip()
    if stored_token:
        if not stored_reconciliation:
            frappe.throw(_("Inventory Policy has a cutover token but no opening Stock Reconciliation"), frappe.ValidationError)
        if requested_reconciliation and requested_reconciliation != stored_reconciliation:
            frappe.throw(
                _("This Inventory Policy already has an immutable cutover for another opening reconciliation"),
                frappe.ValidationError,
            )
        if not cstr(getattr(policy_doc, "cutover_at", None)).strip():
            frappe.throw(_("Inventory Policy has an incomplete immutable cutover identity"), frappe.ValidationError)
        return _cutover_identity_response(policy_doc, status="already_active")
    if cstr(getattr(policy_doc, "cutover_at", None)).strip():
        frappe.throw(_("Inventory Policy has a cutover time but no cutover token"), frappe.ValidationError)

    if not configured_inventory_exception_owner(
        company=company,
        warehouse=warehouse,
        policy_name=policy_name,
    ):
        _throw_cutover_block("inventory_exception_owner_not_configured")

    reconciliation_name = requested_reconciliation or stored_reconciliation
    if not reconciliation_name:
        frappe.throw(_("Submit the opening Stock Reconciliation before activating cutover"), frappe.ValidationError)
    reconciliation = frappe.get_doc("Stock Reconciliation", reconciliation_name)
    opening_failure = opening_reconciliation_failure(
        reconciliation,
        company=company,
        warehouse=warehouse,
    )
    if opening_failure:
        _throw_cutover_block(opening_failure)

    menu_summary = get_menu_authoring_summary(company=company)
    if not bool(menu_summary.get("ready")):
        _throw_cutover_block("menu_recipe_catalog_not_ready")
    if frappe.db.exists("DocType", "BOM"):
        preparation_failures = preparation_variance_preflight(
            company=company,
            warehouse=warehouse,
        )
        if preparation_failures:
            _throw_cutover_block(preparation_failures[0])

    device_meta = frappe.get_meta("KoPOS Device")
    required_device_fields = (
        "inventory_report_received_at",
        "inventory_observed_at",
        "inventory_config_version",
        "inventory_catalog_version",
        "inventory_overlay_version",
        "inventory_overlay_hash",
        "inventory_sales_pending",
        "inventory_sales_syncing",
        "inventory_sales_failed",
        "inventory_sales_dead_letter",
        "inventory_commands_pending",
        "inventory_commands_syncing",
        "inventory_commands_failed",
        "inventory_commands_dead_letter",
    )
    if not all(device_meta.has_field(fieldname) for fieldname in required_device_fields):
        _throw_cutover_block("device_inventory_report_fields_not_migrated")
    device_rows = frappe.db.sql(
        """
        SELECT d.name, d.config_version, d.inventory_config_version,
               d.inventory_report_received_at, d.inventory_observed_at,
               d.inventory_catalog_version, d.inventory_overlay_version,
               d.inventory_overlay_hash, d.inventory_sales_pending,
               d.inventory_sales_syncing, d.inventory_sales_failed,
               d.inventory_sales_dead_letter, d.inventory_commands_pending,
               d.inventory_commands_syncing, d.inventory_commands_failed,
               d.inventory_commands_dead_letter
        FROM `tabKoPOS Device` d
        INNER JOIN `tabPOS Profile` p ON p.name = d.pos_profile
        WHERE d.enabled = 1 AND p.company = %s AND p.warehouse = %s
        """,
        (company, warehouse),
        as_dict=True,
    ) or []
    device_failures = device_activation_failures(
        device_rows,
        max_source_age_minutes=int(getattr(policy_doc, "max_source_age_minutes", 30) or 30),
        now=now_datetime(),
        overlay_is_current=lambda row: _device_overlay_current(dict(row)),
    )
    if device_failures:
        _throw_cutover_block(device_failures[0])

    role_failures = outlet_erp_role_failures(
        company=company,
        warehouse=warehouse,
    )
    if role_failures:
        _throw_cutover_block(role_failures[0])

    owner_failures = monitoring_owner_failures(
        getattr(frappe, "conf", None),
        automation_identity_ready=automation_identity_is_configured(company=company, warehouse=warehouse),
        purchase_review_owner=purchase_review_owner(company=company, warehouse=warehouse),
        # Cutover leaves the outlet in Review First. Monitoring, unattended
        # document identity, and Draft PO review ownership are checked when
        # their later capability is enabled, not when core stock work starts.
        require_monitor_destination=False,
        require_automation_identity=False,
        require_purchase_review_owner=False,
    )
    if owner_failures:
        _throw_cutover_block(owner_failures[0])

    # The same warehouse-scoped health read model used by the monitor is the
    # final rollout fence.  Do not let a director click through a dead-letter,
    # stale-device, unsafe-runtime, or other integrity-critical condition.
    # The schema existence guard keeps a reduced unit harness from pretending
    # it has a live rollout topology; a real site has this DocType after the
    # connector migration and therefore always consumes the health result.
    if frappe.db.exists("DocType", "FB Projection Log"):
        health = get_autopilot_health(warehouse=warehouse)
        if health_blocks_rollout(health, include_draft_purchase_order=False):
            reasons = critical_health_reasons(health) or ("draft_purchase_order_outbound_configuration",)
            _throw_cutover_block(f"health_critical:{reasons[0]}")

    automation_state = cstr(getattr(policy_doc, "automation_state", None)).strip() or "Review First"
    if automation_state not in {"Review First", "Paused"}:
        frappe.throw(
            _("Cutover activation does not enable Active automation; set the policy to Review First first"),
            frappe.ValidationError,
        )
    token = cstr(frappe.generate_hash(length=48)).strip()
    if not token:
        frappe.throw(_("A cutover token could not be generated"), frappe.ValidationError)
    policy_doc.opening_stock_reconciliation = reconciliation_name
    policy_doc.cutover_token = token
    policy_doc.cutover_at = now_datetime()
    policy_doc.save(ignore_permissions=True)
    frappe.db.commit()
    return _cutover_identity_response(policy_doc, status="activated")


@frappe.whitelist(methods=["POST"])
def start_inventory_tracking(*, warehouse: str) -> dict[str, Any]:
    """Start one outlet from its single checked opening count.

    The beginner page never asks a director to choose an internal policy or
    stock document. Ambiguous setup stays with Support rather than inviting a
    guess at an irreversible starting point.
    """

    _require_company_director("Start stock tracking")
    selected_warehouse = cstr(warehouse).strip()
    context = resolve_director_context(warehouse=selected_warehouse)
    if not context.company or context.warehouse != selected_warehouse:
        frappe.throw(_("Choose an outlet you can manage"), frappe.PermissionError)
    policies = frappe.get_all(
        "FB Inventory Policy",
        filters={"company": context.company, "warehouse": context.warehouse},
        fields=["name", "cutover_token", "opening_stock_reconciliation"],
        limit_page_length=2,
    )
    if len(policies) != 1:
        frappe.throw(
            _("Ask Support to finish this outlet's stock setup before you start."),
            frappe.ValidationError,
        )
    policy = policies[0]
    reconciliation_name = cstr(policy.get("opening_stock_reconciliation")).strip()
    if not cstr(policy.get("cutover_token")).strip() and not reconciliation_name:
        candidates = frappe.get_all(
            "Stock Reconciliation",
            filters={"company": context.company, "docstatus": 1},
            fields=["name"],
            order_by="posting_date desc, posting_time desc, modified desc",
            limit_page_length=51,
        )
        valid = []
        for row in candidates:
            name = cstr(row.get("name")).strip()
            if not name:
                continue
            document = frappe.get_doc("Stock Reconciliation", name)
            if not opening_reconciliation_failure(
                document,
                company=context.company,
                warehouse=context.warehouse,
            ):
                valid.append(name)
        if len(valid) != 1:
            frappe.throw(
                _("Ask Support to check the opening stock count for this outlet before you start."),
                frappe.ValidationError,
            )
        reconciliation_name = valid[0]
    result = _activate_inventory_cutover(
        policy=cstr(policy.get("name")).strip(),
        opening_stock_reconciliation=reconciliation_name or None,
    )
    return {
        "status": cstr(result.get("status")),
        "message": (
            _("Stock tracking was already on. Nothing changed.")
            if result.get("status") == "already_active"
            else _("Stock tracking has started.")
        ),
    }


def _cutover_identity_response(policy_doc: Any, *, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "policy": cstr(getattr(policy_doc, "name", None)),
        "company": cstr(getattr(policy_doc, "company", None)),
        "warehouse": cstr(getattr(policy_doc, "warehouse", None)),
        "automation_state": cstr(getattr(policy_doc, "automation_state", None)) or "Review First",
        "cutover_token": cstr(getattr(policy_doc, "cutover_token", None)),
        "cutover_at": _iso_with_offset(getattr(policy_doc, "cutover_at", None)),
        "opening_stock_reconciliation": cstr(getattr(policy_doc, "opening_stock_reconciliation", None)),
    }


def _throw_cutover_block(reason: str) -> None:
    frappe.log_error(
        message=cstr(reason)[:1000],
        title="JiJi stock tracking start was blocked",
    )
    normalized = cstr(reason).casefold()
    if "opening_stock" in normalized or "reconciliation" in normalized:
        message = _("Ask Support to check the opening stock count for this outlet.")
    elif "menu" in normalized or "recipe" in normalized:
        message = _("Finish setting up every product's ingredients before starting stock tracking.")
    elif "device" in normalized or "overlay" in normalized:
        message = _("Connect every tablet and let it finish updating, then try again.")
    elif "owner" in normalized or "routing" in normalized:
        message = _("Ask Support to choose who receives urgent stock problems.")
    else:
        message = _("JiJi found a setup problem. Ask Support to check this outlet before you start.")
    frappe.throw(
        message,
        frappe.ValidationError,
    )


@frappe.whitelist(methods=["GET"])
def get_menu_authoring_summary(company: str | None = None) -> dict[str, Any]:
    """Return a non-financial completion checklist for Company Directors."""

    _require_company_director("Menu authoring")
    context = resolve_director_context(company=cstr(company).strip() or None)
    selected_company = context.company or None
    companies = list(context.allowed_companies)
    company_selection_required = bool(companies) and not selected_company
    if not selected_company:
        return {
            "status": context.status,
            "selected_company": None,
            "company_selection_required": company_selection_required,
            "companies": companies,
            "ready": False,
        }

    item_meta = frappe.get_meta("Item")
    required_item_fields = (
        "custom_fb_item_role",
        "custom_fb_inventory_excluded",
        "custom_fb_inventory_exclusion_reason",
    )
    item_fields_ready = all(item_meta.has_field(fieldname) for fieldname in required_item_fields)
    item_fields = ["name", "item_code", "item_name", "is_sales_item", "disabled"]
    item_fields.extend(fieldname for fieldname in required_item_fields if item_meta.has_field(fieldname))
    recipe_schema_ready = bool(
        frappe.db.exists("DocType", "FB Recipe")
        and frappe.get_meta("FB Recipe").has_field("canonical_hash")
    )
    recipe_rows = (
        frappe.get_all(
            "FB Recipe",
            filters={"company": selected_company},
            fields=["name", "status", "sellable_item", "company", "canonical_hash"],
            limit_page_length=2_001,
        )
        if recipe_schema_ready
        else []
    )
    if len(recipe_rows) > 2_000:
        raise RuntimeError("Menu recipe summary is larger than its safe page")
    priced_items = frappe.get_all(
        "Item Price",
        filters={"price_list": ["in", list(context.allowed_price_lists) or [""]]},
        pluck="item_code",
        limit_page_length=2_001,
    )
    if len(priced_items) > 2_000:
        raise RuntimeError("Menu item summary is larger than its safe page")
    item_names = {
        cstr(item).strip() for item in priced_items if cstr(item).strip()
    } | {
        cstr(row.get("sellable_item")).strip()
        for row in recipe_rows
        if cstr(row.get("sellable_item")).strip()
    }
    if len(item_names) > 2_000:
        raise RuntimeError("Menu item summary is larger than its safe page")
    item_rows = (
        frappe.get_all(
            "Item",
            filters={"name": ["in", sorted(item_names)], "disabled": 0},
            fields=item_fields,
            limit_page_length=2_000,
        )
        if item_names
        else []
    )
    bom_count = (
        frappe.db.count("BOM", {"docstatus": 1, "item": ["in", sorted(item_names)]})
        if item_names and frappe.db.exists("DocType", "BOM")
        else 0
    )
    recipe_names = [cstr(row.get("name")) for row in recipe_rows]
    modifier_rows = (
        frappe.get_all(
            "FB Recipe Modifier Effect",
            filters={"parent": ["in", recipe_names], "parenttype": "FB Recipe"},
            pluck="modifier_group",
            limit_page_length=2_001,
        )
        if recipe_names
        else []
    )
    if len(modifier_rows) > 2_000:
        raise RuntimeError("Menu modifier summary is larger than its safe page")
    modifier_count = len(set(modifier_rows))
    profiles = director_pos_profiles(context)
    promotion_rows = (
        frappe.get_all(
            "KoPOS Promotion POS Profile",
            filters={"pos_profile": ["in", list(profiles)]},
            pluck="parent",
            limit_page_length=2_001,
        )
        if profiles
        else []
    )
    if len(promotion_rows) > 2_000:
        raise RuntimeError("Menu promotion summary is larger than its safe page")
    promotion_count = len(set(promotion_rows))
    return {
        "status": "ok",
        **summarize_menu_authoring(
            item_rows=item_rows,
            recipe_rows=recipe_rows,
            bom_count=int(bom_count or 0),
            modifier_count=int(modifier_count or 0),
            promotion_count=int(promotion_count or 0),
            company=selected_company,
            company_selection_required=company_selection_required,
            item_fields_ready=item_fields_ready,
            recipe_schema_ready=recipe_schema_ready,
        ),
        "companies": companies,
    }


@frappe.whitelist(methods=["GET"])
def get_menu_catalog_preview(pos_profile: str) -> dict[str, Any]:
    """Preview one outlet catalog without sending recipe or cost data to POS."""

    _require_company_director("Menu catalog preview")
    profile_name = cstr(pos_profile).strip()
    if not profile_name:
        frappe.throw(_("Choose an outlet first"), frappe.ValidationError)
    profile = frappe.get_cached_doc("POS Profile", profile_name)
    company = cstr(getattr(profile, "company", None)).strip()
    warehouse = cstr(getattr(profile, "warehouse", None)).strip()
    if not company or not warehouse:
        frappe.throw(_("Ask Support to finish setting up this outlet"), frappe.ValidationError)
    context = resolve_director_context(company=company, warehouse=warehouse)
    if profile_name not in director_pos_profiles(context):
        frappe.throw(_("You do not have access to this outlet."), frappe.PermissionError)
    try:
        items = get_items(
            warehouse=warehouse,
            selling_price_list=cstr(getattr(profile, "selling_price_list", None)).strip() or None,
            pos_profile=profile.as_dict(),
        )
        snapshots = get_item_recipe_snapshots_map(items, company=company)
        groups = get_item_modifier_groups_map(
            items,
            company=company,
            recipe_snapshots_by_item=snapshots,
        )
    except Exception as error:
        log_sanitized_error(
            title="JiJi outlet menu preview failed",
            error=error,
        )
        return {
            "status": "blocked",
            "reason": "The outlet menu cannot be checked until its recipes and customer choices are ready.",
        }
    missing = []
    for item in items:
        item_id = cstr(item.get("id") or item.get("item_code")).strip()
        if cint(item.get("inventory_excluded")) or item_id in snapshots:
            continue
        missing.append(
            cstr(item.get("item_name") or item.get("label")).strip()
            or cstr(frappe.db.get_value("Item", item_id, "item_name")).strip()
            or "A product"
        )
    return {
        "status": "ok",
        "saleable_items": len(items),
        "recipe_ready_items": len(snapshots),
        "explicit_exclusions": sum(1 for item in items if cint(item.get("inventory_excluded"))),
        "modifier_groups": len({group for group_ids in groups.values() for group in group_ids}),
        "missing_recipe_items": sorted(item for item in missing if item)[:12],
    }


@frappe.whitelist(methods=["GET"])
def get_menu_recipe_csv_template() -> dict[str, str]:
    """Return the fixed director recipe template without exposing cost data."""

    _require_system_manager_for_internal_tool()
    return {"filename": "jiji-recipe-components-template.csv", "content": csv_template()}


@frappe.whitelist(methods=["POST"])
def validate_menu_recipe_csv(*, csv_text: str) -> dict[str, Any]:
    """Dry-run a director spreadsheet and return row-level commissioning errors."""

    _require_system_manager_for_internal_tool()
    result = validate_recipe_csv(csv_text)
    return {
        "schema_version": "jiji-menu-recipe-csv-v1",
        "valid": bool(result.get("valid")),
        "recipe_count": len(result.get("recipes") or []),
        "errors": result.get("errors") or [],
        "recipes": result.get("recipes") or [],
    }


@frappe.whitelist(methods=["POST"])
def get_promotion_economics(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Calculate exact director-only promotion economics; never expose it to POS."""

    _require_company_director("Promotion economics")
    value = _parse_json_object(payload, "Promotion economics payload")
    promotion_name = cstr(value.get("promotion")).strip()
    if not promotion_name:
        frappe.throw(_("Choose a saved Promotion before checking economics"), frappe.ValidationError)
    unknown_fields = sorted(set(value) - {"promotion", "pos_profile", "scenarios"})
    if unknown_fields:
        frappe.throw(
            _("Promotion economics accepts only a saved Promotion, POS Profile and scenario volumes; Item and COGS data are server-owned"),
            frappe.ValidationError,
        )
    try:
        scenarios = normalize_scenarios(value.get("scenarios"))
    except PromotionEconomicsError as error:
        frappe.throw(cstr(error), frappe.ValidationError)
    try:
        promotion = frappe.get_doc("KoPOS Promotion", promotion_name)
        profile_name = _promotion_profile_name(
            promotion,
            cstr(value.get("pos_profile")).strip() or None,
        )
        _require_director_pos_profile(profile_name)
        items = _build_promotion_economics_items(
            promotion_name=promotion_name,
            pos_profile=profile_name,
        )
        source_hash = _promotion_economics_source_hash(
            promotion,
            profile_name=profile_name,
            resolved_items=items,
        )
        result = calculate_promotion_economics(items=items, scenarios=scenarios)
        result["actual_results"] = _promotion_actual_results(
            promotion=promotion,
            pos_profile=profile_name,
        )
        result["source"] = "promotion_document"
        result["economics_hash"] = source_hash
        below_cost = _promotion_has_loss(promotion, items, result)
        _record_promotion_economics_check(
            promotion,
            source_hash=source_hash,
            status="Blocked" if below_cost else "Ready",
            reason=(
                "Promotion price is below calculated COGS; a second Company Director approval with a reason is required"
                if below_cost
                else ""
            ),
        )
        result["publication_status"] = "Blocked" if below_cost else "Ready"
        return {
            "status": "ok",
            "economics": result,
            "economics_hash": source_hash,
            "publication_status": result["publication_status"],
        }
    except (PromotionEconomicsError, RecipeCompilerError, PromotionRuntimeError) as error:
        promotion = frappe.get_doc("KoPOS Promotion", promotion_name)
        profile_name = _promotion_profile_name(
            promotion,
            cstr(value.get("pos_profile")).strip() or None,
        )
        _require_director_pos_profile(profile_name)
        source_hash = _promotion_economics_source_hash(
            promotion,
            profile_name=profile_name,
            error=cstr(error),
        )
        _record_promotion_economics_check(
            promotion,
            source_hash=source_hash,
            status="Blocked",
            reason=cstr(error),
        )
        return {
            "status": "blocked",
            "reason": cstr(error),
            "planning_mode": "Review First",
            "economics_hash": source_hash,
            "publication_status": "Blocked",
        }


def _build_promotion_economics_items(
    *, promotion_name: str, pos_profile: str | None, promotion: Any = None,
) -> list[dict[str, Any]]:
    """Resolve a saved promotion into exact, director-only economics inputs.

    Prices and costs are resolved on the server. The browser may provide
    scenario volumes, but it cannot provide or override COGS, recipe, or
    valuation values.
    """

    if not frappe.db.exists("DocType", "KoPOS Promotion"):
        raise PromotionEconomicsError("KoPOS Promotion is not installed")
    promotion = promotion or frappe.get_doc("KoPOS Promotion", promotion_name)
    guided = int(getattr(promotion, "pricing_version", 1) or 1) == 2
    item_codes = _promotion_item_codes(promotion)
    if promotion.eligible_item_groups:
        group_names = {
            cstr(row.item_group).strip()
            for row in promotion.eligible_item_groups
            if cstr(row.item_group).strip()
        }
        item_codes.update(
            cstr(row.name).strip()
            for row in frappe.get_all(
                "Item",
                filters={"item_group": ["in", sorted(group_names)], "disabled": 0},
                fields=["name"],
                limit_page_length=10_000,
            )
            if cstr(row.name).strip()
        )
    if not item_codes:
        raise PromotionEconomicsError("select at least one eligible Item before checking economics")

    profile_name = _promotion_profile_name(promotion, pos_profile)
    if profile_name:
        profile = frappe.get_cached_doc("POS Profile", profile_name).as_dict()
    else:
        profile = get_default_pos_profile()
    warehouse = cstr((profile or {}).get("warehouse")).strip()
    price_list = cstr((profile or {}).get("selling_price_list")).strip()
    tax_rate = _promotion_tax_rate(profile)
    max_source_age_minutes = _promotion_source_age_limit(warehouse)
    company = cstr((profile or {}).get("company")).strip()

    item_meta = frappe.get_meta("Item")
    item_fields = ["name", "standard_rate", "stock_uom", "item_group"]
    if item_meta.has_field("valuation_rate"):
        item_fields.append("valuation_rate")
    item_rows = frappe.get_all(
        "Item",
        filters={"name": ["in", sorted(item_codes)], "disabled": 0},
        fields=item_fields,
        limit_page_length=10_000,
    )
    item_by_code = {cstr(row.name).strip(): row for row in item_rows}
    resolved: list[dict[str, Any]] = []
    for item_code in sorted(item_codes):
        item = item_by_code.get(item_code)
        if not item:
            raise PromotionEconomicsError(f"eligible Item {item_code} does not exist or is disabled")
        baseline_price_sen = _promotion_price_sen(item_code, item, price_list)
        recipe_rows = frappe.get_all(
            "FB Recipe",
            filters={"sellable_item": item_code, "status": "Active"},
            fields=["name", "effective_from", "modified"],
            order_by="effective_from desc, modified desc",
            limit_page_length=1,
        )
        if not recipe_rows:
            raise PromotionEconomicsError(f"{item_code} has no active recipe; publication is blocked")
        recipe_doc = frappe.get_doc("FB Recipe", recipe_rows[0].name)
        recipe = {
            "yield_qty": recipe_doc.yield_qty,
            "yield_qty_decimal": getattr(recipe_doc, "yield_qty_decimal", None),
            "default_serving_qty": recipe_doc.default_serving_qty,
            "default_serving_qty_decimal": getattr(recipe_doc, "default_serving_qty_decimal", None),
            "components": [
                row.as_dict()
                for row in (recipe_doc.components or [])
                if int(row.affects_cogs if row.affects_cogs is not None else 1)
            ],
        }
        try:
            components = compile_recipe_components(recipe)
        except RecipeCompilerError:
            raise
        component_costs: list[dict[str, Any]] = []
        cogs_sen = Decimal("0")
        for component_item, quantity in components.items():
            rate = _component_valuation_rate(
                component_item,
                warehouse,
                max_source_age_minutes=max_source_age_minutes,
            )
            rate_sen = _currency_to_sen(rate, f"{component_item} valuation rate")
            cogs_sen += quantity * rate_sen
            component = {
                "item": component_item,
                "qty": str(quantity),
                "prepared": _prepared_component_evidence(
                    item=component_item,
                    company=company,
                ),
                "inventory": _promotion_inventory_evidence(
                    item=component_item,
                    warehouse=warehouse,
                ),
            }
            component_costs.append(component)
        promoted_price_sen = int(baseline_price_sen) if guided else _promotion_discounted_price_sen(
            promotion,
            baseline_price_sen,
        )
        tax_sen = _tax_for_net_price(promoted_price_sen, tax_rate)
        baseline_tax_sen = _tax_for_net_price(baseline_price_sen, tax_rate)
        resolved.append({
            "item": item_code,
            "units": 1,
            "baseline_price_sen": int(baseline_price_sen),
            "baseline_net_revenue_sen": int(baseline_price_sen),
            "promoted_price_sen": promoted_price_sen,
            "net_revenue_sen": promoted_price_sen,
            "tax_sen": tax_sen,
            "baseline_tax_sen": baseline_tax_sen,
            "cogs_sen": int(cogs_sen.to_integral_value(rounding="ROUND_CEILING")) if guided else int(cogs_sen),
            "item_group": cstr(getattr(item, "item_group", None) if not isinstance(item, dict) else item.get("item_group")),
            "components": component_costs,
        })
    if guided:
        from kopos_connector.pricing.adapter import promotion_cost_cases

        resolved = promotion_cost_cases(promotion, profile_name, resolved)
        for row in resolved:
            row["tax_sen"] = _tax_for_net_price(row["net_revenue_sen"], tax_rate)
            row["baseline_tax_sen"] = _tax_for_net_price(row["baseline_price_sen"], tax_rate)
    return resolved


def _promotion_has_loss(promotion, items, result):
    if int(getattr(promotion, "pricing_version", 1) or 1) == 2:
        return any(row["net_revenue_sen"] < row["cogs_sen"] for row in items)
    return int(result["gross_profit_sen"]) < 0


def review_promotion_economics(promotion, pos_profile):
    """Read-only editor check; activation records this evidence atomically."""
    try:
        items = _build_promotion_economics_items(promotion_name=promotion.name,
                                                pos_profile=pos_profile, promotion=promotion)
        source_hash = _promotion_economics_source_hash(promotion, profile_name=pos_profile, resolved_items=items)
        result = calculate_promotion_economics(items=items)
        reason = "One or more qualifying baskets sell below ingredient cost" if _promotion_has_loss(promotion, items, result) else ""
    except (PromotionEconomicsError, RecipeCompilerError, PromotionRuntimeError) as error:
        source_hash = _promotion_economics_source_hash(promotion, profile_name=pos_profile, error=cstr(error))
        reason = cstr(error)
    approved = (cstr(getattr(promotion, "economics_status", "")) == "Override approved"
                and cstr(getattr(promotion, "economics_override_hash", "")) == source_hash
                and bool(cstr(getattr(promotion, "economics_override_reason", "")))
                and bool(cstr(getattr(promotion, "economics_override_by", "")))
                and cstr(getattr(promotion, "economics_override_by", "")) != cstr(getattr(promotion, "economics_checked_by", "")))
    return {"source_hash": source_hash, "ready": not reason or approved, "override": approved, "reason": reason}


def _promotion_tax_rate(profile: dict[str, Any] | None) -> Decimal:
    """Resolve the same tax authority used by the POS invoice path.

    Item Price values in this connector are tax-exclusive; the POS captures
    the tax as a separate total.  Requiring an explicit POS Profile tax
    configuration here prevents a director report from silently treating an
    unknown tax setup as zero tax.
    """

    data = profile or {}
    enabled = data.get("custom_kopos_enable_sst")
    raw_rate = data.get("custom_kopos_sst_rate")
    if enabled in (None, ""):
        raise PromotionEconomicsError(
            "net revenue tax evidence is missing; configure the POS Profile SST rate before publication"
        )
    if not cint(enabled):
        return Decimal("0")
    if raw_rate in (None, ""):
        raise PromotionEconomicsError(
            "net revenue tax evidence is missing; configure the POS Profile SST rate before publication"
        )
    try:
        rate = Decimal(str(raw_rate)) / Decimal("100")
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PromotionEconomicsError("POS Profile SST rate is invalid") from error
    if not rate.is_finite() or rate < 0 or rate > 1:
        raise PromotionEconomicsError("POS Profile SST rate is invalid")
    return rate


def _tax_for_net_price(price_sen: int | Decimal, rate: Decimal) -> int:
    return int(
        (Decimal(str(price_sen)) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def _promotion_source_age_limit(warehouse: str) -> int:
    if warehouse and frappe.db.exists("DocType", "FB Inventory Policy"):
        row = frappe.get_all(
            "FB Inventory Policy",
            filters={"warehouse": warehouse},
            fields=["max_source_age_minutes"],
            order_by="modified desc",
            limit_page_length=1,
        )
        if row and row[0].get("max_source_age_minutes") not in (None, ""):
            try:
                return max(1, int(row[0].get("max_source_age_minutes")))
            except (TypeError, ValueError):
                pass
    return 30


def _prepared_component_evidence(*, item: str, company: str) -> dict[str, Any] | None:
    if not item or not frappe.db.exists("DocType", "BOM"):
        return None
    meta = frappe.get_meta("BOM")
    optional = [
        fieldname
        for fieldname in (
            "custom_kopos_batch_qty",
            "custom_kopos_min_ready_qty",
            "custom_kopos_preparation_lead_minutes",
        )
        if meta.has_field(fieldname)
    ]
    fields = ["name", "quantity"] + optional
    filters: dict[str, Any] = {
        "item": item,
        "docstatus": 1,
        "is_active": 1,
    }
    if company:
        filters["company"] = company
    rows = frappe.get_all(
        "BOM",
        filters=filters,
        fields=fields,
        order_by="modified desc, name desc",
        limit_page_length=1,
    )
    if not rows:
        return None
    row = rows[0]
    result: dict[str, Any] = {
        "bom": cstr(row.get("name")).strip(),
        "batch_qty": row.get("custom_kopos_batch_qty") or row.get("quantity"),
        "min_ready_qty": row.get("custom_kopos_min_ready_qty"),
        "lead_minutes": row.get("custom_kopos_preparation_lead_minutes"),
    }
    return result


def _promotion_inventory_evidence(*, item: str, warehouse: str) -> dict[str, Any] | None:
    if not item or not warehouse or not frappe.db.exists("DocType", "Bin"):
        return None
    meta = frappe.get_meta("Bin")
    fields = ["actual_qty", "modified"]
    if meta.has_field("reserved_qty"):
        fields.append("reserved_qty")
    row = frappe.db.get_value(
        "Bin",
        {"item_code": item, "warehouse": warehouse},
        fields,
        as_dict=True,
    )
    if not row:
        return None
    return {
        "usable_stock": str(row.get("actual_qty") or "0"),
        "reserved_stock": str(row.get("reserved_qty") or "0"),
        "modified": cstr(row.get("modified")),
    }


def _promotion_actual_results(*, promotion: Any, pos_profile: str | None) -> dict[str, Any]:
    """Read post-cutover promotion attribution and submitted ERP valuation.

    FB Order promotion payloads remain the attribution authority. Actual COGS is
    deliberately read from the immutable resolved ingredient vector and the
    submitted Material Issue created by the inventory worker; current Bin or
    Item valuation is suitable for planning, not historical promotion results.
    """

    promotion_id = cstr(getattr(promotion, "name", "")).strip()
    if not promotion_id or not frappe.db.exists("DocType", "FB Order"):
        return {
            "status": "not_available",
            "reason": "promotion_order_provenance_authority_missing",
        }

    profile = None
    if pos_profile:
        profile = frappe.get_cached_doc("POS Profile", pos_profile)
    else:
        profile = get_default_pos_profile()
    profile_data = profile.as_dict() if hasattr(profile, "as_dict") else (profile or {})
    warehouse = cstr(
        profile_data.get("warehouse")
        if isinstance(profile_data, dict)
        else getattr(profile, "warehouse", None)
    ).strip()
    company = cstr(
        profile_data.get("company")
        if isinstance(profile_data, dict)
        else getattr(profile, "company", None)
    ).strip()
    if not warehouse:
        return {
            "status": "not_available",
            "reason": "The selected outlet is missing its stock location.",
        }

    cutover = _promotion_actual_cutover(warehouse, company=company)
    if cutover is None:
        return {
            "status": "not_available",
            "reason": "Stock tracking has not started for the selected outlet.",
            "warehouse": warehouse,
        }

    meta = frappe.get_meta("FB Order")
    fields = [
        "name",
        "promotion_payload_json",
        "promotion_reconciliation_status",
        "net_total",
        "tax_total",
        "tax_rate",
    ]
    for fieldname in ("sale_datetime", "booth_warehouse", "docstatus"):
        if meta.has_field(fieldname):
            fields.append(fieldname)
    filters: dict[str, Any] = {"booth_warehouse": warehouse}
    if meta.has_field("docstatus"):
        filters["docstatus"] = 1
    if meta.has_field("sale_datetime"):
        filters["sale_datetime"] = [">=", cutover]
    rows = frappe.get_all(
        "FB Order",
        filters=filters,
        fields=fields,
        order_by="sale_datetime asc, name asc",
        limit_page_length=5000,
    )
    evidence: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get("promotion_payload_json")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = None
        if not isinstance(payload, dict):
            continue
        promoted_line_quantities = _promotion_line_quantities(payload, promotion_id)
        if not promoted_line_quantities:
            has_target_allocation = any(
                isinstance(allocation, dict)
                and cstr(allocation.get("promotion_id")).strip() == promotion_id
                for item in (payload.get("items") or [])
                if isinstance(item, dict)
                for allocation in (item.get("promotion_allocations") or [])
            )
            if has_target_allocation:
                evidence.append({
                    "promotion_payload": payload,
                    "tax_rate": row.get("tax_rate"),
                    "actual_revenue_reason": "promotion allocation quantity evidence is missing or invalid",
                    "actual_cogs_status": "not_available",
                    "actual_cogs_reason": "Ingredient use is not available for this sale.",
                })
            continue
        line_rows = _promotion_order_line_rows(cstr(row.get("name")))
        enriched_payload, revenue_reason = _promotion_payload_with_line_evidence(
            payload,
            line_rows,
            promotion_id,
        )
        record: dict[str, Any] = {
            "promotion_payload": payload,
            "actual_revenue_payload": enriched_payload,
            "actual_revenue_reason": revenue_reason,
            "actual_cogs_status": "not_available",
            "actual_cogs_reason": "Ingredient use is not available for this sale.",
            "tax_rate": row.get("tax_rate"),
        }
        reconciliation_status = cstr(row.get("promotion_reconciliation_status")).strip()
        if reconciliation_status and reconciliation_status != "matched":
            record["actual_cogs_reason"] = (
                "promotion attribution is not server-reconciled; review the order before using actual economics"
            )
            evidence.append(record)
            continue
        try:
            record["net_total_sen"] = _currency_to_sen(
                row.get("net_total"),
                "FB Order net total",
            )
            record["tax_total_sen"] = _currency_to_sen(
                row.get("tax_total"),
                "FB Order tax total",
            )
        except PromotionEconomicsError as error:
            record["actual_cogs_reason"] = cstr(error)
            evidence.append(record)
            continue
        cogs_evidence = _promotion_order_cogs_evidence(
            order_name=cstr(row.get("name")),
            promoted_line_quantities=promoted_line_quantities,
            line_rows=line_rows,
        )
        if cogs_evidence.get("status") == "available":
            record["actual_cogs_status"] = "available"
            record["actual_cogs_sen"] = cogs_evidence.get("cogs_sen")
            record["actual_cogs_reason"] = ""
        else:
            record["actual_cogs_reason"] = cstr(
                cogs_evidence.get("reason")
                or "Ingredient use or its cost is not available for this sale."
            )
        evidence.append(record)

    result = summarize_actual_promotion_results(
        records=evidence,
        promotion_id=promotion_id,
    )
    result["warehouse"] = warehouse
    result["cutover_at"] = get_datetime(cutover).isoformat()
    if pos_profile:
        result["pos_profile"] = pos_profile
    return result


def _promotion_actual_cutover(warehouse: str, *, company: str = "") -> Any | None:
    if not warehouse or not frappe.db.exists("DocType", "FB Inventory Policy"):
        return None
    filters: dict[str, Any] = {"warehouse": warehouse}
    if company:
        filters["company"] = company
    rows = frappe.get_all(
        "FB Inventory Policy",
        filters=filters,
        fields=["cutover_at", "cutover_token"],
        order_by="modified desc",
        limit_page_length=1,
    )
    if not rows or not cstr(rows[0].get("cutover_token")).strip() or not rows[0].get("cutover_at"):
        return None
    try:
        return get_datetime(rows[0].get("cutover_at"))
    except (TypeError, ValueError, OverflowError):
        return None


def _promotion_line_quantities(payload: dict[str, Any], promotion_id: str) -> dict[str, Any]:
    quantities: dict[str, Decimal] = {}
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        line_id = cstr(item.get("line_id")).strip()
        if not line_id:
            continue
        for allocation in item.get("promotion_allocations") or []:
            if not isinstance(allocation, dict) or cstr(allocation.get("promotion_id")).strip() != promotion_id:
                continue
            quantity = allocation.get("quantity")
            if quantity in (None, ""):
                continue
            try:
                quantities[line_id] = quantities.get(line_id, Decimal("0")) + Decimal(str(quantity))
            except (InvalidOperation, TypeError, ValueError):
                continue
    return quantities


def _promotion_order_line_rows(order_name: str) -> list[Any]:
    if not order_name or not frappe.db.exists("DocType", "FB Order Line"):
        return []
    return frappe.get_all(
        "FB Order Line",
        filters={"parent": order_name, "parenttype": "FB Order"},
        fields=["name", "line_id", "resolved_sale", "qty", "unit_price", "line_total"],
        limit_page_length=5000,
    )


def _promotion_payload_with_line_evidence(
    payload: dict[str, Any],
    line_rows: list[Any],
    promotion_id: str,
) -> tuple[dict[str, Any], str | None]:
    """Add server-owned line amounts to a non-authoritative read copy."""

    rows_by_line = {
        cstr(row.get("line_id")).strip(): row
        for row in line_rows
        if cstr(row.get("line_id")).strip()
    }
    enriched = dict(payload)
    enriched_items: list[Any] = []
    missing_reason: str | None = None
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            enriched_items.append(item)
            continue
        copy = dict(item)
        target_allocations = [
            allocation
            for allocation in (item.get("promotion_allocations") or [])
            if isinstance(allocation, dict)
            and cstr(allocation.get("promotion_id")).strip() == promotion_id
        ]
        if target_allocations:
            line_id = cstr(item.get("line_id")).strip()
            line = rows_by_line.get(line_id)
            if not line:
                if any(item.get(fieldname) in (None, "") for fieldname in ("qty", "unit_price_sen", "line_total_sen")):
                    missing_reason = f"promoted line {line_id or '(unknown)'} revenue evidence is missing"
            else:
                try:
                    copy["qty"] = cstr(line.get("qty"))
                    copy["unit_price_sen"] = int(_currency_to_sen(
                        line.get("unit_price"),
                        f"FB Order line {line_id} unit price",
                    ))
                    copy["line_total_sen"] = int(_currency_to_sen(
                        line.get("line_total"),
                        f"FB Order line {line_id} total",
                    ))
                except PromotionEconomicsError as error:
                    missing_reason = cstr(error)
        enriched_items.append(copy)
    enriched["items"] = enriched_items
    return enriched, missing_reason


def _promotion_order_cogs_evidence(
    *,
    order_name: str,
    promoted_line_quantities: dict[str, Any],
    line_rows: list[Any] | None = None,
) -> dict[str, Any]:
    if not order_name or not frappe.db.exists("DocType", "FB Resolved Sale"):
        return {"status": "not_available", "reason": "resolved ingredient consumption authority is missing"}
    sales_rows = frappe.get_all(
        "FB Resolved Sale",
        filters={"fb_order": order_name},
        fields=[
            "name",
            "fb_order_line",
            "backend_line_uuid",
            "qty",
            "booth_warehouse",
            "stock_entry_issue",
        ],
        limit_page_length=5000,
    )
    if not sales_rows:
        return {"status": "not_available", "reason": "Ingredient use has not been recorded for these sales yet."}
    if line_rows is None:
        line_rows = _promotion_order_line_rows(order_name)
    line_id_by_sale = {
        cstr(line.get("resolved_sale")).strip(): cstr(line.get("line_id")).strip()
        for line in line_rows
        if cstr(line.get("resolved_sale")).strip() and cstr(line.get("line_id")).strip()
    }
    line_id_by_line_name = {
        cstr(line.get("name")).strip(): cstr(line.get("line_id")).strip()
        for line in line_rows
        if cstr(line.get("name")).strip() and cstr(line.get("line_id")).strip()
    }
    resolved_sales: list[dict[str, Any]] = []
    entry_names: set[str] = set()
    for row in sales_rows:
        sale_name = cstr(row.get("name")).strip()
        line_id = line_id_by_sale.get(sale_name) or line_id_by_line_name.get(
            cstr(row.get("fb_order_line")).strip()
        )
        if not line_id:
            # A direct line link is still useful for data created before the
            # child-row mapping was introduced; it is only accepted when it
            # is the exact promotion line identity.
            backend_line_uuid = cstr(row.get("backend_line_uuid")).strip()
            if backend_line_uuid in promoted_line_quantities:
                line_id = backend_line_uuid
        if not line_id:
            continue
        stock_entry = cstr(row.get("stock_entry_issue")).strip()
        if stock_entry:
            entry_names.add(stock_entry)
        try:
            sale_doc = frappe.get_doc("FB Resolved Sale", sale_name)
        except Exception:
            return {
                "status": "not_available",
                "reason": f"resolved sale {sale_name} could not be read",
            }
        components = []
        for component in list(getattr(sale_doc, "resolved_components", None) or []):
            components.append(
                {
                    "item": cstr(getattr(component, "item", None)),
                    "qty": getattr(component, "qty", None),
                    "qty_decimal": getattr(component, "qty_decimal", None),
                    "stock_qty": getattr(component, "stock_qty", None),
                    "stock_qty_decimal": getattr(component, "stock_qty_decimal", None),
                    "uom": cstr(getattr(component, "uom", None)),
                    "stock_uom": cstr(getattr(component, "stock_uom", None)),
                    "warehouse": cstr(getattr(component, "warehouse", None)),
                    "affects_stock": int(getattr(component, "affects_stock", 0) or 0),
                    "affects_cogs": int(getattr(component, "affects_cogs", 0) or 0),
                }
            )
        resolved_sales.append(
            {
                "line_id": line_id,
                "qty": row.get("qty"),
                "warehouse": cstr(row.get("booth_warehouse")),
                "stock_entry": stock_entry,
                "components": components,
            }
        )
    if not resolved_sales:
        return {"status": "not_available", "reason": "promotion line cannot be linked to resolved ingredient consumption"}

    stock_entries: dict[str, dict[str, Any]] = {}
    for entry_name in sorted(entry_names):
        try:
            entry_doc = frappe.get_doc("Stock Entry", entry_name)
        except Exception:
            return {
                "status": "not_available",
                "reason": f"submitted Stock Entry {entry_name} could not be read",
            }
        if cstr(getattr(entry_doc, "custom_fb_order", "")).strip() != order_name:
            return {"status": "not_available", "reason": f"Stock Entry {entry_name} has the wrong FB Order identity"}
        rows = []
        for item in list(getattr(entry_doc, "items", None) or []):
            row: dict[str, Any] = {
                "item_code": cstr(getattr(item, "item_code", None)),
                "s_warehouse": cstr(getattr(item, "s_warehouse", None)),
                "stock_uom": cstr(getattr(item, "stock_uom", None) or getattr(item, "uom", None)),
                "uom": cstr(getattr(item, "uom", None)),
                "qty": getattr(item, "qty", None),
                "transfer_qty": getattr(item, "transfer_qty", None),
            }
            try:
                if getattr(item, "basic_amount", None) not in (None, ""):
                    row["basic_amount_sen"] = _currency_to_exact_sen(
                        getattr(item, "basic_amount"),
                        f"Stock Entry {entry_name} {row['item_code']} basic amount",
                    )
                elif getattr(item, "amount", None) not in (None, ""):
                    row["basic_amount_sen"] = _currency_to_exact_sen(
                        getattr(item, "amount"),
                        f"Stock Entry {entry_name} {row['item_code']} amount",
                    )
                if getattr(item, "basic_rate", None) not in (None, ""):
                    row["basic_rate_sen"] = _currency_to_exact_sen(
                        getattr(item, "basic_rate"),
                        f"Stock Entry {entry_name} {row['item_code']} basic rate",
                    )
                elif getattr(item, "valuation_rate", None) not in (None, ""):
                    row["basic_rate_sen"] = _currency_to_exact_sen(
                        getattr(item, "valuation_rate"),
                        f"Stock Entry {entry_name} {row['item_code']} valuation rate",
                    )
            except PromotionEconomicsError as error:
                return {"status": "not_available", "reason": cstr(error)}
            rows.append(row)
        stock_entries[entry_name] = {
            "name": entry_name,
            "docstatus": getattr(entry_doc, "docstatus", None),
            "stock_entry_type": cstr(getattr(entry_doc, "stock_entry_type", None)),
            "purpose": cstr(getattr(entry_doc, "purpose", None)),
            "items": rows,
        }
    return calculate_actual_cogs_from_stock_entries(
        resolved_sales=resolved_sales,
        stock_entries=stock_entries,
        promoted_line_quantities=promoted_line_quantities,
    )


def _promotion_item_codes(promotion: Any) -> set[str]:
    return {
        cstr(row.item_code).strip()
        for row in (promotion.eligible_items or [])
        if cstr(row.item_code).strip()
    }


def _promotion_profile_name(promotion: Any, requested_profile: str | None) -> str:
    profile_name = cstr(requested_profile).strip()
    if not profile_name and promotion.eligible_pos_profiles:
        profile_name = cstr(promotion.eligible_pos_profiles[0].pos_profile).strip()
    return profile_name


def _promotion_economics_source_hash(
    promotion: Any,
    *,
    profile_name: str,
    resolved_items: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> str:
    """Hash Promotion, recipe, price and valuation evidence for one review.

    The hash is intentionally based on source evidence, not a caller-provided
    COGS number.  A later recipe, price, valuation or Promotion edit therefore
    invalidates a previous economics review and any exception approval.
    """

    profile = {}
    if profile_name:
        profile_doc = frappe.get_cached_doc("POS Profile", profile_name)
        profile = {
            "name": profile_name,
            "warehouse": cstr(getattr(profile_doc, "warehouse", "")),
            "selling_price_list": cstr(getattr(profile_doc, "selling_price_list", "")),
        }
    item_codes = sorted(_promotion_item_codes(promotion))
    if promotion.eligible_item_groups:
        groups = sorted(
            cstr(row.item_group).strip()
            for row in promotion.eligible_item_groups
            if cstr(row.item_group).strip()
        )
        item_codes.extend(
            sorted(
                cstr(row.name).strip()
                for row in frappe.get_all(
                    "Item",
                    filters={"item_group": ["in", groups], "disabled": 0},
                    fields=["name"],
                    limit_page_length=10_000,
                )
                if cstr(row.name).strip()
            )
        )

    source_items: list[dict[str, Any]] = []
    warehouse = cstr(profile.get("warehouse")).strip()
    for item_code in sorted(set(item_codes)):
        item_row = frappe.db.get_value(
            "Item",
            item_code,
            ["modified", "standard_rate", "valuation_rate", "disabled"],
            as_dict=True,
        ) or {}
        recipe_rows = (
            frappe.get_all(
                "FB Recipe",
                filters={"sellable_item": item_code, "status": "Active"},
                fields=["name", "effective_from", "modified", "canonical_hash"],
                order_by="effective_from desc, modified desc",
                limit_page_length=1,
            )
            if frappe.db.exists("DocType", "FB Recipe")
            else []
        )
        price_rows = (
            frappe.get_all(
                "Item Price",
                filters={"item_code": item_code, "price_list": profile.get("selling_price_list"), "selling": 1},
                fields=["price_list_rate", "valid_from", "valid_upto", "modified"],
                order_by="valid_from desc, modified desc",
                limit_page_length=20,
            )
            if profile.get("selling_price_list") and frappe.db.exists("DocType", "Item Price")
            else []
        )
        recipe_evidence: list[dict[str, Any]] = []
        for recipe_row in recipe_rows:
            recipe_doc = frappe.get_doc("FB Recipe", recipe_row.name)
            component_evidence = []
            for component in recipe_doc.components or []:
                component_item = cstr(getattr(component, "item_code", "")).strip()
                if not component_item:
                    continue
                component_evidence.append(
                    {
                        "item": component_item,
                        "qty": cstr(getattr(component, "qty", "")),
                        "qty_decimal": cstr(getattr(component, "qty_decimal", "")),
                        "uom": cstr(getattr(component, "uom", "")),
                        "stock_qty": cstr(getattr(component, "stock_qty", "")),
                        "stock_qty_decimal": cstr(getattr(component, "stock_qty_decimal", "")),
                        "stock_uom": cstr(getattr(component, "stock_uom", "")),
                        "stock_conversion_factor": cstr(getattr(component, "stock_conversion_factor", "")),
                        "stock_conversion_factor_decimal": cstr(getattr(component, "stock_conversion_factor_decimal", "")),
                        "loss_factor_pct": cstr(getattr(component, "loss_factor_pct", "")),
                        "loss_factor_pct_decimal": cstr(getattr(component, "loss_factor_pct_decimal", "")),
                        "affects_cogs": int(getattr(component, "affects_cogs", 1) or 0),
                        "valuation": _valuation_source_evidence(component_item, warehouse),
                    }
                )
            recipe_evidence.append(
                {
                    "name": cstr(recipe_row.name),
                    "effective_from": cstr(getattr(recipe_row, "effective_from", "")),
                    "modified": cstr(getattr(recipe_row, "modified", "")),
                    "canonical_hash": cstr(getattr(recipe_row, "canonical_hash", "")),
                    "yield_qty": cstr(getattr(recipe_doc, "yield_qty", "")),
                    "yield_qty_decimal": cstr(getattr(recipe_doc, "yield_qty_decimal", "")),
                    "yield_uom": cstr(getattr(recipe_doc, "yield_uom", "")),
                    "default_serving_qty": cstr(getattr(recipe_doc, "default_serving_qty", "")),
                    "default_serving_qty_decimal": cstr(getattr(recipe_doc, "default_serving_qty_decimal", "")),
                    "default_serving_uom": cstr(getattr(recipe_doc, "default_serving_uom", "")),
                    "components": component_evidence,
                }
            )
        source_items.append(
            {
                "item": item_code,
                "item_source": item_row,
                "prices": price_rows,
                "recipes": recipe_evidence,
            }
        )
    return economics_source_hash(
        {
            "promotion": {
                **({"pricing_version": 2, "schedule_json": cstr(getattr(promotion, "schedule_json", ""))}
                   if int(getattr(promotion, "pricing_version", 1) or 1) == 2 else {}),
                "name": cstr(getattr(promotion, "name", "")),
                "display_label": cstr(getattr(promotion, "display_label", "")),
                "customer_message": cstr(getattr(promotion, "customer_message", "")),
                "is_active": int(getattr(promotion, "is_active", 0) or 0),
                "promotion_type": cstr(getattr(promotion, "promotion_type", "")),
                "activation_mode": cstr(getattr(promotion, "activation_mode", "")),
                "offline_allowed": int(getattr(promotion, "offline_allowed", 0) or 0),
                "priority": cstr(getattr(promotion, "priority", "")),
                "stacking_policy": cstr(getattr(promotion, "stacking_policy", "")),
                "eligible_scope_mode": cstr(getattr(promotion, "eligible_scope_mode", "")),
                "repeat_mode": cstr(getattr(promotion, "repeat_mode", "")),
                "discount_type": cstr(getattr(promotion, "discount_type", "")),
                "discount_value": cstr(getattr(promotion, "discount_value", "")),
                "buy_qty": cstr(getattr(promotion, "buy_qty", "")),
                "discount_qty": cstr(getattr(promotion, "discount_qty", "")),
                "discount_target": cstr(getattr(promotion, "discount_target", "")),
                "comparison_basis": cstr(getattr(promotion, "comparison_basis", "")),
                "discount_basis": cstr(getattr(promotion, "discount_basis", "")),
                "modifier_policy": cstr(getattr(promotion, "modifier_policy", "")),
                "min_qty": cstr(getattr(promotion, "min_qty", "")),
                "min_amount": cstr(getattr(promotion, "min_amount", "")),
                "outlet_scope_mode": cstr(getattr(promotion, "outlet_scope_mode", "")),
                "valid_from": cstr(getattr(promotion, "valid_from", "")),
                "valid_upto": cstr(getattr(promotion, "valid_upto", "")),
                "eligible_items": sorted(_promotion_item_codes(promotion)),
                "eligible_item_groups": sorted(
                    cstr(row.item_group).strip()
                    for row in (promotion.eligible_item_groups or [])
                    if cstr(row.item_group).strip()
                ),
                "eligible_pos_profiles": sorted(
                    cstr(row.pos_profile).strip()
                    for row in (promotion.eligible_pos_profiles or [])
                    if cstr(row.pos_profile).strip()
                ),
            },
            "profile": profile,
            "items": source_items,
            "resolved_items": resolved_items or [],
            "error": error or "",
        }
    )


def _valuation_source_evidence(item_code: str, warehouse: str) -> dict[str, Any]:
    if warehouse and frappe.db.exists("DocType", "Bin"):
        bin_row = frappe.db.get_value(
            "Bin",
            {"item_code": item_code, "warehouse": warehouse},
            ["valuation_rate", "modified"],
            as_dict=True,
        )
        if bin_row:
            return {
                "source": "Bin",
                "valuation_rate": cstr(getattr(bin_row, "valuation_rate", None) if not isinstance(bin_row, dict) else bin_row.get("valuation_rate")),
                "modified": cstr(getattr(bin_row, "modified", None) if not isinstance(bin_row, dict) else bin_row.get("modified")),
            }
    item_row = frappe.db.get_value(
        "Item",
        item_code,
        ["valuation_rate", "modified"],
        as_dict=True,
    ) or {}
    return {
        "source": "Item",
        "valuation_rate": cstr(getattr(item_row, "valuation_rate", None) if not isinstance(item_row, dict) else item_row.get("valuation_rate")),
        "modified": cstr(getattr(item_row, "modified", None) if not isinstance(item_row, dict) else item_row.get("modified")),
    }


def _record_promotion_economics_check(
    promotion: Any,
    *,
    source_hash: str,
    status: str,
    reason: str,
    commit: bool = True,
) -> None:
    """Persist the latest server-side economics evidence on the Promotion."""

    actor = cstr(getattr(frappe.session, "user", "")).strip()
    changed = any(
        cstr(getattr(promotion, fieldname, "")).strip() != value
        for fieldname, value in (
            ("economics_status", status),
            ("economics_source_hash", source_hash),
            ("economics_block_reason", reason),
            ("economics_checked_by", actor),
        )
    )
    if not changed:
        return
    promotion.economics_status = status
    promotion.economics_source_hash = source_hash
    promotion.economics_checked_at = now_datetime()
    promotion.economics_checked_by = actor
    promotion.economics_block_reason = reason
    # A new check supersedes an earlier one-time exception.  The exception is
    # bound to the source hash and must be granted again after any change.
    promotion.economics_override_hash = ""
    promotion.economics_override_reason = ""
    promotion.economics_override_by = ""
    promotion.economics_override_at = None
    promotion.flags.allow_economics_review_write = True
    promotion.save(ignore_permissions=True)
    if commit:
        frappe.db.commit()


def validate_promotion_economics_for_publication(
    promotion: Any,
    *,
    pos_profile: str,
) -> dict[str, Any]:
    """Fail closed unless current server evidence permits publication."""

    try:
        items = _build_promotion_economics_items(
            promotion_name=cstr(getattr(promotion, "name", "")).strip(),
            pos_profile=pos_profile,
        )
        current_hash = _promotion_economics_source_hash(
            promotion,
            profile_name=pos_profile,
            resolved_items=items,
        )
        result = calculate_promotion_economics(items=items)
    except (PromotionEconomicsError, RecipeCompilerError, PromotionRuntimeError) as error:
        current_hash = _promotion_economics_source_hash(
            promotion,
            profile_name=pos_profile,
            error=cstr(error),
        )
        result = None
        reason = cstr(error)

    if result is not None and _promotion_has_loss(promotion, items, result):
        reason = "Promotion price is below calculated COGS"

    stored_status = cstr(getattr(promotion, "economics_status", "")).strip()
    stored_hash = cstr(getattr(promotion, "economics_source_hash", "")).strip()
    override_hash = cstr(getattr(promotion, "economics_override_hash", "")).strip()
    override_reason = cstr(getattr(promotion, "economics_override_reason", "")).strip()
    override_by = cstr(getattr(promotion, "economics_override_by", "")).strip()
    if (
        stored_status == "Override approved"
        and override_hash == current_hash
        and override_reason
        and override_by
    ):
        return {"source_hash": current_hash, "override": True}
    if stored_status == "Ready" and stored_hash == current_hash and result is not None and not _promotion_has_loss(promotion, items, result):
        return {"source_hash": current_hash, "override": False}
    raise PromotionEconomicsError(
        "Promotion publication is blocked because COGS evidence is missing, stale, or below cost; "
        "run the server-side economics check and obtain a second Company Director approval when required"
    )


@frappe.whitelist(methods=["POST"])
def approve_promotion_economics_override(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Grant one source-hash-bound below-cost/missing-cost exception."""

    _require_company_director("Promotion economics override")
    value = _parse_json_object(payload, "Promotion economics override payload")
    unknown_fields = sorted(set(value) - {"promotion", "economics_hash", "reason", "pos_profile"})
    if unknown_fields:
        frappe.throw(_("Promotion override accepts only the saved Promotion, exact economics hash, reason and optional POS Profile"), frappe.ValidationError)
    promotion_name = cstr(value.get("promotion")).strip()
    source_hash = cstr(value.get("economics_hash")).strip()
    reason = cstr(value.get("reason")).strip()
    if not promotion_name:
        frappe.throw(_("Choose a saved Promotion"), frappe.ValidationError)
    if len(source_hash) != 64 or any(character not in "0123456789abcdef" for character in source_hash.lower()):
        frappe.throw(_("economics_hash must be the exact hash returned by the server economics check"), frappe.ValidationError)
    if len(reason) < 20 or len(reason) > 500:
        frappe.throw(_("The one-time override reason must contain 20-500 characters"), frappe.ValidationError)

    promotion = frappe.get_doc("KoPOS Promotion", promotion_name)
    checked_by = cstr(getattr(promotion, "economics_checked_by", "")).strip()
    actor = cstr(getattr(frappe.session, "user", "")).strip()
    if not checked_by:
        frappe.throw(_("Run the server-side economics check before requesting an override"), frappe.ValidationError)
    if checked_by == actor:
        frappe.throw(_("A second Company Director must approve this override"), frappe.ValidationError)
    profile_name = _promotion_profile_name(promotion, cstr(value.get("pos_profile")).strip() or None)
    _require_director_pos_profile(profile_name)
    try:
        items = _build_promotion_economics_items(
            promotion_name=promotion_name,
            pos_profile=profile_name,
        )
        current_hash = _promotion_economics_source_hash(
            promotion,
            profile_name=profile_name,
            resolved_items=items,
        )
        result = calculate_promotion_economics(items=items)
        if not _promotion_has_loss(promotion, items, result):
            frappe.throw(_("A one-time override is only required for below-cost or missing/stale COGS evidence"), frappe.ValidationError)
    except (PromotionEconomicsError, RecipeCompilerError, PromotionRuntimeError) as error:
        current_hash = _promotion_economics_source_hash(
            promotion,
            profile_name=profile_name,
            error=cstr(error),
        )
    if current_hash != source_hash or cstr(getattr(promotion, "economics_source_hash", "")).strip() != source_hash:
        frappe.throw(_("The economics evidence changed; run the server-side check again"), frappe.ValidationError)

    promotion.economics_status = "Override approved"
    promotion.economics_override_hash = source_hash
    promotion.economics_override_reason = reason
    promotion.economics_override_by = actor
    promotion.economics_override_at = now_datetime()
    promotion.flags.allow_economics_review_write = True
    promotion.save(ignore_permissions=True)
    frappe.db.commit()
    return {
        "status": "approved",
        "promotion": promotion.name,
        "economics_hash": source_hash,
        "message": "One-time exception approved for this exact economics evidence",
    }


def _promotion_price_sen(item_code: str, item: Any, price_list: str) -> Decimal:
    from kopos_connector.api.catalog import get_item_price_amounts_map

    if price_list:
        stock_uom = item.get("stock_uom") if isinstance(item, dict) else getattr(item, "stock_uom", "")
        amounts = get_item_price_amounts_map(
            [item_code], price_list, {item_code: cstr(stock_uom)},
        )
        if item_code not in amounts:
            raise PromotionEconomicsError(
                f"{item_code} has no current menu price for this outlet; update its selling price before checking the offer"
            )
        return _currency_to_sen(amounts[item_code], f"{item_code} selling price")
    standard = item.get("standard_rate") if isinstance(item, dict) else getattr(item, "standard_rate", None)
    return _currency_to_sen(standard, f"{item_code} selling price")


def _promotion_discounted_price_sen(promotion: Any, baseline_price_sen: Decimal) -> int:
    promotion_type = cstr(promotion.promotion_type).strip()
    if promotion_type not in {"item_discount", "order_discount"}:
        raise PromotionEconomicsError(
            f"{promotion_type or 'this'} promotion type requires a reviewed economics rule before publication"
        )
    discount_type = cstr(promotion.discount_type).strip()
    value = Decimal(str(promotion.discount_value or 0))
    if value < 0:
        raise PromotionEconomicsError("discount value cannot be negative")
    if discount_type == "percentage":
        discounted = baseline_price_sen * (Decimal("1") - value / Decimal("100"))
    elif discount_type == "fixed_amount":
        discounted = baseline_price_sen - value * Decimal("100")
    elif discount_type == "fixed_price":
        discounted = value * Decimal("100")
    elif discount_type == "free_item":
        discounted = Decimal("0")
    else:
        raise PromotionEconomicsError(f"unsupported discount type {discount_type or 'blank'}")
    return max(0, int(discounted.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def _component_valuation_rate(
    item_code: str,
    warehouse: str,
    *,
    max_source_age_minutes: int = 30,
) -> Any:
    if warehouse and frappe.db.exists("DocType", "Bin"):
        row = frappe.db.get_value(
            "Bin",
            {"item_code": item_code, "warehouse": warehouse},
            ["valuation_rate", "modified"],
            as_dict=True,
        )
        value = row.get("valuation_rate") if row else None
        if value not in (None, "", 0):
            count_observed_at = _current_exact_count_observed_at(
                item_code=item_code,
                warehouse=warehouse,
            )
            _require_current_valuation_evidence(
                item_code=item_code,
                source="accepted blind count" if count_observed_at else "Bin",
                modified=count_observed_at or (row.get("modified") if row else None),
                max_source_age_minutes=max_source_age_minutes,
            )
            return value
    item_meta = frappe.get_meta("Item")
    if item_meta.has_field("valuation_rate"):
        row = frappe.db.get_value(
            "Item",
            item_code,
            ["valuation_rate", "modified"],
            as_dict=True,
        ) or {}
        value = row.get("valuation_rate")
        if value not in (None, "", 0):
            _require_current_valuation_evidence(
                item_code=item_code,
                source="Item",
                modified=row.get("modified"),
                max_source_age_minutes=max_source_age_minutes,
            )
            return value
    raise PromotionEconomicsError(
        f"{item_code} has no current warehouse valuation; update stock evidence before publication"
    )


def _current_exact_count_observed_at(*, item_code: str, warehouse: str) -> Any | None:
    """Return fresh quantity evidence only when the stock ledger did not move.

    An accepted zero-variance blind count confirms that the existing Bin rate
    still applies even when the last valuation-changing Stock Ledger Entry is
    older than the outlet's freshness window.  A changed watermark refuses the
    count immediately; a variance count posts a reconciliation and refreshes
    the Bin through the standard ledger path instead.
    """

    if not (
        frappe.db.exists("DocType", "FB Inventory Count Observation")
        and frappe.db.exists("DocType", "FB Inventory Count Task")
    ):
        return None
    current_rows = frappe.db.sql(
        "SELECT MAX(modified) FROM `tabStock Ledger Entry` WHERE warehouse = %s",
        (warehouse,),
    )
    current_watermark = cstr(current_rows[0][0] if current_rows else "").strip()
    if not current_watermark:
        return None
    observations = frappe.get_all(
        "FB Inventory Count Observation",
        filters={"warehouse": warehouse, "status": "Accepted"},
        fields=["observed_at", "stock_watermark", "lines_json"],
        order_by="observed_at desc",
        limit_page_length=20,
    )
    for observation in observations:
        if cstr(observation.get("stock_watermark")).strip() != current_watermark:
            continue
        try:
            lines = json.loads(cstr(observation.get("lines_json")) or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(lines, list):
            continue
        if any(
            isinstance(line, dict)
            and cstr(line.get("item_id")).strip() == item_code
            for line in lines
        ):
            return observation.get("observed_at")
    return None


def _require_current_valuation_evidence(
    *,
    item_code: str,
    source: str,
    modified: Any,
    max_source_age_minutes: int,
) -> None:
    if modified in (None, ""):
        raise PromotionEconomicsError(
            f"{item_code} has no valuation timestamp; refresh stock evidence before publication"
        )
    try:
        observed = get_datetime(modified)
        current = get_datetime(now_datetime())
        if getattr(observed, "tzinfo", None) is None and getattr(current, "tzinfo", None) is not None:
            observed = observed.replace(tzinfo=current.tzinfo)
        if getattr(observed, "tzinfo", None) is not None and getattr(current, "tzinfo", None) is None:
            current = current.replace(tzinfo=observed.tzinfo)
        age_minutes = (current - observed).total_seconds() / 60
    except (TypeError, ValueError, OverflowError) as error:
        raise PromotionEconomicsError(
            f"{item_code} has an invalid {source} valuation timestamp"
        ) from error
    if age_minutes > max(1, int(max_source_age_minutes or 30)):
        raise PromotionEconomicsError(
            f"{item_code} valuation evidence is stale; refresh stock evidence before publication"
        )


def _currency_to_sen(value: Any, label: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PromotionEconomicsError(f"{label} is not a valid currency amount") from error
    if not amount.is_finite() or amount < 0:
        raise PromotionEconomicsError(f"{label} is missing or invalid")
    return (amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _currency_to_exact_sen(value: Any, label: str) -> Decimal:
    """Convert ERP currency to Decimal sen without rounding before allocation."""

    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PromotionEconomicsError(f"{label} is missing or invalid") from error
    if not amount.is_finite() or amount < 0:
        raise PromotionEconomicsError(f"{label} is missing or invalid")
    return amount * Decimal("100")


@frappe.whitelist(methods=["GET"])
def get_edge_snapshot(
    *,
    device_id: str,
    known_version: str | None = None,
    known_overlay_version: str | None = None,
    search: str | None = None,
    limit: int | str | None = None,
) -> dict[str, Any]:
    """Return the authenticated device's read-only commercial and stock view.

    This is deliberately a thin adapter over the existing catalog owner. It
    does not create a second snapshot format or grant a device manager access.
    """

    _device, profile = require_device_operational_scope(device_id=device_id)
    company = cstr(getattr(profile, "company", None)).strip()
    warehouse = cstr(getattr(profile, "warehouse", None)).strip()
    payload = build_catalog_payload(
        device_id=device_id,
        known_version=known_version,
        known_overlay_version=known_overlay_version,
    )
    try:
        catalog_item_ids, catalog_modifier_ids = get_catalog_target_ids(
            pos_profile=profile,
            company=company,
        )
    except Exception as error:
        # A failed commercial-identity lookup must hide manager controls rather
        # than accidentally treating an ingredient as a sellable target.
        log_sanitized_error("KoPOS edge catalog target lookup failed", error)
        catalog_item_ids, catalog_modifier_ids = set(), set()
    try:
        snapshot_kwargs: dict[str, Any] = {
            "catalog_item_ids": catalog_item_ids,
            "catalog_modifier_ids": catalog_modifier_ids,
        }
        profile_name = cstr(getattr(profile, "name", None)).strip()
        if profile_name:
            snapshot_kwargs["pos_profile"] = profile_name
        inventory_snapshot = build_edge_inventory_snapshot(
            company=company,
            warehouse=warehouse,
            search=search,
            limit=limit,
            **snapshot_kwargs,
        )
    except (frappe.ValidationError, frappe.PermissionError):
        raise
    except Exception as error:
        # The operational view is optional to commercial catalog delivery. A
        # transient stock query failure must leave the base catalog usable and
        # give the tablet a clear stale/check state instead of raw SQL detail.
        log_sanitized_error("KoPOS edge inventory snapshot failed", error)
        inventory_snapshot = {
            "schema_version": "inventory-edge-v1",
            "status": "unavailable",
            "company": company,
            "warehouse": warehouse,
            "generated_at": _iso_with_offset(now_datetime()),
            "freshness": "stale",
            "reliability": "Please check",
            "reliability_reason": (
                "Stock view is temporarily unavailable; refresh the tablet or ask a manager "
                "to check stock"
            ),
            "items": [],
            "modifier_options": [],
            "tasks": [],
            "truncated": {"items": False, "modifier_options": False},
        }
    # ``_get_inventory_tasks`` is already the authority for assigned counts and
    # standard-document preparation, receiving, and transfer work.  Reuse its
    # read model and apply one device-boundary allow-list rather than creating
    # another task persistence path. Task loading is optional to the stock
    # read, so a transient task query does not discard current stock data.
    try:
        task_response = _get_inventory_tasks(device_id=device_id)
    except Exception as error:
        log_sanitized_error("KoPOS edge inventory task read failed", error)
        task_response = {
            "tasks_status": "unavailable",
            "tasks_error_label": (
                "Assigned inventory tasks are temporarily unavailable. "
                "Refresh the tablet or ask a manager."
            ),
            "tasks": [],
            "count_task": None,
        }
    inventory_snapshot = attach_bounded_tasks(inventory_snapshot, task_response)
    payload["inventory_snapshot"] = inventory_snapshot
    return payload


@frappe.whitelist(methods=["GET"])
def get_stock_update_status(
    *,
    device_id: str | None = None,
    warehouse: str | None = None,
) -> dict[str, Any]:
    """Return a small, outlet-bound view of unfinished ingredient stock work.

    The commercial order is already authoritative at this point.  This read
    model tells an authenticated tablet or permitted Director page whether the
    last confirmed amounts are current. It never performs inventory work.
    """

    resolved_device_id = cstr(device_id).strip()
    if resolved_device_id:
        _device, profile = require_device_operational_scope(device_id=resolved_device_id)
        company = cstr(getattr(profile, "company", None)).strip()
        resolved_warehouse = cstr(getattr(profile, "warehouse", None)).strip()
    else:
        _require_company_director("Stock update status")
        resolved_warehouse = cstr(warehouse).strip()
        if not resolved_warehouse:
            frappe.throw(_("Outlet stock location is required"), frappe.ValidationError)
        company = cstr(
            frappe.db.get_value(
                "FB Inventory Policy",
                {"warehouse": resolved_warehouse},
                "company",
            )
            or frappe.db.get_value("Warehouse", resolved_warehouse, "company")
        ).strip()
        if not company:
            frappe.throw(_("This outlet stock location is not linked to a company"), frappe.ValidationError)
        resolve_director_context(company=company, warehouse=resolved_warehouse)
    now = now_datetime()
    policy = frappe.db.get_value(
        "FB Inventory Policy",
        {"company": company, "warehouse": resolved_warehouse},
        ["cutover_at"],
        as_dict=True,
    ) if frappe.db.exists("DocType", "FB Inventory Policy") else None
    cutover_at = (policy or {}).get("cutover_at")
    if not cutover_at:
        return _stock_update_status_response(
            company=company,
            warehouse=resolved_warehouse,
            rows=[],
            latest_completed_at=None,
            availability_checked_at=_health_marker("last_availability", warehouse=resolved_warehouse),
            now=now,
        )

    order_rows = frappe.db.sql(
        """
        SELECT o.name AS order_name,
               o.sale_datetime,
               o.stock_status,
               NULL AS projection_state,
               NULL AS created_at,
               NULL AS last_attempt_at
        FROM `tabFB Order` o
        WHERE o.booth_warehouse = %s
          AND o.company = %s
          AND o.status = 'Submitted'
          AND o.stock_status IN ('Pending', 'Failed')
          AND o.sale_datetime >= %s
          AND NOT EXISTS (
              SELECT 1
              FROM `tabFB Projection Log` completed
              WHERE completed.source_doctype = 'FB Order'
                AND completed.source_name = o.name
                AND completed.projection_type = 'Stock Issue'
                AND completed.state IN ('Succeeded', 'Reversed')
          )
        ORDER BY o.sale_datetime ASC
        LIMIT %s
        """,
        (resolved_warehouse, company, cutover_at, STOCK_UPDATE_STATUS_LIMIT + 1),
        as_dict=True,
    )
    projection_rows = frappe.db.sql(
        """
        SELECT o.name AS order_name,
               o.sale_datetime,
               o.stock_status,
               pl.state AS projection_state,
               pl.created_at,
               pl.last_attempt_at
        FROM `tabFB Projection Log` pl
        INNER JOIN `tabFB Order` o ON o.name = pl.source_name
        WHERE pl.projection_type = 'Stock Issue'
          AND pl.source_doctype = 'FB Order'
          AND pl.state IN ('Pending', 'Processing', 'Failed', 'Dead Letter')
          AND o.booth_warehouse = %s
          AND o.company = %s
          AND o.status = 'Submitted'
          AND o.sale_datetime >= %s
        ORDER BY o.sale_datetime ASC, pl.created_at DESC
        LIMIT %s
        """,
        (resolved_warehouse, company, cutover_at, STOCK_UPDATE_STATUS_LIMIT + 1),
        as_dict=True,
    )
    rows = _dedupe_stock_update_rows([*order_rows, *projection_rows])
    overflow = (
        len(order_rows) > STOCK_UPDATE_STATUS_LIMIT
        or len(projection_rows) > STOCK_UPDATE_STATUS_LIMIT
        or len(rows) > STOCK_UPDATE_STATUS_LIMIT
    )
    if overflow:
        # A bounded operational response must not understate a backlog.  The
        # exact rows remain in ERP; this state directs a manager to the full
        # support workflow without returning an unbounded payload.
        rows = list(rows[:STOCK_UPDATE_STATUS_LIMIT])
        rows.append({
            "order_name": "bounded-overflow",
            "sale_datetime": rows[0].get("sale_datetime") if rows else cutover_at,
            "stock_status": "Failed",
            "projection_state": "Dead Letter",
        })
    latest_rows = frappe.db.sql(
        """
        SELECT pl.last_attempt_at
        FROM `tabFB Projection Log` pl
        INNER JOIN `tabFB Order` o ON o.name = pl.source_name
        WHERE pl.source_doctype = 'FB Order'
          AND pl.projection_type = 'Stock Issue'
          AND pl.state IN ('Succeeded', 'Reversed')
          AND pl.target_doctype = 'Stock Entry'
          AND pl.target_name IS NOT NULL
          AND o.booth_warehouse = %s
          AND o.company = %s
          AND o.sale_datetime >= %s
        ORDER BY pl.last_attempt_at DESC
        LIMIT 1
        """,
        (resolved_warehouse, company, cutover_at),
        as_dict=True,
    )
    latest_completed_at = latest_rows[0].get("last_attempt_at") if latest_rows else None
    return _stock_update_status_response(
        company=company,
        warehouse=resolved_warehouse,
        rows=list(rows),
        latest_completed_at=latest_completed_at,
        availability_checked_at=_health_marker("last_availability", warehouse=resolved_warehouse),
        now=now,
    )


def _stock_update_status_response(
    *,
    company: str,
    warehouse: str,
    rows: list[dict[str, Any]],
    latest_completed_at: Any,
    availability_checked_at: Any,
    now: Any,
) -> dict[str, Any]:
    """Collapse technical order/projection rows into one human-facing state."""

    active_rows = _dedupe_stock_update_rows(rows)
    attention_rows = [
        row for row in active_rows
        if cstr(row.get("stock_status")).strip() == "Failed"
        or cstr(row.get("projection_state")).strip() == "Dead Letter"
    ]
    oldest_pending_at = min(
        (get_datetime(row.get("sale_datetime")) for row in active_rows if row.get("sale_datetime")),
        key=_datetime_sort_key,
        default=None,
    )
    oldest_age_seconds = _stock_update_age_seconds(oldest_pending_at, now)
    latest_completed_datetime = get_datetime(latest_completed_at) if latest_completed_at else None
    availability_checked_datetime = get_datetime(availability_checked_at) if availability_checked_at else None
    availability_pending = bool(
        latest_completed_datetime
        and (
            availability_checked_datetime is None
            or _datetime_sort_key(availability_checked_datetime) < _datetime_sort_key(latest_completed_datetime)
        )
    )
    availability_age_seconds = _stock_update_age_seconds(latest_completed_datetime, now)

    state = "current"
    if attention_rows:
        state = "needs_attention"
    elif (active_rows or availability_pending) and max(
        oldest_age_seconds or 0,
        availability_age_seconds or 0,
    ) >= STOCK_UPDATE_TAKING_LONGER_SECONDS:
        state = "taking_longer"
    elif active_rows or availability_pending:
        state = "updating"
    return {
        "schema_version": "inventory-stock-update-status-v1",
        "status": "ok",
        "state": state,
        "company": company,
        "warehouse": warehouse,
        "pending_sales": len(active_rows),
        "attention_sales": len(attention_rows),
        "oldest_pending_at": _stock_update_status_iso(oldest_pending_at),
        "latest_completed_at": _stock_update_status_iso(latest_completed_at),
        "availability_checked_at": _stock_update_status_iso(availability_checked_at),
        "checked_at": _stock_update_status_iso(now),
    }


def _stock_update_status_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return value.strip() or None
    return _iso_with_offset(value)


def _dedupe_stock_update_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_order: dict[str, dict[str, Any]] = {}
    state_priority = {"": 0, "Pending": 1, "Processing": 2, "Failed": 3, "Dead Letter": 4}
    for row in rows:
        order_name = cstr(row.get("order_name")).strip()
        if not order_name:
            continue
        previous = by_order.get(order_name)
        candidate_state = cstr(row.get("projection_state")).strip()
        previous_state = cstr((previous or {}).get("projection_state")).strip()
        if previous is None or state_priority.get(candidate_state, 0) > state_priority.get(previous_state, 0):
            by_order[order_name] = row
    return sorted(
        by_order.values(),
        key=lambda row: _datetime_sort_key(get_datetime(row.get("sale_datetime"))),
    )


def _stock_update_age_seconds(value: Any, now: Any) -> int | None:
    if value in (None, ""):
        return None
    observed = get_datetime(value)
    comparable_now = get_datetime(now)
    if getattr(observed, "tzinfo", None) is None and getattr(comparable_now, "tzinfo", None) is not None:
        observed = observed.replace(tzinfo=comparable_now.tzinfo)
    elif getattr(observed, "tzinfo", None) is not None and getattr(comparable_now, "tzinfo", None) is None:
        comparable_now = comparable_now.replace(tzinfo=observed.tzinfo)
    return max(0, int((comparable_now - observed).total_seconds()))


def _get_count_task(
    *, device_id: str, task_id: str | None = None, include_barcodes: bool = True
) -> dict[str, Any]:
    """Return the next outlet-pool or assigned blind-count task.

    An unassigned task is deliberately visible to the outlet pool without
    exposing expected quantities.  A staff member claims it explicitly before
    entering observations, which makes ownership auditable and prevents two
    tablets from silently submitting the same assignment.
    """

    device, profile_doc = require_device_operational_scope(device_id=device_id)
    profile = {
        "name": cstr(getattr(profile_doc, "name", None)).strip(),
        "company": cstr(getattr(profile_doc, "company", None)).strip(),
        "warehouse": cstr(getattr(profile_doc, "warehouse", None)).strip(),
    }
    assigned_users = sorted({
        cstr(row.get("user")).strip()
        for row in resolve_staff_access_for_device(device, profile_doc=profile_doc)
        if cstr(row.get("user")).strip()
    })
    if not assigned_users:
        return {"status": "ok", "task": None}
    filters: dict[str, Any] = {
        "warehouse": cstr(profile.get("warehouse")),
        "status": ["in", ["Assigned", "Claimed", "In Progress", "Submitted", "Review"]],
    }
    if task_id:
        filters["name"] = cstr(task_id).strip()
    if not frappe.db.exists("DocType", "FB Inventory Count Task"):
        return {"status": "ok", "task": None}
    rows = frappe.get_all(
        "FB Inventory Count Task",
        filters=filters,
        fields=["name", "company", "revision", "warehouse", "assignee", "stock_watermark", "status"],
        order_by="modified asc",
        limit_page_length=100,
    )
    visible = [
        row for row in rows
        if not cstr(row.get("assignee")).strip()
        or cstr(row.get("assignee")).strip() in assigned_users
    ]
    if not visible:
        return {"status": "ok", "task": None}
    return {
        "status": "ok",
        "task": _count_task_response(visible[0], include_barcodes=include_barcodes),
    }


@frappe.whitelist(methods=["POST"])
def claim_count_task(*, device_id: str, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Claim one unassigned outlet count for the signed-in staff member."""

    device = require_device_context(device_id=device_id)
    value = _parse_device_json_object(payload, "Count claim payload")
    required_fields = {
        "schema_version", "command_id", "task_type", "device_id", "device_config_version",
        "staff_user", "employee", "company", "outlet", "warehouse", "source_document",
        "source_revision", "observed_at", "task_id", "task_revision", "payload_hash",
    }
    if set(value) != required_fields:
        frappe.throw(_("Count claim payload fields are incomplete or unexpected"), frappe.ValidationError)
    if cstr(value.get("schema_version")).strip() != "inventory-command-v1":
        frappe.throw(_("Unsupported count claim command schema version"), frappe.ValidationError)
    if cstr(value.get("task_type")).strip() != "claim_count_task":
        frappe.throw(_("Count claim task_type is invalid"), frappe.ValidationError)
    task_id = cstr(value.get("task_id")).strip()
    staff_user = cstr(value.get("staff_user") or value.get("staff_id")).strip()
    employee = cstr(value.get("employee")).strip()
    source_document = cstr(value.get("source_document")).strip()
    if not task_id or not staff_user or not employee or source_document != task_id:
        frappe.throw(_("Count claim requires its task, staff, employee, and source identity"), frappe.ValidationError)
    config_version = value.get("device_config_version")
    source_revision = value.get("source_revision")
    task_revision = value.get("task_revision")
    if isinstance(config_version, bool) or not isinstance(config_version, int) or config_version < 0:
        frappe.throw(_("Count claim device_config_version is invalid"), frappe.ValidationError)
    if isinstance(source_revision, bool) or not isinstance(source_revision, int) or source_revision < 1:
        frappe.throw(_("Count claim source_revision is invalid"), frappe.ValidationError)
    if task_revision != source_revision:
        frappe.throw(_("Count claim task_revision must match source_revision"), frappe.ValidationError)
    _require_explicit_offset_timestamp(value.get("observed_at"), "Count claim observed_at")
    payload_hash = cstr(value.get("payload_hash")).strip().lower()
    if len(payload_hash) != 64 or any(character not in "0123456789abcdef" for character in payload_hash):
        frappe.throw(_("Count claim payload_hash is invalid"), frappe.ValidationError)
    if _payload_hash({key: item for key, item in value.items() if key != "payload_hash"}) != payload_hash:
        frappe.throw(_("Count claim payload_hash does not match its payload"), frappe.ValidationError)
    device = lock_device_for_operational_mutation(device_id=device_id)
    authenticated_device_id = cstr(getattr(device, "device_id", None) or getattr(device, "name", None)).strip()
    if cstr(value.get("device_id")).strip() != authenticated_device_id:
        frappe.throw(_("Count claim device_id does not match the authenticated device"), frappe.PermissionError)
    if config_version != cint(getattr(device, "config_version", None)):
        frappe.throw(_("Device configuration changed; refresh before claiming a count"), frappe.ValidationError)
    profile = resolve_catalog_pos_profile(device_id=device_id) or {}
    for fieldname, expected in (
        ("company", cstr(profile.get("company")).strip()),
        ("outlet", cstr(profile.get("name")).strip()),
        ("warehouse", cstr(profile.get("warehouse")).strip()),
    ):
        if not expected or cstr(value.get(fieldname)).strip() != expected:
            frappe.throw(_("Count claim {0} is outside the authenticated outlet").format(fieldname), frappe.PermissionError)
    access = _require_device_staff(device, staff_user)
    if cstr(access.get("employee")).strip() != employee:
        frappe.throw(_("Count claim employee does not match central staff access"), frappe.PermissionError)
    warehouse = cstr(profile.get("warehouse")).strip()
    if not warehouse:
        frappe.throw(_("This device has no assigned inventory warehouse"), frappe.ValidationError)
    _lock_count_task(task_id)
    task = frappe.get_doc("FB Inventory Count Task", task_id)
    if cstr(task.warehouse).strip() != warehouse:
        frappe.throw(_("This count is outside the device warehouse"), frappe.PermissionError)
    current_assignee = cstr(task.assignee).strip()
    if current_assignee and current_assignee != staff_user:
        frappe.throw(_("This count has already been claimed by another staff member"), frappe.PermissionError)
    if cstr(task.status).strip() not in {"Assigned", "Claimed", "In Progress"}:
        frappe.throw(_("This count is no longer available to claim"), frappe.ValidationError)
    if current_assignee:
        if cint(task.revision) != cint(source_revision) + 1:
            frappe.throw(_("This count changed after the claim was sent; refresh the assignment"), frappe.ValidationError)
        return {"status": "replayed", "task": _count_task_response({
            "name": task.name,
            "revision": task.revision,
            "warehouse": task.warehouse,
            "assignee": task.assignee,
            "stock_watermark": task.stock_watermark,
        }), "employee": cstr(access.get("employee"))}
    if cint(task.revision) != cint(source_revision):
        frappe.throw(_("This count changed before it was claimed; refresh the assignment"), frappe.ValidationError)
    if not current_assignee:
        task.assignee = staff_user
        task.revision = cint(task.revision) + 1
        task.status = "Claimed"
        task.save(ignore_permissions=True)
        frappe.db.commit()
    return {"status": "accepted", "task": _count_task_response({
        "name": task.name,
        "revision": task.revision,
        "warehouse": task.warehouse,
        "assignee": task.assignee,
        "stock_watermark": task.stock_watermark,
    }), "employee": cstr(access.get("employee"))}


def _get_inventory_tasks(*, device_id: str) -> dict[str, Any]:
    """Return a small, safe queue of work assigned to this outlet.

    The tablet should not make staff remember ERP document numbers.  This read
    model exposes identifiers, quantities, and instructions only; rates,
    valuation, supplier terms, and other financial fields never cross the
    device boundary.  Standard ERPNext documents remain the authorities.
    """

    device = require_device_context(device_id=device_id)
    profile = resolve_catalog_pos_profile(device_id=device_id) or {}
    warehouse = cstr(profile.get("warehouse")).strip()
    company = cstr(profile.get("company")).strip()
    if not warehouse:
        frappe.throw(_("This device has no assigned inventory warehouse"), frappe.ValidationError)
    task_kind_limit = 20
    task_line_limit = 100
    tasks: list[dict[str, Any]] = []
    tasks_truncated = False
    preparation_visible = _preparation_tasks_visible_to_device(
        device=device,
        company=company,
        warehouse=warehouse,
    )

    count_response = _get_count_task(device_id=device_id, include_barcodes=False)
    # ``_get_count_task`` already returns the device-safe count response. Keep
    # that object as the dedicated edge field so the count screen does not
    # have to reconstruct frozen UOM authority from the generic task list.
    safe_count_task = count_response.get("task")
    if isinstance(safe_count_task, dict) and safe_count_task:
        tasks.append({
            "kind": "count",
            "document": cstr(safe_count_task.get("name")),
            "title": "Assigned stock count",
            "revision": safe_count_task.get("revision"),
            "stock_watermark": safe_count_task.get("stock_watermark"),
            "assignee": cstr(safe_count_task.get("assignee")),
            "warehouse": warehouse,
            "lines": safe_count_task.get("lines", []),
        })

    if preparation_visible and frappe.db.exists("DocType", "Work Order"):
        work_order_meta = frappe.get_meta("Work Order")
        work_order_fields = [
            "name", "modified", "production_item", "item_name", "qty", "produced_qty",
            "bom_no", "fg_warehouse", "status", "docstatus",
        ]
        if work_order_meta.has_field("custom_kopos_preparation_fingerprint"):
            work_order_fields.append("custom_kopos_preparation_fingerprint")
        work_order_rows = frappe.get_all(
            "Work Order",
            filters={
                "company": company,
                "fg_warehouse": warehouse,
                "docstatus": ["in", [0, 1]],
                "status": ["not in", ["Completed", "Stopped", "Cancelled"]],
            },
            fields=work_order_fields,
            order_by="modified asc",
            limit_page_length=task_kind_limit + 1,
        )
        if len(work_order_rows) > task_kind_limit:
            tasks_truncated = True
        work_orders = work_order_rows[:task_kind_limit]
        active_work_order_boms = {
            cstr(row.get("bom_no")).strip()
            for row in work_orders
            if cstr(row.get("bom_no")).strip()
        }

        # Preparation is a derived alert, not a scheduler-created document.
        # It remains visible until staff accepts it; the acceptance command
        # then creates the Draft Work Order under the same database lock.
        preparation_alerts = derived_preparation_alerts(company=company, warehouse=warehouse)
        available_preparation_slots = max(0, task_kind_limit - len(work_orders))
        visible_alerts = preparation_alerts[: available_preparation_slots + 1]
        if len(visible_alerts) > available_preparation_slots:
            tasks_truncated = True
        for alert in visible_alerts[:available_preparation_slots]:
            if alert.get("kind") != "preparation":
                continue
            if cstr(alert.get("bom_no")).strip() in active_work_order_boms:
                continue
            revision = cstr(alert.get("revision")).strip()
            # Real preparation alerts originate from a standard BOM row. Give
            # the tablet the same opaque representation that the command
            # validator derives from the locked BOM document. Setup-only
            # alerts use a non-document fingerprint revision and must remain
            # untouched.
            if cstr(alert.get("bom_no")).strip():
                revision = _authoritative_document_revision({"modified": revision})
            blocked_reason = cstr(alert.get("blocked_reason")).strip() or None
            item_name = cstr(alert.get("item_name") or alert.get("item_code")).strip()
            title = cstr(alert.get("title")).strip() or (
                "Batch preparation setup needed"
                if blocked_reason and not cstr(alert.get("bom_no")).strip()
                else "Prepare {0}".format(item_name)
            )
            tasks.append({
                "kind": "preparation",
                "document": cstr(alert.get("document")),
                "title": title,
                "item_code": cstr(alert.get("item_code")),
                "item_name": item_name,
                "qty": alert.get("qty"),
                "bom_no": cstr(alert.get("bom_no")),
                "warehouse": warehouse,
                "status": "Alert",
                "preparation_alert": True,
                "preparation_fingerprint": cstr(alert.get("fingerprint")),
                "batch_qty": alert.get("batch_qty"),
                "min_ready_qty": alert.get("min_ready_qty"),
                "trigger_qty": alert.get("trigger_qty"),
                "current_qty": alert.get("current_qty"),
                "lead_minutes": alert.get("lead_minutes"),
                "preparation_instructions": cstr(alert.get("preparation_instructions")),
                "blocked_reason": blocked_reason,
                "revision": revision,
            })
        for row in work_orders:
            component_lines = _work_order_component_lines(cstr(row.get("name")))
            tasks.append({
                "kind": "preparation",
                "document": cstr(row.get("name")),
                "revision": _authoritative_document_revision(row),
                "title": "Prepare {0}".format(cstr(row.get("item_name") or row.get("production_item"))),
                "item_code": cstr(row.get("production_item")),
                "item_name": cstr(row.get("item_name")),
                "qty": row.get("qty"),
                "produced_qty": row.get("produced_qty"),
                "bom_no": cstr(row.get("bom_no")),
                "warehouse": warehouse,
                "status": cstr(row.get("status")),
                "docstatus": cint(row.get("docstatus")),
                "preparation_alert": False,
                "lines": component_lines,
                "preparation_instructions": _work_order_preparation_instructions(
                    cstr(row.get("bom_no"))
                ),
            })

    if frappe.db.exists("DocType", "Purchase Order") and frappe.db.exists("DocType", "Purchase Order Item"):
        purchase_order_rows = frappe.db.sql(
            """
            SELECT purchase_order.name, purchase_order.company, purchase_order.modified,
                   purchase_order.set_warehouse
            FROM `tabPurchase Order` purchase_order
            INNER JOIN `tabPurchase Order Item` line ON line.parent = purchase_order.name
            WHERE line.parenttype = 'Purchase Order'
              AND purchase_order.company = %s
              AND purchase_order.docstatus = 1
              AND purchase_order.per_received < 100
              AND purchase_order.status NOT IN ('Closed', 'Cancelled')
              AND COALESCE(NULLIF(line.warehouse, ''), purchase_order.set_warehouse) = %s
              AND line.qty > line.received_qty
            GROUP BY purchase_order.name, purchase_order.company, purchase_order.modified,
                     purchase_order.set_warehouse
            ORDER BY purchase_order.modified ASC, purchase_order.name ASC
            LIMIT %s
            """,
            (company, warehouse, task_kind_limit + 1),
            as_dict=True,
        )
        if len(purchase_order_rows) > task_kind_limit:
            tasks_truncated = True
        purchase_orders = purchase_order_rows[:task_kind_limit]
        for order in purchase_orders:
            purchase_item_meta = frappe.get_meta("Purchase Order Item")
            purchase_item_fields = ["name", "item_code", "item_name", "qty", "received_qty", "warehouse", "uom"]
            for optional_field in ("stock_qty", "conversion_factor", "stock_uom"):
                if purchase_item_meta.has_field(optional_field):
                    purchase_item_fields.append(optional_field)
            selected_purchase_fields = ", ".join(
                f"line.`{fieldname}`" for fieldname in purchase_item_fields
            )
            rows = frappe.db.sql(
                f"""
                SELECT {selected_purchase_fields}
                FROM `tabPurchase Order Item` line
                WHERE line.parent = %s AND line.parenttype = 'Purchase Order'
                  AND COALESCE(NULLIF(line.warehouse, ''), %s) = %s
                  AND line.qty > line.received_qty
                ORDER BY line.idx ASC
                LIMIT %s
                """,
                (order["name"], cstr(order.get("set_warehouse")).strip(), warehouse, task_line_limit + 1),
                as_dict=True,
            )
            lines_truncated = len(rows) > task_line_limit
            if lines_truncated:
                tasks_truncated = True
            lines: list[dict[str, Any]] = []
            for row in rows[:task_line_limit]:
                total_quantity = Decimal(str(row.get("qty") or 0))
                received_quantity = Decimal(str(row.get("received_qty") or 0))
                if total_quantity <= received_quantity:
                    continue
                item_code = cstr(row.get("item_code")).strip()
                authority = _guided_uom_authority(row, item_code=item_code, label="Purchase Order line")
                remaining_quantity = max(total_quantity - received_quantity, Decimal("0"))
                lines.append({
                    "purchase_order_item": cstr(row.get("name")),
                    "item_code": item_code,
                    "item_name": cstr(row.get("item_name")),
                    "qty": row.get("qty"),
                    "remaining_qty": str(remaining_quantity),
                    "received_qty": row.get("received_qty"),
                    "stock_qty": str(remaining_quantity * authority["conversion_factor"]),
                    "warehouse": warehouse,
                    "uom": authority["uom"],
                    "conversion_factor": str(authority["conversion_factor"]),
                    "stock_uom": authority["stock_uom"],
                })
            if lines:
                receiving_paused = _supplier_receiving_is_paused(
                    company=company,
                    warehouse=warehouse,
                    purchase_order=cstr(order.get("name")),
                )
                tasks.append({
                    "kind": "receiving",
                    "document": cstr(order.get("name")),
                    "title": "Receive supplier delivery",
                    "revision": _authoritative_document_revision(order),
                    "warehouse": warehouse,
                    "lines": lines,
                    "lines_truncated": lines_truncated,
                    **({
                        "blocked_reason": (
                            "A manager is checking a difference in this delivery. "
                            "Wait until it is cleared before recording more."
                        )
                    } if receiving_paused else {}),
                })

    if frappe.db.exists("DocType", "Material Request") and frappe.db.exists("DocType", "Material Request Item"):
        request_fields = ["name", "modified"]
        if frappe.get_meta("Material Request").has_field("custom_kopos_transit_warehouse"):
            request_fields.append("custom_kopos_transit_warehouse")
        transfer_route_tracking_installed = _transfer_route_tracking_installed()
        request_item_meta = frappe.get_meta("Material Request Item")
        transfer_source_field_installed = request_item_meta.has_field("from_warehouse")
        request_item_fields = ["name", "idx", "item_code", "item_name", "qty", "uom", "warehouse"]
        if transfer_source_field_installed:
            request_item_fields.append("from_warehouse")
        if request_item_meta.has_field("stock_qty"):
            request_item_fields.append("stock_qty")
        if request_item_meta.has_field("stock_uom"):
            request_item_fields.append("stock_uom")
        if request_item_meta.has_field("conversion_factor"):
            request_item_fields.append("conversion_factor")
        request_rows = frappe.db.sql(
            f"""
            SELECT request.name, request.modified{', request.custom_kopos_transit_warehouse' if 'custom_kopos_transit_warehouse' in request_fields else ''}
            FROM `tabMaterial Request` request
            INNER JOIN `tabMaterial Request Item` line ON line.parent = request.name
            WHERE line.parenttype = 'Material Request'
              AND request.company = %s
              AND request.docstatus = 1
              AND request.material_request_type = 'Material Transfer'
              AND request.status NOT IN ('Stopped', 'Cancelled', 'Transferred')
              AND ({'line.from_warehouse = %s OR ' if transfer_source_field_installed else ''}line.warehouse = %s)
            GROUP BY request.name, request.modified{', request.custom_kopos_transit_warehouse' if 'custom_kopos_transit_warehouse' in request_fields else ''}
            ORDER BY request.modified ASC, request.name ASC
            LIMIT %s
            """,
            (
                (company, warehouse, warehouse, task_kind_limit + 1)
                if transfer_source_field_installed
                else (company, warehouse, task_kind_limit + 1)
            ),
            as_dict=True,
        )
        if len(request_rows) > task_kind_limit:
            tasks_truncated = True
        requests = request_rows[:task_kind_limit]
        for request in requests:
            transit_warehouse = cstr(request.get("custom_kopos_transit_warehouse")).strip()
            rows = _actionable_transfer_request_lines(
                material_request=cstr(request.get("name")),
                warehouse=warehouse,
                transit_warehouse=transit_warehouse,
                fields=request_item_fields,
                source_field_installed=transfer_source_field_installed,
                route_tracking_installed=transfer_route_tracking_installed,
                limit=task_line_limit + 1,
            )
            dispatch_lines: list[dict[str, Any]] = []
            receipt_lines: list[dict[str, Any]] = []
            lines_truncated = len(rows) > task_line_limit
            if lines_truncated:
                tasks_truncated = True
            for row in rows[:task_line_limit]:
                item_code = cstr(row.get("item_code")).strip()
                source_warehouse = cstr(row.get("from_warehouse")).strip()
                destination_warehouse = cstr(row.get("warehouse")).strip()
                request_item = cstr(row.get("name")).strip()
                if not item_code or not source_warehouse or not destination_warehouse or not request_item:
                    continue
                uom_authority = _guided_uom_authority(row, item_code=item_code, label="Material Request line")
                requested_qty = uom_authority["stock_qty"]
                dispatched_qty = _non_negative_decimal(
                    row.get("kopos_dispatched_stock_qty"),
                    "recorded dispatched stock quantity",
                )
                received_qty = _non_negative_decimal(
                    row.get("kopos_received_stock_qty"),
                    "recorded received stock quantity",
                )
                remaining_dispatched = max(requested_qty - dispatched_qty, Decimal("0"))
                remaining_received = max(dispatched_qty - received_qty, Decimal("0"))
                factor = uom_authority["conversion_factor"]
                if source_warehouse == warehouse and remaining_dispatched > 0:
                    dispatch_lines.append({
                        "material_request_item": request_item,
                        "item_code": item_code,
                        "item_name": cstr(row.get("item_name")) or item_code,
                        "qty": str(remaining_dispatched / factor),
                        "requested_qty": str(uom_authority["qty"]),
                        "stock_qty": str(remaining_dispatched),
                        "dispatched_qty": str(dispatched_qty / factor),
                        "received_qty": str(received_qty / factor),
                        "stock_dispatched_qty": str(dispatched_qty),
                        "stock_received_qty": str(received_qty),
                        "uom": uom_authority["uom"],
                        "stock_uom": uom_authority["stock_uom"],
                        "conversion_factor": str(factor),
                        "source_warehouse": source_warehouse,
                        "destination_warehouse": destination_warehouse,
                        "transit_warehouse": transit_warehouse,
                    })
                if destination_warehouse == warehouse and remaining_received > 0:
                    receipt_lines.append({
                        "material_request_item": request_item,
                        "item_code": item_code,
                        "item_name": cstr(row.get("item_name")) or item_code,
                        "qty": str(remaining_received / factor),
                        "requested_qty": str(uom_authority["qty"]),
                        "stock_qty": str(remaining_received),
                        "dispatched_qty": str(dispatched_qty / factor),
                        "received_qty": str(received_qty / factor),
                        "stock_dispatched_qty": str(dispatched_qty),
                        "stock_received_qty": str(received_qty),
                        "uom": uom_authority["uom"],
                        "stock_uom": uom_authority["stock_uom"],
                        "conversion_factor": str(factor),
                        "source_warehouse": source_warehouse,
                        "destination_warehouse": destination_warehouse,
                        "transit_warehouse": transit_warehouse,
                    })
            route_blocked_reason = None
            if not transit_warehouse:
                route_blocked_reason = "A Company Director must finish the transfer setup for this outlet before staff can move stock."
            elif not transfer_source_field_installed:
                route_blocked_reason = "JiJi setup is not finished for stock transfers. Ask Support to update the store system."
            elif not transfer_route_tracking_installed:
                route_blocked_reason = "JiJi setup is not finished for stock transfers. Ask Support to update the store system."
            if dispatch_lines:
                tasks.append({
                    "kind": "transfer_dispatch",
                    "document": cstr(request.get("name")),
                    "title": "Send approved outlet transfer",
                    "revision": _authoritative_document_revision(request),
                    "warehouse": warehouse,
                    "lines": dispatch_lines,
                    "lines_truncated": lines_truncated,
                    **({"blocked_reason": route_blocked_reason} if route_blocked_reason else {}),
                })
            if receipt_lines:
                tasks.append({
                    "kind": "transfer_receipt",
                    "document": cstr(request.get("name")),
                    "title": "Receive approved outlet transfer",
                    "revision": _authoritative_document_revision(request),
                    "warehouse": warehouse,
                    "lines": receipt_lines,
                    "lines_truncated": lines_truncated,
                    **({"blocked_reason": route_blocked_reason} if route_blocked_reason else {}),
                })

    _attach_task_line_barcodes(tasks)
    return {
        "status": "ok",
        "warehouse": warehouse,
        "tasks": tasks[:100],
        "tasks_truncated": tasks_truncated or len(tasks) > 100,
        "count_task": safe_count_task,
        "generated_at": _iso_with_offset(now_datetime()),
    }


@frappe.whitelist(methods=["GET", "POST"])
def preflight_legacy_inventory_values(
    *, company: str | None = None, warehouse: str | None = None, apply: bool = False, input_digest: str | None = None
) -> dict[str, Any]:
    """Discover legacy availability values before outlet cutover.

    Discovery is read-only by default. Applying the explicit migration is a
    manager action and retains the source fields for the retention window.
    """

    _require_company_director("Legacy inventory migration")
    if not frappe.has_permission("Item", ptype="read"):
        frappe.throw(_("Legacy inventory preflight requires Item read permission"), frappe.PermissionError)
    context = resolve_director_context(
        company=cstr(company).strip() or None,
        warehouse=(cstr(warehouse).strip() or None) if cint(apply) else None,
    )
    if not context.company:
        return {
            "status": context.status,
            "companies": list(context.allowed_companies),
        }
    values = discover_legacy_values(company=context.company)
    if not cint(apply):
        return {"status": "dry_run", "input_digest": legacy_input_digest(values), "values": values, "unknown_count": sum(1 for value in values if value["availability_mode"].strip().lower() not in {"", "auto", "force_available", "force_unavailable"})}
    resolved_company = context.company
    resolved_warehouse = context.warehouse
    if not resolved_company or not resolved_warehouse:
        frappe.throw(_("Company and warehouse are required to apply legacy migration"), frappe.ValidationError)
    if not cstr(input_digest).strip():
        frappe.throw(_("A reviewed legacy migration input digest is required"), frappe.ValidationError)
    try:
        applied = execute_legacy_migration(
            company=resolved_company,
            warehouse=resolved_warehouse,
            expected_digest=cstr(input_digest).strip(),
        )
    except ValueError as error:
        frappe.throw(_("Legacy migration input digest does not match the reviewed dry run: {0}").format(error), frappe.ValidationError)
    return {"status": "applied", **applied}


@frappe.whitelist(methods=["POST"])
def create_availability_hold(
    *,
    payload: str | dict[str, Any],
) -> dict[str, Any]:
    value = _parse_availability_hold_command(payload, action="create")
    device_id = cstr(value["device_id"]).strip()
    device = require_device_context(device_id=device_id)
    device = lock_device_for_operational_mutation(device_id=device_id)
    access = _require_device_staff(device, cstr(value["staff_user"]), manager=True)
    _validate_availability_hold_scope(device, value)
    profile = resolve_catalog_pos_profile(device_id=device_id) or {}
    _assert_catalog_hold_target(
        profile=profile,
        target_type=cstr(value["target_type"]).strip(),
        target_id=cstr(value["target_id"]).strip(),
    )
    if cstr(value["reason_code"]).strip() != "manual_manager_pause":
        frappe.throw(
            _("POS manual holds must use the manager Pause reason"),
            frappe.ValidationError,
        )
    hold_name = create_hold(
        target_type=cstr(value["target_type"]).strip(),
        target_id=cstr(value["target_id"]).strip(),
        company=cstr(profile.get("company")),
        warehouse=cstr(profile.get("warehouse")),
        source="manual",
        reason_code=cstr(value["reason_code"]).strip(),
        reason_label=cstr(value["reason_label"]).strip(),
        actor=cstr(access.get("user")).strip(),
        pos_profile=cstr(profile.get("name")),
        originating_doctype="KoPOS Device",
        originating_name=device_id,
        idempotency_key=cstr(value["command_id"]).strip(),
        command_payload_hash=cstr(value["payload_hash"]).strip(),
    )
    frappe.db.commit()
    return {"status": "accepted", "hold_id": hold_name}


@frappe.whitelist(methods=["POST"])
def release_availability_hold(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    value = _parse_availability_hold_command(payload, action="release")
    device_id = cstr(value["device_id"]).strip()
    device = require_device_context(device_id=device_id)
    device = lock_device_for_operational_mutation(device_id=device_id)
    access = _require_device_staff(device, cstr(value["staff_user"]), manager=True)
    _validate_availability_hold_scope(device, value)
    hold_id = cstr(value["hold_id"]).strip()
    hold = frappe.get_doc("FB Availability Hold", hold_id)
    profile = resolve_catalog_pos_profile(device_id=device_id) or {}
    if cstr(hold.warehouse).strip() != cstr(profile.get("warehouse")).strip() or cstr(hold.company).strip() != cstr(profile.get("company")).strip():
        frappe.throw(_("This hold is outside the device outlet"), frappe.PermissionError)
    if (
        cstr(hold.target_type).strip() != cstr(value["target_type"]).strip()
        or cstr(hold.target_id).strip() != cstr(value["target_id"]).strip()
    ):
        frappe.throw(_("The hold target changed; refresh stock before restoring it"), frappe.ValidationError)
    _assert_catalog_hold_target(
        profile=profile,
        target_type=cstr(value["target_type"]).strip(),
        target_id=cstr(value["target_id"]).strip(),
    )
    source = cstr(hold.source).strip()
    if cstr(value["hold_source"]).strip() != source:
        frappe.throw(_("The hold source changed; refresh stock before restoring it"), frappe.ValidationError)
    if source == "automation":
        name = manager_override_automation_hold(
            hold_id,
            actor=cstr(access.get("user")).strip(),
            reason=cstr(value["reason"]).strip(),
            command_id=cstr(value["command_id"]).strip(),
            payload_hash=cstr(value["payload_hash"]).strip(),
        )
        expires_at = frappe.db.get_value("FB Availability Hold", name, "expires_at")
        if not expires_at:
            frappe.throw(_("The manager override expiry was not saved"), frappe.ValidationError)
        response: dict[str, Any] = {
            "status": "accepted",
            "hold_id": name,
            "override_minutes": 30,
            "expires_at": _iso_with_offset(get_datetime(expires_at)),
        }
    elif source == "manual":
        profile_name = cstr(profile.get("name")).strip()
        if (
            cstr(hold.reason_code).strip() != "manual_manager_pause"
            or not profile_name
            or cstr(hold.pos_profile).strip() != profile_name
        ):
            frappe.throw(
                _("This manual hold was not created by the manager Pause flow for this outlet"),
                frappe.PermissionError,
            )
        name = release_hold(
            hold_id,
            actor=cstr(access.get("user")).strip(),
            command_id=cstr(value["command_id"]).strip(),
            payload_hash=cstr(value["payload_hash"]).strip(),
        )
        response = {"status": "accepted", "hold_id": name}
    else:
        frappe.throw(
            _("POS can restore only its own manual manager hold or temporarily override an automation hold"),
            frappe.PermissionError,
        )
    frappe.db.commit()
    return response


@frappe.whitelist(methods=["POST"])
def confirm_count_reconciliation(*, device_id: str, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Submit one reviewed Stock Reconciliation under the inventory identity.

    The tablet supplies only the immutable observation identity and recorded
    staff identity.  The device/profile binding, task, warehouse, employee,
    manager token scope, and standard document are all rechecked here.  The
    already-submitted path is deliberately checked before consuming a token so
    a retry after a transport timeout is a harmless replay.
    """

    value = _parse_count_confirmation_payload(payload)
    device, profile = require_device_operational_scope(
        device_id=device_id,
        company=cstr(value.get("company")).strip() or None,
        warehouse=cstr(value.get("warehouse")).strip(),
    )
    device = lock_device_for_operational_mutation(device_id=device_id)
    authenticated_device_id = cstr(getattr(device, "device_id", None)).strip()
    if cstr(value.get("device_id")).strip() and cstr(value.get("device_id")).strip() != authenticated_device_id:
        frappe.throw(_("Count confirmation device_id does not match the authenticated device"), frappe.PermissionError)
    profile_company = cstr(getattr(profile, "company", None)).strip()
    profile_outlet = cstr(getattr(profile, "name", None)).strip()
    profile_warehouse = cstr(getattr(profile, "warehouse", None)).strip()
    if cstr(value.get("outlet")).strip() and cstr(value.get("outlet")).strip() != profile_outlet:
        frappe.throw(_("Count confirmation outlet is outside the authenticated device"), frappe.PermissionError)
    if not profile_company or not profile_warehouse or not profile_outlet:
        frappe.throw(_("The authenticated device has no company, outlet and warehouse binding"), frappe.ValidationError)

    staff_id = cstr(value.get("staff_user") or value.get("staff_id")).strip()
    access = _require_central_count_staff_access(
        staff_id=staff_id,
        company=profile_company,
        warehouse=profile_warehouse,
        outlet=profile_outlet,
    )
    supplied_employee = cstr(value.get("employee")).strip()
    central_employee = cstr(access.get("employee")).strip()
    if not supplied_employee or not central_employee or supplied_employee != central_employee:
        frappe.throw(_("The count employee does not match central KoPOS Staff Access"), frappe.PermissionError)
    supplied_version = value.get("device_config_version")
    if supplied_version is not None and supplied_version != cint(getattr(device, "config_version", 0)):
        frappe.throw(_("Device configuration changed; refresh before confirming this count"), frappe.ValidationError)

    observation_id = cstr(value["observation_id"]).strip()
    task_id = cstr(value["task_id"]).strip()
    task_revision = cint(value["task_revision"])
    warehouse = profile_warehouse
    command_id = cstr(value["command_id"]).strip()
    token = cstr(value["manager_approval_token"]).strip()
    task = frappe.get_doc("FB Inventory Count Task", task_id)
    if cstr(task.company).strip() != profile_company:
        frappe.throw(_("This count belongs to another company"), frappe.PermissionError)
    if cstr(task.warehouse).strip() != warehouse:
        frappe.throw(_("This count is outside the device warehouse"), frappe.PermissionError)
    if cint(task.revision) != task_revision:
        frappe.throw(_("This count assignment revision is stale"), frappe.ValidationError)
    if cstr(task.assignee).strip() != staff_id:
        frappe.throw(_("The recorded count staff member is not the task assignee"), frappe.PermissionError)
    if cstr(task.status).strip() not in {"Submitted", "Review", "Reviewed"}:
        frappe.throw(_("This count is not ready for manager confirmation"), frappe.ValidationError)

    observation = _load_count_confirmation_observation(observation_id)
    if not observation:
        frappe.throw(_("The count observation does not exist"), frappe.ValidationError)
    _lock_count_confirmation_observation(observation["name"])
    observation = _load_count_confirmation_observation(observation_id)
    if not observation:
        frappe.throw(_("The count observation does not exist"), frappe.ValidationError)
    _validate_count_confirmation_observation(
        observation=observation,
        observation_id=observation_id,
        task_id=task_id,
        task_revision=task_revision,
        warehouse=warehouse,
        staff_id=staff_id,
        employee=supplied_employee,
    )
    current_watermark = _stock_ledger_watermark(warehouse)
    observed_watermark = cstr(observation.get("stock_watermark")).strip()
    if current_watermark and current_watermark != observed_watermark:
        frappe.db.set_value(
            "FB Inventory Count Observation",
            observation["name"],
            "status",
            "Conflict",
            update_modified=False,
        )
        if frappe.db.exists("FB Inventory Count Task", task_id):
            frappe.db.set_value("FB Inventory Count Task", task_id, "status", "Review", update_modified=False)
        upsert_inventory_exception(
            reason_code="inventory_count_stale_watermark",
            summary="Stock moved before manager confirmation of a blind count",
            next_action="Create a new blind count after the stock ledger is stable",
            severity="Warning",
            company=profile_company,
            warehouse=warehouse,
            source_doctype="FB Inventory Count Observation",
            source_name=observation_id,
        )
        frappe.db.commit()
        frappe.throw(_("Stock moved before this count could be confirmed; the observation was preserved"), frappe.ValidationError)
    existing_command = cstr(observation.get("confirmation_command_id")).strip()
    if existing_command and existing_command != command_id:
        frappe.throw(_("This count observation was already used by another confirmation command"), frappe.ValidationError)
    conflicting_observation = frappe.db.get_value(
        "FB Inventory Count Observation",
        {"confirmation_command_id": command_id, "observation_id": ["!=", observation_id]},
        "observation_id",
    )
    if cstr(conflicting_observation).strip():
        frappe.throw(_("This confirmation command was already used for another count"), frappe.ValidationError)

    stock_change_required = _count_observation_stock_change_required(
        observation=observation,
        company=profile_company,
        warehouse=warehouse,
    )
    reconciliation_name = cstr(observation.get("reconciliation")).strip()
    if cstr(observation.get("status")).strip() == "Accepted" and existing_command == command_id:
        return {
            "status": "replayed",
            "observation_id": observation_id,
            "reconciliation": reconciliation_name or None,
            "document": reconciliation_name or observation_id,
            "stock_changed": stock_change_required,
        }
    if not reconciliation_name and stock_change_required:
        frappe.throw(_("The count observation has no draft reconciliation to confirm"), frappe.ValidationError)
    reconciliation = frappe.get_doc("Stock Reconciliation", reconciliation_name) if reconciliation_name else None
    if reconciliation is not None and cint(getattr(reconciliation, "docstatus", 0)) == 1:
        _record_count_confirmation_identity(
            observation["name"],
            command_id=command_id,
            device_id=authenticated_device_id or device_id,
        )
        frappe.db.commit()
        return {"status": "replayed", "observation_id": observation_id, "reconciliation": reconciliation.name}
    if reconciliation is not None and cint(getattr(reconciliation, "docstatus", 0)) != 0:
        frappe.throw(_("The count reconciliation is no longer a draft"), frappe.ValidationError)
    if cstr(observation.get("status")).strip() == "Conflict":
        frappe.throw(_("This count cannot be confirmed because stock moved before review"), frappe.ValidationError)

    context = {
        "task_id": task_id,
        "task_revision": task_revision,
        "observation_id": observation_id,
        "warehouse": warehouse,
    }
    try:
        approval = verify_manager_approval_token(
            token,
            device_id=device_id,
            staff_id=staff_id,
            action="inventory_count_reconciliation",
            shift_id=task_id,
            resource_id=observation_id,
            amount_sen=0,
            context_hash=canonical_context_hash(context),
            idempotency_key=command_id,
        )
    except Exception:
        frappe.throw(_("Manager approval for this count is invalid or has already been used"), frappe.PermissionError)
    manager_id = cstr(approval.get("manager_id")).strip()
    if not stock_change_required:
        # ERPNext correctly rejects an empty Stock Reconciliation. An exact
        # blind count still needs manager approval, but it must close as
        # audited evidence without inventing a stock movement. Older versions
        # created a draft before discovering that it contained no changes, so
        # remove that draft while accepting the immutable observation.
        if reconciliation is not None:
            frappe.db.set_value(
                "FB Inventory Count Observation",
                observation["name"],
                "reconciliation",
                None,
                update_modified=False,
            )
            frappe.delete_doc(
                "Stock Reconciliation",
                reconciliation.name,
                ignore_permissions=True,
                force=True,
            )
        _record_count_confirmation_identity(
            observation["name"],
            command_id=command_id,
            device_id=authenticated_device_id or device_id,
            manager_id=manager_id,
            status="Accepted",
        )
        if frappe.db.exists("FB Inventory Count Task", task_id):
            frappe.db.set_value("FB Inventory Count Task", task_id, "status", "Reviewed", update_modified=False)
        frappe.db.commit()
        return {
            "status": "accepted",
            "observation_id": observation_id,
            "reconciliation": None,
            "document": observation_id,
            "stock_changed": False,
        }
    if reconciliation is None:
        frappe.throw(_("The count observation has no draft reconciliation to confirm"), frappe.ValidationError)
    try:
        with inventory_automation_identity(
            company=profile_company,
            warehouse=warehouse,
            create_doctypes=("Stock Reconciliation",),
            submit_doctypes=("Stock Reconciliation",),
        ):
            reconciliation.submit()
    except AutomationIdentityError as error:
        frappe.throw(_("Count reconciliation automation is not configured safely: {0}").format(error.reason), frappe.ValidationError)
    _record_count_confirmation_identity(
        observation["name"],
        command_id=command_id,
        device_id=authenticated_device_id or device_id,
        manager_id=manager_id,
        status="Accepted",
    )
    if frappe.db.exists("FB Inventory Count Task", task_id):
        frappe.db.set_value("FB Inventory Count Task", task_id, "status", "Reviewed", update_modified=False)
    frappe.db.commit()
    return {"status": "accepted", "observation_id": observation_id, "reconciliation": reconciliation.name}


@frappe.whitelist(methods=["POST"])
def create_count_reconciliation_after_director_review(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Create a draft reconciliation only after a Company Director reviews a protected count.

    The tablet never sees a valuation or the hidden review ceiling.  This ERP
    action preserves the original observation and checks the stock watermark a
    second time before it creates the normal draft Stock Reconciliation.
    """

    _require_company_director("Count variance review")
    value = _parse_json_object(payload, "Count variance review payload")
    observation_id = cstr(value.get("observation_id")).strip()
    reason = cstr(value.get("reason")).strip()
    if not observation_id or not reason:
        frappe.throw(_("Count variance review requires observation_id and reason"), frappe.ValidationError)
    if len(reason) > 2_000:
        frappe.throw(_("Count variance review reason is too long"), frappe.ValidationError)
    observation_fields = [
        "name", "company", "task_id", "warehouse", "stock_watermark",
        "observed_at", "lines_json", "status", "reconciliation",
    ]
    # Authorize before taking a row lock. A director must not be able to hold a
    # row belonging to another company merely by guessing its public identity.
    unlocked_observation = frappe.db.get_value(
        "FB Inventory Count Observation",
        {"observation_id": observation_id},
        observation_fields,
        as_dict=True,
    )
    if not unlocked_observation:
        frappe.throw(_("The count observation does not exist"), frappe.ValidationError)
    unlocked_company = cstr(unlocked_observation.get("company")).strip()
    unlocked_warehouse = cstr(unlocked_observation.get("warehouse")).strip()
    if not unlocked_company:
        unlocked_task = frappe.get_doc(
            "FB Inventory Count Task", cstr(unlocked_observation.get("task_id"))
        )
        unlocked_company = cstr(getattr(unlocked_task, "company", None)).strip()
    resolve_director_context(company=unlocked_company, warehouse=unlocked_warehouse)

    # Serialize competing clicks only after the first scope check. The
    # deterministic Stock Reconciliation name remains the final replay guard,
    # while this row lock prevents two permitted directors from both inserting it.
    frappe.db.sql(
        "SELECT name FROM `tabFB Inventory Count Observation` WHERE observation_id = %s FOR UPDATE",
        (observation_id,),
    )
    observation = frappe.db.get_value(
        "FB Inventory Count Observation",
        {"observation_id": observation_id},
        observation_fields,
        as_dict=True,
    )
    if not observation:
        frappe.throw(_("The count observation does not exist"), frappe.ValidationError)
    task = None
    company = cstr(observation.get("company")).strip()
    warehouse = cstr(observation.get("warehouse")).strip()
    if not company:
        task = frappe.get_doc("FB Inventory Count Task", cstr(observation.get("task_id")))
        company = cstr(getattr(task, "company", None)).strip()
    # Recheck the locked row in case its scope changed between the first read
    # and the lock. No mutation happens until this second check succeeds.
    resolve_director_context(company=company, warehouse=warehouse)
    existing_reconciliation = cstr(observation.get("reconciliation")).strip()
    if existing_reconciliation:
        # Keep replay independent of the task row where possible.  The
        # observation already carries the company used to open the exception;
        # older rows may need the task as a compatibility fallback.
        _resolve_count_director_review_exception(
            company=company,
            warehouse=warehouse,
            observation_id=observation_id,
        )
        frappe.db.commit()
        return {
            "status": "replayed",
            "observation_id": observation_id,
            "reconciliation": existing_reconciliation,
        }
    if cstr(observation.get("status")).strip() != "Review":
        frappe.throw(_("This count does not require a director variance review"), frappe.ValidationError)
    task = frappe.get_doc("FB Inventory Count Task", cstr(observation.get("task_id")))
    company = company or cstr(getattr(task, "company", None)).strip()
    current_watermark = _stock_ledger_watermark(warehouse)
    if current_watermark and current_watermark != cstr(observation.get("stock_watermark")).strip():
        frappe.db.set_value("FB Inventory Count Observation", observation["name"], "status", "Conflict", update_modified=False)
        if frappe.db.exists("FB Inventory Count Task", observation["task_id"]):
            frappe.db.set_value("FB Inventory Count Task", observation["task_id"], "status", "Review", update_modified=False)
        exception = upsert_inventory_exception(
            reason_code="inventory_count_stale_watermark",
            summary="A director-reviewed count was preserved but stock moved before reconciliation",
            next_action="Create a new blind count after the stock ledger is stable",
            severity="Warning",
            company=company,
            warehouse=warehouse,
            source_doctype="FB Inventory Count Observation",
            source_name=observation_id,
        )
        frappe.db.commit()
        return {"status": "conflict", "observation_id": observation_id, "exception": exception}
    reconciliation_name = "KOPOS-COUNT-" + hashlib.sha256(observation_id.encode("utf-8")).hexdigest()[:24]
    if frappe.db.exists("Stock Reconciliation", reconciliation_name):
        frappe.db.set_value("FB Inventory Count Observation", observation["name"], "reconciliation", reconciliation_name, update_modified=False)
        frappe.db.commit()
        _resolve_count_director_review_exception(
            company=company,
            warehouse=warehouse,
            observation_id=observation_id,
        )
        frappe.db.commit()
        return {"status": "replayed", "observation_id": observation_id, "reconciliation": reconciliation_name}
    try:
        lines = json.loads(cstr(observation.get("lines_json")))
    except (TypeError, ValueError, json.JSONDecodeError):
        frappe.throw(_("The preserved count lines are invalid"), frappe.ValidationError)
    if not isinstance(lines, list) or not lines:
        frappe.throw(_("The preserved count lines are invalid"), frappe.ValidationError)
    reconciliation = frappe.new_doc("Stock Reconciliation")
    reconciliation.flags.ignore_permissions = True
    reconciliation.name = reconciliation_name
    reconciliation.company = cstr(task.company)
    reconciliation.purpose = "Stock Reconciliation"
    # Stored rows are site-local naive once the device write is coerced, but a row
    # written before that fix can still carry an offset; _site_local_naive leaves a
    # naive value untouched and normalises an aware one.
    posted_at = _site_local_naive(get_datetime(observation.get("observed_at")))
    reconciliation.posting_date = posted_at.date()
    reconciliation.posting_time = posted_at.time()
    reconciliation.items = []
    for line in lines:
        if not isinstance(line, dict):
            frappe.throw(_("The preserved count lines are invalid"), frappe.ValidationError)
        reconciliation.append("items", {
            "item_code": cstr(line.get("item_id")).strip(),
            "warehouse": warehouse,
            "qty": line.get("quantity"),
            "uom": cstr(line.get("uom") or "Nos").strip(),
        })
    reconciliation.insert(ignore_permissions=True)
    fields = {
        "reconciliation": reconciliation.name,
        "director_reviewed_by": cstr(getattr(frappe.session, "user", None)).strip(),
        "director_reviewed_at": now_datetime(),
        "director_review_reason": reason,
    }
    available = {
        fieldname: fieldvalue
        for fieldname, fieldvalue in fields.items()
        if frappe.get_meta("FB Inventory Count Observation").has_field(fieldname)
    }
    if available:
        frappe.db.set_value("FB Inventory Count Observation", observation["name"], available, update_modified=False)
    frappe.db.set_value("FB Inventory Count Task", task.name, "status", "Review", update_modified=False)
    frappe.db.commit()
    _resolve_count_director_review_exception(
        company=company,
        warehouse=warehouse,
        observation_id=observation_id,
    )
    frappe.db.commit()
    return {"status": "reviewed", "observation_id": observation_id, "reconciliation": reconciliation.name}


@frappe.whitelist(methods=["POST"])
def accept_preparation_task(*, device_id: str, payload: str | dict[str, Any]) -> dict[str, Any]:
    return _handle_guided_task(device_id=device_id, payload=payload, task_type="accept_preparation_task")


@frappe.whitelist(methods=["POST"])
def start_preparation_task(*, device_id: str, payload: str | dict[str, Any]) -> dict[str, Any]:
    return _handle_guided_task(device_id=device_id, payload=payload, task_type="start_preparation_task")


@frappe.whitelist(methods=["POST"])
def complete_preparation_task(*, device_id: str, payload: str | dict[str, Any]) -> dict[str, Any]:
    return _handle_guided_task(device_id=device_id, payload=payload, task_type="complete_preparation_task")


@frappe.whitelist(methods=["POST"])
def submit_purchase_receipt(*, device_id: str, payload: str | dict[str, Any]) -> dict[str, Any]:
    return _handle_guided_task(device_id=device_id, payload=payload, task_type="submit_purchase_receipt")


@frappe.whitelist(methods=["POST"])
def submit_transfer_dispatch(*, device_id: str, payload: str | dict[str, Any]) -> dict[str, Any]:
    return _handle_guided_task(device_id=device_id, payload=payload, task_type="submit_transfer_dispatch")


@frappe.whitelist(methods=["POST"])
def submit_transfer_receipt(*, device_id: str, payload: str | dict[str, Any]) -> dict[str, Any]:
    return _handle_guided_task(device_id=device_id, payload=payload, task_type="submit_transfer_receipt")


@frappe.whitelist(methods=["POST"])
def record_waste_event(*, device_id: str, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Record physical waste and issue its stock immediately and exactly once."""

    return _handle_guided_task(device_id=device_id, payload=payload, task_type="record_waste_event")


@frappe.whitelist(methods=["POST"])
def submit_count_observation(*, device_id: str, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Persist one blind-count observation and at most one draft reconciliation."""

    device = require_device_context(device_id=device_id)
    value = _parse_count_payload(payload)
    if cstr(value.get("device_id")).strip() != cstr(device_id).strip():
        frappe.throw(_("Count observation device_id does not match the authenticated device"), frappe.PermissionError)
    device = lock_device_for_operational_mutation(device_id=device_id)
    if value.get("device_config_version") != cint(getattr(device, "config_version", 0)):
        frappe.throw(_("Device configuration changed; refresh before submitting this count"), frappe.ValidationError)
    profile = resolve_catalog_pos_profile(device_id=device_id) or {}
    warehouse = cstr(profile.get("warehouse")).strip()
    if not warehouse or warehouse != value["warehouse"]:
        frappe.throw(_("Count warehouse does not match the device warehouse"), frappe.ValidationError)
    if cstr(profile.get("company")).strip() != cstr(value.get("company")).strip():
        frappe.throw(_("Count company does not match the device company"), frappe.PermissionError)
    if cstr(profile.get("name")).strip() != cstr(value.get("outlet")).strip():
        frappe.throw(_("Count outlet does not match the device outlet"), frappe.PermissionError)
    if cstr(value.get("source_document")).strip() != cstr(value.get("task_id")).strip() or value.get("source_revision") != value.get("task_revision"):
        frappe.throw(_("Count source identity does not match its task"), frappe.ValidationError)
    observation_id = cstr(value["observation_id"]).strip()
    observation_name = "KOPOS-COUNT-" + hashlib.sha256(observation_id.encode("utf-8")).hexdigest()[:24]
    observation_doctype_exists = frappe.db.exists("DocType", "FB Inventory Count Observation")
    observation_hash = _payload_hash(value)
    existing_observation = (
        frappe.db.get_value(
            "FB Inventory Count Observation", {"observation_id": observation_id}, ["name", "status", "reconciliation", "payload_hash"]
        )
        if observation_doctype_exists
        else None
    )
    if existing_observation:
        existing_hash = cstr(existing_observation[3]).strip()
        if existing_hash and existing_hash != observation_hash:
            frappe.throw(_("Count observation ID was reused with different content"), frappe.ValidationError)
        return {"status": "replayed", "observation_id": observation_id, "reconciliation": cstr(existing_observation[2]) or None}
    if observation_doctype_exists and frappe.get_meta("FB Inventory Count Observation").has_field("command_id"):
        conflicting_command = frappe.db.get_value(
            "FB Inventory Count Observation",
            {"command_id": value["command_id"], "observation_id": ["!=", observation_id]},
            "observation_id",
        )
        if cstr(conflicting_command).strip():
            frappe.throw(_("Count command_id was already used for another observation"), frappe.ValidationError)
    actor_access = _validate_count_assignment(device, value, warehouse)
    if cstr(actor_access.get("employee")).strip() != cstr(value.get("employee")).strip():
        frappe.throw(_("Count Employee does not match central KoPOS Staff Access"), frappe.PermissionError)
    watermark_rows = frappe.db.sql(
        "SELECT MAX(modified) FROM `tabStock Ledger Entry` WHERE warehouse = %s",
        (warehouse,),
    )
    current_watermark = cstr(watermark_rows[0][0] if watermark_rows else "").strip()
    conflict = bool(current_watermark and current_watermark != cstr(value["stock_watermark"]).strip())
    variance = _count_variance(
        company=cstr(profile.get("company")).strip(),
        warehouse=warehouse,
        lines=value["lines"],
    )
    stock_change_required = variance["absolute_quantity"] != Decimal("0")
    requires_director = _count_requires_director_review(warehouse=warehouse, variance=variance)
    observation_doc = None
    if observation_doctype_exists:
        observation_doc = frappe.new_doc("FB Inventory Count Observation")
        observation_doc.observation_id = observation_id
        observation_doc.payload_hash = observation_hash
        if frappe.get_meta("FB Inventory Count Observation").has_field("schema_version"):
            observation_doc.schema_version = value["schema_version"]
        if frappe.get_meta("FB Inventory Count Observation").has_field("command_id"):
            observation_doc.command_id = value["command_id"]
        if frappe.get_meta("FB Inventory Count Observation").has_field("device_id"):
            observation_doc.device_id = value["device_id"]
        if frappe.get_meta("FB Inventory Count Observation").has_field("device_config_version"):
            observation_doc.device_config_version = value["device_config_version"]
        if frappe.get_meta("FB Inventory Count Observation").has_field("staff_user"):
            observation_doc.staff_user = value["staff_user"]
        if frappe.get_meta("FB Inventory Count Observation").has_field("company"):
            observation_doc.company = value["company"]
        if frappe.get_meta("FB Inventory Count Observation").has_field("outlet"):
            observation_doc.outlet = value["outlet"]
        if frappe.get_meta("FB Inventory Count Observation").has_field("source_document"):
            observation_doc.source_document = value["source_document"]
        if frappe.get_meta("FB Inventory Count Observation").has_field("source_revision"):
            observation_doc.source_revision = value["source_revision"]
        observation_doc.task_id = value["task_id"]
        observation_doc.task_revision = value["task_revision"]
        observation_doc.warehouse = warehouse
        observation_doc.actor_id = value["actor_id"]
        if frappe.get_meta("FB Inventory Count Observation").has_field("employee"):
            observation_doc.employee = cstr(actor_access.get("employee")).strip()
        observation_doc.stock_watermark = value["stock_watermark"]
        observation_doc.observed_at = _site_local_naive(
            _require_explicit_offset_timestamp(value["observed_at"], "Count observation observed_at")
        )
        observation_doc.lines_json = json.dumps(value["lines"], sort_keys=True, separators=(",", ":"))
        # A safe observation still needs an online manager confirmation before
        # ERP submits the Draft Stock Reconciliation.  Only a stock-watermark
        # conflict is terminal at this boundary; a director-gated observation
        # remains Review until the director creates its draft.
        observation_doc.status = "Conflict" if conflict else "Review"
        if frappe.get_meta("FB Inventory Count Observation").has_field("variance_percent"):
            # Persist the Decimal _count_variance already computed. This row is
            # director evidence, so the stored number should be the one the
            # decision used, not a float round trip of it.
            observation_doc.variance_percent = format(variance["percent"], "f")
        if frappe.get_meta("FB Inventory Count Observation").has_field("variance_value"):
            observation_doc.variance_value = variance["value"]
        if frappe.get_meta("FB Inventory Count Observation").has_field("variance_requires_director"):
            observation_doc.variance_requires_director = 1 if requires_director else 0
        # The API has already authenticated the device, actor, assignment and
        # warehouse. Device users intentionally have no direct permission on
        # inventory evidence DocTypes; the service boundary owns this write.
        observation_doc.insert(ignore_permissions=True)
    if conflict:
        if frappe.db.exists("FB Inventory Count Task", value["task_id"]):
            frappe.db.set_value("FB Inventory Count Task", value["task_id"], "status", "Review", update_modified=False)
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
    if requires_director:
        if frappe.db.exists("FB Inventory Count Task", value["task_id"]):
            frappe.db.set_value("FB Inventory Count Task", value["task_id"], "status", "Review", update_modified=False)
        exception = upsert_inventory_exception(
            reason_code="inventory_count_director_review",
            summary="A blind count exceeds the configured review threshold",
            next_action="A Company Director must review the count before a Stock Reconciliation is created",
            severity="Warning",
            company=cstr(profile.get("company")),
            warehouse=warehouse,
            source_doctype="FB Inventory Count Observation",
            source_name=observation_id,
        )
        frappe.db.commit()
        return {
            "status": "review_required",
            "observation_id": observation_id,
            "exception": exception,
            "reconciliation": None,
        }
    if not stock_change_required:
        if frappe.db.exists("FB Inventory Count Task", value["task_id"]):
            frappe.db.set_value("FB Inventory Count Task", value["task_id"], "status", "Submitted", update_modified=False)
        frappe.db.commit()
        review = _count_review_projection(
            task_name=value["task_id"],
            task_revision=value["task_revision"],
            warehouse=warehouse,
            assignee=value["actor_id"],
        )
        return {
            "status": "review_required",
            "observation_id": observation_id,
            "reconciliation": None,
            "review": review,
        }
    existing = frappe.db.exists("Stock Reconciliation", observation_name)
    if existing:
        return {"status": "replayed", "observation_id": observation_id, "reconciliation": existing}
    reconciliation = frappe.new_doc("Stock Reconciliation")
    reconciliation.flags.ignore_permissions = True
    reconciliation.name = observation_name
    reconciliation.company = cstr(profile.get("company"))
    reconciliation.purpose = "Stock Reconciliation"
    # Post in site-local time. Taking .date() off the sender's offset put a count
    # recorded at 23:30 +08:00 on the previous accounting day.
    posted_at = _site_local_naive(
        _require_explicit_offset_timestamp(value["observed_at"], "Count observation observed_at")
    )
    reconciliation.posting_date = posted_at.date()
    reconciliation.posting_time = posted_at.time()
    reconciliation.items = []
    for line in value["lines"]:
        reconciliation.append("items", {
            "item_code": cstr(line["item_id"]).strip(),
            "warehouse": warehouse,
            "qty": line["quantity"],
            "uom": cstr(line.get("uom") or "Nos").strip(),
        })
    reconciliation.insert(ignore_permissions=True)
    if observation_doc is not None:
        observation_doc.reconciliation = reconciliation.name
        observation_doc.save(ignore_permissions=True)
    if frappe.db.exists("FB Inventory Count Task", value["task_id"]):
        frappe.db.set_value("FB Inventory Count Task", value["task_id"], "status", "Submitted", update_modified=False)
    frappe.db.commit()
    review = _count_review_projection(
        task_name=value["task_id"],
        task_revision=value["task_revision"],
        warehouse=warehouse,
        assignee=value["actor_id"],
    )
    return {
        "status": "review_required",
        "observation_id": observation_id,
        "reconciliation": reconciliation.name,
        "review": review,
    }


@frappe.whitelist(methods=["POST"])
def report_device_inventory_state(*, device_id: str, payload: str | dict[str, Any]) -> dict[str, Any]:
    device = require_device_context(device_id=device_id)
    report = _parse_report_payload(payload)
    if cstr(report.get("device_id")).strip() != cstr(device_id).strip():
        frappe.throw(_("Inventory report device_id does not match the authenticated device"), frappe.ValidationError)
    revision = report["report_revision"]
    payload_hash = _payload_hash(report)
    device = lock_device_for_operational_mutation(device_id=device_id)
    current_revision = cint(getattr(device, "inventory_report_revision", 0))
    current_hash = cstr(getattr(device, "inventory_report_payload_hash", None)).strip()
    if revision < current_revision:
        # A device state report is a *snapshot*, not an event: only the newest one
        # carries meaning. An offline-first outbox retries, so older snapshots
        # legitimately arrive after newer ones. Throwing made every late snapshot a
        # permanent failure the tablet retried forever, which starved the real
        # inventory commands queued behind it. Acknowledge and drop it instead.
        return {
            "status": "superseded",
            "device_id": device_id,
            "report_revision": current_revision,
        }
    if revision == current_revision and current_hash and current_hash != payload_hash:
        frappe.throw(_("Device inventory report revision was reused with different content"), frappe.ValidationError)
    if revision == current_revision and current_hash == payload_hash:
        return {"status": "replayed", "device_id": device_id, "report_revision": revision}
    if cint(report["config_version"]) != cint(getattr(device, "config_version", 0)):
        frappe.throw(_("Device configuration changed; refresh before reporting inventory state"), frappe.ValidationError)
    device_values = {
        "inventory_report_schema_version": report["schema_version"],
        "inventory_report_revision": revision,
        "inventory_observed_at": _site_local_naive(
            _require_explicit_offset_timestamp(report["observed_at"], "Inventory report observed_at")
        ),
        "inventory_report_received_at": now_datetime(),
        "inventory_config_version": report["config_version"],
        "inventory_catalog_version": report["catalog_version"],
        "inventory_overlay_version": report["overlay_version"],
        "inventory_overlay_hash": report["overlay_hash"],
        "inventory_sales_pending": report["sales_outbox"]["pending"],
        "inventory_sales_syncing": report["sales_outbox"]["syncing"],
        "inventory_sales_failed": report["sales_outbox"]["failed"],
        "inventory_sales_dead_letter": report["sales_outbox"]["dead_letter"],
        "inventory_oldest_unsaved_sale_at": _site_local_naive(
            _require_explicit_offset_timestamp(
                report["oldest_unsaved_sale_timestamp"], "Inventory report oldest_unsaved_sale_timestamp"
            )
        )
        if report["oldest_unsaved_sale_timestamp"] is not None
        else None,
        "inventory_commands_pending": report["inventory_outbox"]["pending"],
        "inventory_commands_syncing": report["inventory_outbox"]["syncing"],
        "inventory_commands_failed": report["inventory_outbox"]["failed"],
        "inventory_commands_dead_letter": report["inventory_outbox"]["dead_letter"],
        "inventory_report_payload_hash": payload_hash,
    }
    frappe.db.set_value("KoPOS Device", device.name, device_values, update_modified=False)
    frappe.db.commit()
    return {"status": "accepted", "device_id": device_id, "report_revision": revision}


def _parse_report_payload(payload: str | dict[str, Any]) -> dict[str, Any]:
    value = _parse_device_json_object(payload, "Inventory report")
    required = {"schema_version", "device_id", "config_version", "report_revision", "observed_at", "catalog_version", "overlay_version", "overlay_hash", "sales_outbox", "inventory_outbox", "oldest_unsaved_sale_timestamp"}
    if set(value) != required:
        frappe.throw(_("Inventory report fields are incomplete or unexpected"), frappe.ValidationError)
    if cstr(value.get("schema_version")) != "inventory-device-state-v2":
        frappe.throw(_("Unsupported inventory report schema version"), frappe.ValidationError)
    if isinstance(value["report_revision"], bool) or not isinstance(value["report_revision"], int) or value["report_revision"] < 1:
        frappe.throw(_("Inventory report revision must be a positive integer"), frappe.ValidationError)
    if isinstance(value["config_version"], bool) or not isinstance(value["config_version"], int) or value["config_version"] < 0:
        frappe.throw(_("Inventory report config_version must be a non-negative integer"), frappe.ValidationError)
    for fieldname in ("catalog_version", "overlay_version", "overlay_hash"):
        if not isinstance(value[fieldname], str) or not value[fieldname].strip():
            frappe.throw(_("Inventory report {0} is required").format(fieldname), frappe.ValidationError)
    _require_explicit_offset_timestamp(
        value["observed_at"], "Inventory report observed_at"
    )
    oldest_sale = value.get("oldest_unsaved_sale_timestamp")
    if oldest_sale is not None:
        _require_explicit_offset_timestamp(
            oldest_sale, "Inventory report oldest_unsaved_sale_timestamp"
        )
    expected_queue_keys = {
        "sales_outbox": {"pending", "syncing", "failed", "dead_letter"},
        "inventory_outbox": {"pending", "syncing", "failed", "dead_letter"},
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
    value = _parse_device_json_object(payload, "Count observation")
    required = {
        "schema_version", "command_id", "task_type", "device_id", "device_config_version",
        "staff_user", "employee", "company", "outlet", "warehouse", "source_document",
        "source_revision", "payload_hash", "observation_id", "task_id", "task_revision",
        "actor_id", "stock_watermark", "observed_at", "lines",
    }
    if not isinstance(value, dict) or set(value) != required:
        frappe.throw(_("Count observation fields are incomplete or unexpected"), frappe.ValidationError)
    if cstr(value.get("schema_version")).strip() != "inventory-command-v1":
        frappe.throw(_("Unsupported inventory count command schema version"), frappe.ValidationError)
    if cstr(value.get("task_type")).strip() != "submit_count_observation":
        frappe.throw(_("Inventory count task_type is invalid"), frappe.ValidationError)
    if not isinstance(value.get("device_id"), str) or not cstr(value["device_id"]).strip():
        frappe.throw(_("Count observation device_id is required"), frappe.ValidationError)
    if not isinstance(value.get("device_config_version"), int) or isinstance(value.get("device_config_version"), bool) or value["device_config_version"] < 0:
        frappe.throw(_("Count observation device_config_version is invalid"), frappe.ValidationError)
    for fieldname in ("command_id", "staff_user", "employee", "company", "outlet", "source_document"):
        if not isinstance(value.get(fieldname), str) or not cstr(value[fieldname]).strip():
            frappe.throw(_("Count observation {0} is required").format(fieldname), frappe.ValidationError)
    payload_hash = cstr(value.get("payload_hash")).strip()
    if len(payload_hash) != 64 or payload_hash != payload_hash.lower() or any(character not in "0123456789abcdef" for character in payload_hash):
        frappe.throw(_("Count observation payload_hash must be a lowercase SHA-256 digest"), frappe.ValidationError)
    try:
        expected_hash = _payload_hash({key: item for key, item in value.items() if key != "payload_hash"})
    except (TypeError, ValueError):
        frappe.throw(_("Count observation payload contains unsupported data"), frappe.ValidationError)
    if expected_hash != payload_hash:
        frappe.throw(_("Count observation payload_hash does not match the command payload"), frappe.ValidationError)
    if isinstance(value["task_revision"], bool) or not isinstance(value["task_revision"], int) or value["task_revision"] < 1:
        frappe.throw(_("Count task revision must be positive"), frappe.ValidationError)
    if value.get("source_document") != value.get("task_id") or value.get("source_revision") != value.get("task_revision"):
        frappe.throw(_("Count observation source identity does not match its task"), frappe.ValidationError)
    if value.get("staff_user") != value.get("actor_id"):
        frappe.throw(_("Count observation staff_user and actor_id conflict"), frappe.ValidationError)
    if not isinstance(value["lines"], list) or not value["lines"]:
        frappe.throw(_("Count observation must contain lines"), frappe.ValidationError)
    if len(value["lines"]) > _DEVICE_MAX_LINES:
        frappe.throw(
            _("Count observation cannot contain more than {0} lines").format(_DEVICE_MAX_LINES),
            frappe.ValidationError,
        )
    for fieldname in ("observation_id", "task_id", "warehouse", "actor_id", "stock_watermark", "observed_at"):
        if not isinstance(value[fieldname], str) or not value[fieldname].strip():
            frappe.throw(_("Count observation {0} is required").format(fieldname), frappe.ValidationError)
    _require_explicit_offset_timestamp(
        value["observed_at"], "Count observation observed_at"
    )
    for line in value["lines"]:
        if not isinstance(line, dict) or set(line) != {
            "item_id", "stock_uom", "purchase_uom", "conversion_factor",
            "full_packs", "loose_quantity", "total_quantity",
        } or not cstr(line.get("item_id")).strip():
            frappe.throw(_("Count line is malformed"), frappe.ValidationError)
        if not all(isinstance(line.get(fieldname), str) and cstr(line.get(fieldname)).strip() for fieldname in ("stock_uom", "purchase_uom", "conversion_factor", "full_packs", "loose_quantity", "total_quantity")):
            frappe.throw(_("Count line units and quantities are required"), frappe.ValidationError)
        for fieldname in ("conversion_factor", "full_packs", "loose_quantity", "total_quantity"):
            if len(line[fieldname]) > _DEVICE_MAX_DECIMAL_TEXT_LENGTH:
                frappe.throw(
                    _("Count line {0} is too long").format(fieldname),
                    frappe.ValidationError,
                )
        try:
            full_packs = Decimal(str(line["full_packs"]))
            loose_quantity = Decimal(str(line["loose_quantity"]))
            total_quantity = Decimal(str(line["total_quantity"]))
            conversion_factor = Decimal(str(line["conversion_factor"]))
        except (InvalidOperation, ValueError, TypeError):
            frappe.throw(_("Count pack or loose quantity is invalid"), frappe.ValidationError)
            raise AssertionError("frappe.throw must raise")
        if any(not number.is_finite() or number < 0 for number in (full_packs, loose_quantity, total_quantity)) or not conversion_factor.is_finite() or conversion_factor <= 0:
            frappe.throw(_("Count pack, loose, and total quantities must be zero or more, with a positive conversion"), frappe.ValidationError)
        if full_packs != full_packs.to_integral_value():
            frappe.throw(_("Full packs must be a whole number"), frappe.ValidationError)
    return value


def _parse_count_confirmation_payload(payload: str | dict[str, Any]) -> dict[str, Any]:
    value = _parse_device_json_object(payload, "Count confirmation payload")
    allowed = {
        "schema_version",
        "command_id",
        "task_type",
        "observation_id",
        "task_id",
        "task_revision",
        "warehouse",
        "staff_user",
        "staff_id",
        "employee",
        "company",
        "outlet",
        "device_id",
        "device_config_version",
        "source_document",
        "source_revision",
        "observed_at",
        "payload_hash",
        "manager_approval_token",
    }
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        frappe.throw(
            _("Count confirmation contains unsupported fields: {0}").format(", ".join(unexpected)),
            frappe.ValidationError,
        )
    required_text = (
        "schema_version", "command_id", "observation_id", "task_id", "warehouse", "employee",
        "company", "outlet", "device_id", "source_document", "observed_at",
        "payload_hash", "manager_approval_token",
    )
    for fieldname in required_text:
        if not isinstance(value.get(fieldname), str) or not value[fieldname].strip():
            frappe.throw(_("Count confirmation {0} is required").format(fieldname), frappe.ValidationError)
    staff_user = cstr(value.get("staff_user") or value.get("staff_id")).strip()
    if not staff_user:
        frappe.throw(_("Count confirmation staff_user is required"), frappe.ValidationError)
    if value.get("staff_user") and value.get("staff_id") and cstr(value["staff_user"]).strip() != cstr(value["staff_id"]).strip():
        frappe.throw(_("Count confirmation staff user fields conflict"), frappe.ValidationError)
    task_revision = value.get("task_revision")
    if isinstance(task_revision, bool) or not isinstance(task_revision, int) or task_revision < 1:
        frappe.throw(_("Count confirmation task_revision must be a positive integer"), frappe.ValidationError)
    command_id = cstr(value["command_id"]).strip()
    if len(command_id) > 160:
        frappe.throw(_("Count confirmation command_id is too long"), frappe.ValidationError)
    config_version = value.get("device_config_version")
    if isinstance(config_version, bool) or not isinstance(config_version, int) or config_version < 0:
        frappe.throw(_("Count confirmation device_config_version is invalid"), frappe.ValidationError)
    if value.get("schema_version") != "inventory-command-v1":
        frappe.throw(_("Count confirmation schema_version is invalid"), frappe.ValidationError)
    if value.get("task_type") != "confirm_count_reconciliation":
        frappe.throw(_("Count confirmation task_type is invalid"), frappe.ValidationError)
    if value.get("source_document") != value.get("task_id"):
        frappe.throw(_("Count confirmation source_document does not match task_id"), frappe.ValidationError)
    source_revision = value.get("source_revision")
    if source_revision != task_revision:
        frappe.throw(_("Count confirmation source_revision does not match task_revision"), frappe.ValidationError)
    _require_explicit_offset_timestamp(
        value["observed_at"], "Count confirmation observed_at"
    )
    payload_hash = cstr(value["payload_hash"]).strip()
    if len(payload_hash) != 64 or payload_hash != payload_hash.lower() or any(character not in "0123456789abcdef" for character in payload_hash):
        frappe.throw(_("Count confirmation payload_hash is invalid"), frappe.ValidationError)
    try:
        expected_hash = _payload_hash({key: item for key, item in value.items() if key != "payload_hash"})
    except (TypeError, ValueError):
        frappe.throw(_("Count confirmation payload contains unsupported data"), frappe.ValidationError)
    if expected_hash != payload_hash:
        frappe.throw(_("Count confirmation payload_hash does not match the command payload"), frappe.ValidationError)
    if not isinstance(value.get("device_id"), str) or not cstr(value["device_id"]).strip():
        frappe.throw(_("Count confirmation device_id is required"), frappe.ValidationError)
    value["staff_user"] = staff_user
    value["task_revision"] = task_revision
    return value


def _load_count_confirmation_observation(observation_id: str) -> dict[str, Any] | None:
    if not frappe.db.exists("DocType", "FB Inventory Count Observation"):
        return None
    meta = frappe.get_meta("FB Inventory Count Observation")
    if not meta.has_field("confirmation_command_id"):
        frappe.throw(_("Count confirmation identity fields are not installed; run bench migrate before confirming counts"), frappe.ValidationError)
    fields = [
        "name",
        "observation_id",
        "task_id",
        "task_revision",
        "warehouse",
        "actor_id",
        "employee",
        "stock_watermark",
        "status",
        "reconciliation",
        "confirmation_command_id",
        "confirmation_device_id",
        "confirmation_manager_id",
    ]
    fields.extend([fieldname for fieldname in ("lines_json",) if meta.has_field(fieldname)])
    return frappe.db.get_value(
        "FB Inventory Count Observation",
        {"observation_id": observation_id},
        fields,
        as_dict=True,
    )


def _lock_count_confirmation_observation(name: str) -> None:
    frappe.db.sql(
        "SELECT name FROM `tabFB Inventory Count Observation` WHERE name = %s LIMIT 1 FOR UPDATE",
        (name,),
    )


def _lock_count_task(task_name: str) -> None:
    """Serialize assignment so two tablets cannot both claim one blind count."""

    frappe.db.sql(
        "SELECT name FROM `tabFB Inventory Count Task` WHERE name = %s LIMIT 1 FOR UPDATE",
        (task_name,),
    )


def _validate_count_confirmation_observation(
    *,
    observation: dict[str, Any],
    observation_id: str,
    task_id: str,
    task_revision: int,
    warehouse: str,
    staff_id: str,
    employee: str,
) -> None:
    if cstr(observation.get("observation_id")).strip() != observation_id:
        frappe.throw(_("Count observation identity is inconsistent"), frappe.ValidationError)
    if cstr(observation.get("task_id")).strip() != task_id:
        frappe.throw(_("Count observation belongs to another count task"), frappe.ValidationError)
    if cint(observation.get("task_revision")) != task_revision:
        frappe.throw(_("Count observation revision is stale"), frappe.ValidationError)
    if cstr(observation.get("warehouse")).strip() != warehouse:
        frappe.throw(_("Count observation belongs to another warehouse"), frappe.PermissionError)
    if cstr(observation.get("actor_id")).strip() != staff_id:
        frappe.throw(_("The supplied staff user does not match the recorded count actor"), frappe.PermissionError)
    if cstr(observation.get("employee")).strip() != employee:
        frappe.throw(_("The supplied Employee does not match the recorded count Employee"), frappe.PermissionError)
    if not cstr(observation.get("stock_watermark")).strip():
        frappe.throw(_("The count observation has no stock ledger watermark"), frappe.ValidationError)
    if cstr(observation.get("status")).strip() not in {"Accepted", "Review"}:
        frappe.throw(_("This count observation is not eligible for manager confirmation"), frappe.ValidationError)


def _count_observation_stock_change_required(
    *, observation: dict[str, Any], company: str, warehouse: str
) -> bool:
    try:
        lines = json.loads(cstr(observation.get("lines_json")))
    except (TypeError, ValueError, json.JSONDecodeError):
        frappe.throw(_("The count observation lines cannot be verified"), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise")
    if not isinstance(lines, list) or not lines:
        frappe.throw(_("The count observation lines cannot be verified"), frappe.ValidationError)
    variance = _count_variance(company=company, warehouse=warehouse, lines=lines)
    return variance["absolute_quantity"] != Decimal("0")


def _record_count_confirmation_identity(
    observation_name: str,
    *,
    command_id: str,
    device_id: str,
    manager_id: str | None = None,
    status: str | None = None,
) -> None:
    meta = frappe.get_meta("FB Inventory Count Observation")
    values: dict[str, Any] = {}
    for fieldname, fieldvalue in {
        "confirmation_command_id": command_id,
        "confirmation_device_id": device_id,
        "confirmation_manager_id": manager_id,
        "confirmed_at": now_datetime() if manager_id else None,
        "status": status,
    }.items():
        if fieldvalue not in (None, "") and meta.has_field(fieldname):
            values[fieldname] = fieldvalue
    if values:
        frappe.db.set_value("FB Inventory Count Observation", observation_name, values, update_modified=False)


def _validate_count_assignment(device: Any, value: dict[str, Any], warehouse: str) -> dict[str, Any]:
    if not frappe.db.exists("DocType", "FB Inventory Count Task"):
        frappe.throw(_("Inventory count tasks are not installed"), frappe.ValidationError)
    task = frappe.get_doc("FB Inventory Count Task", value["task_id"])
    if cstr(task.status).strip() not in {"Claimed", "In Progress"}:
        frappe.throw(_("Claim this inventory count before submitting it"), frappe.ValidationError)
    if cint(task.revision) != value["task_revision"] or cstr(task.warehouse).strip() != warehouse:
        frappe.throw(_("Inventory count assignment revision or warehouse is stale"), frappe.ValidationError)
    allowed_actors = {
        cstr(row.get("user")).strip()
        for row in resolve_staff_access_for_device(device)
        if cstr(row.get("user")).strip()
    }
    allowed_actors.update({
        cstr(getattr(device, "api_user", None)).strip(),
        cstr(getattr(frappe.session, "user", None)).strip(),
    })
    if cstr(task.assignee).strip() not in allowed_actors or cstr(value["actor_id"]).strip() != cstr(task.assignee).strip():
        frappe.throw(_("Inventory count actor is not the assigned user"), frappe.PermissionError)
    expected_lines = {
        (cstr(getattr(line, "item_id", None)).strip(), cstr(getattr(line, "stock_uom", None) or getattr(line, "uom", None)).strip())
        for line in (task.lines or [])
    }
    observed_lines = [
        (cstr(line.get("item_id")).strip(), cstr(line.get("stock_uom")).strip())
        for line in value["lines"]
    ]
    if len(observed_lines) != len(set(observed_lines)) or set(observed_lines) != expected_lines:
        frappe.throw(_("Inventory count lines do not match the assigned task"), frappe.ValidationError)
    for task_line in task.lines or []:
        item_id = cstr(getattr(task_line, "item_id", None)).strip()
        stock_uom = cstr(getattr(task_line, "stock_uom", None) or getattr(task_line, "uom", None)).strip()
        purchase_uom = cstr(getattr(task_line, "purchase_uom", None)).strip()
        factor = _count_decimal(getattr(task_line, "conversion_factor", None))
        if not item_id or not stock_uom or not purchase_uom or factor is None or factor <= 0:
            frappe.throw(
                _("Count task line {0} has no frozen purchase-unit conversion; refresh the assignment after ERPNext is configured").format(item_id or "(unknown)"),
                frappe.ValidationError,
            )
        if cstr(getattr(task_line, "uom", None)).strip() and cstr(getattr(task_line, "uom", None)).strip() != stock_uom:
            frappe.throw(_("Count task line {0} has conflicting stock UOM values").format(item_id), frappe.ValidationError)
    value["lines"] = _normalize_count_observation_lines(task.lines or [], value["lines"])
    return next(
        (row for row in resolve_staff_access_for_device(device) if cstr(row.get("user")).strip() == cstr(value["actor_id"]).strip()),
        {"user": value["actor_id"], "employee": ""},
    )


def _count_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _count_decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized else "0"


def _normalize_count_observation_lines(
    task_lines: Iterable[Any], observed_lines: list[dict[str, Any]]
) -> list[dict[str, str]]:
    task_by_item = {
        cstr(getattr(line, "item_id", None)).strip(): line
        for line in task_lines
        if cstr(getattr(line, "item_id", None)).strip()
    }
    normalized: list[dict[str, str]] = []
    for line in observed_lines:
        item_id = cstr(line.get("item_id")).strip()
        task_line = task_by_item.get(item_id)
        if task_line is None:
            frappe.throw(_("Count line {0} is not part of the assigned task").format(item_id), frappe.ValidationError)
        stock_uom = cstr(getattr(task_line, "stock_uom", None) or getattr(task_line, "uom", None)).strip()
        purchase_uom = cstr(getattr(task_line, "purchase_uom", None)).strip()
        expected_factor = _count_decimal(getattr(task_line, "conversion_factor", None))
        supplied_stock = cstr(line.get("stock_uom")).strip()
        supplied_purchase = cstr(line.get("purchase_uom")).strip()
        supplied_factor = _count_decimal(line.get("conversion_factor"))
        if supplied_stock != stock_uom or supplied_purchase != purchase_uom or supplied_factor != expected_factor:
            frappe.throw(
                _("The unit conversion for {0} changed after this count opened; refresh the assignment").format(item_id),
                frappe.ValidationError,
            )
        full_packs = _count_decimal(line.get("full_packs"))
        loose = _count_decimal(line.get("loose_quantity"))
        supplied_total = _count_decimal(line.get("total_quantity"))
        if full_packs is None or loose is None or supplied_total is None or full_packs < 0 or loose < 0 or supplied_total < 0:
            frappe.throw(_("Count quantities for {0} must be zero or more").format(item_id), frappe.ValidationError)
        if full_packs != full_packs.to_integral_value():
            frappe.throw(_("Full packs for {0} must be a whole number").format(item_id), frappe.ValidationError)
        calculated = full_packs * expected_factor + loose
        if calculated != supplied_total:
            frappe.throw(
                _("The total for {0} does not match full packs × conversion plus loose stock").format(item_id),
                frappe.ValidationError,
            )
        normalized.append({
            "item_id": item_id,
            "uom": stock_uom,
            "stock_uom": stock_uom,
            "purchase_uom": purchase_uom,
            "conversion_factor": _count_decimal_text(expected_factor),
            "full_packs": _count_decimal_text(full_packs),
            "loose_quantity": _count_decimal_text(loose),
            "total_quantity": _count_decimal_text(calculated),
            # Standard Stock Reconciliation is expressed in the stock UOM.
            "quantity": _count_decimal_text(calculated),
        })
    return normalized


def _count_task_response(
    task: dict[str, Any], *, include_barcodes: bool = True
) -> dict[str, Any]:
    """Serialize a count task without expected quantities or financial data."""

    response = {
        "name": cstr(task.get("name")),
        "revision": cint(task.get("revision")),
        "warehouse": cstr(task.get("warehouse")),
        "assignee": cstr(task.get("assignee")),
        "stock_watermark": cstr(task.get("stock_watermark")),
    }
    line_fields = ["item_id", "uom"]
    try:
        line_meta = frappe.get_meta("FB Inventory Count Task Line")
        for fieldname in ("stock_uom", "purchase_uom", "conversion_factor"):
            if line_meta.has_field(fieldname):
                line_fields.append(fieldname)
    except Exception:
        # A pre-migration read must remain useful for directors; assignment
        # itself is still blocked until the frozen conversion fields exist.
        pass
    line_rows = frappe.get_all(
        "FB Inventory Count Task Line",
        filters={"parent": response["name"]},
        fields=line_fields,
        order_by="idx asc",
    )
    item_ids = sorted({cstr(row.get("item_id")).strip() for row in line_rows if cstr(row.get("item_id")).strip()})
    item_names: dict[str, str] = {}
    if item_ids:
        item_rows = frappe.get_all(
            "Item",
            filters={"name": ["in", item_ids]},
            fields=["name", "item_name", "item_code"],
            limit_page_length=len(item_ids),
        )
        item_names = {
            cstr(row.get("name")).strip(): cstr(row.get("item_name") or row.get("item_code") or row.get("name")).strip()
            for row in item_rows
            if cstr(row.get("name")).strip()
        }
    response["lines"] = []
    for row in line_rows:
        line = {
            "item_id": cstr(row.get("item_id")),
            "item_name": item_names.get(cstr(row.get("item_id")).strip(), cstr(row.get("item_id"))),
            "uom": cstr(row.get("uom")),
        }
        stock_uom = cstr(row.get("stock_uom") or row.get("uom")).strip()
        purchase_uom = cstr(row.get("purchase_uom")).strip()
        conversion_factor = cstr(row.get("conversion_factor")).strip()
        if stock_uom and purchase_uom and conversion_factor:
            line.update({
                "stock_uom": stock_uom,
                "purchase_uom": purchase_uom,
                "conversion_factor": conversion_factor,
            })
        response["lines"].append(line)
    if include_barcodes:
        _attach_task_line_barcodes([{"lines": response["lines"]}])
    review = _count_review_projection(
        task_name=response["name"],
        task_revision=response["revision"],
        warehouse=response["warehouse"],
        assignee=cstr(task.get("assignee")).strip(),
    )
    if review is not None:
        response["review"] = review
    return response


def _attach_task_line_barcodes(tasks: list[dict[str, Any]]) -> None:
    """Attach one scan code per item without making task reads depend on it."""

    item_codes = sorted({
        cstr(line.get("item_code") or line.get("item_id")).strip()
        for task in tasks
        for line in task.get("lines", [])
        if isinstance(line, dict)
        and cstr(line.get("item_code") or line.get("item_id")).strip()
    })
    if not item_codes:
        return
    try:
        barcodes = get_item_barcodes_map(item_codes)
    except Exception as error:
        log_sanitized_error("KoPOS inventory task barcode lookup failed", error)
        return
    for task in tasks:
        for line in task.get("lines", []):
            if not isinstance(line, dict):
                continue
            item_code = cstr(line.get("item_code") or line.get("item_id")).strip()
            barcode = cstr(barcodes.get(item_code)).strip()
            if barcode:
                line["barcode"] = barcode


def _count_review_projection(
    *, task_name: str, task_revision: int, warehouse: str, assignee: str
) -> dict[str, Any] | None:
    """Return only the accepted local observation's operational review.

    The observation is the immutable local evidence.  This projection exposes
    what a manager needs to confirm—counted quantities and the signed amount
    above or below the saved stock—without returning valuation, thresholds,
    or any other financial field.
    """

    if not task_name or not warehouse or not frappe.db.exists("DocType", "FB Inventory Count Observation"):
        return None
    filters: dict[str, Any] = {
        "task_id": task_name,
        "task_revision": task_revision,
        "warehouse": warehouse,
        "status": ["in", ["Accepted", "Review", "Conflict"]],
    }
    if assignee:
        filters["actor_id"] = assignee
    rows = frappe.get_all(
        "FB Inventory Count Observation",
        filters=filters,
        fields=["name", "observation_id", "status", "lines_json", "reconciliation"],
        order_by="modified desc",
        limit_page_length=10,
    )
    for row in rows:
        observation_id = cstr(row.get("observation_id")).strip()
        if not observation_id:
            continue
        try:
            lines = json.loads(cstr(row.get("lines_json")))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(lines, list) or not lines:
            continue
        review_lines: list[dict[str, Any]] = []
        malformed = False
        for line in lines:
            if not isinstance(line, dict):
                malformed = True
                break
            item_id = cstr(line.get("item_id")).strip()
            uom = cstr(line.get("stock_uom") or line.get("uom") or "Nos").strip()
            counted = _safe_count_quantity(line.get("total_quantity") or line.get("quantity"))
            if not item_id or not uom or counted is None:
                malformed = True
                break
            variance = _count_line_variance_details(
                warehouse=warehouse,
                item_id=item_id,
                counted_quantity=counted,
            )
            review_line: dict[str, Any] = {
                "item_id": item_id,
                "uom": uom,
                "counted_quantity": counted,
                "difference_quantity": variance["difference_quantity"] if variance else None,
                "variance_percent": variance["variance_percent"] if variance else None,
            }
            # Pack breakdown is operational evidence only.  Keep legacy
            # observations readable while exposing the explicit units for new
            # observations.
            if all(line.get(fieldname) not in (None, "") for fieldname in ("full_packs", "loose_quantity", "total_quantity", "purchase_uom", "conversion_factor")):
                review_line.update({
                    "stock_uom": uom,
                    "purchase_uom": cstr(line.get("purchase_uom")),
                    "conversion_factor": cstr(line.get("conversion_factor")),
                    "full_packs": _safe_count_quantity(line.get("full_packs")),
                    "loose_quantity": _safe_count_quantity(line.get("loose_quantity")),
                    "total_quantity": counted,
                })
            if cstr(line.get("item_name")).strip():
                review_line["item_name"] = cstr(line.get("item_name")).strip()
            review_lines.append(review_line)
        if malformed:
            continue
        status = cstr(row.get("status")).strip()
        # A large variance is held for Company Director review before a Draft
        # Stock Reconciliation exists. An exact match also has no draft because
        # ERPNext forbids empty reconciliations, but it is ready for the normal
        # manager evidence step.
        if status == "Review" and not cstr(row.get("reconciliation")).strip():
            try:
                no_stock_change = not _count_observation_stock_change_required(
                    observation=row,
                    company="",
                    warehouse=warehouse,
                )
            except frappe.ValidationError:
                continue
            if not no_stock_change:
                continue
        public_status = {
            "Accepted": "accepted",
            "Review": "review_required",
            "Conflict": "conflict",
        }.get(status)
        if public_status is None:
            continue
        return {
            "observation_id": observation_id,
            "status": public_status,
            "lines": review_lines,
        }
    return None


def _safe_count_quantity(value: Any) -> str | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    normalized = parsed.normalize()
    return format(normalized, "f") if normalized else "0"


def _count_line_variance_percent(
    *, warehouse: str, item_id: str, counted_quantity: str
) -> str | None:
    details = _count_line_variance_details(
        warehouse=warehouse,
        item_id=item_id,
        counted_quantity=counted_quantity,
    )
    return details["variance_percent"] if details else None


def _count_line_variance_details(
    *, warehouse: str, item_id: str, counted_quantity: str
) -> dict[str, str] | None:
    expected_raw = frappe.db.get_value(
        "Bin", {"item_code": item_id, "warehouse": warehouse}, "actual_qty"
    )
    try:
        expected = Decimal(str(expected_raw))
        counted = Decimal(str(counted_quantity))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not expected.is_finite() or not counted.is_finite():
        return None
    denominator = max(abs(expected), Decimal("1"))
    difference = counted - expected
    percent = (difference / denominator * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    normalized_difference = difference.normalize()
    normalized_percent = percent.normalize()
    return {
        "difference_quantity": format(normalized_difference, "f") if normalized_difference else "0",
        "variance_percent": format(normalized_percent, "f") if normalized_percent else "0",
    }


def _count_variance(*, company: str, warehouse: str, lines: list[dict[str, Any]]) -> dict[str, Decimal]:
    """Calculate a private, director-only count variance summary.

    The device receives only the review outcome.  Valuation is read from the
    current Bin and never crosses the device API boundary.
    """

    absolute_quantity = Decimal("0")
    expected_quantity = Decimal("0")
    value = Decimal("0")
    valuation_complete = True
    for line in lines:
        item = cstr(line.get("item_id")).strip()
        try:
            observed = Decimal(str(line.get("quantity")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        expected_raw = frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty")
        try:
            expected = Decimal(str(expected_raw or "0"))
        except (InvalidOperation, TypeError, ValueError):
            expected = Decimal("0")
        difference = abs(observed - expected)
        absolute_quantity += difference
        expected_quantity += abs(expected)
        rate_raw = frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "valuation_rate")
        try:
            rate = Decimal(str(rate_raw))
        except (InvalidOperation, TypeError, ValueError):
            rate = Decimal("0")
            if difference > 0:
                valuation_complete = False
        if rate < 0 or not rate.is_finite():
            valuation_complete = False
            rate = Decimal("0")
        value += difference * rate
    percent = (absolute_quantity / max(expected_quantity, Decimal("1")) * Decimal("100")).quantize(Decimal("0.01"))
    return {
        "absolute_quantity": absolute_quantity,
        "percent": percent,
        "value": value.quantize(Decimal("0.01")),
        "valuation_complete": Decimal("1") if valuation_complete else Decimal("0"),
    }


def _stock_ledger_watermark(warehouse: str) -> str:
    rows = frappe.db.sql(
        "SELECT MAX(modified) FROM `tabStock Ledger Entry` WHERE warehouse = %s",
        (warehouse,),
    )
    return cstr(rows[0][0] if rows else "").strip()


def _count_requires_director_review(*, warehouse: str, variance: dict[str, Decimal]) -> bool:
    policy = frappe.db.get_value(
        "FB Inventory Policy",
        {"warehouse": warehouse},
        ["count_variance_percent_ceiling", "count_variance_value_ceiling"],
        as_dict=True,
    )
    if not policy:
        return False
    percent_limit = policy.get("count_variance_percent_ceiling")
    value_limit = policy.get("count_variance_value_ceiling")
    if percent_limit not in (None, ""):
        try:
            if variance["percent"] > Decimal(str(percent_limit)):
                return True
        except (InvalidOperation, TypeError, ValueError):
            return True
    if value_limit not in (None, ""):
        try:
            if variance["value"] > Decimal(str(value_limit)) or variance["valuation_complete"] == Decimal("0"):
                return True
        except (InvalidOperation, TypeError, ValueError):
            return True
    return False


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


# Device commands are authenticated but still untrusted input.  Keep one
# small boundary validator for every device POST rather than relying on each
# command parser to remember transport-level limits independently.  The
# command-specific parsers below remain responsible for exact fields and
# domain rules.
_DEVICE_MAX_PAYLOAD_BYTES = 256 * 1024
_DEVICE_MAX_STRING_LENGTH = 4096
_DEVICE_MAX_LINES = 100
_DEVICE_MAX_DEPTH = 8
_DEVICE_MAX_COLLECTION_ITEMS = 100
_DEVICE_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_DEVICE_MAX_DECIMAL_TEXT_LENGTH = 40


def _parse_device_json_object(
    payload: str | dict[str, Any], label: str
) -> dict[str, Any]:
    """Decode and bound one device POST before command-specific validation.

    Both raw JSON strings and already-decoded dictionaries are checked.  The
    latter are common in unit tests and Frappe method calls, so their
    canonical UTF-8 representation must be bounded as well; otherwise a
    caller could bypass the wire-size limit by passing a Python dictionary.
    """

    if isinstance(payload, str):
        try:
            raw_size = len(payload.encode("utf-8"))
        except UnicodeError as error:
            frappe.throw(_("{0} contains invalid text").format(label), frappe.ValidationError)
            raise AssertionError("frappe.throw must raise") from error
        if raw_size > _DEVICE_MAX_PAYLOAD_BYTES:
            frappe.throw(
                _("{0} is too large; device requests are limited to {1} KB").format(
                    label, _DEVICE_MAX_PAYLOAD_BYTES // 1024
                ),
                frappe.ValidationError,
            )
        try:
            value: Any = json.loads(payload)
        except RecursionError as error:
            # CPython's decoder gives up on deep nesting before any limit below
            # is reached. That is the same condition the depth check names, so
            # report it the same way instead of escaping as a RuntimeError.
            frappe.throw(
                _("{0} is nested deeper than the supported device command limit").format(label),
                frappe.ValidationError,
            )
            raise AssertionError("frappe.throw must raise") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            frappe.throw(_("{0} is not valid JSON").format(label), frappe.ValidationError)
            raise AssertionError("frappe.throw must raise") from error
    else:
        value = payload

    if not isinstance(value, dict):
        frappe.throw(_("{0} must be an object").format(label), frappe.ValidationError)
    # Bound the shape before encoding it. The depth limit gives up after at most
    # nine frames, where json.dumps on a deeply nested dictionary would raise
    # RecursionError first and escape every limit here. Unsupported types report
    # the same message from either place.
    _validate_device_json_shape(value, label=label)
    try:
        canonical_size = len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError, UnicodeError, OverflowError) as error:
        frappe.throw(_("{0} contains unsupported data").format(label), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if canonical_size > _DEVICE_MAX_PAYLOAD_BYTES:
        frappe.throw(
            _("{0} is too large; device requests are limited to {1} KB").format(
                label, _DEVICE_MAX_PAYLOAD_BYTES // 1024
            ),
            frappe.ValidationError,
        )
    return value


def _validate_device_json_shape(value: Any, *, label: str, depth: int = 0) -> None:
    """Apply shared string, collection, nesting, and number limits."""

    if depth > _DEVICE_MAX_DEPTH:
        frappe.throw(
            _("{0} is nested deeper than the supported device command limit").format(label),
            frappe.ValidationError,
        )
    if isinstance(value, dict):
        if len(value) > _DEVICE_MAX_COLLECTION_ITEMS:
            frappe.throw(
                _("{0} contains too many fields").format(label), frappe.ValidationError
            )
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > _DEVICE_MAX_STRING_LENGTH:
                frappe.throw(
                    _("{0} contains an invalid field name").format(label), frappe.ValidationError
                )
            _validate_device_json_shape(child, label=label, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > _DEVICE_MAX_COLLECTION_ITEMS:
            frappe.throw(
                _("{0} cannot contain more than {1} list entries").format(
                    label, _DEVICE_MAX_COLLECTION_ITEMS
                ),
                frappe.ValidationError,
            )
        for child in value:
            _validate_device_json_shape(child, label=label, depth=depth + 1)
        return
    if isinstance(value, str):
        if len(value) > _DEVICE_MAX_STRING_LENGTH:
            frappe.throw(
                _("{0} contains a string longer than {1} characters").format(
                    label, _DEVICE_MAX_STRING_LENGTH
                ),
                frappe.ValidationError,
            )
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int):
        if abs(value) > _DEVICE_MAX_SAFE_INTEGER:
            frappe.throw(
                _("{0} contains a number outside the safe JSON integer range").format(label),
                frappe.ValidationError,
            )
        return
    if isinstance(value, float):
        if (
            not math.isfinite(value)
            or not value.is_integer()
            or abs(value) > _DEVICE_MAX_SAFE_INTEGER
        ):
            frappe.throw(
                _("{0} contains an invalid JSON number").format(label), frappe.ValidationError
            )
        return
    frappe.throw(
        _("{0} contains unsupported data").format(label), frappe.ValidationError
    )


def _parse_availability_hold_command(
    payload: str | dict[str, Any], *, action: str
) -> dict[str, Any]:
    """Validate the fixed, idempotent POS hold command envelope."""

    value = _parse_device_json_object(payload, "Availability hold command")
    if action == "create":
        required = {
            "schema_version", "command_id", "action", "device_id", "device_config_version",
            "staff_user", "employee", "company", "outlet", "warehouse", "source_document",
            "source_revision", "observed_at", "target_type", "target_id", "reason_code",
            "reason_label", "payload_hash",
        }
    else:
        required = {
            "schema_version", "command_id", "action", "device_id", "device_config_version",
            "staff_user", "employee", "company", "outlet", "warehouse", "source_document",
            "source_revision", "observed_at", "hold_id", "reason", "target_type", "target_id",
            "hold_source", "payload_hash",
        }
    if set(value) != required:
        frappe.throw(_("Availability hold command fields are incomplete or unexpected"), frappe.ValidationError)
    if cstr(value.get("schema_version")).strip() != "inventory-command-v1":
        frappe.throw(_("Unsupported availability hold command schema version"), frappe.ValidationError)
    if value.get("action") != action:
        frappe.throw(_("Availability hold command action is invalid"), frappe.ValidationError)
    for fieldname in (
        "command_id", "device_id", "staff_user", "employee", "company", "outlet", "warehouse",
        "source_document", "target_id",
    ):
        if not isinstance(value.get(fieldname), str) or not cstr(value[fieldname]).strip():
            frappe.throw(_("Availability hold command {0} is required").format(fieldname), frappe.ValidationError)
    if action == "create":
        for fieldname in ("reason_code", "reason_label"):
            if not isinstance(value.get(fieldname), str) or not cstr(value[fieldname]).strip():
                frappe.throw(_("Availability hold command {0} is required").format(fieldname), frappe.ValidationError)
    else:
        for fieldname in ("hold_id", "reason"):
            if not isinstance(value.get(fieldname), str) or not cstr(value[fieldname]).strip():
                frappe.throw(_("Availability hold command {0} is required").format(fieldname), frappe.ValidationError)
        if value.get("hold_source") not in {"manual", "automation"}:
            frappe.throw(_("Availability hold command hold_source is invalid"), frappe.ValidationError)
    config_version = value.get("device_config_version")
    source_revision = value.get("source_revision")
    if isinstance(config_version, bool) or not isinstance(config_version, int) or config_version < 0:
        frappe.throw(_("Availability hold command device_config_version is invalid"), frappe.ValidationError)
    valid_source_revision = (
        isinstance(source_revision, str) and bool(cstr(source_revision).strip())
    ) or (
        isinstance(source_revision, int)
        and not isinstance(source_revision, bool)
        and source_revision >= 1
    )
    if not valid_source_revision:
        frappe.throw(_("Availability hold command source_revision is invalid"), frappe.ValidationError)
    if value.get("target_type") not in {"Item", "Modifier"}:
        frappe.throw(_("Availability hold command target_type is invalid"), frappe.ValidationError)
    _require_explicit_offset_timestamp(
        value["observed_at"], "Availability hold command observed_at"
    )
    command_id = cstr(value["command_id"]).strip()
    if len(command_id) > 160:
        frappe.throw(_("Availability hold command command_id is too long"), frappe.ValidationError)
    payload_hash = cstr(value.get("payload_hash")).strip().lower()
    if len(payload_hash) != 64 or any(character not in "0123456789abcdef" for character in payload_hash):
        frappe.throw(_("Availability hold command payload_hash is invalid"), frappe.ValidationError)
    expected_hash = _payload_hash({key: item for key, item in value.items() if key != "payload_hash"})
    if expected_hash != payload_hash:
        frappe.throw(_("Availability hold command payload_hash does not match its payload"), frappe.ValidationError)
    return value


def _validate_availability_hold_scope(device: Any, value: dict[str, Any]) -> None:
    """Bind a hold command to this authenticated device and outlet."""

    device_id = cstr(getattr(device, "device_id", None) or getattr(device, "name", None)).strip()
    if cstr(value.get("device_id")).strip() != device_id:
        frappe.throw(_("Availability hold device_id does not match the authenticated device"), frappe.PermissionError)
    configured_version = getattr(device, "config_version", None)
    if cint(value.get("device_config_version")) != cint(configured_version):
        frappe.throw(_("Device configuration changed; refresh before changing availability"), frappe.ValidationError)
    profile = resolve_catalog_pos_profile(device_id=device_id) or {}
    for fieldname, expected in (
        ("company", cstr(profile.get("company")).strip()),
        ("outlet", cstr(profile.get("name")).strip()),
        ("warehouse", cstr(profile.get("warehouse")).strip()),
    ):
        if not expected or cstr(value.get(fieldname)).strip() != expected:
            frappe.throw(_("Availability hold {0} is outside the authenticated outlet").format(fieldname), frappe.PermissionError)
    access = _require_device_staff(device, cstr(value.get("staff_user")), manager=True)
    if cstr(access.get("employee")).strip() != cstr(value.get("employee")).strip():
        frappe.throw(_("Availability hold employee does not match central staff access"), frappe.PermissionError)


def _assert_catalog_hold_target(*, profile: Any, target_type: str, target_id: str) -> None:
    """Keep POS manager controls limited to the commercial catalog boundary."""

    company = cstr(profile.get("company")).strip() if isinstance(profile, dict) else cstr(getattr(profile, "company", None)).strip()
    try:
        item_ids, modifier_ids = get_catalog_target_ids(pos_profile=profile, company=company)
    except Exception as error:
        log_sanitized_error("KoPOS hold catalog target lookup failed", error)
        frappe.throw(
            _("The commercial catalog could not be verified; refresh before changing availability"),
            frappe.ValidationError,
        )
        return
    allowed = item_ids if target_type == "Item" else modifier_ids if target_type == "Modifier" else set()
    if target_id not in allowed:
        frappe.throw(
            _("POS can change availability only for a commercial catalog Item or modifier"),
            frappe.PermissionError,
        )


def _require_director_pos_profile(pos_profile: str | None) -> None:
    profile_name = cstr(pos_profile).strip()
    if not profile_name:
        frappe.throw(_("Choose an outlet first"), frappe.ValidationError)
    profile = frappe.db.get_value(
        "POS Profile",
        profile_name,
        ["company", "warehouse"],
        as_dict=True,
    )
    if not profile:
        frappe.throw(_("Choose an outlet that exists"), frappe.ValidationError)
    context = resolve_director_context(
        company=cstr(profile.get("company")).strip(),
        warehouse=cstr(profile.get("warehouse")).strip(),
    )
    if profile_name not in director_pos_profiles(context):
        frappe.throw(_("You do not have access to this outlet."), frappe.PermissionError)


def _require_company_director(action: str) -> None:
    del action
    require_director_role()


def _require_system_manager_for_internal_tool() -> None:
    if "System Manager" not in set(frappe.get_roles(frappe.session.user)):
        frappe.throw(
            _("This tool is available to Support only."),
            frappe.PermissionError,
        )


_GUIDED_TASK_COMMON_FIELDS = frozenset({
    "schema_version", "command_id", "task_type", "device_id", "device_config_version",
    "staff_user", "employee", "company", "outlet", "warehouse", "source_document",
    "source_revision", "observed_at", "payload_hash", "recorded_by", "draft_key",
})
_GUIDED_TASK_FIELDS: dict[str, frozenset[str]] = {
    "accept_preparation_task": frozenset({"work_order", "bom_no", "preparation_fingerprint"}),
    "start_preparation_task": frozenset({"work_order"}),
    "complete_preparation_task": frozenset({
        "work_order", "actual_yield", "qty", "fg_warehouse", "items", "waste_qty", "batch_no", "expiry_date",
    }),
    "submit_purchase_receipt": frozenset({"purchase_order", "lines", "delivery_note"}),
    "submit_transfer_dispatch": frozenset({"material_request", "lines", "quantity", "batch_no"}),
    "submit_transfer_receipt": frozenset({"material_request", "lines", "quantity", "batch_no"}),
    # Waste has no counterparty document: the outlet observes it. The device
    # supplies its own identity so the command stays replay-safe.
    "record_waste_event": frozenset({
        "waste_id", "source_type", "source_item", "source_quantity",
        "selected_modifiers", "lines", "reason_code", "reason_text",
    }),
}

_GUIDED_TASK_MAX_PAYLOAD_BYTES = _DEVICE_MAX_PAYLOAD_BYTES
_GUIDED_TASK_MAX_LINES = _DEVICE_MAX_LINES
_GUIDED_TASK_MAX_STRING_LENGTH = 140
_GUIDED_TASK_MAX_COMMAND_ID_LENGTH = 160
_GUIDED_TASK_MAX_DRAFT_KEY_LENGTH = 256
_GUIDED_TASK_MAX_DECIMAL_LENGTH = 40
_GUIDED_TASK_LINE_FIELDS: dict[str, frozenset[str]] = {
    "complete_preparation_task": frozenset({"item_code", "qty", "warehouse", "batch_no", "expiry_date"}),
    "submit_purchase_receipt": frozenset({
        "purchase_order_item", "item_code", "qty", "warehouse", "uom", "stock_uom",
        "conversion_factor", "batch_no", "expiry_date", "missing_qty", "damaged_qty", "excess_qty",
    }),
    "submit_transfer_dispatch": frozenset({
        "material_request_item", "item_code", "qty", "uom", "stock_uom", "conversion_factor",
        "source_warehouse", "destination_warehouse", "batch_no",
    }),
    "submit_transfer_receipt": frozenset({
        "material_request_item", "item_code", "qty", "uom", "stock_uom", "conversion_factor",
        "source_warehouse", "destination_warehouse", "batch_no",
    }),
    "record_waste_event": frozenset({"item_code", "qty", "uom", "batch_no"}),
}


def _parse_guided_task_payload(
    payload: str | dict[str, Any], *, task_type: str, device_id: str,
) -> dict[str, Any]:
    """Validate the one versioned, hashed envelope used by guided stock work."""

    value = _parse_device_json_object(payload, f"{task_type} payload")
    allowed_task_fields = _GUIDED_TASK_FIELDS.get(task_type)
    if allowed_task_fields is None:
        frappe.throw(_("Unsupported guided inventory task"), frappe.ValidationError)
    allowed = _GUIDED_TASK_COMMON_FIELDS | allowed_task_fields
    if set(value) - allowed:
        unexpected = ", ".join(sorted(set(value) - allowed))
        frappe.throw(_("{0} contains unsupported fields: {1}").format(task_type, unexpected), frappe.ValidationError)
    required_common_fields = _GUIDED_TASK_COMMON_FIELDS - {"recorded_by", "draft_key"}
    if not required_common_fields.issubset(value):
        missing = ", ".join(sorted(required_common_fields - set(value)))
        frappe.throw(_("{0} is missing required fields: {1}").format(task_type, missing), frappe.ValidationError)
    if cstr(value.get("schema_version")).strip() != "inventory-command-v1":
        frappe.throw(_("Unsupported guided inventory command schema version"), frappe.ValidationError)
    if cstr(value.get("task_type")).strip() != task_type:
        frappe.throw(_("Guided inventory task type does not match the endpoint"), frappe.ValidationError)
    for fieldname in (
        "command_id", "device_id", "staff_user", "employee", "company", "outlet", "warehouse", "source_document",
    ):
        if not isinstance(value.get(fieldname), str) or not cstr(value[fieldname]).strip():
            frappe.throw(_("{0} {1} is required").format(task_type, fieldname), frappe.ValidationError)
        if len(cstr(value[fieldname])) > _GUIDED_TASK_MAX_STRING_LENGTH:
            frappe.throw(_("{0} {1} is too long").format(task_type, fieldname), frappe.ValidationError)
    if cstr(value.get("device_id")).strip() != device_id:
        frappe.throw(_("Guided task device_id does not match the authenticated device"), frappe.ValidationError)
    config_version = value.get("device_config_version")
    if isinstance(config_version, bool) or not isinstance(config_version, int) or config_version < 0:
        frappe.throw(_("Guided task device_config_version is invalid"), frappe.ValidationError)
    source_revision = value.get("source_revision")
    if not (
        isinstance(source_revision, str) and cstr(source_revision).strip()
    ) and not (
        isinstance(source_revision, int) and not isinstance(source_revision, bool) and source_revision >= 1
    ):
        frappe.throw(_("Guided task source_revision is invalid"), frappe.ValidationError)
    _require_explicit_offset_timestamp(
        value["observed_at"], "Guided task observed_at"
    )
    command_id = cstr(value["command_id"]).strip()
    if len(command_id) > _GUIDED_TASK_MAX_COMMAND_ID_LENGTH:
        frappe.throw(_("Guided task command_id is too long"), frappe.ValidationError)
    for fieldname in ("task_type", "schema_version"):
        if len(cstr(value.get(fieldname))) > _GUIDED_TASK_MAX_STRING_LENGTH:
            frappe.throw(_("Guided task {0} is too long").format(fieldname), frappe.ValidationError)
    for fieldname in ("source_revision", "recorded_by"):
        if isinstance(value.get(fieldname), str) and len(value[fieldname]) > _GUIDED_TASK_MAX_STRING_LENGTH:
            frappe.throw(_("Guided task {0} is too long").format(fieldname), frappe.ValidationError)
    if isinstance(value.get("delivery_note"), str) and len(value["delivery_note"]) > _GUIDED_TASK_MAX_STRING_LENGTH:
        frappe.throw(_("Guided task delivery_note is too long"), frappe.ValidationError)
    if isinstance(value.get("draft_key"), str) and len(value["draft_key"]) > _GUIDED_TASK_MAX_DRAFT_KEY_LENGTH:
        frappe.throw(_("Guided task draft_key is too long"), frappe.ValidationError)
    observed_at = value.get("observed_at")
    if not isinstance(observed_at, str) or len(observed_at) > 64:
        frappe.throw(_("Guided task observed_at is invalid"), frappe.ValidationError)
    payload_hash = cstr(value.get("payload_hash")).strip()
    if len(payload_hash) != 64 or payload_hash != payload_hash.lower() or any(
        character not in "0123456789abcdef" for character in payload_hash
    ):
        frappe.throw(_("Guided task payload_hash must be a lowercase SHA-256 digest"), frappe.ValidationError)
    try:
        expected_hash = _payload_hash({key: item for key, item in value.items() if key != "payload_hash"})
    except (TypeError, ValueError):
        frappe.throw(_("Guided task payload contains unsupported data"), frappe.ValidationError)
    if expected_hash != payload_hash:
        frappe.throw(_("Guided task payload_hash does not match the command payload"), frappe.ValidationError)
    line_field_names = _GUIDED_TASK_LINE_FIELDS.get(task_type)
    for fieldname in ("lines", "items"):
        if fieldname not in value:
            continue
        rows = value[fieldname]
        if not isinstance(rows, list):
            frappe.throw(_("Guided task {0} must be a list").format(fieldname), frappe.ValidationError)
        if len(rows) > _GUIDED_TASK_MAX_LINES:
            frappe.throw(_("Guided task {0} cannot contain more than {1} lines").format(fieldname, _GUIDED_TASK_MAX_LINES), frappe.ValidationError)
        if line_field_names is None:
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                frappe.throw(_("Guided task line {0} is invalid").format(index + 1), frappe.ValidationError)
            unexpected_line_fields = set(row) - line_field_names
            if unexpected_line_fields:
                frappe.throw(_("Guided task line {0} contains unsupported fields").format(index + 1), frappe.ValidationError)
            for line_field, line_value in row.items():
                if isinstance(line_value, str):
                    max_length = _GUIDED_TASK_MAX_DECIMAL_LENGTH if line_field in {
                        "qty", "conversion_factor", "missing_qty", "damaged_qty", "excess_qty",
                    } else 64 if line_field == "expiry_date" else _GUIDED_TASK_MAX_STRING_LENGTH
                    if len(line_value) > max_length:
                        frappe.throw(_("Guided task line {0} field {1} is too long").format(index + 1, line_field), frappe.ValidationError)
    required_task_fields = {
        "accept_preparation_task": (),
        "start_preparation_task": ("work_order",),
        "complete_preparation_task": ("work_order",),
        "submit_purchase_receipt": ("purchase_order", "lines"),
        "submit_transfer_dispatch": ("material_request", "lines"),
        "submit_transfer_receipt": ("material_request", "lines"),
        "record_waste_event": ("waste_id", "reason_code"),
    }[task_type]
    for fieldname in required_task_fields:
        if fieldname not in value or value[fieldname] in (None, ""):
            frappe.throw(_("Guided task {0} is required").format(fieldname), frappe.ValidationError)
    if task_type == "accept_preparation_task":
        has_work_order = bool(cstr(value.get("work_order")).strip())
        has_alert = bool(cstr(value.get("bom_no")).strip()) and bool(
            cstr(value.get("preparation_fingerprint")).strip()
        )
        if not has_work_order and not has_alert:
            frappe.throw(
                _("Preparation acceptance requires the assigned alert BOM and fingerprint"),
                frappe.ValidationError,
            )
    if task_type == "record_waste_event":
        source_type = cstr(value.get("source_type") or "physical_item").strip()
        if source_type not in {"physical_item", "made_item"}:
            frappe.throw(_("Waste source is invalid"), frappe.ValidationError)
        if source_type == "physical_item":
            lines = value.get("lines")
            if not isinstance(lines, list) or not lines:
                frappe.throw(_("Add at least one item and amount"), frappe.ValidationError)
            if any(fieldname in value for fieldname in ("source_item", "source_quantity", "selected_modifiers")):
                frappe.throw(_("Physical waste contains made-product details"), frappe.ValidationError)
        else:
            if "lines" in value:
                frappe.throw(_("Made-product waste cannot provide ingredient amounts"), frappe.ValidationError)
            source_item = value.get("source_item")
            if not isinstance(source_item, str) or not cstr(source_item).strip():
                frappe.throw(_("Choose the made product"), frappe.ValidationError)
            if len(source_item) > _GUIDED_TASK_MAX_STRING_LENGTH:
                frappe.throw(_("The made product is invalid"), frappe.ValidationError)
            raw_quantity = value.get("source_quantity")
            if isinstance(raw_quantity, bool) or not isinstance(raw_quantity, (str, int)):
                frappe.throw(_("Made-product amount is invalid"), frappe.ValidationError)
            if len(cstr(raw_quantity)) > _GUIDED_TASK_MAX_DECIMAL_LENGTH:
                frappe.throw(_("Made-product amount is invalid"), frappe.ValidationError)
            if _non_negative_decimal(raw_quantity, "made-product amount") <= 0:
                frappe.throw(_("Made-product amount must be greater than zero"), frappe.ValidationError)
            selected_modifiers = value.get("selected_modifiers", [])
            if not isinstance(selected_modifiers, list):
                frappe.throw(_("Customer choices must be a list"), frappe.ValidationError)
            if len(selected_modifiers) > _GUIDED_TASK_MAX_LINES:
                frappe.throw(_("Too many customer choices were selected"), frappe.ValidationError)
            for modifier in selected_modifiers:
                if not isinstance(modifier, str) or not cstr(modifier).strip():
                    frappe.throw(_("A customer choice is invalid"), frappe.ValidationError)
                if len(modifier) > _GUIDED_TASK_MAX_STRING_LENGTH:
                    frappe.throw(_("A customer choice is invalid"), frappe.ValidationError)
    return value


def _handle_guided_task(*, device_id: str, payload: str | dict[str, Any], task_type: str) -> dict[str, Any]:
    """Execute one fixed POS task against a standard ERPNext document."""

    require_device_context(device_id=device_id)
    value = _parse_guided_task_payload(payload, task_type=task_type, device_id=device_id)
    command_id = cstr(value.get("command_id")).strip()
    device = lock_device_for_operational_mutation(device_id=device_id)
    if value.get("device_config_version") != cint(getattr(device, "config_version", 0)):
        frappe.throw(
            _("Device configuration changed; refresh before completing this stock task"),
            frappe.ValidationError,
        )
    _validate_guided_task_actor(device, value, task_type)
    _validate_guided_task_scope(device_id, value, task_type)
    source_field = {
        "accept_preparation_task": "work_order",
        "start_preparation_task": "work_order",
        "complete_preparation_task": "work_order",
        "submit_purchase_receipt": "purchase_order",
        "submit_transfer_dispatch": "material_request",
        "submit_transfer_receipt": "material_request",
        "record_waste_event": "waste_id",
    }[task_type]
    source_value = cstr(value.get(source_field)).strip()
    if task_type == "accept_preparation_task" and not source_value:
        source_value = cstr(value.get("bom_no")).strip()
    if source_value != cstr(value.get("source_document")).strip():
        frappe.throw(_("Guided task source_document does not match the assigned standard document"), frappe.ValidationError)
    _validate_guided_task_actionable_quantities(value, task_type=task_type)
    requirements = _GUIDED_TASK_AUTOMATION_REQUIREMENTS.get(task_type)
    receipt = _get_guided_command_receipt(value)
    if receipt:
        if task_type == "record_waste_event":
            _submit_existing_waste_with_automation(
                value,
                cstr(receipt.get("result_document")).strip(),
            )
        return _guided_command_response(receipt, status="replayed")
    doctype = {
        "accept_preparation_task": "Work Order",
        "start_preparation_task": "Work Order",
        "complete_preparation_task": "Stock Entry",
        "submit_purchase_receipt": "Purchase Receipt",
        "submit_transfer_dispatch": "Stock Entry",
        "submit_transfer_receipt": "Stock Entry",
        # The event is the audited source document; submitting it immediately
        # creates the idempotent Material Issue that changes stock.
        "record_waste_event": "FB Waste Event",
    }[task_type]
    existing = _find_command_document(doctype, command_id, payload_hash=cstr(value.get("payload_hash")).strip())
    if existing:
        if task_type == "record_waste_event":
            _submit_existing_waste_with_automation(value, existing)
        receipt = _create_guided_command_receipt(value)
        _complete_guided_command_receipt(
            receipt,
            result_doctype=doctype,
            result_document=existing,
            outcome="Completed",
        )
        frappe.db.commit()
        return _guided_command_response(receipt, status="replayed")
    receipt = _create_guided_command_receipt(value)
    if requirements is None:
        document = _execute_guided_task_mutation(value, command_id=command_id, task_type=task_type)
    else:
        try:
            with inventory_automation_identity(
                company=cstr(value.get("company")).strip(),
                warehouse=cstr(value.get("warehouse")).strip(),
                create_doctypes=requirements["create_doctypes"],
                submit_doctypes=requirements["submit_doctypes"],
            ):
                document = _execute_guided_task_mutation(
                    value,
                    command_id=command_id,
                    task_type=task_type,
                )
        except AutomationIdentityError as error:
            frappe.throw(
                _("Guided inventory automation is not configured safely: {0}").format(error.reason),
                frappe.ValidationError,
            )
    result_doctype = _guided_result_doctype(task_type, document)
    outcome = "Exception" if result_doctype == "FB Inventory Exception" else "Completed"
    _complete_guided_command_receipt(
        receipt,
        result_doctype=result_doctype,
        result_document=document,
        outcome=outcome,
    )
    frappe.db.commit()
    return _guided_command_response(receipt, status="accepted")


def _validate_guided_task_actionable_quantities(value: dict[str, Any], *, task_type: str) -> None:
    if task_type == "submit_purchase_receipt":
        lines = value.get("lines")
        if not isinstance(lines, list) or not lines:
            frappe.throw(_("Receiving requires at least one item"), frappe.ValidationError)
        total = Decimal("0")
        for line in lines:
            if not isinstance(line, dict):
                frappe.throw(_("Receiving contains an invalid item"), frappe.ValidationError)
            for fieldname in ("qty", "missing_qty", "damaged_qty", "excess_qty"):
                total += _non_negative_decimal(line.get(fieldname, 0), f"receiving {fieldname}")
        if total <= 0:
            frappe.throw(
                _("Enter at least one received, missing, damaged, or extra amount"),
                frappe.ValidationError,
            )
    if task_type in {"submit_transfer_dispatch", "submit_transfer_receipt"}:
        lines = value.get("lines")
        if not isinstance(lines, list) or not lines:
            frappe.throw(_("Transfer requires at least one item"), frappe.ValidationError)
        for line in lines:
            if not isinstance(line, dict) or _non_negative_decimal(line.get("qty", 0), "transfer quantity") <= 0:
                frappe.throw(_("Every transfer amount must be greater than zero"), frappe.ValidationError)


def _get_guided_command_receipt(value: dict[str, Any]) -> Any | None:
    if not frappe.db.exists("DocType", "FB Inventory Command Receipt"):
        frappe.throw(
            _("Inventory command receipts are not installed; run bench migrate before using stock tasks"),
            frappe.ValidationError,
        )
    receipt = frappe.db.get_value(
        "FB Inventory Command Receipt",
        {"command_id": cstr(value.get("command_id")).strip()},
        [
            "name", "command_id", "payload_hash", "task_type", "state", "outcome",
            "device_id", "company", "warehouse", "source_document", "source_revision",
            "result_doctype", "result_document",
        ],
        as_dict=True,
    )
    if not receipt:
        return None
    expected = {
        "payload_hash": cstr(value.get("payload_hash")).strip(),
        "task_type": cstr(value.get("task_type")).strip(),
        "device_id": cstr(value.get("device_id")).strip(),
        "company": cstr(value.get("company")).strip(),
        "warehouse": cstr(value.get("warehouse")).strip(),
        "source_document": cstr(value.get("source_document")).strip(),
        "source_revision": cstr(value.get("source_revision")).strip(),
    }
    for fieldname, expected_value in expected.items():
        if cstr(receipt.get(fieldname)).strip() != expected_value:
            frappe.throw(_("Inventory command ID was reused with different content"), frappe.ValidationError)
    if cstr(receipt.get("state")).strip() != "Completed":
        frappe.throw(_("This stock update is still being saved. Try again."), frappe.ValidationError)
    if not cstr(receipt.get("result_doctype")).strip() or not cstr(receipt.get("result_document")).strip():
        frappe.throw(_("The saved stock update has no result document"), frappe.ValidationError)
    return receipt


def _create_guided_command_receipt(value: dict[str, Any]) -> Any:
    receipt = frappe.new_doc("FB Inventory Command Receipt")
    receipt.command_id = cstr(value.get("command_id")).strip()
    receipt.payload_hash = cstr(value.get("payload_hash")).strip()
    receipt.task_type = cstr(value.get("task_type")).strip()
    receipt.state = "Processing"
    receipt.device_id = cstr(value.get("device_id")).strip()
    receipt.company = cstr(value.get("company")).strip()
    receipt.warehouse = cstr(value.get("warehouse")).strip()
    receipt.source_document = cstr(value.get("source_document")).strip()
    receipt.source_revision = cstr(value.get("source_revision")).strip()
    receipt.insert(ignore_permissions=True)
    return receipt


def _complete_guided_command_receipt(
    receipt: Any,
    *,
    result_doctype: str,
    result_document: str,
    outcome: str,
) -> None:
    if not cstr(result_document).strip():
        frappe.throw(_("This stock task did not create a result"), frappe.ValidationError)
    receipt.state = "Completed"
    receipt.outcome = outcome
    receipt.result_doctype = result_doctype
    receipt.result_document = cstr(result_document).strip()
    receipt.completed_at = now_datetime()
    receipt.save(ignore_permissions=True)


def _guided_result_doctype(task_type: str, document: str) -> str:
    if task_type == "submit_purchase_receipt":
        return "Purchase Receipt" if frappe.db.exists("Purchase Receipt", document) else "FB Inventory Exception"
    return {
        "accept_preparation_task": "Work Order",
        "start_preparation_task": "Work Order",
        "complete_preparation_task": "Stock Entry",
        "submit_transfer_dispatch": "Stock Entry",
        "submit_transfer_receipt": "Stock Entry",
        "record_waste_event": "FB Waste Event",
    }[task_type]


def _guided_command_response(receipt: Any, *, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "task_type": cstr(receipt.get("task_type") if isinstance(receipt, dict) else receipt.task_type).strip(),
        "outcome": cstr(receipt.get("outcome") if isinstance(receipt, dict) else receipt.outcome).strip().lower(),
        "document_type": cstr(receipt.get("result_doctype") if isinstance(receipt, dict) else receipt.result_doctype).strip(),
        "document": cstr(receipt.get("result_document") if isinstance(receipt, dict) else receipt.result_document).strip(),
    }


_GUIDED_TASK_AUTOMATION_REQUIREMENTS = {
    "accept_preparation_task": {
        "create_doctypes": ("Work Order",),
        "submit_doctypes": (),
    },
    "start_preparation_task": {
        "create_doctypes": (),
        "submit_doctypes": ("Work Order",),
    },
    "complete_preparation_task": {
        "create_doctypes": ("Stock Entry",),
        "submit_doctypes": ("Stock Entry",),
    },
    "submit_purchase_receipt": {
        "create_doctypes": ("Purchase Receipt",),
        "submit_doctypes": ("Purchase Receipt",),
    },
    "submit_transfer_dispatch": {
        "create_doctypes": ("Stock Entry",),
        "submit_doctypes": ("Stock Entry",),
    },
    "submit_transfer_receipt": {
        "create_doctypes": ("Stock Entry",),
        "submit_doctypes": ("Stock Entry",),
    },
    "record_waste_event": {
        # The authenticated device route owns the immutable waste audit record.
        # The configured least-privilege ERP identity owns the stock movement.
        "create_doctypes": ("Stock Entry",),
        "submit_doctypes": ("Stock Entry",),
    },
}


def _execute_guided_task_mutation(
    value: dict[str, Any], *, command_id: str, task_type: str
) -> str:
    """Create or advance the one standard document selected by a guided task."""

    if task_type == "accept_preparation_task":
        return _accept_preparation_task(value, command_id=command_id)
    if task_type == "start_preparation_task":
        return _submit_work_order(value, command_id)
    if task_type == "complete_preparation_task":
        return _create_manufacture_entry(value, command_id)
    if task_type == "submit_purchase_receipt":
        return _create_purchase_receipt(value, command_id)
    if task_type == "record_waste_event":
        return _record_waste_event(value, command_id)
    return _create_transfer_entry(
        value,
        command_id,
        dispatch=task_type == "submit_transfer_dispatch",
    )


def _find_command_document(doctype: str, command_id: str, *, payload_hash: str) -> str | None:
    if not frappe.db.exists("DocType", doctype):
        frappe.throw(_("{0} is not installed").format(doctype), frappe.ValidationError)
    meta = frappe.get_meta(doctype)
    if not meta.has_field("custom_kopos_inventory_command_id"):
        frappe.throw(
            _("{0} is missing the inventory command identity field; run bench migrate before enabling guided stock work").format(doctype),
            frappe.ValidationError,
        )
    if not meta.has_field("custom_kopos_inventory_payload_hash"):
        frappe.throw(
            _("{0} is missing the inventory payload hash field; run bench migrate before enabling guided stock work").format(doctype),
            frappe.ValidationError,
        )
    existing = frappe.db.get_value(
        doctype,
        {"custom_kopos_inventory_command_id": command_id},
        ["name", "custom_kopos_inventory_payload_hash"],
        as_dict=True,
    )
    if not existing:
        return None
    stored_hash = cstr(existing.get("custom_kopos_inventory_payload_hash")).strip()
    if stored_hash != payload_hash:
        frappe.throw(_("Inventory command ID was reused with different content"), frappe.ValidationError)
    return cstr(existing.get("name")).strip() or None


def _accept_preparation_task(value: dict[str, Any], *, command_id: str) -> str:
    """Accept either an existing Draft Work Order or a derived BOM alert."""

    if cstr(value.get("work_order")).strip():
        return _accept_existing_work_order(value, command_id=command_id)
    return _create_work_order_from_preparation_alert(value, command_id=command_id)


def _create_work_order_from_preparation_alert(value: dict[str, Any], *, command_id: str) -> str:
    """Create exactly one Draft Work Order from current standard BOM evidence.

    The BOM and its outlet Bin are locked before the alert is re-evaluated.
    The alert fingerprint therefore cannot be accepted after a recipe,
    threshold, hold, shelf-life, or stock change.  The unique fingerprint on
    Work Order makes concurrent acceptance safe even when two tablets act on
    the same stale alert.
    """

    bom_name = cstr(value.get("bom_no")).strip()
    fingerprint = cstr(value.get("preparation_fingerprint")).strip()
    if not bom_name or not fingerprint:
        frappe.throw(
            _("Preparation acceptance requires the assigned alert BOM and fingerprint"),
            frappe.ValidationError,
        )
    _lock_guided_source_document("BOM", bom_name)
    bom = frappe.get_doc("BOM", bom_name)
    if cint(getattr(bom, "docstatus", 0)) != 1 or not cint(getattr(bom, "is_active", 0)):
        frappe.throw(_("This preparation recipe is no longer published"), frappe.ValidationError)
    _validate_guided_source_revision(value, bom, "BOM")
    company = cstr(value.get("company")).strip()
    warehouse = cstr(value.get("warehouse")).strip()
    if cstr(getattr(bom, "company", None)).strip() != company:
        frappe.throw(_("This preparation recipe belongs to another company"), frappe.PermissionError)

    # Lock the exact stock evidence used to derive the alert.  A missing Bin is
    # valid zero stock and is represented by the stable ``no-bin`` marker.
    frappe.db.sql(
        "SELECT name FROM `tabBin` WHERE item_code = %s AND warehouse = %s FOR UPDATE",
        (cstr(getattr(bom, "item", None)).strip(), warehouse),
    )
    existing = frappe.db.get_value(
        "Work Order",
        {"custom_kopos_preparation_fingerprint": fingerprint},
        ["name", "company", "fg_warehouse", "production_item", "bom_no", "docstatus"],
        as_dict=True,
    )
    if existing:
        if (
            cstr(existing.get("company")).strip() != company
            or cstr(existing.get("fg_warehouse")).strip() != warehouse
            or cstr(existing.get("bom_no")).strip() != bom_name
        ):
            frappe.throw(_("Preparation fingerprint is bound to another outlet"), frappe.PermissionError)
        return cstr(existing.get("name")).strip()

    current_alert = next(
        (
            alert
            for alert in derived_preparation_alerts(company=company, warehouse=warehouse)
            if cstr(alert.get("bom_no")).strip() == bom_name
            and cstr(alert.get("fingerprint")).strip() == fingerprint
        ),
        None,
    )
    if not current_alert:
        frappe.throw(
            _("This preparation alert is stale; refresh the task before accepting it"),
            frappe.ValidationError,
        )
    automation_state = cstr(current_alert.get("automation_state")).strip()
    if automation_state == "Paused":
        frappe.throw(
            _("Inventory automation is paused for this outlet; resume it before preparing this batch"),
            frappe.ValidationError,
        )
    if automation_state not in {"Review First", "Active"}:
        frappe.throw(
            _("This outlet has an invalid inventory automation state; a Company Director must correct it before preparing this batch"),
            frappe.ValidationError,
        )
    blocked_reason = cstr(current_alert.get("blocked_reason")).strip()
    if blocked_reason:
        frappe.throw(_("This preparation alert is blocked: {0}").format(blocked_reason), frappe.ValidationError)

    document = frappe.new_doc("Work Order")
    document.company = company
    document.production_item = cstr(getattr(bom, "item", None)).strip()
    document.bom_no = bom_name
    document.qty = _decimal_exact(current_alert.get("batch_qty"))
    document.fg_warehouse = warehouse
    if frappe.get_meta("Work Order").has_field("wip_warehouse"):
        wip = frappe.db.get_value("Company", company, "default_wip_warehouse")
        if wip:
            document.wip_warehouse = wip
    _set_document_value(document, "custom_kopos_preparation_fingerprint", fingerprint)
    _set_document_value(document, "custom_kopos_inventory_command_id", command_id)
    _set_document_value(document, "custom_kopos_inventory_payload_hash", cstr(value.get("payload_hash")).strip())
    _apply_guided_task_audit(document, value)
    document.flags.ignore_permissions = True
    document.insert(ignore_permissions=True)
    return cstr(document.name).strip()


def _accept_existing_work_order(value: dict[str, Any], *, command_id: str) -> str:
    """Record acceptance of an ERP-created Draft Work Order, never create one.

    A director-created Draft Work Order may still be accepted for compatibility.
    New routine preparation uses the derived-alert path above so the tablet
    never selects an arbitrary standard document.
    """

    name = cstr(value.get("work_order")).strip()
    if not name:
        frappe.throw(_("Preparation acceptance requires a Work Order"), frappe.ValidationError)
    _lock_guided_source_document("Work Order", name)
    document = frappe.get_doc("Work Order", name)
    _validate_work_order_scope(document, value)
    _validate_guided_source_revision(value, document, "Work Order")
    if document.docstatus != 0:
        frappe.throw(_("Only a Draft Work Order can be accepted for preparation"), frappe.ValidationError)
    _set_document_value(document, "custom_kopos_inventory_command_id", command_id)
    _set_document_value(document, "custom_kopos_inventory_payload_hash", cstr(value.get("payload_hash")).strip())
    _apply_guided_task_audit(document, value)
    document.flags.ignore_permissions = True
    document.save()
    return document.name


def _submit_work_order(value: dict[str, Any], command_id: str) -> str:
    name = cstr(value.get("work_order")).strip()
    if not name:
        frappe.throw(_("Preparation start requires a Work Order"), frappe.ValidationError)
    _lock_guided_source_document("Work Order", name)
    document = frappe.get_doc("Work Order", name)
    _validate_work_order_scope(document, value)
    _validate_guided_source_revision(value, document, "Work Order")
    _set_document_value(document, "custom_kopos_inventory_command_id", command_id)
    _set_document_value(document, "custom_kopos_inventory_payload_hash", cstr(value.get("payload_hash")).strip())
    _apply_guided_task_audit(document, value)
    if document.docstatus == 0:
        # This guided outlet flow has no separate physical transfer-to-WIP
        # step. Use ERPNext's standard direct-backflush mode so the eventual
        # Manufacture entry consumes the exact outlet ingredient rows shown on
        # the tablet rather than inventing stock in the WIP warehouse.
        _set_document_value(document, "skip_transfer", 1)
        document.flags.ignore_permissions = True
        document.submit()
    elif document.docstatus != 1:
        frappe.throw(_("This Work Order is not available to start"), frappe.ValidationError)
    return document.name


def _create_manufacture_entry(value: dict[str, Any], command_id: str) -> str:
    work_order = cstr(value.get("work_order")).strip()
    if not work_order:
        frappe.throw(_("Preparation completion requires a Work Order"), frappe.ValidationError)
    _lock_guided_source_document("Work Order", work_order)
    order = frappe.get_doc("Work Order", work_order)
    _validate_work_order_scope(order, value)
    _validate_guided_source_revision(value, order, "Work Order")
    if order.docstatus != 1:
        frappe.throw(_("Start the Work Order before recording batch completion"), frappe.ValidationError)
    actual_yield = _non_negative_decimal(
        value.get("actual_yield") or value.get("qty"),
        "actual usable yield",
    )
    target_warehouse = value.get("fg_warehouse") or getattr(order, "fg_warehouse", None)
    if actual_yield <= 0:
        frappe.throw(_("Preparation completion requires a positive usable yield"), frappe.ValidationError)
    _require_item_batch_metadata(cstr(getattr(order, "production_item", "")).strip(), value)
    # ERPNext owns BOM expansion, finished-good flags, conversion factors, and
    # serial/batch defaults. Its builder is the only thing that may construct this
    # document, so a guided POS completion is equivalent to the normal Work Order
    # form. Only the import is guarded: an ImportError raised *inside* the builder
    # means a broken install, and must surface rather than silently hand the work
    # to a substitute that posts stock from tablet-supplied rows.
    try:
        from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry
    except (ImportError, ModuleNotFoundError) as error:
        frappe.throw(
            _("ERPNext manufacturing is required to record batch completion"),
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise") from error

    stock_entry_values = make_stock_entry(
        work_order,
        "Manufacture",
        qty=actual_yield,
        target_warehouse=target_warehouse,
    )
    document = frappe.get_doc(stock_entry_values)
    _set_document_value(document, "company", value.get("company") or getattr(order, "company", None))
    _set_document_value(document, "work_order", work_order)
    _set_document_value(document, "fg_completed_qty", actual_yield)
    _set_document_value(document, "to_warehouse", target_warehouse)
    _set_document_value(document, "custom_kopos_inventory_command_id", command_id)
    _set_document_value(document, "custom_kopos_inventory_payload_hash", cstr(value.get("payload_hash")).strip())
    waste_quantity = value.get("waste_qty")
    waste_decimal = _non_negative_decimal(waste_quantity, "preparation waste quantity") if waste_quantity not in (None, "") else Decimal("0")
    if waste_quantity not in (None, ""):
        _set_document_value(
            document,
            "custom_kopos_preparation_waste_qty",
            waste_decimal,
        )
    _apply_guided_task_audit(document, value)
    _apply_batch_completion_details(document, value, order=order)
    document.insert(ignore_permissions=True)
    document.flags.ignore_permissions = True
    document.submit()
    record_preparation_variance(
        order=order,
        stock_entry=document,
        actual_yield=actual_yield,
        waste_qty=waste_decimal,
    )
    return document.name


def _apply_batch_completion_details(document: Any, value: dict[str, Any], *, order: Any) -> None:
    """Apply measured inputs and finished-batch metadata to ERPNext rows."""

    supplied_rows = value.get("items", [])
    if not isinstance(supplied_rows, list):
        frappe.throw(_("Batch completion requires the measured ingredient rows"), frappe.ValidationError)
    expected_components = {
        (
            cstr(getattr(row, "item_code", None)).strip(),
            cstr(getattr(row, "source_warehouse", None)).strip(),
        )
        for row in (getattr(order, "required_items", None) or [])
        if cstr(getattr(row, "item_code", None)).strip()
    }
    component_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for supplied in supplied_rows:
        if not isinstance(supplied, dict):
            frappe.throw(_("Batch completion contains an invalid measured ingredient row"), frappe.ValidationError)
        item_code = cstr(supplied.get("item_code")).strip()
        source_warehouse = cstr(supplied.get("warehouse") or supplied.get("s_warehouse")).strip()
        key = (item_code, source_warehouse)
        if not item_code or not source_warehouse or key not in expected_components:
            frappe.throw(_("Batch completion ingredient does not match the Work Order"), frappe.ValidationError)
        if key in component_rows:
            frappe.throw(_("Batch completion repeats a measured ingredient"), frappe.ValidationError)
        _non_negative_decimal(supplied.get("qty"), "measured ingredient quantity")
        component_rows[key] = supplied
    if expected_components != set(component_rows):
        frappe.throw(_("Record one actual quantity for every Work Order ingredient"), frappe.ValidationError)
    detail_meta = frappe.get_meta("Stock Entry Detail")
    batch_no = cstr(value.get("batch_no")).strip()
    expiry_date = cstr(value.get("expiry_date")).strip()
    for row in getattr(document, "items", []) or []:
        item_code = cstr(getattr(row, "item_code", None)).strip()
        if getattr(row, "is_finished_item", 0):
            if batch_no and detail_meta.has_field("batch_no"):
                row.batch_no = batch_no
            if expiry_date and detail_meta.has_field("expiry_date"):
                row.expiry_date = expiry_date
            continue
        row_key = (item_code, cstr(getattr(row, "s_warehouse", None)).strip())
        measured = component_rows.get(row_key)
        if not measured:
            frappe.throw(_("Batch completion ingredient does not match the generated Stock Entry"), frappe.ValidationError)
        if measured.get("qty") not in (None, ""):
            row.qty = measured["qty"]
        source_warehouse = cstr(measured.get("warehouse") or measured.get("s_warehouse")).strip()
        if source_warehouse:
            row.s_warehouse = source_warehouse
        measured_batch = cstr(measured.get("batch_no") or batch_no).strip()
        measured_expiry = cstr(measured.get("expiry_date")).strip()
        if measured_batch and detail_meta.has_field("batch_no"):
            row.batch_no = measured_batch
        if measured_expiry and detail_meta.has_field("expiry_date"):
            row.expiry_date = measured_expiry


WASTE_REASON_CODES = frozenset({"Expired", "Spoiled", "Damaged", "Prep Error", "Other"})


def _submit_waste_document_now(document: Any) -> str:
    """Submit one waste event and require its Material Issue before success."""

    docstatus = cint(getattr(document, "docstatus", 0))
    stock_entry = cstr(getattr(document, "stock_entry", None)).strip()
    if docstatus == 1:
        if not stock_entry:
            frappe.throw(
                _("This waste record is submitted but its stock update is missing"),
                frappe.ValidationError,
            )
        return cstr(document.name).strip()
    if docstatus != 0 or cstr(getattr(document, "status", None)).strip() != "Draft":
        frappe.throw(_("This waste record cannot update stock"), frappe.ValidationError)
    # Device authentication, staff attribution, outlet scope, and exact payload
    # validation have already succeeded at this boundary. Do not require the
    # automation user to hold a broad manager role merely to submit the audit
    # wrapper; its Stock Entry permissions were checked before reaching here.
    document.flags.ignore_permissions = True
    document.submit()
    document.reload()
    if cint(getattr(document, "docstatus", 0)) != 1 or not cstr(
        getattr(document, "stock_entry", None)
    ).strip():
        frappe.throw(_("Waste was not applied to stock"), frappe.ValidationError)
    return cstr(document.name).strip()


def _submit_existing_waste_with_automation(value: dict[str, Any], waste_event: str) -> str:
    """Finish an exact pre-receipt waste replay under the outlet identity."""

    name = cstr(waste_event).strip()
    if not name:
        frappe.throw(_("The saved waste record is missing"), frappe.ValidationError)
    document = frappe.get_doc("FB Waste Event", name)
    if cstr(getattr(document, "company", None)).strip() != cstr(value.get("company")).strip():
        frappe.throw(_("The saved waste record belongs to another company"), frappe.PermissionError)
    if cstr(getattr(document, "warehouse", None)).strip() != cstr(value.get("warehouse")).strip():
        frappe.throw(_("The saved waste record belongs to another warehouse"), frappe.PermissionError)
    if cint(getattr(document, "docstatus", 0)) == 1:
        return _submit_waste_document_now(document)
    requirements = _GUIDED_TASK_AUTOMATION_REQUIREMENTS["record_waste_event"]
    try:
        with inventory_automation_identity(
            company=cstr(value.get("company")).strip(),
            warehouse=cstr(value.get("warehouse")).strip(),
            create_doctypes=requirements["create_doctypes"],
            submit_doctypes=requirements["submit_doctypes"],
        ):
            return _submit_waste_document_now(document)
    except AutomationIdentityError as error:
        frappe.throw(
            _("Guided inventory automation is not configured safely: {0}").format(error.reason),
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise") from error


def _record_waste_event(value: dict[str, Any], command_id: str) -> str:
    """Record physical waste and post its Material Issue in one transaction.

    Waste is the one stock movement the outlet observes rather than being handed
    a document for, so there is no Purchase Order or Material Request to execute
    against. It is still not a free-form ledger write: the submitted FB Waste
    Event preserves who recorded what and why, while its idempotent Material
    Issue changes stock immediately when this queued command reaches ERP.
    """

    company = cstr(value.get("company")).strip()
    warehouse = cstr(value.get("warehouse")).strip()
    waste_id = cstr(value.get("waste_id")).strip()
    reason_code = cstr(value.get("reason_code")).strip()
    if not company or not warehouse:
        frappe.throw(_("Waste requires the outlet company and warehouse"), frappe.ValidationError)
    if reason_code not in WASTE_REASON_CODES:
        frappe.throw(_("Waste reason is not one of the approved reasons"), frappe.ValidationError)

    source_type = cstr(value.get("source_type") or "physical_item").strip()
    resolved_recipe = ""
    resolved_recipe_hash = ""
    selected_modifiers: list[str] = []
    if source_type == "made_item":
        try:
            made_waste = resolve_made_item_waste(
                item=cstr(value.get("source_item")).strip(),
                company=company,
                quantity=value.get("source_quantity"),
                selected_modifiers=value.get("selected_modifiers") or [],
                at_time=value.get("observed_at"),
            )
        except (RecipeCompilerError, InvalidOperation, TypeError, ValueError) as error:
            frappe.log_error(
                message=cstr(error)[:1000],
                title="JiJi made-product waste could not resolve ingredients",
            )
            frappe.throw(
                _("JiJi could not work out this product's waste. Check its ingredients and customer choices, then try again."),
                frappe.ValidationError,
            )
            raise AssertionError("frappe.throw must raise") from error
        lines = made_waste["lines"]
        resolved_recipe = cstr(made_waste.get("recipe")).strip()
        resolved_recipe_hash = cstr(made_waste.get("recipe_hash")).strip()
        selected_modifiers = [
            cstr(modifier).strip()
            for modifier in made_waste.get("selected_modifiers") or []
        ]
    else:
        lines = value.get("lines")
    if not isinstance(lines, list) or not lines:
        frappe.throw(_("Waste requires at least one item and amount"), frappe.ValidationError)

    document = frappe.new_doc("FB Waste Event")
    document.waste_id = waste_id
    document.company = company
    document.warehouse = warehouse
    document.reason_code = reason_code
    document.reason_text = cstr(value.get("reason_text")).strip() or None
    document.status = "Draft"
    document.payload_hash = cstr(value.get("payload_hash")).strip()
    document.source_type = "Made Item" if source_type == "made_item" else "Physical Item"
    document.source_item = cstr(value.get("source_item")).strip() or None
    document.source_quantity_decimal = (
        format(_non_negative_decimal(value.get("source_quantity"), "made-product amount"), "f")
        if source_type == "made_item"
        else None
    )
    document.selected_modifiers_json = (
        json.dumps(selected_modifiers, separators=(",", ":"), ensure_ascii=True)
        if source_type == "made_item"
        else None
    )
    document.recipe = resolved_recipe or None
    document.recipe_canonical_hash = resolved_recipe_hash or None
    document.resolved_lines_json = json.dumps(
        [
            {
                "item": cstr(line.get("item") or line.get("item_code")).strip(),
                "qty": cstr(line.get("qty_decimal") or line.get("qty")).strip(),
                "uom": cstr(line.get("uom")).strip(),
            }
            for line in lines
            if isinstance(line, dict)
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    _set_document_value(document, "custom_kopos_inventory_command_id", command_id)
    _set_document_value(document, "custom_kopos_inventory_payload_hash", cstr(value.get("payload_hash")).strip())
    _apply_guided_task_audit(document, value)

    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            frappe.throw(_("Waste line {0} is invalid").format(index + 1), frappe.ValidationError)
        item_code = cstr(line.get("item_code") or line.get("item")).strip()
        if not item_code:
            frappe.throw(_("Waste line {0} needs an item").format(index + 1), frappe.ValidationError)
        raw_qty = cstr(line.get("qty_decimal") or line.get("qty")).strip()
        quantity = _non_negative_decimal(
            raw_qty,
            _("Waste line {0} quantity").format(index + 1),
        )
        if quantity <= 0:
            frappe.throw(_("Waste line {0} quantity must be greater than zero").format(index + 1), frappe.ValidationError)
        document.append("lines", {
            "item": item_code,
            # Frappe keeps its Float copy; qty_decimal is the exact authority the
            # waste service issues stock from.
            "qty": str(quantity),
            "qty_decimal": format(quantity, "f"),
            "uom": cstr(line.get("uom")).strip() or None,
        })

    document.insert(ignore_permissions=True)
    return _submit_waste_document_now(document)


def _create_purchase_receipt(value: dict[str, Any], command_id: str) -> str:
    purchase_order = cstr(value.get("purchase_order")).strip()
    if not purchase_order:
        frappe.throw(_("Receiving requires a Purchase Order"), frappe.ValidationError)
    _lock_guided_source_document("Purchase Order", purchase_order)
    po = frappe.get_doc("Purchase Order", purchase_order)
    if po.docstatus != 1:
        frappe.throw(_("Only a submitted Purchase Order can be received"), frappe.ValidationError)
    company = cstr(value.get("company")).strip()
    warehouse = cstr(value.get("warehouse")).strip()
    if not company or not warehouse:
        frappe.throw(_("Receiving requires the authenticated company and warehouse"), frappe.ValidationError)
    if cstr(getattr(po, "company", None)).strip() != company:
        frappe.throw(_("This Purchase Order belongs to another company"), frappe.PermissionError)
    if _supplier_receiving_is_paused(
        company=company,
        warehouse=warehouse,
        purchase_order=purchase_order,
    ):
        frappe.throw(
            _("A manager is checking a difference in this delivery. Wait until it is cleared before recording more."),
            frappe.ValidationError,
        )
    _validate_guided_source_revision(value, po, "Purchase Order")
    document = frappe.new_doc("Purchase Receipt")
    _set_document_value(document, "supplier", po.supplier)
    _set_document_value(document, "company", po.company)
    _set_document_value(document, "custom_kopos_inventory_command_id", command_id)
    _set_document_value(document, "custom_kopos_inventory_payload_hash", cstr(value.get("payload_hash")).strip())
    _apply_guided_task_audit(document, value)
    po_rows = {cstr(row.name): row for row in po.items}
    lines = value.get("lines")
    if not isinstance(lines, list) or not lines:
        frappe.throw(_("Receiving requires accepted item lines"), frappe.ValidationError)
    consumed_by_row: dict[str, Decimal] = {}
    seen_row_keys: set[str] = set()
    discrepancy_rows: list[dict[str, Any]] = []
    delivery_note = cstr(value.get("delivery_note")).strip()
    for line in lines:
        if not isinstance(line, dict):
            frappe.throw(_("Receiving contains an invalid line"), frappe.ValidationError)
        source = po_rows.get(cstr(line.get("purchase_order_item")))
        item_code = cstr(line.get("item_code") or getattr(source, "item_code", None)).strip()
        line_warehouse = cstr(line.get("warehouse")).strip()
        source_warehouse = cstr(getattr(source, "warehouse", None)).strip() if source else ""
        if not line_warehouse or line_warehouse != warehouse or not source_warehouse or line_warehouse != source_warehouse:
            frappe.throw(_("Receiving cannot post outside the device warehouse"), frappe.PermissionError)
        if not source or not item_code:
            frappe.throw(_("Receiving line does not match the submitted Purchase Order"), frappe.ValidationError)
        if item_code != cstr(getattr(source, "item_code", None)).strip():
            frappe.throw(_("Receiving Item does not match the submitted Purchase Order row"), frappe.ValidationError)
        source_uom = _guided_uom_authority(source, item_code=item_code, label="Purchase Order line")
        line_uom = cstr(line.get("uom")).strip()
        line_stock_uom = cstr(line.get("stock_uom")).strip()
        line_conversion = line.get("conversion_factor")
        if line_uom != source_uom["uom"]:
            frappe.throw(_("Receiving UOM does not match the submitted Purchase Order row"), frappe.ValidationError)
        if line_stock_uom != source_uom["stock_uom"]:
            frappe.throw(_("Receiving stock UOM does not match the submitted Purchase Order row"), frappe.ValidationError)
        if line_conversion in (None, "") or _decimal_exact(line_conversion) != source_uom["conversion_factor"]:
            frappe.throw(_("Receiving pack conversion does not match the submitted Purchase Order row"), frappe.ValidationError)
        accepted = _non_negative_decimal(line.get("qty", 0), "accepted quantity")
        missing = _non_negative_decimal(line.get("missing_qty", 0), "missing quantity")
        damaged = _non_negative_decimal(line.get("damaged_qty", 0), "damaged quantity")
        excess = _non_negative_decimal(line.get("excess_qty", 0), "excess quantity")
        remaining = max(
            Decimal(str(getattr(source, "qty", 0) or 0))
            - Decimal(str(getattr(source, "received_qty", 0) or 0)),
            Decimal("0"),
        )
        row_key = cstr(source.name)
        if row_key in seen_row_keys:
            frappe.throw(_("Receive each Purchase Order row once per tablet command"), frappe.ValidationError)
        seen_row_keys.add(row_key)
        consumed_by_row[row_key] = consumed_by_row.get(row_key, Decimal("0")) + accepted + missing + damaged
        if consumed_by_row[row_key] > remaining:
            frappe.throw(_("Receiving quantities exceed the remaining Purchase Order quantity"), frappe.ValidationError)
        if excess > 0:
            discrepancy_rows.append({"item": item_code, "kind": "excess", "quantity": excess})
        if missing > 0:
            discrepancy_rows.append({"item": item_code, "kind": "missing", "quantity": missing})
        if damaged > 0:
            discrepancy_rows.append({"item": item_code, "kind": "damaged", "quantity": damaged})
        _require_item_batch_metadata(item_code, line)
        if accepted <= 0:
            continue
        item_payload = {
            "item_code": item_code,
            "qty": accepted,
            "uom": source_uom["uom"],
            "stock_uom": source_uom["stock_uom"],
            "conversion_factor": source_uom["conversion_factor"],
            "rate": getattr(source, "rate", 0),
            "purchase_order": purchase_order,
            "purchase_order_item": source.name,
            "warehouse": line_warehouse,
        }
        if cstr(line.get("batch_no")).strip() and frappe.get_meta("Purchase Receipt Item").has_field("batch_no"):
            item_payload["batch_no"] = cstr(line["batch_no"]).strip()
        if cstr(line.get("expiry_date")).strip() and frappe.get_meta("Purchase Receipt Item").has_field("expiry_date"):
            item_payload["expiry_date"] = cstr(line["expiry_date"]).strip()
        document.append("items", item_payload)
    if document.items:
        document.insert(ignore_permissions=True)
        document.flags.ignore_permissions = True
        document.submit()
        receipt_name = document.name
    else:
        receipt_name = ""
    if discrepancy_rows:
        summary = "; ".join(
            f"{row['item']}: {row['kind']} {row['quantity']}" for row in discrepancy_rows
        )
        if delivery_note:
            summary = f"{summary}. Staff note: {delivery_note}"
        exception_name = upsert_inventory_exception(
            reason_code="supplier_receiving_discrepancy",
            summary=f"Supplier delivery differs from the Purchase Order ({summary})",
            next_action="Manager checks the delivery; a director decides on excess or supplier follow-up",
            severity="Warning",
            company=cstr(po.company),
            warehouse=cstr(value.get("warehouse")),
            source_doctype="Purchase Order",
            source_name=purchase_order,
            item=discrepancy_rows[0]["item"],
        )
        if not receipt_name:
            return exception_name
    return receipt_name


def _supplier_receiving_is_paused(*, company: str, warehouse: str, purchase_order: str) -> bool:
    """Stop duplicate delivery reports while the exact discrepancy is open."""

    if not frappe.db.exists("DocType", "FB Inventory Exception"):
        return False
    return bool(frappe.db.get_value(
        "FB Inventory Exception",
        {
            "reason_code": "supplier_receiving_discrepancy",
            "status": "Open",
            "company": cstr(company).strip(),
            "warehouse": cstr(warehouse).strip(),
            "source_doctype": "Purchase Order",
            "source_name": cstr(purchase_order).strip(),
        },
        "name",
    ))


def _create_transfer_entry(value: dict[str, Any], command_id: str, *, dispatch: bool) -> str:
    company = cstr(value.get("company")).strip()
    material_request_name = cstr(value.get("material_request") or value.get("source_document")).strip()
    outlet_warehouse = cstr(value.get("warehouse")).strip()
    if not material_request_name:
        frappe.throw(_("Transfer requires the submitted Material Request number"), frappe.ValidationError)
    if not frappe.db.exists("Material Request", material_request_name):
        frappe.throw(_("The transfer Material Request does not exist"), frappe.ValidationError)
    _lock_guided_source_document("Material Request", material_request_name)
    material_request = frappe.get_doc("Material Request", material_request_name)
    if material_request.docstatus != 1 or cstr(getattr(material_request, "material_request_type", "")).strip() != "Material Transfer":
        frappe.throw(_("Only a submitted Material Transfer Request can be executed on POS"), frappe.ValidationError)
    if cstr(getattr(material_request, "company", "")).strip() != company:
        frappe.throw(_("The transfer Material Request belongs to another company"), frappe.PermissionError)
    _validate_guided_source_revision(value, material_request, "Material Request")
    if not frappe.get_meta("Material Request").has_field("custom_kopos_transit_warehouse"):
        frappe.throw(_("The protected transfer transit route is not installed; run bench migrate"), frappe.ValidationError)
    if not _transfer_route_tracking_installed():
        frappe.throw(_("The protected transfer tracking fields are not installed; run bench migrate"), frappe.ValidationError)
    if not frappe.get_meta("Material Request Item").has_field("from_warehouse"):
        frappe.throw(_("The standard Material Request source warehouse field is not installed; run bench migrate"), frappe.ValidationError)
    transit_warehouse = cstr(getattr(material_request, "custom_kopos_transit_warehouse", None)).strip()
    from_warehouse = outlet_warehouse if dispatch else transit_warehouse
    to_warehouse = transit_warehouse if dispatch else outlet_warehouse
    if not company or not from_warehouse or not to_warehouse:
        frappe.throw(_("This submitted transfer has no protected transit warehouse"), frappe.ValidationError)
    if cstr(frappe.db.get_value("Warehouse", transit_warehouse, "company")).strip() != company:
        frappe.throw(_("The transfer transit warehouse belongs to another company"), frappe.PermissionError)
    lines = value.get("lines")
    if not isinstance(lines, list) or not lines:
        frappe.throw(_("Transfer requires item lines"), frappe.ValidationError)
    requested_rows = {
        cstr(row.name).strip(): row
        for row in (material_request.items or [])
        if cstr(row.name).strip()
    }
    document = frappe.new_doc("Stock Entry")
    document.stock_entry_type = "Material Transfer"
    document.purpose = "Material Transfer"
    document.company = company
    _set_document_value(document, "custom_kopos_inventory_command_id", command_id)
    _set_document_value(document, "custom_kopos_inventory_payload_hash", cstr(value.get("payload_hash")).strip())
    _set_document_value(document, "custom_kopos_transfer_material_request", material_request_name)
    _apply_guided_task_audit(document, value)
    seen_request_items: set[str] = set()
    short_picks: list[dict[str, Any]] = []
    for line in lines:
        if not isinstance(line, dict) or not cstr(line.get("item_code")).strip():
            frappe.throw(_("Transfer contains an invalid line"), frappe.ValidationError)
        item_code = cstr(line.get("item_code")).strip()
        request_item_name = cstr(line.get("material_request_item")).strip()
        request_item = requested_rows.get(request_item_name)
        if not request_item:
            frappe.throw(_("Transfer line is not present on the submitted Material Request"), frappe.ValidationError)
        if request_item_name in seen_request_items:
            frappe.throw(_("Transfer repeats a Material Request line in one tablet command"), frappe.ValidationError)
        seen_request_items.add(request_item_name)
        if cstr(getattr(request_item, "item_code", None)).strip() != item_code:
            frappe.throw(_("Transfer Item does not match the submitted Material Request line"), frappe.ValidationError)
        request_source = cstr(getattr(request_item, "from_warehouse", None)).strip()
        request_destination = cstr(getattr(request_item, "warehouse", None)).strip()
        if not request_source or not request_destination or request_source == request_destination:
            frappe.throw(_("The submitted transfer line has no distinct source and destination warehouse"), frappe.ValidationError)
        if cstr(frappe.db.get_value("Warehouse", request_source, "company")).strip() != company or cstr(
            frappe.db.get_value("Warehouse", request_destination, "company")
        ).strip() != company:
            frappe.throw(_("The submitted transfer source or destination is outside the Material Request company"), frappe.PermissionError)
        if dispatch:
            route_matches = request_source == outlet_warehouse
        else:
            route_matches = request_destination == outlet_warehouse
        if not route_matches:
            frappe.throw(_("Transfer line is outside this outlet's approved route"), frappe.PermissionError)
        request_uom = _guided_uom_authority(
            request_item, item_code=item_code, label="Material Request line"
        )
        if cstr(line.get("uom")).strip() != request_uom["uom"]:
            frappe.throw(_("Transfer UOM does not match the submitted Material Request line"), frappe.ValidationError)
        if cstr(line.get("stock_uom")).strip() != request_uom["stock_uom"]:
            frappe.throw(_("Transfer stock UOM does not match the submitted Material Request line"), frappe.ValidationError)
        if line.get("conversion_factor") in (None, "") or _decimal_exact(line.get("conversion_factor")) != request_uom["conversion_factor"]:
            frappe.throw(_("Transfer conversion does not match the submitted Material Request line"), frappe.ValidationError)
        requested_qty = request_uom["stock_qty"]
        dispatched_qty, received_qty = _transfer_route_progress(
            material_request=material_request_name,
            material_request_item=request_item_name,
            source_warehouse=request_source,
            transit_warehouse=transit_warehouse,
            destination_warehouse=request_destination,
        )
        remaining_qty = requested_qty - dispatched_qty if dispatch else dispatched_qty - received_qty
        remaining_qty = max(remaining_qty, Decimal("0"))
        line_quantity = _non_negative_decimal(line.get("qty"), "transfer quantity")
        line_stock_quantity = line_quantity * request_uom["conversion_factor"]
        if line_quantity <= 0 or line_stock_quantity <= 0:
            frappe.throw(_("Transfer quantity must be greater than zero"), frappe.ValidationError)
        if line_stock_quantity > remaining_qty:
            frappe.throw(_("Transfer quantity exceeds the amount currently approved for this route"), frappe.ValidationError)
        _require_transfer_batch_metadata(item_code, line)
        item_payload = {
            "item_code": item_code,
            "qty": line_quantity,
            "uom": request_uom["uom"],
            "stock_uom": request_uom["stock_uom"],
            "conversion_factor": request_uom["conversion_factor"],
            "s_warehouse": from_warehouse,
            "t_warehouse": to_warehouse,
            "batch_no": line.get("batch_no"),
        }
        detail_meta = frappe.get_meta("Stock Entry Detail")
        if detail_meta.has_field("transfer_qty"):
            item_payload["transfer_qty"] = line_stock_quantity
        item_payload["custom_kopos_transfer_request_item"] = request_item_name
        # A dispatch moves stock only into transit.  It intentionally does not
        # set the standard Material Request link because ERPNext would then
        # treat stock in transit as delivered.  The receipt links the standard
        # request after the destination manager confirms physical arrival.
        if not dispatch:
            if detail_meta.has_field("material_request"):
                item_payload["material_request"] = material_request_name
            if detail_meta.has_field("material_request_item"):
                item_payload["material_request_item"] = request_item_name
        document.append("items", item_payload)
        if line_stock_quantity < remaining_qty:
            short_picks.append({
                "item": item_code,
                "requested": requested_qty,
                "dispatched": dispatched_qty + line_stock_quantity,
                "remaining": remaining_qty - line_stock_quantity,
            })
    document.insert(ignore_permissions=True)
    document.flags.ignore_permissions = True
    document.submit()
    for short_pick in short_picks:
        # A short dispatch leaves stock at source; a short receipt leaves it in
        # transit, which is the missing/damaged case that needs investigating.
        if dispatch:
            summary = (
                f"{short_pick['item']} was partially dispatched to transit; "
                f"{short_pick['remaining']} remains on the approved transfer"
            )
            next_action = "A Company Director reviews the remaining quantity or revises the submitted Material Request"
        else:
            summary = (
                f"{short_pick['item']} arrived short at the destination; "
                f"{short_pick['remaining']} remains unaccounted for in transit"
            )
            next_action = "Manager checks the delivery and quarantine; a Company Director investigates the stock still in transit"
        upsert_inventory_exception(
            reason_code="inventory_transfer_short_pick" if dispatch else "inventory_transfer_receipt_shortfall",
            summary=summary,
            next_action=next_action,
            severity="Warning",
            company=company,
            warehouse=outlet_warehouse,
            item=short_pick["item"],
            source_doctype="Material Request",
            source_name=material_request_name,
        )
    return document.name


def _transfer_route_tracking_installed() -> bool:
    """Require durable per-request-item route evidence before POS transfers run."""

    if not frappe.db.exists("DocType", "Stock Entry") or not frappe.db.exists("DocType", "Stock Entry Detail"):
        return False
    return (
        frappe.get_meta("Stock Entry").has_field("custom_kopos_transfer_material_request")
        and frappe.get_meta("Stock Entry Detail").has_field("custom_kopos_transfer_request_item")
    )


def _actionable_transfer_request_lines(
    *, material_request: str, warehouse: str, transit_warehouse: str,
    fields: list[str], source_field_installed: bool,
    route_tracking_installed: bool, limit: int,
) -> list[dict[str, Any]]:
    """Limit after route progress, so completed early rows cannot hide work.

    A submitted Material Request can contain more lines than the tablet page.
    Selecting the first rows and checking progress afterwards would return the
    same completed rows forever. The tracked path derives dispatch and receipt
    progress in SQL first, returns only actionable lines, and avoids one query
    per line. A route whose custom fields are not installed remains visible to
    its source outlet as blocked setup work and cannot create stock movement.
    """

    if not source_field_installed:
        return []
    if not route_tracking_installed or not transit_warehouse:
        rows = frappe.get_all(
            "Material Request Item",
            filters={
                "parent": material_request,
                "parenttype": "Material Request",
                "from_warehouse": warehouse,
            },
            fields=fields,
            order_by="idx asc",
            limit_page_length=limit,
        )
        return [
            {
                **dict(row),
                "kopos_dispatched_stock_qty": Decimal("0"),
                "kopos_received_stock_qty": Decimal("0"),
            }
            for row in rows
        ]

    selected_fields = ", ".join(f"line.`{fieldname}`" for fieldname in fields)
    if "stock_qty" in fields:
        fallback = (
            "line.`qty` * COALESCE(NULLIF(line.`conversion_factor`, 0), 1)"
            if "conversion_factor" in fields
            else "line.`qty`"
        )
        requested_stock_quantity = (
            f"CASE WHEN COALESCE(line.`stock_qty`, 0) > 0 "
            f"THEN line.`stock_qty` ELSE {fallback} END"
        )
    elif "conversion_factor" in fields:
        requested_stock_quantity = (
            "line.`qty` * COALESCE(NULLIF(line.`conversion_factor`, 0), 1)"
        )
    else:
        requested_stock_quantity = "line.`qty`"

    dispatched_quantity = """
        COALESCE((
            SELECT COALESCE(SUM(detail.transfer_qty), SUM(detail.qty), 0)
            FROM `tabStock Entry` entry
            INNER JOIN `tabStock Entry Detail` detail ON detail.parent = entry.name
            WHERE entry.docstatus = 1
              AND entry.purpose = 'Material Transfer'
              AND entry.custom_kopos_transfer_material_request = %s
              AND detail.custom_kopos_transfer_request_item = line.name
              AND detail.s_warehouse = line.from_warehouse
              AND detail.t_warehouse = %s
        ), 0)
    """
    received_quantity = """
        COALESCE((
            SELECT COALESCE(SUM(detail.transfer_qty), SUM(detail.qty), 0)
            FROM `tabStock Entry` entry
            INNER JOIN `tabStock Entry Detail` detail ON detail.parent = entry.name
            WHERE entry.docstatus = 1
              AND entry.purpose = 'Material Transfer'
              AND entry.custom_kopos_transfer_material_request = %s
              AND detail.custom_kopos_transfer_request_item = line.name
              AND detail.s_warehouse = %s
              AND detail.t_warehouse = line.warehouse
        ), 0)
    """
    return frappe.db.sql(
        f"""
        SELECT scoped.*
        FROM (
            SELECT {selected_fields},
                   {requested_stock_quantity} AS kopos_requested_stock_qty,
                   {dispatched_quantity} AS kopos_dispatched_stock_qty,
                   {received_quantity} AS kopos_received_stock_qty
            FROM `tabMaterial Request Item` line
            WHERE line.parent = %s AND line.parenttype = 'Material Request'
        ) scoped
        WHERE (
            scoped.from_warehouse = %s
            AND scoped.kopos_requested_stock_qty > scoped.kopos_dispatched_stock_qty
        ) OR (
            scoped.warehouse = %s
            AND scoped.kopos_dispatched_stock_qty > scoped.kopos_received_stock_qty
        )
        ORDER BY scoped.idx ASC
        LIMIT %s
        """,
        (
            material_request, transit_warehouse,
            material_request, transit_warehouse,
            material_request, warehouse, warehouse, limit,
        ),
        as_dict=True,
    ) or []


def _material_request_stock_quantity(row: Any) -> Decimal:
    """Return a Material Request line quantity in stock UOM without guessing."""

    return _guided_uom_authority(row, item_code="", label="Material Request line")["stock_qty"]


def _guided_row_value(row: Any, fieldname: str, default: Any = None) -> Any:
    getter = getattr(row, "get", None)
    if callable(getter):
        return getter(fieldname, default)
    return getattr(row, fieldname, default)


def _guided_uom_authority(row: Any, *, item_code: str, label: str) -> dict[str, Any]:
    """Resolve one ERP document line's source/stock UOM authority.

    POS quantities are entered in the source document UOM.  ERPNext stock
    documents receive the same UOM and its exact conversion factor; all route
    progress is compared in stock UOM.  A same-UOM line has an explicit factor
    of one.  No other missing conversion is inferred.
    """

    uom = cstr(_guided_row_value(row, "uom")).strip()
    stock_uom = cstr(_guided_row_value(row, "stock_uom")).strip()
    item_code = cstr(item_code).strip()
    if not stock_uom and item_code and frappe.db.exists("Item", item_code):
        stock_uom = cstr(frappe.db.get_value("Item", item_code, "stock_uom")).strip()
    if not uom or not stock_uom:
        frappe.throw(_("{0} is missing its source and stock UOM authority").format(label), frappe.ValidationError)

    quantity = _non_negative_decimal(_guided_row_value(row, "qty"), f"{label} quantity")
    raw_factor = _guided_row_value(row, "conversion_factor")
    stock_quantity = _guided_row_value(row, "stock_qty")
    if raw_factor in (None, ""):
        if uom == stock_uom:
            factor = Decimal("1")
        elif stock_quantity not in (None, "") and quantity > 0:
            factor = _decimal_exact(stock_quantity) / quantity
        else:
            frappe.throw(_("{0} is missing its exact UOM conversion").format(label), frappe.ValidationError)
    else:
        factor = _decimal_exact(raw_factor)
    if not factor.is_finite() or factor <= 0:
        frappe.throw(_("{0} has an invalid UOM conversion").format(label), frappe.ValidationError)
    calculated_stock_quantity = quantity * factor
    if stock_quantity not in (None, ""):
        stored_stock_quantity = _decimal_exact(stock_quantity)
        if stored_stock_quantity != calculated_stock_quantity:
            frappe.throw(_("{0} stock quantity does not match its UOM conversion").format(label), frappe.ValidationError)
        calculated_stock_quantity = stored_stock_quantity
    return {
        "uom": uom,
        "stock_uom": stock_uom,
        "conversion_factor": factor,
        "qty": quantity,
        "stock_qty": calculated_stock_quantity,
    }


def _transfer_route_progress(
    *, material_request: str, material_request_item: str, source_warehouse: str,
    transit_warehouse: str, destination_warehouse: str,
) -> tuple[Decimal, Decimal]:
    """Derive dispatched and received stock from submitted two-stage entries.

    This intentionally ignores Material Request ``transferred_qty`` while the
    stock is in transit.  ERPNext's normal link is written only at destination
    receipt, so its own Material Request lifecycle still advances when stock is
    actually delivered.
    """

    if not _transfer_route_tracking_installed() or not all((
        material_request, material_request_item, source_warehouse,
        transit_warehouse, destination_warehouse,
    )):
        return Decimal("0"), Decimal("0")
    rows = frappe.db.sql(
        """
        SELECT detail.s_warehouse, detail.t_warehouse,
               COALESCE(SUM(detail.transfer_qty), SUM(detail.qty), 0) AS quantity
        FROM `tabStock Entry` entry
        INNER JOIN `tabStock Entry Detail` detail ON detail.parent = entry.name
        WHERE entry.docstatus = 1
          AND entry.purpose = 'Material Transfer'
          AND entry.custom_kopos_transfer_material_request = %s
          AND detail.custom_kopos_transfer_request_item = %s
        GROUP BY detail.s_warehouse, detail.t_warehouse
        """,
        (material_request, material_request_item),
        as_dict=True,
    ) or []
    dispatched = Decimal("0")
    received = Decimal("0")
    for row in rows:
        quantity = _non_negative_decimal(
            row.get("quantity"), "recorded transfer stock quantity"
        )
        source = cstr(row.get("s_warehouse")).strip()
        destination = cstr(row.get("t_warehouse")).strip()
        if source == source_warehouse and destination == transit_warehouse:
            dispatched += quantity
        elif source == transit_warehouse and destination == destination_warehouse:
            received += quantity
    return dispatched, received


def _validate_guided_task_scope(device_id: str, value: dict[str, Any], task_type: str) -> None:
    """Keep a device command inside its assigned outlet warehouses."""

    profile = resolve_catalog_pos_profile(device_id=device_id) or {}
    assigned_warehouse = cstr(profile.get("warehouse")).strip()
    assigned_company = cstr(profile.get("company")).strip()
    assigned_outlet = cstr(profile.get("name")).strip()
    if not assigned_warehouse:
        frappe.throw(_("This device has no assigned inventory warehouse"), frappe.ValidationError)
    if not assigned_company:
        frappe.throw(_("This device has no assigned company"), frappe.ValidationError)
    if not assigned_outlet:
        frappe.throw(_("This device has no assigned outlet"), frappe.ValidationError)
    if cstr(value.get("company")).strip() != assigned_company:
        frappe.throw(_("This guided task is outside the device company"), frappe.PermissionError)
    if cstr(value.get("outlet")).strip() != assigned_outlet:
        frappe.throw(_("This guided task is outside the device outlet"), frappe.PermissionError)
    if task_type == "accept_preparation_task":
        controlled = cstr(value.get("warehouse")).strip()
    elif task_type == "start_preparation_task":
        controlled = cstr(value.get("warehouse")).strip()
    elif task_type == "complete_preparation_task":
        controlled = cstr(value.get("fg_warehouse") or value.get("warehouse")).strip()
    elif task_type == "submit_purchase_receipt":
        controlled = cstr(value.get("warehouse")).strip()
    elif task_type in {"submit_transfer_dispatch", "submit_transfer_receipt"}:
        # Transfer routes are derived server-side from this assigned outlet and
        # the policy transit warehouse.  The device cannot select either end.
        controlled = cstr(value.get("warehouse")).strip()
    else:
        controlled = cstr(value.get("warehouse")).strip()
    if controlled and controlled != assigned_warehouse:
        frappe.throw(_("This guided task is outside the device warehouse"), frappe.PermissionError)


def _validate_work_order_scope(order: Any, value: dict[str, Any]) -> None:
    """Bind an accepted, started, or completed batch to the outlet task."""

    if cstr(getattr(order, "company", None)).strip() != cstr(value.get("company")).strip():
        frappe.throw(_("This Work Order belongs to another company"), frappe.PermissionError)
    expected_warehouse = cstr(value.get("warehouse")).strip()
    if not expected_warehouse or cstr(getattr(order, "fg_warehouse", None)).strip() != expected_warehouse:
        frappe.throw(_("This Work Order is not assigned to this outlet warehouse"), frappe.PermissionError)


def _validate_guided_task_actor(device: Any, value: dict[str, Any], task_type: str) -> dict[str, Any]:
    """Bind the physical action to an active outlet user and capability."""

    staff_id = cstr(value.get("staff_user") or value.get("staff_id")).strip()
    if not staff_id:
        frappe.throw(_("Guided task requires the signed-in staff user"), frappe.ValidationError)
    access = find_staff_access_for_device(device, staff_id)
    if access is None:
        frappe.throw(_("Signed-in staff user is not active on this outlet tablet"), frappe.PermissionError)
    recorded_employee = cstr(access.get("employee")).strip()
    if not recorded_employee or cstr(value.get("employee")).strip() != recorded_employee:
        frappe.throw(_("Guided task Employee does not match central staff access"), frappe.PermissionError)
    if task_type in {"submit_purchase_receipt", "submit_transfer_dispatch", "submit_transfer_receipt"}:
        if access.get("access_level") != "Manager":
            frappe.throw(
                _("A manager must sign in before confirming this stock movement"),
                frappe.PermissionError,
            )
    recorded_by = cstr(value.get("recorded_by")).strip()
    if recorded_by and find_staff_access_for_device(device, recorded_by) is None:
        frappe.throw(_("The staff member who recorded this task is not active on this outlet tablet"), frappe.PermissionError)
    return access


def _require_device_staff(device: Any, staff_user: str | None, *, manager: bool = False) -> dict[str, Any]:
    user = cstr(staff_user).strip()
    if not user:
        frappe.throw(_("Inventory command requires the signed-in staff user"), frappe.ValidationError)
    access = find_staff_access_for_device(device, user)
    if access is None:
        frappe.throw(_("Signed-in staff user is not active on this outlet tablet"), frappe.PermissionError)
    if manager and access.get("access_level") != "Manager":
        frappe.throw(_("A manager must sign in before changing availability"), frappe.PermissionError)
    return access


def _require_central_count_staff_access(
    *, staff_id: str, company: str, warehouse: str, outlet: str
) -> dict[str, Any]:
    """Resolve the recorded count actor from central Staff Access only."""

    if not frappe.db.exists("DocType", "KoPOS Staff Access") or not frappe.db.exists("DocType", "KoPOS Staff Outlet"):
        frappe.throw(_("Central KoPOS Staff Access is not installed; refresh staff access before confirming counts"), frappe.ValidationError)
    assignments = frappe.get_all(
        "KoPOS Staff Outlet",
        filters={"company": company, "warehouse": warehouse, "outlet": outlet},
        fields=["parent"],
        limit_page_length=10_000,
    )
    parents = sorted({cstr(row.get("parent")).strip() for row in assignments if cstr(row.get("parent")).strip()})
    if not parents:
        frappe.throw(_("No central staff access is assigned to this outlet"), frappe.PermissionError)
    rows = frappe.get_all(
        "KoPOS Staff Access",
        filters={"name": ["in", parents], "user": staff_id, "active": 1},
        fields=["user", "employee", "access_level"],
        limit_page_length=2,
    )
    if len(rows) != 1 or not cstr(rows[0].get("employee")).strip():
        frappe.throw(_("The recorded count staff member is not in central KoPOS Staff Access for this outlet"), frappe.PermissionError)
    return dict(rows[0])


def _set_document_value(document: Any, fieldname: str, value: Any) -> None:
    if value not in (None, "") and frappe.get_meta(document.doctype).has_field(fieldname):
        setattr(document, fieldname, value)


def _apply_guided_task_audit(document: Any, value: dict[str, Any]) -> None:
    """Persist the physical recorder and online confirmer without exposing cost data."""

    recorded_by = cstr(value.get("recorded_by") or value.get("staff_user") or value.get("staff_id")).strip()
    confirmed_by = cstr(value.get("staff_user") or value.get("staff_id")).strip()
    _set_document_value(document, "custom_kopos_inventory_recorded_by", recorded_by)
    _set_document_value(document, "custom_kopos_inventory_confirmed_by", confirmed_by)


def _non_negative_decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError) as error:
        frappe.throw(_("{0} must be a non-negative number").format(label), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if not parsed.is_finite() or parsed < 0:
        frappe.throw(_("{0} must be a non-negative number").format(label), frappe.ValidationError)
    return parsed


def _decimal_exact(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        frappe.throw(_("Inventory quantity authority is invalid"), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if not parsed.is_finite():
        frappe.throw(_("Inventory quantity authority is invalid"), frappe.ValidationError)
    return parsed


def _require_item_batch_metadata(item_code: str, line: dict[str, Any]) -> None:
    """Require batch/expiry details when the Item master says they are needed."""

    if not frappe.db.exists("Item", item_code):
        return
    item = frappe.db.get_value("Item", item_code, ["has_batch_no", "has_expiry_date"], as_dict=True) or {}
    batch_no = cstr(line.get("batch_no")).strip()
    expiry_date = cstr(line.get("expiry_date")).strip()
    if cint(item.get("has_batch_no")) and not batch_no:
        frappe.throw(_("{0} requires a batch number before it can be received").format(item_code), frappe.ValidationError)
    if cint(item.get("has_expiry_date")) and not expiry_date:
        frappe.throw(_("{0} requires an expiry date before it can be received").format(item_code), frappe.ValidationError)


def _require_transfer_batch_metadata(item_code: str, line: dict[str, Any]) -> None:
    """Require the existing batch identity for a physical inter-outlet move.

    An expiry date already belongs to the batch master; staff should not have to
    retype it during dispatch or receipt.  A batch-tracked Item still requires
    its batch number so ERPNext can keep its own FEFO/Serial-and-Batch-Bundle
    behavior authoritative.
    """

    if not frappe.db.exists("Item", item_code):
        return
    item = frappe.db.get_value("Item", item_code, "has_batch_no")
    if cint(item) and not cstr(line.get("batch_no")).strip():
        frappe.throw(_("{0} requires a batch number before it can be transferred").format(item_code), frappe.ValidationError)


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_for_hash(payload).encode("utf-8")).hexdigest()


def _canonical_json_for_hash(value: Any) -> str:
    """Match the POS canonical JSON rules for command payload identities.

    Guided commands are signed over the exact wire object.  Python's default
    ``json.dumps`` escapes non-ASCII text differently from JSON.stringify and
    can serialize floats such as ``1.0`` differently, so keep this tiny
    canonicalizer deliberately aligned with the TypeScript implementation.
    """

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int):
        if abs(value) > 9_007_199_254_740_991:
            raise ValueError("command payload integer is outside the safe JSON range")
        return str(value)
    if isinstance(value, float):
        if not value.is_integer() or abs(value) > 9_007_199_254_740_991:
            raise ValueError("command payload number must be a finite safe integer")
        return str(int(value))
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical_json_for_hash(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise ValueError("command payload keys must be strings")
            parts.append(
                f"{json.dumps(key, ensure_ascii=False)}:{_canonical_json_for_hash(value[key])}"
            )
        return "{" + ",".join(parts) + "}"
    raise ValueError("command payload contains unsupported data")


def _authoritative_document_revision(document_or_row: Any) -> str:
    """Return an opaque revision derived from a standard document's ``modified``.

    The standard ERPNext document timestamp remains the only mutable authority;
    no second counter or custom revision field is introduced for guided work.
    """

    if isinstance(document_or_row, dict):
        modified = document_or_row.get("modified")
    else:
        modified = getattr(document_or_row, "modified", None)
    if not modified:
        raise ValueError("standard inventory task document has no modified timestamp")
    parsed = get_datetime(modified)
    if not parsed:
        raise ValueError("standard inventory task document has an invalid modified timestamp")
    return cstr(_iso_with_offset(parsed)).strip()


def _lock_guided_source_document(doctype: str, name: str) -> None:
    """Lock the standard source document before comparing its revision."""

    safe_doctypes = {"BOM", "Work Order", "Purchase Order", "Material Request"}
    if doctype not in safe_doctypes or not name:
        frappe.throw(_("Guided task source document is invalid"), frappe.ValidationError)
    frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` WHERE name = %s FOR UPDATE",
        (name,),
    )


def _validate_guided_source_revision(value: dict[str, Any], document: Any, label: str) -> None:
    expected = _authoritative_document_revision(document)
    supplied = cstr(value.get("source_revision")).strip()
    if supplied == expected:
        return
    # Tablets that received a task before canonical revision formatting was
    # deployed may retain ERPNext's raw DB timestamp. Accept only that precise
    # legacy shape when it resolves to the same instant; stale timestamps still
    # fail closed and current tasks must use the canonical offset form.
    legacy_revision = None
    for timestamp_format in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            legacy_revision = cstr(
                _iso_with_offset(datetime.strptime(supplied, timestamp_format))
            ).strip()
            break
        except ValueError:
            continue
    if legacy_revision == expected:
        return
    frappe.throw(
        _("{0} changed after this tablet task was issued; refresh the task").format(label),
        frappe.ValidationError,
    )


def _get_policy(warehouse: str) -> dict[str, Any] | None:
    fields = ["name", "company", "automation_state", "max_source_age_minutes"]
    if frappe.db.exists("DocType", "FB Inventory Policy"):
        meta = frappe.get_meta("FB Inventory Policy")
        if meta.has_field("inventory_exception_owner"):
            fields.append("inventory_exception_owner")
        for fieldname in ("cutover_token", "cutover_at", "opening_stock_reconciliation"):
            if meta.has_field(fieldname):
                fields.append(fieldname)
    names = frappe.get_all(
        "FB Inventory Policy",
        filters={"warehouse": warehouse},
        fields=fields,
        limit_page_length=1,
    )
    return names[0] if names else None


def _stock_tracking_started(policy: dict[str, Any] | None) -> bool:
    """True only when the immutable start identity is complete."""
    if not policy:
        return False
    return all(
        cstr(policy.get(fieldname)).strip()
        for fieldname in ("cutover_token", "cutover_at", "opening_stock_reconciliation")
    )


def _work_order_component_lines(work_order_name: str) -> list[dict[str, Any]]:
    """Expose the approved BOM inputs as a non-financial preparation checklist."""

    if not work_order_name:
        return []
    try:
        work_order = frappe.get_doc("Work Order", work_order_name)
    except Exception:
        return []
    lines: list[dict[str, Any]] = []
    for row in getattr(work_order, "required_items", None) or []:
        item_code = cstr(getattr(row, "item_code", None)).strip()
        if not item_code:
            continue
        quantity = getattr(row, "required_qty", None)
        if quantity in (None, ""):
            quantity = getattr(row, "qty", None)
        lines.append({
            "item_code": item_code,
            "item_name": cstr(getattr(row, "item_name", None)).strip() or item_code,
            "qty": quantity,
            "warehouse": cstr(getattr(row, "source_warehouse", None)).strip(),
        })
    return lines


def _work_order_preparation_instructions(bom_name: str) -> str:
    """Return the short approved BOM instruction without exposing costing data."""

    if not bom_name or not frappe.db.exists("DocType", "BOM"):
        return ""
    try:
        meta = frappe.get_meta("BOM")
        if not meta.has_field("custom_kopos_preparation_instructions"):
            return ""
        return cstr(
            frappe.db.get_value("BOM", bom_name, "custom_kopos_preparation_instructions")
        ).strip()[:2_000]
    except Exception:
        # This GET boundary must remain non-blocking for the outlet task list.
        return ""


def _preparation_tasks_visible_to_device(*, device: Any, company: str, warehouse: str) -> bool:
    """Prefer one preparation tablet, with a manager-capable outlet fallback."""

    if not frappe.db.exists("DocType", "FB Inventory Policy"):
        return False
    policy_meta = frappe.get_meta("FB Inventory Policy")
    fields = ["max_source_age_minutes"]
    if policy_meta.has_field("preparation_device"):
        fields.append("preparation_device")
    policy = frappe.db.get_value(
        "FB Inventory Policy",
        {"company": company, "warehouse": warehouse},
        fields,
        as_dict=True,
    ) or {}
    preferred_name = cstr(policy.get("preparation_device")).strip()
    current_name = cstr(getattr(device, "name", None)).strip()
    if preferred_name and preferred_name == current_name:
        return True
    if preferred_name and _device_is_current_for_preparation(
        preferred_name,
        max_age_minutes=cint(policy.get("max_source_age_minutes") or 30),
    ):
        return False
    return any(
        cstr(access.get("access_level")).strip() == "Manager"
        for access in resolve_staff_access_for_device(device)
    )


def _device_is_current_for_preparation(device_name: str, *, max_age_minutes: int) -> bool:
    row = frappe.db.get_value("KoPOS Device", device_name, ["enabled", "last_seen_at"], as_dict=True) or {}
    if not cint(row.get("enabled")) or not row.get("last_seen_at"):
        return False
    try:
        return now_datetime() - get_datetime(row.get("last_seen_at")) <= timedelta(minutes=max(1, max_age_minutes))
    except (TypeError, ValueError):
        return False


def _age_minutes(value: Any, now: Any) -> int | None:
    if value is None:
        return None
    left = value
    right = now
    if getattr(left, "tzinfo", None) is not None and getattr(right, "tzinfo", None) is None:
        left = left.astimezone(ZoneInfo("Asia/Kuala_Lumpur")).replace(tzinfo=None)
    elif getattr(left, "tzinfo", None) is None and getattr(right, "tzinfo", None) is not None:
        right = right.astimezone(ZoneInfo("Asia/Kuala_Lumpur")).replace(tzinfo=None)
    return max(0, int((right - left).total_seconds() // 60))


def _datetime_sort_key(value: Any) -> float:
    current = value
    if getattr(current, "tzinfo", None) is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
    return current.timestamp()


def _active_projection_oldest_age(rows: list[dict[str, Any]], *, now: Any) -> int | None:
    """Age only work that can still require recovery.

    Succeeded and Reversed logs are historical evidence.  Including them in
    the oldest backlog age makes a healthy outlet look permanently overdue.
    """

    oldest = min(
        (
            get_datetime(row.get("oldest_created_at"))
            for row in rows
            if cstr(row.get("state")).strip() in ACTIVE_PROJECTION_STATES
            and row.get("oldest_created_at")
        ),
        key=_datetime_sort_key,
        default=None,
    )
    return _age_minutes(oldest, now)


def _device_is_dirty(row: dict[str, Any]) -> bool:
    """Return whether sales or inventory work remains on a device."""

    return any(
        int(row.get(fieldname) or 0) > 0
        for fieldname in (
            "inventory_sales_pending",
            "inventory_sales_syncing",
            "inventory_sales_failed",
            "inventory_sales_dead_letter",
            "inventory_commands_pending",
            "inventory_commands_syncing",
            "inventory_commands_failed",
            "inventory_commands_dead_letter",
        )
    )


def _next_success_deadline(last_success: Any, *, expected_interval_minutes: int = INVENTORY_RECOVER_INTERVAL_MINUTES) -> Any:
    if last_success is None:
        return None
    return last_success + timedelta(minutes=max(1, int(expected_interval_minutes)))


def _health_exception_response(row: dict[str, Any], *, now: Any) -> dict[str, Any]:
    last_seen = get_datetime(row.get("last_seen")) if row.get("last_seen") else None
    return {
        "name": cstr(row.get("name")),
        "severity": cstr(row.get("severity")) or "Warning",
        "reason_code": cstr(row.get("reason_code")),
        "summary": cstr(row.get("summary")),
        "next_action": cstr(row.get("next_action")),
        "source_doctype": cstr(row.get("source_doctype")),
        "source_name": cstr(row.get("source_name")),
        "item": cstr(row.get("item")),
        "last_seen": _iso_with_offset(last_seen) if last_seen else None,
        "age_minutes": _age_minutes(last_seen, now),
    }


def _health_count_summary(warehouse: str, *, now: Any) -> dict[str, Any]:
    if not frappe.db.exists("DocType", "FB Inventory Count Task"):
        return {"status": "not_installed", "open": 0, "by_status": {}, "oldest_age_minutes": None}
    try:
        rows = frappe.get_all(
            "FB Inventory Count Task",
            filters={"warehouse": warehouse, "status": ["in", sorted(OPEN_COUNT_TASK_STATUSES)]},
            fields=["name", "status", "modified"],
            order_by="modified asc",
            limit_page_length=100,
        )
    except Exception as error:
        log_sanitized_error("Inventory health count summary failed", error)
        return {"status": "unavailable", "open": 0, "by_status": {}, "oldest_age_minutes": None}
    by_status: dict[str, int] = {}
    timestamps = []
    for row in rows or []:
        status = cstr(row.get("status")).strip() or "Unknown"
        by_status[status] = by_status.get(status, 0) + 1
        if row.get("modified"):
            timestamps.append(get_datetime(row.get("modified")))
    oldest = min(timestamps, key=_datetime_sort_key, default=None)
    return {
        "status": "ok",
        "open": len(rows or []),
        "by_status": by_status,
        "oldest_age_minutes": _age_minutes(oldest, now),
    }


def _health_count_director_reviews(warehouse: str, *, now: Any) -> dict[str, Any]:
    """Return safe count observations waiting for director review.

    The manager page needs enough evidence to make one informed decision, but
    not the hidden valuation or monetary ceiling that caused the review.
    ``lines_json`` is immutable observation evidence; this helper only derives
    the operational pack breakdown and signed quantity difference for display.
    """

    if not frappe.db.exists("DocType", "FB Inventory Count Observation"):
        return {"status": "not_installed", "open": 0, "items": []}
    try:
        meta = frappe.get_meta("FB Inventory Count Observation")
        required = {
            "observation_id", "warehouse", "status", "lines_json", "reconciliation",
            "variance_requires_director",
        }
        if not required.issubset({field.fieldname for field in meta.fields}):
            return {"status": "unavailable", "open": 0, "items": []}
        fields = [
            "name", "observation_id", "task_id", "task_revision", "warehouse",
            "observed_at", "lines_json", "status", "reconciliation", "variance_percent",
            "variance_requires_director",
        ]
        rows = frappe.get_all(
            "FB Inventory Count Observation",
            filters={
                "warehouse": warehouse,
                "status": "Review",
                "variance_requires_director": 1,
            },
            fields=fields,
            order_by="observed_at asc",
            limit_page_length=50,
        )
    except Exception as error:
        log_sanitized_error("Inventory health director count review lookup failed", error)
        return {"status": "unavailable", "open": 0, "items": []}

    reviews: list[dict[str, Any]] = []
    for row in rows or []:
        if cstr(row.get("reconciliation")).strip():
            continue
        try:
            raw_lines = json.loads(cstr(row.get("lines_json")))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(raw_lines, list) or not raw_lines:
            continue
        safe_lines: list[dict[str, Any]] = []
        for line in raw_lines[:100]:
            if not isinstance(line, dict):
                continue
            item_id = cstr(line.get("item_id")).strip()
            total = _safe_count_quantity(line.get("total_quantity") or line.get("quantity"))
            if not item_id or total is None:
                continue
            try:
                line_variance = _count_line_variance_details(
                    warehouse=warehouse,
                    item_id=item_id,
                    counted_quantity=total,
                )
            except Exception:
                line_variance = None
            uom = cstr(line.get("stock_uom") or line.get("uom") or "Nos").strip()
            safe_line: dict[str, Any] = {
                "item_id": item_id,
                "item_name": cstr(line.get("item_name")).strip() or item_id,
                "uom": uom,
                "total_quantity": total,
                "counted_display": amount_phrase(total, uom),
                "difference_quantity": line_variance["difference_quantity"] if line_variance else None,
                "difference_display": (
                    difference_phrase(line_variance["difference_quantity"], uom)
                    if line_variance
                    else None
                ),
                "variance_percent": line_variance["variance_percent"] if line_variance else None,
            }
            if all(
                line.get(fieldname) not in (None, "")
                for fieldname in ("full_packs", "loose_quantity", "purchase_uom", "conversion_factor")
            ):
                purchase_uom = cstr(line.get("purchase_uom")).strip()
                full_packs = _safe_count_quantity(line.get("full_packs"))
                loose_quantity = _safe_count_quantity(line.get("loose_quantity"))
                safe_line.update({
                    "purchase_uom": purchase_uom,
                    "conversion_factor": cstr(line.get("conversion_factor")),
                    "full_packs": full_packs,
                    "loose_quantity": loose_quantity,
                })
                if purchase_uom and full_packs is not None and loose_quantity is not None:
                    safe_line["pack_breakdown_display"] = (
                        f"{amount_phrase(full_packs, purchase_uom)} + "
                        f"{amount_phrase(loose_quantity, uom)}"
                    )
            safe_lines.append(safe_line)
        if not safe_lines:
            continue
        try:
            observed_at = get_datetime(row.get("observed_at")) if row.get("observed_at") else None
        except (TypeError, ValueError, OverflowError):
            observed_at = None
        reviews.append({
            "observation_id": cstr(row.get("observation_id")),
            "task_id": cstr(row.get("task_id")),
            "task_revision": row.get("task_revision"),
            "observed_at": _iso_with_offset(observed_at) if observed_at else None,
            "age_minutes": _age_minutes(observed_at, now),
            "variance_percent": cstr(row.get("variance_percent")) or None,
            "threshold_reason": "The counted amount is very different from the saved amount, so a director must check it.",
            "lines": safe_lines,
        })
    return {"status": "ok", "open": len(reviews), "items": reviews}


def _health_plan_summary(warehouse: str, *, now: Any) -> dict[str, Any]:
    if not frappe.db.exists("DocType", "FB Inventory Plan"):
        return {"status": "not_installed", "open": 0, "by_status": {}, "oldest_age_minutes": None}
    try:
        rows = frappe.get_all(
            "FB Inventory Plan",
            filters={"warehouse": warehouse, "status": ["in", sorted(OPEN_PLAN_STATUSES)]},
            fields=["name", "status", "modified"],
            order_by="modified asc",
            limit_page_length=100,
        )
    except Exception as error:
        log_sanitized_error("Inventory health plan summary failed", error)
        return {"status": "unavailable", "open": 0, "by_status": {}, "oldest_age_minutes": None}
    by_status: dict[str, int] = {}
    timestamps = []
    for row in rows or []:
        status = cstr(row.get("status")).strip() or "Unknown"
        by_status[status] = by_status.get(status, 0) + 1
        if row.get("modified"):
            timestamps.append(get_datetime(row.get("modified")))
    oldest = min(timestamps, key=_datetime_sort_key, default=None)
    return {
        "status": "ok",
        "open": len(rows or []),
        "by_status": by_status,
        "oldest_age_minutes": _age_minutes(oldest, now),
    }


def _runtime_artifact_identity() -> dict[str, Any]:
    """Return the exact installed connector identity used by rollout preflight.

    Statuses are deliberately narrow:

    - ``verified``   the computed hash matches the SHA-256 this site was told to
                     expect (``kopos_expected_runtime_inventory_sha256`` in site
                     config, written by the rollout that installed the wheel).
    - ``present``    the package imported and hashed, but no expected SHA is
                     recorded, so nothing has been verified against release
                     evidence. A dirty checkout looks exactly like this.
    - ``unmatched``  the installed runtime is not the artifact this site expects.
    - ``unavailable`` the identity could not be computed at all.

    Reporting ``verified`` for a successful import alone painted Settings green
    for any install, including one that never matched an accepted release.
    """

    try:
        from importlib import metadata

        from kopos_connector.acceptance.target_preflight_machine import _runtime_inventory_sha256

        try:
            version = metadata.version("kopos_connector")
        except metadata.PackageNotFoundError:
            version = None
        runtime_sha256 = _runtime_inventory_sha256()
        expected = cstr(
            (frappe.conf or {}).get("kopos_expected_runtime_inventory_sha256")
        ).strip().lower()
        if not expected:
            status = "present"
        elif expected == cstr(runtime_sha256).strip().lower():
            status = "verified"
        else:
            status = "unmatched"
        return {
            "status": status,
            "package": "kopos_connector",
            "version": version,
            "runtime_inventory_sha256": runtime_sha256,
            "expected_runtime_inventory_sha256": expected or None,
        }
    except Exception as error:
        log_sanitized_error("Inventory health runtime artifact identity failed", error)
        return {
            "status": "unavailable",
            "package": "kopos_connector",
            "version": None,
            "runtime_inventory_sha256": None,
            "expected_runtime_inventory_sha256": None,
        }


def _scheduler_health() -> dict[str, Any]:
    cache = frappe.cache()
    getter = getattr(cache, "get_value", None)
    values: dict[str, Any] = {}
    for marker in ("last_start", "last_success", "last_failure"):
        raw = getter(f"kopos:inventory-autopilot:scheduler:{marker}") if callable(getter) else None
        parsed = get_datetime(raw) if raw else None
        values[marker] = _iso_with_offset(parsed) if parsed else None
    last_success = get_datetime(values["last_success"]) if values.get("last_success") else None
    next_deadline = _next_success_deadline(last_success)
    return {
        "expected": "every 5 minutes",
        "expected_interval_minutes": INVENTORY_RECOVER_INTERVAL_MINUTES,
        "grace_minutes": INVENTORY_SCHEDULER_WARNING_MINUTES - INVENTORY_RECOVER_INTERVAL_MINUTES,
        **values,
        "next_success_deadline": _iso_with_offset(next_deadline) if next_deadline else None,
    }


def _device_overlay_current(row: dict[str, Any]) -> bool:
    """Compare a device acknowledgement with the cached generated overlay.

    Rebuilding the full catalog here made the manager health route depend on
    every catalog query and could keep a monitor request loading indefinitely.
    Catalog generation publishes this identity to Redis; missing or expired
    cache data fails closed as unacknowledged and never blocks health polling.
    """

    return device_overlay_is_current(
        device_name=cstr(row.get("name")),
        acknowledged_version=cstr(row.get("inventory_overlay_version")),
        acknowledged_hash=cstr(row.get("inventory_overlay_hash")),
        acknowledged_catalog_version=cstr(row.get("inventory_catalog_version")),
    )


def _device_overlay_age(row: dict[str, Any], *, max_age_minutes: int, now: Any) -> int | None:
    """Estimate generated-overlay age from the cached validity contract.

    The catalog cache stores ``valid_until`` rather than a second mutable
    generated timestamp.  Deriving the timestamp from the same policy age
    lets health make the required 30-minute acknowledgement decision without
    introducing another authority.
    """

    try:
        getter = getattr(frappe.cache(), "get_value", None)
        if not callable(getter):
            return None
        raw_identity = getter(f"kopos:inventory-autopilot:overlay:{cstr(row.get('name')).strip()}")
        if isinstance(raw_identity, bytes):
            raw_identity = raw_identity.decode("utf-8")
        identity = json.loads(raw_identity) if isinstance(raw_identity, str) else raw_identity
        if not isinstance(identity, dict) or not identity.get("valid_until"):
            return None
        valid_until = get_datetime(identity.get("valid_until"))
        generated_at = valid_until - timedelta(minutes=max(1, int(max_age_minutes)))
        return _age_minutes(generated_at, now)
    except Exception:
        return None


def _health_marker_key(marker: str, warehouse: str | None = None) -> str:
    suffix = ""
    resolved_warehouse = cstr(warehouse).strip()
    if resolved_warehouse:
        suffix = ":" + hashlib.sha256(resolved_warehouse.encode("utf-8")).hexdigest()[:24]
    return f"kopos:inventory-autopilot:health:{marker}{suffix}"


def _set_health_marker(marker: str, warehouse: str | None = None) -> None:
    cache = frappe.cache()
    setter = getattr(cache, "set_value", None)
    if callable(setter):
        setter(_health_marker_key(marker, warehouse), _iso_with_offset(now_datetime()), expires_in_sec=7 * 24 * 60 * 60)


def _health_marker(marker: str, warehouse: str | None = None) -> str | None:
    cache = frappe.cache()
    getter = getattr(cache, "get_value", None)
    if not callable(getter):
        return None
    value = getter(_health_marker_key(marker, warehouse))
    return cstr(value).strip() or None


def _iso_with_offset(value: Any) -> str | None:
    if value is None:
        return None
    current = value
    if getattr(current, "tzinfo", None) is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
    return current.isoformat()


def _site_timezone() -> ZoneInfo:
    """Return the site timezone, failing back to UTC rather than guessing an outlet."""

    name = cstr(getattr(frappe.utils, "get_system_timezone", lambda: "")()).strip()
    if not name:
        getter = getattr(frappe.db, "get_single_value", None)
        name = cstr(getter("System Settings", "time_zone") if callable(getter) else "").strip()
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _site_local_naive(value: datetime | None) -> datetime | None:
    """Convert an offset-aware wire timestamp for a Frappe Datetime column.

    Frappe stores Datetime as a site-local **naive** MariaDB DATETIME. Handing
    MariaDB the offset-aware ISO string the tablet sends
    (``2026-08-20T05:23:24.910Z``) raises error 1292 and the whole write is lost,
    so every wire timestamp must be converted before it reaches a Datetime field.
    ``fake_frappe`` does not enforce column types, which is why only a real
    MariaDB surfaces this.
    """

    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(_site_timezone()).replace(tzinfo=None)


def _require_explicit_offset_timestamp(value: Any, label: str) -> datetime:
    """Parse a wire timestamp without guessing the sender's timezone."""

    try:
        parsed = datetime.fromisoformat(cstr(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone offset is required")
    except (TypeError, ValueError):
        frappe.throw(
            _("{0} is invalid or missing an explicit UTC offset").format(label),
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise")
    return parsed
