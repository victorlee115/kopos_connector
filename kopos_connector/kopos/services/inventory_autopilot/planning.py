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
    persist_inventory_plan,
)
from kopos_connector.kopos.services.inventory_autopilot.exceptions import (
    upsert_inventory_exception,
)
from kopos_connector.kopos.services.inventory_autopilot.forecast import (
    ForecastResult,
    evaluate_forecast,
)
from kopos_connector.kopos.services.inventory_autopilot.replenishment import (
    ReplenishmentInput,
    ReplenishmentLine,
    build_replenishment_plan,
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
        _set_planning_marker()
    return results


def aggregate_consumption(
    rows: Iterable[dict[str, Any]], *, timezone: ZoneInfo = BUSINESS_TIMEZONE
) -> tuple[tuple[date, ...], dict[str, tuple[Decimal, ...]]]:
    """Turn resolved-component rows into dense open-day series.

    A day is an operating day only when at least one valid post-cutover stock
    component was observed.  Missing items on an observed day are represented
    as zero, which keeps the seasonal comparison honest without inventing
    closed days.
    """

    totals: dict[tuple[date, str], Decimal] = defaultdict(lambda: Decimal("0"))
    operating_days: set[date] = set()
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
        totals[(local_day, item)] += quantity
        operating_days.add(local_day)
    ordered_days = tuple(sorted(operating_days))
    item_names = tuple(sorted({item for _, item in totals}))
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
        "automation_state": cstr(policy.get("automation_state")),
        "inventory_contract_version": cstr(policy.get("inventory_contract_version")),
        "cutover_token": token,
        "cutover_at": cstr(cutover_at),
        "permitted_actions": cstr(policy.get("permitted_actions")),
        "quantity_ceiling": cstr(policy.get("quantity_ceiling")),
        "value_ceiling": cstr(policy.get("value_ceiling")),
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
    operating_days, series = aggregate_consumption(rows)
    item_data: list[dict[str, Any]] = []
    forecast_results: list[ForecastResult] = []
    all_recipe_uom = bool(series)
    all_source_current = bool(series)
    all_shelf_life_safe = True
    replenishment_inputs: list[ReplenishmentInput] = []
    proposed_actions: list[dict[str, Any]] = []
    for item, actuals in series.items():
        config = _item_configuration(item=item, warehouse=warehouse, company=company)
        result = evaluate_forecast(
            actuals,
            operating_days=[True] * len(operating_days),
            shelf_life_days=config.get("shelf_life_days"),
        )
        # A shelf-life cap is derived only after the measured forecast exists;
        # no sell-through rate is guessed when history is insufficient.
        if result.forecast is not None and config.get("shelf_life_days"):
            shelf_life_cap = result.forecast * Decimal(str(config["shelf_life_days"]))
            result = evaluate_forecast(
                actuals,
                operating_days=[True] * len(operating_days),
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
        if forecast_quantity is None:
            continue
        lead_days = config.get("lead_time_days")
        safety_stock = result.positive_underforecast_p90 or Decimal("0")
        replenishment_inputs.append(
            ReplenishmentInput(
                item=item,
                warehouse=warehouse,
                current_stock=config["current_stock"],
                reservations=config["reservations"],
                unposted_consumption=config["unposted_consumption"],
                open_supply=config["open_supply"],
                forecast_through_lead_time=forecast_quantity * max(Decimal("1"), lead_days or Decimal("0")),
                safety_stock=safety_stock,
                supplier_pack=config["supplier_pack"],
                supplier_minimum=config["supplier_minimum"],
                shelf_life_cap=config.get("shelf_life_cap"),
            )
        )
        item_data.append({
            "item": item,
            "actual_days": len(actuals),
            "forecast": str(forecast_quantity),
            "forecast_state": result.state,
            "model": result.selected_model,
            "reasons": result.reasons,
            "config": config,
        })

    forecast_state = overall_forecast_state(forecast_results)
    input_hash = _hash({
        "planning_day": planning_day.isoformat(),
        "operating_days": [value.isoformat() for value in operating_days],
        "items": item_data,
    })
    base_gates = _base_gates(policy, warehouse, max_source_age=int(policy.get("max_source_age_minutes") or 30))
    gates = {
        **base_gates,
        "recipe_uom_complete": all_recipe_uom,
        "forecast_reliable": forecast_state == "Reliable",
        "source_current": all_source_current,
        "shelf_life_cap": all_shelf_life_safe,
    }
    raw_lines = build_replenishment_plan(replenishment_inputs)
    quantity_ceiling = _decimal(policy.get("quantity_ceiling"))
    total_quantity = sum((line.quantity for line in raw_lines), Decimal("0"))
    if quantity_ceiling is not None and quantity_ceiling > 0 and total_quantity > quantity_ceiling:
        gates["quantity_ceiling"] = False
    allowed_actions = _permitted_actions(policy.get("permitted_actions"))
    if not allowed_actions:
        gates["quantity_ceiling"] = False
    lines = tuple(
        {
            "item": line.item,
            "action": "Purchase",
            "warehouse": line.warehouse,
            "quantity": str(line.quantity),
            "uom": _stock_uom(line.item),
            "reason": line.reason,
        }
        for line in raw_lines
        if "Purchase" in allowed_actions
    )
    result = persist_inventory_plan(
        company=company,
        warehouse=warehouse,
        planning_date=planning_day,
        input_hash=input_hash,
        policy_hash=policy_hash,
        forecast_state=forecast_state,
        gates=gates,
        lines=lines,
    )
    created_documents: list[dict[str, Any]] = []
    if result.get("status") == "created" and all(gates.get(name) is True for name in gates) and lines:
        mr_result = create_and_submit_material_request(
            company=company,
            purpose="Purchase",
            required_date=planning_day + timedelta(days=1),
            lines=tuple(
                ReplenishmentLine(line["item"], line["warehouse"], Decimal(line["quantity"]), line["reason"])
                for line in lines
            ),
            gates=gates,
        )
        created_documents.append(mr_result)
        if mr_result.get("material_request") and frappe.get_meta("FB Inventory Plan").has_field("created_documents"):
            frappe.db.set_value(
                "FB Inventory Plan",
                result["plan"],
                "created_documents",
                json.dumps(created_documents, sort_keys=True, default=str),
                update_modified=False,
            )
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


def _resolved_component_rows(*, warehouse: str, cutover_at: Any) -> list[dict[str, Any]]:
    if not frappe.db.exists("DocType", "FB Resolved Sale") or not frappe.db.exists("DocType", "FB Resolved Component"):
        return []
    try:
        return frappe.db.sql(
            """
            SELECT rs.creation AS observed_at, rc.item, rc.stock_qty, rc.affects_stock
            FROM `tabFB Resolved Sale` rs
            INNER JOIN `tabFB Resolved Component` rc ON rc.parent = rs.name
            INNER JOIN `tabItem` i ON i.name = rc.item
            WHERE rs.booth_warehouse = %s
              AND rs.creation >= %s
              AND i.is_stock_item = 1
              AND rc.affects_stock = 1
              AND rc.stock_qty > 0
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


def _item_configuration(*, item: str, warehouse: str, company: str) -> dict[str, Any]:
    meta = frappe.get_meta("Item")
    fields = ["name", "stock_uom", "is_stock_item"]
    for fieldname in ("shelf_life_in_days", "custom_kopos_shelf_life_days", "custom_kopos_supplier_pack_size"):
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
    supplier = _supplier_configuration(item)
    current_stock = _decimal(bin_values.get("actual_qty")) or Decimal("0")
    reservations = _decimal(bin_values.get("reserved_qty")) or Decimal("0")
    open_supply = (_decimal(bin_values.get("ordered_qty")) or Decimal("0")) + (_decimal(bin_values.get("indented_qty")) or Decimal("0"))
    source_current = bool(bin_fields) and supplier["source_current"]
    shelf_life_cap = None
    return {
        "recipe_uom_complete": is_stock_item and bool(stock_uom),
        "source_current": source_current,
        "current_stock": current_stock,
        "reservations": reservations,
        "open_supply": open_supply,
        "unposted_consumption": Decimal("0"),
        "lead_time_days": supplier["lead_time_days"],
        "supplier_pack": supplier["supplier_pack"],
        "supplier_minimum": supplier["supplier_minimum"],
        "shelf_life_days": int(shelf_life_days) if shelf_life_days is not None else None,
        "shelf_life_cap": shelf_life_cap,
        "stock_uom": stock_uom,
    }


def _supplier_configuration(item: str) -> dict[str, Any]:
    if not frappe.db.exists("DocType", "Item Supplier"):
        return {"source_current": False, "lead_time_days": None, "supplier_pack": Decimal("0"), "supplier_minimum": Decimal("0")}
    meta = frappe.get_meta("Item Supplier")
    fields = ["parent", "supplier"]
    pack_field = next((name for name in ("custom_kopos_supplier_pack_size", "supplier_pack_size", "pack_size") if meta.has_field(name)), None)
    minimum_field = next((name for name in ("custom_kopos_supplier_minimum_qty", "min_order_qty", "supplier_minimum_qty") if meta.has_field(name)), None)
    lead_field = next((name for name in ("lead_time_days", "custom_kopos_lead_time_days") if meta.has_field(name)), None)
    for fieldname in (pack_field, minimum_field, lead_field):
        if fieldname:
            fields.append(fieldname)
    rows = frappe.get_all("Item Supplier", filters={"parent": item, "parenttype": "Item"}, fields=fields, limit_page_length=1)
    row = rows[0] if rows else {}
    pack = _decimal(row.get(pack_field)) if pack_field else None
    minimum = _decimal(row.get(minimum_field)) if minimum_field else Decimal("0")
    lead = _decimal(row.get(lead_field)) if lead_field else None
    return {
        "source_current": bool(row.get("supplier")) and pack is not None and pack > 0 and lead is not None and lead >= 0,
        "lead_time_days": lead,
        "supplier_pack": pack or Decimal("0"),
        "supplier_minimum": minimum or Decimal("0"),
    }


def _base_gates(policy: dict[str, Any], warehouse: str, *, max_source_age: int) -> dict[str, bool]:
    return {
        "policy_active": cstr(policy.get("automation_state")).strip() == "Active",
        "input_hash_match": True,
        "no_unresolved_count": _no_unresolved_count(warehouse),
        "devices_current": _devices_current(warehouse, max_source_age),
        "projection_backlog_clear": _projection_backlog_clear(warehouse),
        "recipe_uom_complete": True,
        "forecast_reliable": False,
        "source_current": True,
        "quantity_ceiling": True,
        "shelf_life_cap": True,
        "intent_not_open": True,
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
            SELECT d.inventory_report_received_at, d.inventory_observed_at,
                   d.config_version, d.inventory_config_version,
                   d.inventory_overlay_version, d.inventory_overlay_hash,
                   d.inventory_sales_pending, d.inventory_sales_syncing,
                   d.inventory_sales_failed, d.inventory_sales_dead_letter,
                   d.inventory_commands_pending, d.inventory_commands_failed
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
        if any(int(row.get(key) or 0) for key in ("inventory_sales_pending", "inventory_sales_syncing", "inventory_sales_failed", "inventory_sales_dead_letter", "inventory_commands_pending", "inventory_commands_failed")):
            return False
        if not row.get("inventory_overlay_version") or not row.get("inventory_overlay_hash"):
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


def _stock_uom(item: str) -> str:
    return cstr(frappe.db.get_value("Item", item, "stock_uom")).strip() or "Nos"


def _business_today() -> date:
    current = now_datetime()
    if current.tzinfo is None:
        current = current.replace(tzinfo=BUSINESS_TIMEZONE)
    return current.astimezone(BUSINESS_TIMEZONE).date()


def _set_planning_marker() -> None:
    setter = getattr(frappe.cache(), "set_value", None)
    if callable(setter):
        setter(PLANNING_MARKER_KEY, now_datetime().isoformat(), expires_in_sec=7 * 24 * 60 * 60)


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


def _truthy(value: Any) -> bool:
    return value in (1, True, "1", "true", "True")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()
