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
from kopos_connector.api.catalog import get_default_pos_profile, resolve_catalog_pos_profile
from kopos_connector.api.catalog import build_catalog_payload
from kopos_connector.kopos.services.inventory_autopilot.holds import (
    create_hold,
    release_hold,
)
from kopos_connector.kopos.services.inventory_autopilot.legacy_migration import (
    discover_legacy_values,
    legacy_input_digest,
    migrate_legacy_values,
)
from kopos_connector.kopos.services.inventory_autopilot.exceptions import upsert_inventory_exception
from kopos_connector.kopos.services.inventory_autopilot.document_coordinator import (
    create_and_submit_material_request,
    create_draft_purchase_order,
    outbound_configuration_safe,
    persist_inventory_plan,
)
from kopos_connector.kopos.services.inventory_autopilot.replenishment import ReplenishmentLine
from kopos_connector.kopos.services.inventory_autopilot.promotion_economics import calculate_promotion_economics, PromotionEconomicsError
from kopos_connector.kopos.services.inventory_autopilot.recipe_compiler import (
    RecipeCompilerError,
    compile_recipe_components,
)
from kopos_connector.utils.manager_approval import verify_manager_approval_token


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
def get_menu_authoring_summary() -> dict[str, Any]:
    """Return a non-financial completion checklist for Company Directors."""

    _require_company_director("Menu authoring")
    if not frappe.has_permission("FB Recipe", ptype="read"):
        frappe.throw(_("Menu authoring requires Company Director permission"), frappe.PermissionError)
    item_fields = ["name", "is_stock_item", "disabled"]
    if frappe.get_meta("Item").has_field("custom_fb_item_role"):
        item_fields.append("custom_fb_item_role")
    item_rows = frappe.get_all(
        "Item",
        filters={"disabled": 0},
        fields=item_fields,
        limit_page_length=10_000,
    )
    recipe_rows = frappe.get_all(
        "FB Recipe",
        fields=["name", "status", "sellable_item"],
        limit_page_length=10_000,
    ) if frappe.db.exists("DocType", "FB Recipe") else []
    active_items = {cstr(row.get("sellable_item")).strip() for row in recipe_rows if cstr(row.get("status")).strip() == "Active"}
    stock_items = {
        cstr(row.get("name")).strip()
        for row in item_rows
        if cstr(row.get("name")).strip()
    }
    unclassified_items = sum(1 for row in item_rows if not cstr(row.get("custom_fb_item_role")).strip()) if "custom_fb_item_role" in item_fields else len(item_rows)
    bom_count = frappe.db.count("BOM", {"docstatus": 1}) if frappe.db.exists("DocType", "BOM") else 0
    modifier_count = frappe.db.count("FB Modifier Group") if frappe.db.exists("DocType", "FB Modifier Group") else 0
    promotion_count = frappe.db.count("KoPOS Promotion", {"is_active": 1}) if frappe.db.exists("DocType", "KoPOS Promotion") else 0
    missing = len(stock_items - active_items)
    return {
        "items_ready": len(active_items & stock_items),
        "items_missing_recipe": missing,
        "published_recipes": sum(1 for row in recipe_rows if cstr(row.get("status")).strip() == "Active"),
        "draft_recipes": sum(1 for row in recipe_rows if cstr(row.get("status")).strip() == "Draft"),
        "boms": int(bom_count or 0),
        "modifier_groups": int(modifier_count or 0),
        "active_promotions": int(promotion_count or 0),
        "unclassified_items": unclassified_items,
        "ready": missing == 0 and unclassified_items == 0 and bool(recipe_rows),
    }


