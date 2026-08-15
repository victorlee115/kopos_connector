"""Hourly, fail-closed forecast and replenishment planning.

This module is intentionally a thin scheduler adapter around the pure forecast
and replenishment functions.  It reads only post-cutover resolved component
snapshots, writes one explainable ``FB Inventory Plan`` per policy/day, and
delegates document creation to the existing coordinator.  Missing operational
evidence blocks automation; it never becomes a guessed default.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import cstr, get_datetime, now_datetime

from kopos_connector.kopos.services.inventory_autopilot.document_coordinator import (
    create_and_submit_material_request,
    create_eligible_draft_purchase_order,
    has_open_material_request_intent,
    persist_inventory_plan,
)
from kopos_connector.kopos.services.inventory_autopilot.automation_identity import (
    automation_identity_is_configured,
)
from kopos_connector.kopos.services.inventory_autopilot.exceptions import (
    resolve_inventory_exception,
    upsert_inventory_exception,
)
from kopos_connector.kopos.services.inventory_autopilot.forecast import (
    ForecastResult,
    evaluate_forecast,
)
from kopos_connector.kopos.services.inventory_autopilot.overlay import device_overlay_is_current
from kopos_connector.kopos.services.inventory_autopilot.replenishment import (
    ReplenishmentInput,
    ReplenishmentLine,
    build_replenishment_plan,
    shelf_life_allows_replenishment,
)


BUSINESS_TIMEZONE = ZoneInfo("Asia/Kuala_Lumpur")
PLANNING_MARKER_KEY = "kopos:inventory-autopilot:health:last_plan"
_MISSING = object()


def generate_inventory_plans() -> list[dict[str, Any]]:
    """Generate one idempotent plan for every configured outlet policy.

    Scheduler boundaries must not abort the other outlets.  A malformed policy
    therefore creates one actionable exception and the next policy still runs.
    """

    if not frappe.db.exists("DocType", "FB Inventory Policy"):
        return []
    policies = frappe.get_all(
        "FB Inventory Policy",
        filters={"automation_state": ["in", ["Review First", "Active"]]},
        fields=[
            "name",
            "company",
            "warehouse",
            "transit_warehouse",
            "automation_state",
            "inventory_contract_version",
            "cutover_token",
            "cutover_at",
            "permitted_actions",
            "quantity_ceiling",
            "value_ceiling",
            "max_source_age_minutes",
        ],
        limit_page_length=10_000,
    )
    results: list[dict[str, Any]] = []
    for policy in policies:
        try:
            results.append(_generate_for_policy(policy))
            _set_planning_marker(cstr(policy.get("warehouse")).strip())
        except Exception as error:
            exception = upsert_inventory_exception(
                reason_code="inventory_planning_failure",
                summary="Inventory planning could not produce a safe plan",
                next_action=f"Review the outlet planning inputs and scheduler error: {cstr(error)}",
                severity="Critical",
                company=cstr(policy.get("company")),
                warehouse=cstr(policy.get("warehouse")),
                source_doctype="FB Inventory Policy",
                source_name=cstr(policy.get("name")),
            )
            results.append({"status": "failed", "policy": cstr(policy.get("name")), "exception": exception})
    if results:
        frappe.db.commit()
    return results


def aggregate_consumption(
    rows: Iterable[dict[str, Any]],
    *,
    operating_days: Iterable[date] | None = None,
    tracked_items: Iterable[str] | None = None,
    timezone: ZoneInfo = BUSINESS_TIMEZONE,
) -> tuple[tuple[date, ...], dict[str, tuple[Decimal, ...]]]:
    """Turn resolved-component rows into dense open-day series.

    Production callers pass closed-shift days as explicit operating evidence.
    A real zero-demand open day is then retained as zero, while a day without
    an outlet shift is not invented. The optional fallback to observed days is
    kept for pure compatibility callers and never drives production planning.
    """

    totals: dict[tuple[date, str], Decimal] = defaultdict(lambda: Decimal("0"))
    explicit_days = set(operating_days) if operating_days is not None else None
    observed_days: set[date] = set()
    for row in rows:
        item = cstr(row.get("item")).strip()
        if not item or not _truthy(row.get("affects_stock")):
            continue
        quantity = _decimal(row.get("stock_qty"), allow_zero=False)
        if quantity is None:
            continue
        observed = row.get("observed_at") or row.get("creation")
        if not observed:
            continue
        observed_at = get_datetime(observed)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone)
        local_day = observed_at.astimezone(timezone).date()
        if explicit_days is not None and local_day not in explicit_days:
            continue
        totals[(local_day, item)] += quantity
        observed_days.add(local_day)
    ordered_days = tuple(sorted(explicit_days if explicit_days is not None else observed_days))
    item_names = tuple(sorted(
        {cstr(item).strip() for item in (tracked_items or ()) if cstr(item).strip()}
        | {item for _, item in totals}
    ))
    series = {
        item: tuple(totals.get((operating_day, item), Decimal("0")) for operating_day in ordered_days)
        for item in item_names
    }
    return ordered_days, series


def overall_forecast_state(results: Iterable[ForecastResult]) -> str:
    values = tuple(results)
    if not values or any(result.state == "Not ready" for result in values):
        return "Not ready"
    if any(result.state == "Please check" for result in values):
        return "Please check"
    return "Reliable"


def _generate_for_policy(policy: dict[str, Any]) -> dict[str, Any]:
    company = cstr(policy.get("company")).strip()
    warehouse = cstr(policy.get("warehouse")).strip()
    cutover_at = policy.get("cutover_at")
    token = cstr(policy.get("cutover_token")).strip()
    planning_day = _business_today()
    policy_hash = _hash({
        "company": company,
        "warehouse": warehouse,
        "transit_warehouse": cstr(policy.get("transit_warehouse")).strip(),
        "automation_state": cstr(policy.get("automation_state")),
        "inventory_contract_version": cstr(policy.get("inventory_contract_version")),
        "cutover_token": token,
        "cutover_at": cstr(cutover_at),
        "permitted_actions": cstr(policy.get("permitted_actions")),
        "quantity_ceiling": cstr(policy.get("quantity_ceiling")),
        "value_ceiling": cstr(policy.get("value_ceiling")),
        "max_source_age_minutes": cstr(policy.get("max_source_age_minutes")),
    })
    if not company or not warehouse or not token or not cutover_at:
        return _persist_blocked_plan(
            policy=policy,
            planning_day=planning_day,
            policy_hash=policy_hash,
            input_hash=_hash({"reason": "cutover_identity_missing"}),
            forecast_state="Not ready",
            gates=_base_gates(policy, warehouse, max_source_age=30),
            lines=(),
            reason="cutover_identity_missing",
        )

    rows = _resolved_component_rows(warehouse=warehouse, cutover_at=cutover_at)
    operating_days = _valid_operating_days(
        warehouse=warehouse,
        cutover_at=cutover_at,
        before_day=planning_day,
    )
    tracked_items = _tracked_component_items(company=company)
    if not operating_days:
        return _persist_blocked_plan(
            policy=policy,
            planning_day=planning_day,
            policy_hash=policy_hash,
            input_hash=_hash({"reason": "operating_day_evidence_missing"}),
            forecast_state="Not ready",
            gates=_base_gates(policy, warehouse, max_source_age=int(policy.get("max_source_age_minutes") or 30)),
            lines=(),
            reason="operating_day_evidence_missing",
        )
    if not tracked_items:
        return _persist_blocked_plan(
            policy=policy,
            planning_day=planning_day,
            policy_hash=policy_hash,
            input_hash=_hash({"reason": "recipe_component_coverage_missing"}),
            forecast_state="Not ready",
            gates=_base_gates(policy, warehouse, max_source_age=int(policy.get("max_source_age_minutes") or 30)),
            lines=(),
            reason="recipe_component_coverage_missing",
        )
    operating_days, series = aggregate_consumption(
        rows,
        operating_days=operating_days,
        tracked_items=tracked_items,
    )
    item_data: list[dict[str, Any]] = []
    forecast_results: list[ForecastResult] = []
    all_recipe_uom = bool(series)
    all_source_current = bool(series)
    all_shelf_life_safe = True
    replenishment_inputs: list[ReplenishmentInput] = []
    proposed_actions: list[dict[str, Any]] = []
    for item, actuals in series.items():
        config = _item_configuration(
            item=item,
            warehouse=warehouse,
            company=company,
            cutover_at=cutover_at,
            max_source_age=int(policy.get("max_source_age_minutes") or 30),
        )
        result = evaluate_forecast(
            actuals,
            operating_days=[True] * len(operating_days),
            operating_dates=operating_days,
            forecast_date=planning_day + timedelta(days=1),
            safety_stock=config.get("safety_stock"),
            shelf_life_days=config.get("shelf_life_days"),
        )
        # A shelf-life cap is derived only after the measured forecast exists;
        # no sell-through rate is guessed when history is insufficient.
        if result.forecast is not None and config.get("shelf_life_days"):
            shelf_life_cap = result.forecast * Decimal(str(config["shelf_life_days"]))
            result = evaluate_forecast(
                actuals,
                operating_days=[True] * len(operating_days),
                operating_dates=operating_days,
                forecast_date=planning_day + timedelta(days=1),
                safety_stock=config.get("safety_stock"),
                shelf_life_days=config.get("shelf_life_days"),
                shelf_life_cap=shelf_life_cap,
            )
            config["shelf_life_cap"] = shelf_life_cap
        forecast_results.append(result)
        recipe_uom_complete = bool(config.get("recipe_uom_complete"))
        source_current = bool(config.get("source_current"))
        all_recipe_uom = all_recipe_uom and recipe_uom_complete
        all_source_current = all_source_current and source_current
        if "shelf_life_cap_below_measured_uncertainty" in result.reasons:
            all_shelf_life_safe = False
        forecast_quantity = result.forecast
        item_data.append({
            "item": item,
            "action": cstr(config.get("replenishment_action") or "Purchase"),
            "source_warehouse": cstr(config.get("source_warehouse")).strip() or None,
            "actual_days": len(actuals),
            "forecast": str(forecast_quantity) if forecast_quantity is not None else None,
            "forecast_state": result.state,
            "algorithm_version": result.algorithm_version,
            "model": result.selected_model,
            "training_days": result.training_days,
            "test_days": result.test_days,
            "valid_operating_days": result.valid_operating_days,
            "mae": str(result.mae) if result.mae is not None else None,
            "wape": str(result.wape) if result.wape is not None else None,
            "signed_bias": str(result.signed_bias) if result.signed_bias is not None else None,
            "positive_underforecast_p90": (
                str(result.positive_underforecast_p90)
                if result.positive_underforecast_p90 is not None
                else None
            ),
            "reasons": result.reasons,
            "explanation": result.explanation,
            "config": config,
        })
        if forecast_quantity is None:
            continue
        lead_days = config.get("lead_time_days")
        action = cstr(config.get("replenishment_action") or "Purchase")
        minimum_horizon = (
            Decimal("1") / Decimal("24")
            if action == "Manufacture"
            else Decimal("1")
        )
        safety_stock = config.get("safety_stock") or Decimal("0")
        replenishment_inputs.append(
            ReplenishmentInput(
                item=item,
                warehouse=warehouse,
                current_stock=config["current_stock"],
                reservations=config["reservations"],
                unposted_consumption=config["unposted_consumption"],
                open_supply=config["open_supply"],
                forecast_through_lead_time=forecast_quantity * max(minimum_horizon, lead_days or Decimal("0")),
                safety_stock=safety_stock,
                supplier_pack=(Decimal("0") if action == "Transfer" else config["supplier_pack"]),
                supplier_minimum=(Decimal("0") if action == "Transfer" else config["supplier_minimum"]),
                shelf_life_cap=config.get("shelf_life_cap"),
            )
        )

    forecast_state = overall_forecast_state(forecast_results)
    base_gates = _base_gates(policy, warehouse, max_source_age=int(policy.get("max_source_age_minutes") or 30))
    gates = {
        **base_gates,
        "recipe_uom_complete": all_recipe_uom,
        "forecast_reliable": forecast_state == "Reliable",
        "source_current": all_source_current,
        "shelf_life_cap": all_shelf_life_safe,
    }
    if not all(shelf_life_allows_replenishment(value) for value in replenishment_inputs):
        all_shelf_life_safe = False
        gates["shelf_life_cap"] = False
    item_data_by_item = {
        cstr(row.get("item")).strip(): row
        for row in item_data
        if cstr(row.get("item")).strip()
    }
    raw_lines = build_replenishment_plan(replenishment_inputs)
    for line in raw_lines:
        config = item_data_by_item.get(line.item, {}).get("config") or {}
        if cstr(item_data_by_item.get(line.item, {}).get("action")) == "Transfer":
            source_available = _decimal(config.get("source_available"))
            if source_available is None or source_available < line.quantity:
                all_source_current = False
                gates["source_current"] = False
    quantity_ceiling = _decimal(policy.get("quantity_ceiling"))
    total_quantity = sum((line.quantity for line in raw_lines), Decimal("0"))
    allowed_actions = _permitted_actions(policy.get("permitted_actions"))
    proposed_action_names = {
        cstr(item_data_by_item.get(line.item, {}).get("action") or "Purchase")
        for line in raw_lines
    }
    actions_permitted = bool(allowed_actions) and proposed_action_names.issubset(allowed_actions)
    lines = tuple(
        _planned_document_line(
            line=line,
            item_data=item_data_by_item.get(line.item, {}),
        )
        for line in raw_lines
    )
    request_lines_by_action = {
        action: tuple(
            ReplenishmentLine(
                line["item"],
                line["warehouse"],
                Decimal(line["quantity"]),
                line["reason"],
                cstr(line.get("source_warehouse")).strip() or None,
                cstr(line.get("uom")).strip() or None,
                cstr(line.get("stock_uom")).strip() or None,
                _decimal(line.get("conversion_factor_decimal")),
                _decimal(line.get("stock_quantity_decimal")),
            )
            for line in lines
            if cstr(line.get("action")) == action
        )
        for action in ("Purchase", "Manufacture", "Transfer")
    }
    request_lines = tuple(line for group in request_lines_by_action.values() for line in group)
    value_ceiling = _decimal(policy.get("value_ceiling"))
    valuation_lines = tuple(
        ReplenishmentLine(
            cstr(line.get("item")),
            cstr(line.get("warehouse")),
            _decimal(line.get("stock_quantity_decimal")) or Decimal("0"),
            cstr(line.get("reason")),
        )
        for line in lines
    )
    estimated_value = _estimate_replenishment_value(valuation_lines)
    gates.update(
        automation_ceiling_gates(
            quantity_ceiling=quantity_ceiling,
            value_ceiling=value_ceiling,
            proposed_quantity=total_quantity,
            proposed_value=estimated_value,
        )
    )
    if not actions_permitted:
        # Keep the complete proposal visible, but block every unattended
        # action when any line is outside the director-approved policy.
        gates["quantity_ceiling"] = False
    if request_lines:
        gates["intent_not_open"] = all(
            not has_open_material_request_intent(
                company=company,
                purpose=action,
                required_date=planning_day + timedelta(days=1),
                lines=action_lines,
            )
            for action, action_lines in request_lines_by_action.items()
            if action_lines
        )
    input_hash = _hash({
        "planning_day": planning_day.isoformat(),
        "operating_days": [value.isoformat() for value in operating_days],
        "items": item_data,
        "proposed_actions": lines,
        "estimated_value": str(estimated_value) if estimated_value is not None else None,
    })
    forecast_evidence = build_forecast_evidence(
        operating_days=operating_days,
        item_data=item_data,
    )
    result = persist_inventory_plan(
        company=company,
        warehouse=warehouse,
        planning_date=planning_day,
        input_hash=input_hash,
        policy_hash=policy_hash,
        forecast_state=forecast_state,
        forecast_evidence=forecast_evidence,
        gates=gates,
        lines=lines,
    )
    created_documents: list[dict[str, Any]] = []
    failed_gates = tuple(sorted(name for name, passed in gates.items() if passed is not True))
    exception_identity = {
        "reason_code": "inventory_plan_gate_failed",
        "company": company,
        "warehouse": warehouse,
        "source_doctype": "FB Inventory Policy",
        "source_name": cstr(policy.get("name")),
    }
    if failed_gates:
        upsert_inventory_exception(
            **exception_identity,
            summary="Inventory planning did not create work because a safety check needs attention",
            next_action="Review: " + ", ".join(failed_gates),
            severity="Warning",
        )
    else:
        resolve_inventory_exception(**exception_identity)
    if result.get("status") in {"created", "duplicate"} and not failed_gates and lines:
        for action, action_lines in request_lines_by_action.items():
            if not action_lines:
                continue
            mr_result = create_and_submit_material_request(
                company=company,
                purpose=action,
                required_date=planning_day + timedelta(days=1),
                lines=action_lines,
                plan_hash=input_hash,
                policy_hash=policy_hash,
                transit_warehouse=(
                    cstr(policy.get("transit_warehouse")).strip()
                    if action == "Transfer"
                    else None
                ),
            )
            created_documents.append(mr_result)
            material_request = cstr(mr_result.get("material_request")).strip()
            if (
                action == "Purchase"
                and material_request
                and "Draft Purchase Order" in allowed_actions
                and mr_result.get("status") in {"created", "duplicate"}
            ):
                created_documents.append(create_eligible_draft_purchase_order(
                    company=company,
                    material_request=material_request,
                    plan_hash=input_hash,
                    policy_hash=policy_hash,
                ))
    return {
        "status": result.get("status"),
        "policy": cstr(policy.get("name")),
        "plan": result.get("plan"),
        "forecast_state": forecast_state,
        "operating_days": len(operating_days),
        "gates": gates,
        "created_documents": created_documents,
    }


def _persist_blocked_plan(
    *, policy: dict[str, Any], planning_day: date, policy_hash: str, input_hash: str,
    forecast_state: str, gates: dict[str, bool], lines: Iterable[dict[str, Any]], reason: str,
) -> dict[str, Any]:
    gates = {**gates, "recipe_uom_complete": False, "forecast_reliable": False, "source_current": False}
    result = persist_inventory_plan(
        company=cstr(policy.get("company")),
        warehouse=cstr(policy.get("warehouse")),
        planning_date=planning_day,
        input_hash=input_hash,
        policy_hash=policy_hash,
        forecast_state=forecast_state,
        forecast_evidence={
            "algorithm_version": "inventory-autopilot-forecast-v1",
            "valid_operating_days": 0,
            "data_window": {"first": None, "last": None},
            "items": [],
            "reasons": [reason],
            "explanation": "Required post-cutover planning evidence is not yet complete",
        },
        gates=gates,
        lines=lines,
    )
    upsert_inventory_exception(
        reason_code=reason,
        summary="Inventory planning is waiting for required outlet setup",
        next_action="Complete the cutover, recipe, stock, and supplier setup checklist before activating automation",
        severity="Warning",
        company=cstr(policy.get("company")),
        warehouse=cstr(policy.get("warehouse")),
        source_doctype="FB Inventory Policy",
        source_name=cstr(policy.get("name")),
    )
    return {"status": result.get("status"), "policy": cstr(policy.get("name")), "plan": result.get("plan"), "forecast_state": forecast_state, "gates": gates}


def build_forecast_evidence(
    *, operating_days: Iterable[date], item_data: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Return the readable forecast proof persisted with one plan snapshot."""

    days = tuple(operating_days)
    items = []
    for row in item_data:
        items.append({
            key: row.get(key)
            for key in (
                "item",
                "algorithm_version",
                "model",
                "forecast_state",
                "forecast",
                "training_days",
                "test_days",
                "valid_operating_days",
                "mae",
                "wape",
                "signed_bias",
                "positive_underforecast_p90",
                "reasons",
                "explanation",
            )
        })
    versions = sorted(
        {
            cstr(row.get("algorithm_version")).strip()
            for row in items
            if cstr(row.get("algorithm_version")).strip()
        }
    )
    return {
        "algorithm_version": versions[0] if len(versions) == 1 else versions,
        "horizon": "supplier lead time through the next planning run",
        "data_window": {
            "first": days[0].isoformat() if days else None,
            "last": days[-1].isoformat() if days else None,
        },
        "valid_operating_days": len(days),
        "items": items,
        "explanation": (
            "Forecasts use only earlier post-cutover resolved ingredient consumption; "
            "each Item records model selection and rolling-origin error arithmetic."
        ),
    }


def _resolved_component_rows(*, warehouse: str, cutover_at: Any) -> list[dict[str, Any]]:
    if not frappe.db.exists("DocType", "FB Resolved Sale") or not frappe.db.exists("DocType", "FB Resolved Component"):
        return []
    try:
        quantity_expression = _resolved_component_quantity_expression("rc")
        return frappe.db.sql(
            f"""
            SELECT rs.creation AS observed_at, rc.item, {quantity_expression} AS stock_qty, rc.affects_stock
            FROM `tabFB Resolved Sale` rs
            INNER JOIN `tabFB Resolved Component` rc ON rc.parent = rs.name
            INNER JOIN `tabItem` i ON i.name = rc.item
            WHERE rs.booth_warehouse = %s
              AND rs.creation >= %s
              AND i.is_stock_item = 1
              AND rc.affects_stock = 1
              AND {quantity_expression} > 0
            """,
            (warehouse, cutover_at),
            as_dict=True,
        ) or []
    except Exception as error:
        upsert_inventory_exception(
            reason_code="inventory_history_unavailable",
            summary="Post-cutover ingredient history could not be read",
            next_action=f"Review the resolved-sale schema and scheduler error: {cstr(error)}",
            severity="Critical",
            warehouse=warehouse,
        )
        return []


def _valid_operating_days(*, warehouse: str, cutover_at: Any, before_day: date) -> tuple[date, ...]:
    """Use completed outlet shifts as evidence that a calendar day operated."""

    if not warehouse or not cutover_at or not frappe.db.exists("DocType", "FB Shift"):
        return ()
    rows = frappe.get_all(
        "FB Shift",
        filters={
            "warehouse": warehouse,
            "status": "Closed",
            "opened_at": [">=", cutover_at],
        },
        fields=["opened_at"],
        limit_page_length=10_000,
    )
    days: set[date] = set()
    for row in rows:
        opened_at = row.get("opened_at")
        if not opened_at:
            continue
        observed = get_datetime(opened_at)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=BUSINESS_TIMEZONE)
        local_day = observed.astimezone(BUSINESS_TIMEZONE).date()
        if local_day < before_day:
            days.add(local_day)
    return tuple(sorted(days))