@frappe.whitelist(methods=["POST"])
def get_promotion_economics(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Calculate exact director-only promotion economics; never expose it to POS."""

    _require_company_director("Promotion economics")
    if not frappe.has_permission("KoPOS Promotion", ptype="read"):
        frappe.throw(_("Promotion economics requires Company Director permission"), frappe.PermissionError)
    value = _parse_json_object(payload, "Promotion economics payload")
    try:
        items = value.get("items", [])
        if cstr(value.get("promotion")).strip():
            items = _build_promotion_economics_items(
                promotion_name=cstr(value.get("promotion")).strip(),
                pos_profile=cstr(value.get("pos_profile")).strip() or None,
            )
        result = calculate_promotion_economics(items=items, scenarios=value.get("scenarios"))
        result["source"] = "promotion_document" if cstr(value.get("promotion")).strip() else "director_payload"
        return {"status": "ok", "economics": result}
    except (PromotionEconomicsError, RecipeCompilerError) as error:
        return {"status": "blocked", "reason": cstr(error), "planning_mode": "Review First"}


def _build_promotion_economics_items(
    *, promotion_name: str, pos_profile: str | None,
) -> list[dict[str, Any]]:
    """Resolve a saved promotion into exact, director-only economics inputs.

    Prices and costs are resolved on the server. The browser may provide
    scenario volumes, but it cannot provide or override COGS, recipe, or
    valuation values.
    """

    if not frappe.db.exists("DocType", "KoPOS Promotion"):
        raise PromotionEconomicsError("KoPOS Promotion is not installed")
    promotion = frappe.get_doc("KoPOS Promotion", promotion_name)
    if not int(promotion.is_active or 0):
        raise PromotionEconomicsError("the promotion is inactive")
    item_codes = {
        cstr(row.item_code).strip()
        for row in (promotion.eligible_items or [])
        if cstr(row.item_code).strip()
    }
    group_names = {
        cstr(row.item_group).strip()
        for row in (promotion.eligible_item_groups or [])
        if cstr(row.item_group).strip()
    }
    if group_names:
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

    profile = None
    profile_name = cstr(pos_profile).strip()
    if not profile_name and promotion.eligible_pos_profiles:
        profile_name = cstr(promotion.eligible_pos_profiles[0].pos_profile).strip()
    if profile_name:
        profile = frappe.get_cached_doc("POS Profile", profile_name).as_dict()
    else:
        profile = get_default_pos_profile()
    warehouse = cstr((profile or {}).get("warehouse")).strip()
    price_list = cstr((profile or {}).get("selling_price_list")).strip()

    item_meta = frappe.get_meta("Item")
    item_fields = ["name", "standard_rate", "stock_uom"]
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
            "default_serving_qty": recipe_doc.default_serving_qty,
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
            rate = _component_valuation_rate(component_item, warehouse)
            rate_sen = _currency_to_sen(rate, f"{component_item} valuation rate")
            cogs_sen += quantity * rate_sen
            component_costs.append({"item": component_item, "qty": str(quantity)})
        resolved.append({
            "item": item_code,
            "units": 1,
            "baseline_price_sen": int(baseline_price_sen),
            "promoted_price_sen": _promotion_discounted_price_sen(promotion, baseline_price_sen),
            "cogs_sen": int(cogs_sen),
            "components": component_costs,
        })
    return resolved


def _promotion_price_sen(item_code: str, item: Any, price_list: str) -> Decimal:
    if price_list and frappe.db.exists("DocType", "Item Price"):
        rows = frappe.get_all(
            "Item Price",
            filters={"item_code": item_code, "price_list": price_list, "selling": 1},
            fields=["price_list_rate", "valid_from", "valid_upto", "modified"],
            order_by="valid_from desc, modified desc",
            limit_page_length=20,
        )
        for row in rows:
            if row.price_list_rate not in (None, ""):
                return _currency_to_sen(row.price_list_rate, f"{item_code} selling price")
    return _currency_to_sen(item.standard_rate, f"{item_code} selling price")


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
    return max(0, int(discounted.quantize(Decimal("1"))))


def _component_valuation_rate(item_code: str, warehouse: str) -> Any:
    if warehouse and frappe.db.exists("DocType", "Bin"):
        value = frappe.db.get_value(
            "Bin", {"item_code": item_code, "warehouse": warehouse}, "valuation_rate"
        )
        if value not in (None, "", 0):
            return value
    item_meta = frappe.get_meta("Item")
    if item_meta.has_field("valuation_rate"):
        value = frappe.db.get_value("Item", item_code, "valuation_rate")
        if value not in (None, "", 0):
            return value
    raise PromotionEconomicsError(
        f"{item_code} has no current warehouse valuation; update stock evidence before publication"
    )


def _currency_to_sen(value: Any, label: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PromotionEconomicsError(f"{label} is not a valid currency amount") from error
    if not amount.is_finite() or amount < 0:
        raise PromotionEconomicsError(f"{label} is missing or invalid")
    return (amount * Decimal("100")).quantize(Decimal("1"))


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


@frappe.whitelist(methods=["GET"])
def get_inventory_tasks(*, device_id: str) -> dict[str, Any]:
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
    tasks: list[dict[str, Any]] = []

    count_response = get_count_task(device_id=device_id)
    count_task = count_response.get("task")
    if count_task:
        tasks.append({
            "kind": "count",
            "document": cstr(count_task.get("name")),
            "title": "Assigned stock count",
            "revision": count_task.get("revision"),
            "warehouse": warehouse,
            "lines": count_task.get("lines", []),
        })

    if frappe.db.exists("DocType", "Work Order"):
        work_orders = frappe.get_all(
            "Work Order",
            filters={
                "company": company,
                "fg_warehouse": warehouse,
                "docstatus": ["in", [0, 1]],
                "status": ["not in", ["Completed", "Stopped", "Cancelled"]],
            },
            fields=["name", "production_item", "item_name", "qty", "produced_qty", "bom_no", "fg_warehouse", "status"],
            order_by="modified asc",
            limit_page_length=20,
        )
        for row in work_orders:
            tasks.append({
                "kind": "preparation",
                "document": cstr(row.get("name")),
                "title": "Prepare {0}".format(cstr(row.get("item_name") or row.get("production_item"))),
                "item_code": cstr(row.get("production_item")),
                "item_name": cstr(row.get("item_name")),
                "qty": row.get("qty"),
                "produced_qty": row.get("produced_qty"),
                "bom_no": cstr(row.get("bom_no")),
                "warehouse": warehouse,
                "status": cstr(row.get("status")),
            })

    if frappe.db.exists("DocType", "Purchase Order") and frappe.db.exists("DocType", "Purchase Order Item"):
        purchase_orders = frappe.get_all(
            "Purchase Order",
            filters={"company": company, "docstatus": 1},
            fields=["name", "supplier", "transaction_date", "modified"],
            order_by="modified asc",
            limit_page_length=50,
        )
        for order in purchase_orders:
            rows = frappe.get_all(
                "Purchase Order Item",
                filters={"parent": order["name"], "warehouse": warehouse},
                fields=["name", "item_code", "item_name", "qty", "received_qty", "warehouse"],
                order_by="idx asc",
                limit_page_length=200,
            )
            lines = [
                {
                    "purchase_order_item": cstr(row.get("name")),
                    "item_code": cstr(row.get("item_code")),
                    "item_name": cstr(row.get("item_name")),
                    "qty": row.get("qty"),
                    "received_qty": row.get("received_qty"),
                    "warehouse": warehouse,
                }
                for row in rows
                if Decimal(str(row.get("qty") or 0)) > Decimal(str(row.get("received_qty") or 0))
            ]
            if lines:
                tasks.append({
                    "kind": "receiving",
                    "document": cstr(order.get("name")),
                    "title": "Receive supplier delivery",
                    "supplier": cstr(order.get("supplier")),
                    "warehouse": warehouse,
                    "lines": lines,
                })

    if frappe.db.exists("DocType", "Material Request") and frappe.db.exists("DocType", "Material Request Item"):
        requests = frappe.get_all(
            "Material Request",
            filters={"company": company, "docstatus": 1, "material_request_type": "Material Transfer"},
            fields=["name", "transaction_date", "modified"],
            order_by="modified asc",
            limit_page_length=50,
        )
        for request in requests:
            rows = frappe.get_all(
                "Material Request Item",
                filters={"parent": request["name"]},
                fields=["name", "item_code", "item_name", "qty", "transferred_qty", "warehouse", "target_warehouse"],
                order_by="idx asc",
                limit_page_length=200,
            )
            dispatch_lines = [
                {
                    "item_code": cstr(row.get("item_code")),
                    "item_name": cstr(row.get("item_name")),
                    "qty": row.get("qty"),
                    "transferred_qty": row.get("transferred_qty"),
                    "source_warehouse": warehouse,
                    "destination_warehouse": cstr(row.get("target_warehouse")),
                }
                for row in rows
                if cstr(row.get("warehouse")).strip() == warehouse
                and Decimal(str(row.get("qty") or 0)) > Decimal(str(row.get("transferred_qty") or 0))
            ]
            receipt_lines = [
                {
                    "item_code": cstr(row.get("item_code")),
                    "item_name": cstr(row.get("item_name")),
                    "qty": row.get("qty"),
                    "transferred_qty": row.get("transferred_qty"),
                    "destination_warehouse": warehouse,
                }
                for row in rows
                if cstr(row.get("target_warehouse")).strip() == warehouse
                and Decimal(str(row.get("qty") or 0)) > Decimal(str(row.get("transferred_qty") or 0))
            ]
            if dispatch_lines:
                tasks.append({
                    "kind": "transfer_dispatch",
                    "document": cstr(request.get("name")),
                    "title": "Send approved outlet transfer",
                    "warehouse": warehouse,
                    "lines": dispatch_lines,
                })
            if receipt_lines:
                tasks.append({
                    "kind": "transfer_receipt",
                    "document": cstr(request.get("name")),
                    "title": "Receive approved outlet transfer",
                    "warehouse": warehouse,
                    "lines": receipt_lines,
                })

    return {"status": "ok", "warehouse": warehouse, "tasks": tasks[:100], "generated_at": _iso_with_offset(now_datetime())}


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
    values = discover_legacy_values(company=cstr(company).strip() or None)
    if not cint(apply):
        return {"status": "dry_run", "input_digest": legacy_input_digest(values), "values": values, "unknown_count": sum(1 for value in values if value["availability_mode"].strip().lower() not in {"", "auto", "force_available", "force_unavailable"})}
    resolved_company = cstr(company).strip()
    resolved_warehouse = cstr(warehouse).strip()
    if not resolved_company or not resolved_warehouse:
        frappe.throw(_("Company and warehouse are required to apply legacy migration"), frappe.ValidationError)
    expected_digest = legacy_input_digest(values)
    if cstr(input_digest).strip() != expected_digest:
        frappe.throw(_("Legacy migration input digest does not match the reviewed dry run"), frappe.ValidationError)
    return {"status": "applied", **migrate_legacy_values(company=resolved_company, warehouse=resolved_warehouse, dry_run=False)}


@frappe.whitelist(methods=["POST"])
def create_inventory_material_request(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    _require_company_director("Material Request creation")
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
    requested_purpose = cstr(value.get("purpose") or "Purchase").strip()
    purpose = "Material Transfer" if requested_purpose.lower() in {"transfer", "material transfer"} else ("Manufacture" if requested_purpose.lower() == "manufacture" else "Purchase")
    result = create_and_submit_material_request(
        company=cstr(value.get("company")),
        purpose=purpose,
        required_date=value.get("required_date"),
        lines=lines,
        gates=value.get("gates") if isinstance(value.get("gates"), dict) else {},
    )
    _set_health_marker("last_plan")
    return result


@frappe.whitelist(methods=["POST"])
def create_inventory_plan(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    _require_company_director("Inventory planning")
    if not frappe.has_permission("FB Inventory Plan", ptype="create"):
        frappe.throw(_("Inventory planning requires Company Director permission"), frappe.PermissionError)
    value = _parse_json_object(payload, "Inventory plan payload")
    result = persist_inventory_plan(
        company=cstr(value.get("company")),
        warehouse=cstr(value.get("warehouse")),
        planning_date=value.get("planning_date"),
        input_hash=cstr(value.get("input_hash")),
        policy_hash=cstr(value.get("policy_hash")),
        forecast_state=cstr(value.get("forecast_state") or "Not ready"),
        gates=value.get("gates") if isinstance(value.get("gates"), dict) else {},
        lines=value.get("lines") if isinstance(value.get("lines"), list) else [],
    )
    _set_health_marker("last_plan")
    return result


@frappe.whitelist(methods=["POST"])
def create_inventory_draft_purchase_order(*, payload: str | dict[str, Any]) -> dict[str, Any]:
    _require_company_director("Draft Purchase Order creation")
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
def release_availability_hold(*, device_id: str, hold_id: str, reason: str | None = None) -> dict[str, Any]:
    require_device_context(device_id=device_id)
    hold_source = cstr(frappe.db.get_value("FB Availability Hold", hold_id, "source")).strip()
    if hold_source != "automation":
        frappe.throw(_("POS can release only an automation stock hold; manual, safety, quality and equipment holds require a director"), frappe.PermissionError)
    if not cstr(reason).strip():
        frappe.throw(_("A manager reason is required to release an automation stock hold"), frappe.ValidationError)
    with lock_device_for_operational_mutation(device_id=device_id):
        name = release_hold(hold_id)
        frappe.db.commit()
    return {"status": "accepted", "hold_id": name}


@frappe.whitelist(methods=["POST"])
def confirm_count_reconciliation(*, device_id: str, payload: str | dict[str, Any]) -> dict[str, Any]:
    """Submit one reviewed Stock Reconciliation using the existing approval token."""

    device = require_device_context(device_id=device_id)
    value = _parse_json_object(payload, "Count confirmation payload")
    command_id = cstr(value.get("command_id")).strip()
    observation_id = cstr(value.get("observation_id")).strip()
    token = cstr(value.get("manager_approval_token")).strip()
    staff_id = cstr(value.get("staff_user") or value.get("staff_id") or getattr(device, "api_user", None)).strip()
    if not command_id or not observation_id or not staff_id:
        frappe.throw(_("Count confirmation requires command_id, observation_id and staff_user"), frappe.ValidationError)
    try:
        verify_manager_approval_token(
            token,
            device_id=device_id,
            staff_id=staff_id,
            action="inventory_count_reconciliation",
            resource_id=observation_id,
            idempotency_key=command_id,
        )
    except Exception as error:
        frappe.throw(_("Manager approval for this count is invalid: {0}").format(cstr(error)), frappe.PermissionError)
    observation = frappe.db.get_value(
        "FB Inventory Count Observation",
        {"observation_id": observation_id},
        ["name", "reconciliation", "status"],
        as_dict=True,
    )
    if not observation or not cstr(observation.get("reconciliation")).strip():
        frappe.throw(_("The count observation has no draft reconciliation to confirm"), frappe.ValidationError)
    reconciliation = frappe.get_doc("Stock Reconciliation", observation["reconciliation"])
    if reconciliation.docstatus == 1:
        return {"status": "replayed", "observation_id": observation_id, "reconciliation": reconciliation.name}
    reconciliation.submit()
    frappe.db.set_value("FB Inventory Count Observation", observation["name"], "status", "Accepted", update_modified=False)
    if cstr(value.get("task_id")).strip() and frappe.db.exists("FB Inventory Count Task", value["task_id"]):
        frappe.db.set_value("FB Inventory Count Task", value["task_id"], "status", "Reviewed", update_modified=False)
    frappe.db.commit()
    return {"status": "accepted", "observation_id": observation_id, "reconciliation": reconciliation.name}


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
        reconciliation.flags.ignore_permissions = True
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


def _require_company_director(action: str) -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    if "Company Director" not in roles and "System Manager" not in roles:
        frappe.throw(_("{0} requires Company Director permission").format(action), frappe.PermissionError)


def _handle_guided_task(*, device_id: str, payload: str | dict[str, Any], task_type: str) -> dict[str, Any]:
    """Execute one fixed POS task against a standard ERPNext document."""

    device = require_device_context(device_id=device_id)
    value = _parse_json_object(payload, f"{task_type} payload")
    command_id = cstr(value.get("command_id")).strip()
    config_version = value.get("device_config_version")
    if (
        not command_id
        or len(command_id) > 160
        or isinstance(config_version, bool)
        or not isinstance(config_version, int)
        or config_version < 0
    ):
        frappe.throw(_("{0} requires command_id and device_config_version").format(task_type), frappe.ValidationError)
    if cstr(value.get("device_id") or device_id).strip() != device_id:
        frappe.throw(_("Guided task device_id does not match the authenticated device"), frappe.ValidationError)
    _validate_guided_task_actor(device, value, task_type)
    _validate_guided_task_scope(device_id, value, task_type)
    doctype = {
        "accept_preparation_task": "Work Order",
        "start_preparation_task": "Work Order",
        "complete_preparation_task": "Stock Entry",
        "submit_purchase_receipt": "Purchase Receipt",
        "submit_transfer_dispatch": "Stock Entry",
        "submit_transfer_receipt": "Stock Entry",
    }[task_type]
    with lock_device_for_operational_mutation(device_id=device_id):
        existing = _find_command_document(doctype, command_id)
        if existing:
            return {"status": "replayed", "task_type": task_type, "document": existing}
        if task_type == "accept_preparation_task":
            document = _create_work_order_from_task(value, command_id)
        elif task_type == "start_preparation_task":
            document = _submit_work_order(value)
        elif task_type == "complete_preparation_task":
            document = _create_manufacture_entry(value, command_id)
        elif task_type == "submit_purchase_receipt":
            document = _create_purchase_receipt(value, command_id)
        else:
            document = _create_transfer_entry(value, command_id, dispatch=task_type == "submit_transfer_dispatch")
        frappe.db.commit()
    return {"status": "accepted", "task_type": task_type, "document": document}


def _find_command_document(doctype: str, command_id: str) -> str | None:
    if not frappe.db.exists("DocType", doctype):
        frappe.throw(_("{0} is not installed").format(doctype), frappe.ValidationError)
    meta = frappe.get_meta(doctype)
    if not meta.has_field("custom_kopos_inventory_command_id"):
        return None
    return cstr(frappe.db.get_value(doctype, {"custom_kopos_inventory_command_id": command_id}, "name")).strip() or None


def _create_work_order_from_task(value: dict[str, Any], command_id: str) -> str:
    for fieldname in ("company", "item_code", "qty", "fg_warehouse"):
        if not cstr(value.get(fieldname)).strip() and fieldname != "qty":
            frappe.throw(_("Preparation task requires {0}").format(fieldname), frappe.ValidationError)
    document = frappe.new_doc("Work Order")
    _set_document_value(document, "company", value.get("company"))
    _set_document_value(document, "production_item", value.get("item_code"))
    _set_document_value(document, "qty", value.get("qty"))
    _set_document_value(document, "fg_warehouse", value.get("fg_warehouse"))
    _set_document_value(document, "bom_no", value.get("bom_no"))
    _set_document_value(document, "custom_kopos_inventory_command_id", command_id)
    document.insert(ignore_permissions=True)
    return document.name


def _submit_work_order(value: dict[str, Any]) -> str:
    name = cstr(value.get("work_order")).strip()
    if not name:
        frappe.throw(_("Preparation start requires a Work Order"), frappe.ValidationError)
    document = frappe.get_doc("Work Order", name)
    if document.docstatus == 0:
        document.flags.ignore_permissions = True
        document.submit()
    elif document.docstatus != 1:
        frappe.throw(_("This Work Order is not available to start"), frappe.ValidationError)
    return document.name


def _create_manufacture_entry(value: dict[str, Any], command_id: str) -> str:
    work_order = cstr(value.get("work_order")).strip()
    if not work_order:
        frappe.throw(_("Preparation completion requires a Work Order"), frappe.ValidationError)
    order = frappe.get_doc("Work Order", work_order)
    if order.docstatus != 1:
        frappe.throw(_("Start the Work Order before recording batch completion"), frappe.ValidationError)
    document = frappe.new_doc("Stock Entry")
    document.stock_entry_type = "Manufacture"
    document.purpose = "Manufacture"
    _set_document_value(document, "company", value.get("company") or getattr(order, "company", None))
    _set_document_value(document, "work_order", work_order)
    _set_document_value(document, "fg_completed_qty", value.get("actual_yield") or value.get("qty"))
    _set_document_value(document, "to_warehouse", value.get("fg_warehouse") or getattr(order, "fg_warehouse", None))
    _set_document_value(document, "custom_kopos_inventory_command_id", command_id)
    for row in value.get("items", []):
        if not isinstance(row, dict) or not cstr(row.get("item_code")).strip():
            frappe.throw(_("Batch completion contains an invalid component row"), frappe.ValidationError)
        item_payload = {
            "item_code": row["item_code"],
            "qty": row.get("qty"),
            "s_warehouse": row.get("warehouse") or row.get("s_warehouse"),
        }
        if cstr(value.get("batch_no")).strip() and frappe.get_meta("Stock Entry Detail").has_field("batch_no"):
            item_payload["batch_no"] = cstr(value["batch_no"]).strip()
        if cstr(value.get("expiry_date")).strip() and frappe.get_meta("Stock Entry Detail").has_field("expiry_date"):
            item_payload["expiry_date"] = cstr(value["expiry_date"]).strip()
        document.append("items", item_payload)
    document.insert(ignore_permissions=True)
    document.flags.ignore_permissions = True
    document.submit()
    return document.name


def _create_purchase_receipt(value: dict[str, Any], command_id: str) -> str:
    purchase_order = cstr(value.get("purchase_order")).strip()
    if not purchase_order:
        frappe.throw(_("Receiving requires a Purchase Order"), frappe.ValidationError)
    po = frappe.get_doc("Purchase Order", purchase_order)
    if po.docstatus != 1:
        frappe.throw(_("Only a submitted Purchase Order can be received"), frappe.ValidationError)
    document = frappe.new_doc("Purchase Receipt")
    _set_document_value(document, "supplier", po.supplier)
    _set_document_value(document, "company", po.company)
    _set_document_value(document, "custom_kopos_inventory_command_id", command_id)
    po_rows = {cstr(row.name): row for row in po.items}
    lines = value.get("lines")
    if not isinstance(lines, list) or not lines:
        frappe.throw(_("Receiving requires accepted item lines"), frappe.ValidationError)
    for line in lines:
        if not isinstance(line, dict):
            frappe.throw(_("Receiving contains an invalid line"), frappe.ValidationError)
        source = po_rows.get(cstr(line.get("purchase_order_item")))
        item_code = cstr(line.get("item_code") or getattr(source, "item_code", None)).strip()
        line_warehouse = cstr(line.get("warehouse") or getattr(source, "warehouse", None)).strip()
        device_warehouse = cstr(value.get("warehouse")).strip()
        if device_warehouse and line_warehouse and line_warehouse != device_warehouse:
            frappe.throw(_("Receiving cannot post outside the device warehouse"), frappe.PermissionError)
        if not source or not item_code or Decimal(str(line.get("qty", 0))) <= 0:
            frappe.throw(_("Receiving line does not match the submitted Purchase Order"), frappe.ValidationError)
        item_payload = {
            "item_code": item_code,
            "qty": line["qty"],
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
    document.insert(ignore_permissions=True)
    document.flags.ignore_permissions = True
    document.submit()
    return document.name


def _create_transfer_entry(value: dict[str, Any], command_id: str, *, dispatch: bool) -> str:
    company = cstr(value.get("company")).strip()
    material_request_name = cstr(value.get("material_request") or value.get("source_document")).strip()
    from_warehouse = cstr(value.get("from_warehouse") if dispatch else value.get("transit_warehouse")).strip()
    to_warehouse = cstr(value.get("transit_warehouse") if dispatch else value.get("to_warehouse")).strip()
    if not company or not from_warehouse or not to_warehouse:
        frappe.throw(_("Transfer requires company, source, transit and destination warehouses"), frappe.ValidationError)
    if not material_request_name:
        frappe.throw(_("Transfer requires the submitted Material Request number"), frappe.ValidationError)
    if not frappe.db.exists("Material Request", material_request_name):
        frappe.throw(_("The transfer Material Request does not exist"), frappe.ValidationError)
    material_request = frappe.get_doc("Material Request", material_request_name)
    if material_request.docstatus != 1 or cstr(getattr(material_request, "material_request_type", "")).strip() != "Material Transfer":
        frappe.throw(_("Only a submitted Material Transfer Request can be executed on POS"), frappe.ValidationError)
    if cstr(getattr(material_request, "company", "")).strip() != company:
        frappe.throw(_("The transfer Material Request belongs to another company"), frappe.PermissionError)
    lines = value.get("lines")
    if not isinstance(lines, list) or not lines:
        frappe.throw(_("Transfer requires item lines"), frappe.ValidationError)
    requested_rows = {
        (cstr(row.item_code).strip(), cstr(row.warehouse).strip(), cstr(getattr(row, "target_warehouse", "")).strip()): row
        for row in (material_request.items or [])
    }
    document = frappe.new_doc("Stock Entry")
    document.stock_entry_type = "Material Transfer"
    document.purpose = "Material Transfer"
    document.company = company
    _set_document_value(document, "custom_kopos_inventory_command_id", command_id)
    for line in lines:
        if not isinstance(line, dict) or not cstr(line.get("item_code")).strip() or Decimal(str(line.get("qty", 0))) <= 0:
            frappe.throw(_("Transfer contains an invalid line"), frappe.ValidationError)
        item_code = cstr(line.get("item_code")).strip()
        expected_source = cstr(line.get("source_warehouse") or (from_warehouse if dispatch else "")).strip()
        expected_destination = cstr(line.get("destination_warehouse") or (to_warehouse if dispatch else "")).strip()
        matching = [
            row for (row_item, row_source, row_destination), row in requested_rows.items()
            if row_item == item_code
            and (not dispatch or row_source == from_warehouse)
            and (not row_destination or row_destination == to_warehouse)
            and (not expected_source or row_source == expected_source)
            and (not expected_destination or row_destination == expected_destination)
        ]
        if not matching:
            frappe.throw(_("Transfer line is not present on the submitted Material Request"), frappe.ValidationError)
        requested_qty = sum(Decimal(str(getattr(row, "qty", 0) or 0)) for row in matching)
        if Decimal(str(line.get("qty", 0))) > requested_qty:
            frappe.throw(_("Transfer quantity exceeds the submitted Material Request"), frappe.ValidationError)
        document.append("items", {
            "item_code": item_code,
            "qty": line["qty"],
            "s_warehouse": from_warehouse,
            "t_warehouse": to_warehouse,
            "batch_no": line.get("batch_no"),
        })
    document.insert(ignore_permissions=True)
    document.flags.ignore_permissions = True
    document.submit()
    return document.name


def _validate_guided_task_scope(device_id: str, value: dict[str, Any], task_type: str) -> None:
    """Keep a device command inside its assigned outlet warehouses."""

    profile = resolve_catalog_pos_profile(device_id=device_id) or {}
    assigned_warehouse = cstr(profile.get("warehouse")).strip()
    if not assigned_warehouse:
        frappe.throw(_("This device has no assigned inventory warehouse"), frappe.ValidationError)
    if task_type == "accept_preparation_task":
        controlled = cstr(value.get("fg_warehouse")).strip()
    elif task_type == "complete_preparation_task":
        controlled = cstr(value.get("fg_warehouse") or value.get("warehouse")).strip()
    elif task_type == "submit_purchase_receipt":
        controlled = assigned_warehouse
    elif task_type == "submit_transfer_dispatch":
        controlled = cstr(value.get("from_warehouse")).strip()
    else:
        controlled = cstr(value.get("to_warehouse")).strip()
    if controlled and controlled != assigned_warehouse:
        frappe.throw(_("This guided task is outside the device warehouse"), frappe.PermissionError)


def _validate_guided_task_actor(device: Any, value: dict[str, Any], task_type: str) -> None:
    """Bind the physical action to an active outlet user and capability."""

    staff_id = cstr(value.get("staff_user") or value.get("staff_id")).strip()
    if not staff_id:
        frappe.throw(_("Guided task requires the signed-in staff user"), frappe.ValidationError)
    active_by_user = {
        cstr(getattr(row, "user", None)).strip(): row
        for row in (getattr(device, "device_users", None) or [])
        if cint(getattr(row, "active", 0)) and cstr(getattr(row, "user", None)).strip()
    }
    if staff_id not in active_by_user:
        frappe.throw(_("Signed-in staff user is not active on this outlet tablet"), frappe.PermissionError)
    if task_type in {"submit_purchase_receipt", "submit_transfer_dispatch", "submit_transfer_receipt"}:
        if not cint(getattr(active_by_user[staff_id], "can_manager_override", 0)):
            frappe.throw(
                _("A manager must sign in before confirming this stock movement"),
                frappe.PermissionError,
            )


def _set_document_value(document: Any, fieldname: str, value: Any) -> None:
    if value not in (None, "") and frappe.get_meta(document.doctype).has_field(fieldname):
        setattr(document, fieldname, value)


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
    """Compare a device acknowledgement with the cached generated overlay.

    Rebuilding the full catalog here made the manager health route depend on
    every catalog query and could keep a monitor request loading indefinitely.
    Catalog generation publishes this identity to Redis; missing or expired
    cache data fails closed as unacknowledged and never blocks health polling.
    """

    device_name = cstr(row.get("name")).strip()
    acknowledged_version = cstr(row.get("inventory_overlay_version")).strip()
    acknowledged_hash = cstr(row.get("inventory_overlay_hash")).strip()
    if not device_name or not acknowledged_version or not acknowledged_hash:
        return False
    try:
        getter = getattr(frappe.cache(), "get_value", None)
        if not callable(getter):
            return False
        raw_identity = getter(f"kopos:inventory-autopilot:overlay:{device_name}")
        if isinstance(raw_identity, bytes):
            raw_identity = raw_identity.decode("utf-8")
        if isinstance(raw_identity, str):
            identity = json.loads(raw_identity)
        elif isinstance(raw_identity, dict):
            identity = raw_identity
        else:
            return False
        if not isinstance(identity, dict):
            return False
        valid_until = identity.get("valid_until")
        if valid_until and get_datetime(valid_until) < now_datetime():
            return False
        current_version = cstr(identity.get("version")).strip()
        current_hash = cstr(identity.get("overlay_hash") or current_version).strip()
        return bool(current_version and current_hash) and current_version == acknowledged_version and current_hash == acknowledged_hash
    except Exception:
        # Health must remain a sanitized read model. Cache corruption or expiry
        # is an unacknowledged overlay, not an API traceback or false green.
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