def _tracked_component_items(*, company: str) -> tuple[str, ...]:
    """Return current recipe components while retaining historical demand rows."""

    if not company or not frappe.db.exists("DocType", "FB Recipe") or not frappe.db.exists("DocType", "FB Recipe Component"):
        return ()
    rows = frappe.db.sql(
        """
        SELECT DISTINCT component.item
        FROM `tabFB Recipe` recipe
        INNER JOIN `tabFB Recipe Component` component ON component.parent = recipe.name
        INNER JOIN `tabItem` item ON item.name = component.item
        WHERE recipe.company = %s
          AND recipe.status = 'Active'
          AND component.affects_stock = 1
          AND item.is_stock_item = 1
        ORDER BY component.item ASC
        """,
        (company,),
        as_dict=True,
    ) or []
    return tuple(cstr(row.get("item")).strip() for row in rows if cstr(row.get("item")).strip())


def _item_configuration(
    *,
    item: str,
    warehouse: str,
    company: str,
    cutover_at: Any,
    max_source_age: int,
) -> dict[str, Any]:
    meta = frappe.get_meta("Item")
    fields = [
        "name",
        "stock_uom",
        "purchase_uom",
        "min_order_qty",
        "lead_time_days",
        "is_stock_item",
    ]
    if meta.has_field("custom_fb_item_role"):
        fields.append("custom_fb_item_role")
    for fieldname in ("shelf_life_in_days", "custom_kopos_shelf_life_days"):
        if meta.has_field(fieldname):
            fields.append(fieldname)
    values = frappe.db.get_value("Item", item, fields, as_dict=True) or {}
    stock_uom = cstr(values.get("stock_uom")).strip()
    is_stock_item = _truthy(values.get("is_stock_item"))
    shelf_life_days = _decimal(values.get("custom_kopos_shelf_life_days") or values.get("shelf_life_in_days"))
    bin_meta = frappe.get_meta("Bin") if frappe.db.exists("DocType", "Bin") else None
    bin_fields = [field for field in ("actual_qty", "reserved_qty", "ordered_qty", "indented_qty") if bin_meta and bin_meta.has_field(field)]
    bin_values = frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, bin_fields, as_dict=True) if bin_fields else None
    bin_values = bin_values or {}
    item_role = cstr(values.get("custom_fb_item_role")).strip()
    prepared = _prepared_component_configuration(item=item, company=company) if item_role == "Prep Item" else None
    route = _replenishment_route(item=item, warehouse=warehouse)
    action = "Manufacture" if prepared else cstr(route.get("action") or "Purchase")
    supplier = prepared or (
        _transfer_source_configuration(
            item=item,
            source_warehouse=cstr(route.get("source_warehouse")).strip(),
            destination_warehouse=warehouse,
            company=company,
            max_source_age=max_source_age,
        )
        if action == "Transfer"
        else _supplier_configuration(item, item_values=values)
    )
    unposted_consumption = _unposted_resolved_consumption(
        item=item,
        warehouse=warehouse,
        cutover_at=cutover_at,
    )
    current_stock = _decimal(bin_values.get("actual_qty")) or Decimal("0")
    reservations = _decimal(bin_values.get("reserved_qty")) or Decimal("0")
    open_supply = (_decimal(bin_values.get("ordered_qty")) or Decimal("0")) + (_decimal(bin_values.get("indented_qty")) or Decimal("0"))
    source_current = (
        bool(bin_fields)
        and bool(route.get("route_current"))
        and supplier["source_current"]
        and unposted_consumption is not None
    )
    shelf_life_cap = None
    safety_stock = _configured_safety_stock(item=item, warehouse=warehouse)
    return {
        "recipe_uom_complete": is_stock_item and bool(stock_uom),
        "source_current": source_current,
        "current_stock": current_stock,
        "reservations": reservations,
        "open_supply": open_supply,
        "unposted_consumption": unposted_consumption or Decimal("0"),
        "lead_time_days": supplier["lead_time_days"],
        "supplier_pack": supplier["supplier_pack"],
        "supplier_minimum": supplier["supplier_minimum"],
        "purchase_uom": cstr(values.get("purchase_uom")).strip() or None,
        "purchase_conversion_factor": (
            supplier.get("purchase_conversion_factor") if action == "Purchase" else Decimal("1")
        ),
        "shelf_life_days": int(shelf_life_days) if shelf_life_days is not None else None,
        "shelf_life_cap": shelf_life_cap,
        "safety_stock": safety_stock,
        "stock_uom": stock_uom,
        "replenishment_action": action,
        "source_warehouse": cstr(route.get("source_warehouse")).strip() or None,
        "source_available": supplier.get("source_available"),
    }


def _configured_safety_stock(*, item: str, warehouse: str) -> Decimal | None:
    """Read the standard ERPNext warehouse reorder level as safety stock."""

    if not frappe.db.exists("DocType", "Item Reorder"):
        return None
    rows = frappe.get_all(
        "Item Reorder",
        filters={"parent": item, "warehouse": warehouse},
        fields=["warehouse_reorder_level"],
        limit_page_length=2,
    )
    if len(rows) != 1:
        return None
    value = _decimal(rows[0].get("warehouse_reorder_level"))
    return value if value is not None and value >= 0 else None


def _unposted_resolved_consumption(*, item: str, warehouse: str, cutover_at: Any) -> Decimal | None:
    """Include frozen ingredient consumption not yet posted to Stock Entry.

    It is deliberately read from ``FB Resolved Sale`` rather than current
    recipes so a delayed projection continues to reserve the exact historical
    component vector.  A read failure blocks purchasing instead of assuming
    the unresolved demand is zero.
    """

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
            SELECT COALESCE(SUM({quantity_expression}), 0) AS quantity
            FROM `tabFB Resolved Sale` rs
            INNER JOIN `tabFB Resolved Component` rc ON rc.parent = rs.name
            WHERE rs.booth_warehouse = %s
              AND rs.creation >= %s
              AND rs.stock_entry_issue IS NULL
              AND rc.item = %s
              AND rc.affects_stock = 1
              AND {quantity_expression} > 0
            """,
            (warehouse, cutover_at, item),
            as_dict=True,
        ) or []
    except Exception:
        return None
    return _decimal(rows[0].get("quantity") if rows else "0")


def _supplier_configuration(
    item: str,
    *,
    item_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not frappe.db.exists("DocType", "Item Supplier"):
        return {
            "source_current": False,
            "lead_time_days": None,
            "supplier_pack": Decimal("0"),
            "supplier_minimum": Decimal("0"),
            "purchase_conversion_factor": None,
        }
    rows = frappe.get_all(
        "Item Supplier",
        filters={"parent": item, "parenttype": "Item"},
        fields=["supplier"],
        limit_page_length=2,
    )
    values = item_values or frappe.db.get_value(
        "Item",
        item,
        ["stock_uom", "purchase_uom", "min_order_qty", "lead_time_days"],
        as_dict=True,
    ) or {}
    stock_uom = cstr(values.get("stock_uom")).strip()
    purchase_uom = cstr(values.get("purchase_uom")).strip()
    pack = _purchase_uom_conversion(
        item=item,
        stock_uom=stock_uom,
        purchase_uom=purchase_uom,
    )
    minimum = _decimal(values.get("min_order_qty"))
    lead = _decimal(values.get("lead_time_days"))
    return {
        "source_current": bool(rows and rows[0].get("supplier"))
        and pack is not None
        and pack > 0
        and minimum is not None
        and minimum >= 0
        and lead is not None
        and lead >= 0,
        "lead_time_days": lead,
        "supplier_pack": pack or Decimal("0"),
        "supplier_minimum": minimum or Decimal("0"),
        "purchase_conversion_factor": pack,
    }


def _purchase_uom_conversion(
    *,
    item: str,
    stock_uom: str,
    purchase_uom: str,
) -> Decimal | None:
    """Return the one standard purchase-pack conversion in stock UOM."""

    if not item or not stock_uom or not purchase_uom:
        return None
    if purchase_uom == stock_uom:
        return Decimal("1")
    if not frappe.db.exists("DocType", "UOM Conversion Detail"):
        return None
    rows = frappe.get_all(
        "UOM Conversion Detail",
        filters={
            "parent": item,
            "parenttype": "Item",
            "uom": purchase_uom,
        },
        fields=["conversion_factor"],
        limit_page_length=2,
    )
    if len(rows) != 1:
        return None
    factor = _decimal(rows[0].get("conversion_factor"))
    return factor if factor is not None and factor > 0 else None


def _replenishment_route(*, item: str, warehouse: str) -> dict[str, Any]:
    """Use one explicit standard Item Reorder row; never infer a source."""

    if not frappe.db.exists("DocType", "Item Reorder"):
        return {"action": "Purchase", "source_warehouse": None, "route_current": True}
    meta = frappe.get_meta("Item Reorder")
    fields = ["name", "material_request_type"]
    if meta.has_field("custom_kopos_source_warehouse"):
        fields.append("custom_kopos_source_warehouse")
    rows = frappe.get_all(
        "Item Reorder",
        filters={"parent": item, "parenttype": "Item", "warehouse": warehouse},
        fields=fields,
        limit_page_length=2,
    )
    if not rows:
        return {"action": "Purchase", "source_warehouse": None, "route_current": True}
    if len(rows) != 1:
        return {"action": "Transfer", "source_warehouse": None, "route_current": False}
    action = cstr(rows[0].get("material_request_type") or "Purchase").strip().title()
    if action in {"Transfer", "Material Transfer"}:
        source = cstr(rows[0].get("custom_kopos_source_warehouse")).strip()
        return {"action": "Transfer", "source_warehouse": source or None, "route_current": bool(source)}
    return {
        "action": action if action in {"Purchase", "Manufacture"} else "Purchase",
        "source_warehouse": None,
        "route_current": True,
    }


def _transfer_source_configuration(
    *,
    item: str,
    source_warehouse: str,
    destination_warehouse: str,
    company: str,
    max_source_age: int,
) -> dict[str, Any]:
    """Fail closed unless the explicit source can cover the requested stock."""

    blocked = {
        "source_current": False,
        "lead_time_days": Decimal("0"),
        "supplier_pack": Decimal("0"),
        "supplier_minimum": Decimal("0"),
        "source_available": None,
    }
    if not source_warehouse or source_warehouse == destination_warehouse:
        return blocked
    source = frappe.db.get_value(
        "Warehouse",
        source_warehouse,
        ["company", "is_group", "disabled"],
        as_dict=True,
    ) or {}
    if (
        cstr(source.get("company")).strip() != company
        or _truthy(source.get("is_group"))
        or _truthy(source.get("disabled"))
    ):
        return blocked
    policy = frappe.db.get_value(
        "FB Inventory Policy",
        {"company": company, "warehouse": source_warehouse},
        ["cutover_at", "automation_state"],
        as_dict=True,
    ) or {}
    source_cutover = policy.get("cutover_at")
    if not source_cutover or cstr(policy.get("automation_state")).strip() not in {"Review First", "Active"}:
        return blocked
    bin_values = frappe.db.get_value(
        "Bin",
        {"item_code": item, "warehouse": source_warehouse},
        ["actual_qty", "reserved_qty"],
        as_dict=True,
    ) or {}
    actual = _decimal(bin_values.get("actual_qty"))
    reserved = _decimal(bin_values.get("reserved_qty"))
    unposted = _unposted_resolved_consumption(
        item=item,
        warehouse=source_warehouse,
        cutover_at=source_cutover,
    )
    safety_stock = _configured_safety_stock(item=item, warehouse=source_warehouse)
    if any(value is None for value in (actual, reserved, unposted, safety_stock)):
        return blocked
    available = actual - reserved - unposted - safety_stock
    current = (
        _devices_current(source_warehouse, max_source_age)
        and _no_unresolved_count(source_warehouse)
        and _projection_backlog_clear(source_warehouse)
    )
    return {
        **blocked,
        "source_current": current and available >= 0,
        "source_available": max(available, Decimal("0")),
    }


def _prepared_component_configuration(*, item: str, company: str) -> dict[str, Any] | None:
    """Use one active standard BOM as supply authority for a prepared Item.

    Prepared stock is made internally, not purchased.  The BOM's configured
    batch quantity supplies the same conservative rounding input that a
    supplier pack supplies for an Ingredient.  If the BOM is absent or
    inactive, planning blocks rather than silently treating the prepared Item
    as a purchasable ingredient.
    """

    if not frappe.db.exists("DocType", "BOM"):
        return {
            "source_current": False,
            "lead_time_days": None,
            "supplier_pack": Decimal("0"),
            "supplier_minimum": Decimal("0"),
        }
    bom_meta = frappe.get_meta("BOM")
    fields = ["name", "quantity"]
    if bom_meta.has_field("custom_kopos_batch_qty"):
        fields.append("custom_kopos_batch_qty")
    if bom_meta.has_field("custom_kopos_preparation_lead_minutes"):
        fields.append("custom_kopos_preparation_lead_minutes")
    rows = frappe.get_all(
        "BOM",
        filters={"item": item, "company": company, "docstatus": 1, "is_active": 1},
        fields=fields,
        order_by="modified desc",
        limit_page_length=2,
    )
    if len(rows) != 1:
        return {
            "source_current": False,
            "lead_time_days": None,
            "supplier_pack": Decimal("0"),
            "supplier_minimum": Decimal("0"),
        }
    bom = rows[0]
    batch = _decimal(bom.get("custom_kopos_batch_qty") or bom.get("quantity"))
    lead_minutes = _decimal(bom.get("custom_kopos_preparation_lead_minutes"))
    if batch is None or batch <= 0 or lead_minutes is None or lead_minutes < 0:
        return {
            "source_current": False,
            "lead_time_days": None,
            "supplier_pack": Decimal("0"),
            "supplier_minimum": Decimal("0"),
        }
    return {
        "source_current": True,
        "lead_time_days": lead_minutes / Decimal("1440"),
        "supplier_pack": batch,
        "supplier_minimum": Decimal("0"),
    }


def _base_gates(policy: dict[str, Any], warehouse: str, *, max_source_age: int) -> dict[str, bool]:
    return {
        "policy_active": cstr(policy.get("automation_state")).strip() == "Active",
        "automation_identity": automation_identity_is_configured(
            company=cstr(policy.get("company")).strip(),
            warehouse=warehouse,
        ),
        "input_hash_match": True,
        "no_unresolved_count": _no_unresolved_count(warehouse),
        "devices_current": _devices_current(warehouse, max_source_age),
        "projection_backlog_clear": _projection_backlog_clear(warehouse),
        "recipe_uom_complete": True,
        "forecast_reliable": False,
        "source_current": True,
        # A director must configure both ceilings before automation can act.
        # The plan builder turns these gates on only after validating positive
        # limits against the exact proposed quantity and ERP valuation.
        "quantity_ceiling": False,
        "value_ceiling": False,
        "shelf_life_cap": True,
        "intent_not_open": True,
    }


def automation_ceiling_gates(
    *,
    quantity_ceiling: Decimal | None,
    value_ceiling: Decimal | None,
    proposed_quantity: Decimal,
    proposed_value: Decimal | None,
) -> dict[str, bool]:
    """Fail closed until directors configure both positive action ceilings."""

    return {
        "quantity_ceiling": bool(
            quantity_ceiling is not None
            and quantity_ceiling > 0
            and proposed_quantity <= quantity_ceiling
        ),
        "value_ceiling": bool(
            value_ceiling is not None
            and value_ceiling > 0
            and proposed_value is not None
            and proposed_value <= value_ceiling
        ),
    }


def _no_unresolved_count(warehouse: str) -> bool:
    if not frappe.db.exists("DocType", "FB Inventory Count Task"):
        return True
    try:
        return not bool(frappe.get_all("FB Inventory Count Task", filters={"warehouse": warehouse, "status": ["in", ["Assigned", "Claimed", "In Progress", "Submitted", "Review"]]}, limit_page_length=1))
    except Exception:
        return False


def _devices_current(warehouse: str, max_age_minutes: int) -> bool:
    if not frappe.db.exists("DocType", "KoPOS Device"):
        return False
    try:
        rows = frappe.db.sql(
            """
            SELECT d.name, d.inventory_report_received_at, d.inventory_observed_at,
                   d.config_version, d.inventory_config_version,
                   d.inventory_catalog_version,
                   d.inventory_overlay_version, d.inventory_overlay_hash,
                   d.inventory_sales_pending, d.inventory_sales_syncing,
                   d.inventory_sales_failed, d.inventory_sales_dead_letter,
                   d.inventory_commands_pending, d.inventory_commands_syncing,
                   d.inventory_commands_failed, d.inventory_commands_dead_letter
            FROM `tabKoPOS Device` d
            INNER JOIN `tabPOS Profile` p ON p.name = d.pos_profile
            WHERE d.enabled = 1 AND p.warehouse = %s
            """,
            (warehouse,),
            as_dict=True,
        ) or []
    except Exception:
        return False
    if not rows:
        return False
    now = now_datetime()
    for row in rows:
        times = [get_datetime(value) for value in (row.get("inventory_report_received_at"), row.get("inventory_observed_at")) if value]
        if not times or (now - min(times)).total_seconds() > max_age_minutes * 60:
            return False
        if row.get("config_version") != row.get("inventory_config_version"):
            return False
        if any(int(row.get(key) or 0) for key in ("inventory_sales_pending", "inventory_sales_syncing", "inventory_sales_failed", "inventory_sales_dead_letter")):
            return False
        if any(int(row.get(key) or 0) for key in ("inventory_commands_pending", "inventory_commands_syncing", "inventory_commands_failed", "inventory_commands_dead_letter")):
            return False
        if not device_overlay_is_current(
            device_name=cstr(row.get("name")),
            acknowledged_version=cstr(row.get("inventory_overlay_version")),
            acknowledged_hash=cstr(row.get("inventory_overlay_hash")),
            acknowledged_catalog_version=cstr(row.get("inventory_catalog_version")),
        ):
            return False
    return True


def _projection_backlog_clear(warehouse: str) -> bool:
    try:
        rows = frappe.db.sql(
            """
            SELECT pl.state, COUNT(*) AS count
            FROM `tabFB Projection Log` pl
            INNER JOIN `tabFB Order` o ON o.name = pl.source_name
            WHERE pl.projection_type = 'Stock Issue'
              AND pl.source_doctype = 'FB Order'
              AND o.booth_warehouse = %s
              AND pl.state IN ('Pending', 'Processing', 'Failed', 'Dead Letter')
            GROUP BY pl.state
            """,
            (warehouse,),
            as_dict=True,
        ) or []
        return not any(int(row.get("count") or 0) for row in rows)
    except Exception:
        return False


def _permitted_actions(value: Any) -> set[str]:
    raw = cstr(value).strip()
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return {cstr(item).strip().title() for item in parsed if cstr(item).strip()}
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return {part.strip().title() for part in raw.replace(",", "\n").splitlines() if part.strip()}


def _planned_document_line(
    *,
    line: ReplenishmentLine,
    item_data: dict[str, Any],
) -> dict[str, Any]:
    """Keep stock-demand truth while expressing Purchase work in Purchase UOM."""

    action = cstr(item_data.get("action") or "Purchase")
    config = item_data.get("config") or {}
    stock_uom = cstr(config.get("stock_uom") or _stock_uom(line.item)).strip()
    document_uom = stock_uom
    conversion = Decimal("1")
    document_quantity = line.quantity
    if action == "Purchase":
        purchase_uom = cstr(config.get("purchase_uom")).strip()
        purchase_conversion = _decimal(config.get("purchase_conversion_factor"))
        if purchase_uom and purchase_conversion is not None and purchase_conversion > 0:
            document_uom = purchase_uom
            conversion = purchase_conversion
            document_quantity = line.quantity / conversion
    return {
        "item": line.item,
        "action": action,
        "warehouse": line.warehouse,
        "source_warehouse": cstr(item_data.get("source_warehouse")).strip() or None,
        "quantity": _plain_decimal(document_quantity),
        "quantity_decimal": _plain_decimal(document_quantity),
        "uom": document_uom,
        "stock_quantity_decimal": _plain_decimal(line.quantity),
        "stock_uom": stock_uom,
        "conversion_factor_decimal": _plain_decimal(conversion),
        "reason": line.reason,
    }


def _stock_uom(item: str) -> str:
    return cstr(frappe.db.get_value("Item", item, "stock_uom")).strip() or "Nos"


def _estimate_replenishment_value(lines: Iterable[ReplenishmentLine]) -> Decimal | None:
    """Use current ERP valuation only for a configured value ceiling.

    A missing or non-positive valuation is not converted into a free purchase;
    it makes the ceiling gate fail until a director supplies real authority.
    """

    total = Decimal("0")
    for line in lines:
        rate = _decimal(frappe.db.get_value(
            "Bin", {"item_code": line.item, "warehouse": line.warehouse}, "valuation_rate"
        ))
        if rate is None or rate <= 0:
            return None
        total += line.quantity * rate
    return total


def _business_today() -> date:
    current = now_datetime()
    if current.tzinfo is None:
        current = current.replace(tzinfo=BUSINESS_TIMEZONE)
    return current.astimezone(BUSINESS_TIMEZONE).date()


def _planning_marker_key(warehouse: str) -> str:
    normalized = cstr(warehouse).strip()
    if not normalized:
        raise ValueError("planning health marker requires a warehouse")
    suffix = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"{PLANNING_MARKER_KEY}:{suffix}"


def _set_planning_marker(warehouse: str) -> None:
    setter = getattr(frappe.cache(), "set_value", None)
    if callable(setter):
        setter(
            _planning_marker_key(warehouse),
            now_datetime().isoformat(),
            expires_in_sec=7 * 24 * 60 * 60,
        )


def _decimal(value: Any, *, allow_zero: bool = True) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite() or result < 0 or (not allow_zero and result <= 0):
        return None
    return result


def _plain_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized != 0 else "0"


def _truthy(value: Any) -> bool:
    return value in (1, True, "1", "true", "True")


def _resolved_component_quantity_expression(alias: str) -> str:
    """Prefer the exact child-row text column, with legacy Float fallback."""

    field = "stock_qty_decimal" if frappe.get_meta("FB Resolved Component").has_field("stock_qty_decimal") else None
    return f"COALESCE(NULLIF({alias}.{field}, ''), {alias}.stock_qty)" if field else f"{alias}.stock_qty"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()
