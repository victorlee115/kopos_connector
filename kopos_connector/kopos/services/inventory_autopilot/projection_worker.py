from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import add_to_date, cint, cstr, now_datetime

from kopos_connector.kopos.services.inventory_autopilot.exceptions import (
    upsert_inventory_exception,
)
from kopos_connector.kopos.services.inventory_autopilot.recipe_compiler import (
    compile_recipe_components,
)
from kopos_connector.kopos.services.inventory_autopilot.holds import create_reliable_automation_holds, restore_automation_holds
from kopos_connector.kopos.services.projection.log_service import (
    create_projection_log,
    update_projection_state,
)


INVENTORY_PROJECTION_TYPE = "Stock Issue"
INVENTORY_WORKER_LOCK_KEY = "kopos:inventory-projection-worker:v1"
INVENTORY_WORKER_LOCK_TTL_SECONDS = 5 * 60
INVENTORY_LEASE_SECONDS = 2 * 60
DEFAULT_BATCH_SIZE = 20
DEFAULT_MAX_RETRIES = 8
INVENTORY_RETRY_SETTING = "kopos_inventory_projection_retry_max_attempts"
COMPARE_AND_DELETE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def enqueue_inventory_projection(order_name: str) -> Any:
    resolved_name = cstr(order_name).strip()
    if not resolved_name:
        raise ValueError("FB Order name is required for inventory projection")
    digest = hashlib.sha256(resolved_name.encode("utf-8")).hexdigest()
    return frappe.enqueue(
        "kopos_connector.kopos.services.inventory_autopilot.projection_worker.project_inventory_order",
        queue="short",
        enqueue_after_commit=True,
        job_id=f"kopos-inventory-project-{digest}",
        timeout=5 * 60,
        order_name=resolved_name,
    )


def recover_inventory_projections(*, batch_size: int | None = None) -> list[dict[str, Any]]:
    cache = frappe.cache()
    _set_scheduler_marker(cache, "last_start")
    token = _acquire_lock(cache)
    if not token:
        return []
    try:
        create_reliable_automation_holds()
        restore_automation_holds()
        limit = max(1, min(cint(batch_size or DEFAULT_BATCH_SIZE), 100))
        rows = frappe.get_all(
            "FB Order",
            filters={"status": "Submitted"},
            fields=["name"],
            order_by="modified asc",
            limit_page_length=limit * 4,
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            if len(results) >= limit:
                break
            result = project_inventory_order(cstr(row.get("name")))
            if result.get("state") in {"Succeeded", "Failed", "Not Evaluated"}:
                results.append(result)
        _set_scheduler_marker(cache, "last_success")
        return results
    except Exception:
        _set_scheduler_marker(cache, "last_failure")
        raise
    finally:
        _release_lock(cache, token)


def project_inventory_order(order_name: str) -> dict[str, Any]:
    resolved_name = cstr(order_name).strip()
    if not resolved_name:
        return {"state": "Failed", "error": "FB Order name is required"}
    order = frappe.get_doc("FB Order", resolved_name)
    policy = _policy_for_order(order)
    if not policy or not cstr(getattr(policy, "cutover_token", None)).strip():
        return {"order": resolved_name, "state": "Not Evaluated"}

    resolved_sales = order.get_resolved_sales()
    if not resolved_sales:
        resolved_sales = _prepare_frozen_resolved_sales(order)
    identity = {
        "order": resolved_name,
        "projector_role": "inventory_material_issue",
        "inventory_contract_version": cstr(policy.inventory_contract_version),
        "cutover_token": cstr(policy.cutover_token),
    }
    idempotency_key = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload_hash = hashlib.sha256(
        json.dumps(
            {
                **identity,
                "resolved_sales": [
                    {
                        "name": getattr(sale, "name", None),
                        "components": _compile_resolved_sale(sale),
                    }
                    for sale in resolved_sales
                ],
            },
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    log_name = create_projection_log(
        source_doctype="FB Order",
        source_name=resolved_name,
        projection_type=INVENTORY_PROJECTION_TYPE,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
    )
    if not _claim_log(log_name):
        return {"order": resolved_name, "projection_log": log_name, "state": "Processing"}
    try:
        if not resolved_sales:
            update_projection_state(log_name, "Succeeded", None, None, None)
            return {
                "order": resolved_name,
                "projection_log": log_name,
                "target_name": None,
                "state": "Succeeded",
            }
        from kopos_connector.kopos.services.inventory.stock_issue_service import (
            create_ingredient_stock_entry,
        )

        target_name = create_ingredient_stock_entry(
            order,
            resolved_sales,
            expense_account=(
                cstr(getattr(policy, "expense_account", None)).strip()
                or cstr(getattr(policy, "cogs_account", None)).strip()
                or None
            ),
        )
        if not target_name:
            raise RuntimeError("Material Issue Stock Entry was not created")
        update_projection_state(
            log_name,
            "Succeeded",
            "Stock Entry",
            target_name,
            None,
        )
        return {
            "order": resolved_name,
            "projection_log": log_name,
            "target_name": target_name,
            "state": "Succeeded",
        }
    except Exception as error:
        frappe.db.rollback()
        _record_failure(order, log_name, policy, error)
        return {
            "order": resolved_name,
            "projection_log": log_name,
            "state": "Failed",
            "error": "inventory projection failed; see FB Inventory Exception",
        }


def _policy_for_order(order: Any) -> Any | None:
    company = cstr(getattr(order, "company", None)).strip()
    warehouse = cstr(getattr(order, "booth_warehouse", None)).strip()
    if not company or not warehouse:
        return None
    names = frappe.get_all(
        "FB Inventory Policy",
        filters={"company": company, "warehouse": warehouse},
        pluck="name",
        limit_page_length=1,
    )
    return frappe.get_doc("FB Inventory Policy", names[0]) if names else None


def _prepare_frozen_resolved_sales(order: Any) -> list[Any]:
    """Resolve post-cutover recipe snapshots without touching checkout.

    Recipe failures are deliberately raised inside the worker boundary. The
    caller records an inventory exception while the already-submitted order
    remains commercially successful.
    """

    resolutions: list[dict[str, Any]] = []
    for line_index, line in enumerate(list(getattr(order, "items", []) or []), start=1):
        if not int(getattr(line, "is_recipe_managed", 0) or 0):
            resolutions.append({
                "line": line,
                "line_index": line_index,
                "recipe_doc": None,
                "selected_modifiers": [],
                "resolved_components": [],
            })
            continue
        recipe_doc = order.resolve_recipe_for_line(line_index, line)
        selected_modifiers = order.validate_modifier_selections(line_index, line, recipe_doc)
        resolved_components = order.resolve_components_for_line(
            line_index, line, recipe_doc, selected_modifiers
        )
        resolutions.append({
            "line": line,
            "line_index": line_index,
            "recipe_doc": recipe_doc,
            "selected_modifiers": selected_modifiers,
            "resolved_components": resolved_components,
        })
    order.create_resolved_sales(resolutions)
    # The FB Order is already submitted by the time this recovery worker runs.
    # Persist only the two immutable child-line pointers; calling ``save`` on a
    # submitted parent would either fail or reopen the commercial document.
    for resolution in resolutions:
        line = resolution["line"]
        line_name = cstr(getattr(line, "name", None)).strip()
        if not line_name:
            raise ValueError("Submitted FB Order line is missing its row name")
        frappe.db.set_value(
            "FB Order Line",
            line_name,
            {
                "resolved_sale": getattr(line, "resolved_sale", None),
                "resolved_components_snapshot": getattr(
                    line, "resolved_components_snapshot", "[]"
                ),
            },
            update_modified=False,
        )
    frappe.db.commit()
    return order.get_resolved_sales()


def _compile_resolved_sale(resolved_sale: Any) -> dict[str, str]:
    components = []
    for component in list(getattr(resolved_sale, "resolved_components", []) or []):
        components.append({
            "item": cstr(getattr(component, "item", None)),
            "stock_qty": getattr(component, "stock_qty", None) or getattr(component, "qty", None),
            "stock_uom": getattr(component, "stock_uom", None) or getattr(component, "uom", None),
            "affects_stock": bool(getattr(component, "affects_stock", 0)),
        })
    compiled = compile_recipe_components(
        {"yield_qty": 1, "default_serving_qty": 1, "components": components},
        servings=1,
    )
    return {item: str(quantity) for item, quantity in compiled.items()}


def _claim_log(log_name: str) -> bool:
    now = now_datetime()
    lease_token = uuid4().hex
    expires = add_to_date(now, seconds=INVENTORY_LEASE_SECONDS)
    frappe.db.sql(
        """
        UPDATE `tabFB Projection Log`
        SET lease_token = %s, lease_expires_at = %s, state = 'Processing', last_attempt_at = %s
        WHERE name = %s
          AND state IN ('Pending', 'Failed')
          AND (next_retry_at IS NULL OR next_retry_at <= %s)
          AND (lease_token IS NULL OR lease_token = '' OR lease_expires_at IS NULL OR lease_expires_at <= %s)
        """,
        (lease_token, expires, now, log_name, now, now),
    )
    claimed = cstr(frappe.db.get_value("FB Projection Log", log_name, "lease_token")) == lease_token
    if claimed:
        frappe.db.commit()
    else:
        frappe.db.rollback()
    return claimed


def _record_failure(order: Any, log_name: str, policy: Any, error: Exception) -> None:
    retry_count = cint(frappe.db.get_value("FB Projection Log", log_name, "retry_count"))
    next_retry_count = retry_count + 1
    maximum = _configured_retry_limit()
    state = "Dead Letter" if next_retry_count >= maximum else "Failed"
    update_projection_state(log_name, "Failed", None, None, str(error))
    if state == "Dead Letter":
        frappe.db.set_value("FB Projection Log", log_name, "state", "Dead Letter", update_modified=False)
    if state == "Dead Letter":
        frappe.db.set_value(
            "FB Projection Log",
            log_name,
            {"dead_lettered_at": now_datetime(), "lease_token": None, "lease_expires_at": None},
            update_modified=False,
        )
    else:
        backoff_seconds = min(60 * 60, 60 * (2 ** max(0, next_retry_count - 1)))
        frappe.db.set_value(
            "FB Projection Log",
            log_name,
            "next_retry_at",
            add_to_date(now_datetime(), seconds=backoff_seconds),
            update_modified=False,
        )
    upsert_inventory_exception(
        reason_code="inventory_projection_dead_letter" if state == "Dead Letter" else "inventory_projection_failed",
        summary=(
            "Ingredient stock projection needs review"
            if state != "Dead Letter"
            else "Ingredient stock projection exhausted automatic retries"
        ),
        next_action="Review the frozen recipe and retry the inventory projection after correcting the source data",
        severity="Critical" if state == "Dead Letter" else "Warning",
        company=cstr(getattr(order, "company", None)),
        warehouse=cstr(getattr(order, "booth_warehouse", None)),
        source_doctype="FB Order",
        source_name=cstr(getattr(order, "name", None)),
    )
    frappe.db.commit()


def _configured_retry_limit() -> int:
    config = getattr(frappe, "conf", None)
    getter = getattr(config, "get", None) if config is not None else None
    configured = cint(getter(INVENTORY_RETRY_SETTING) if callable(getter) else None)
    return max(1, min(configured or DEFAULT_MAX_RETRIES, 50))


def _acquire_lock(cache: Any) -> str | None:
    token = uuid4().hex
    redis_client = getattr(cache, "redis_client", None)
    if callable(redis_client):
        redis_client = redis_client()
    if redis_client is None or not hasattr(redis_client, "set"):
        return None
    return token if redis_client.set(INVENTORY_WORKER_LOCK_KEY, token, ex=INVENTORY_WORKER_LOCK_TTL_SECONDS, nx=True) else None


def _release_lock(cache: Any, token: str) -> None:
    redis_client = getattr(cache, "redis_client", None)
    if callable(redis_client):
        redis_client = redis_client()
    if redis_client is not None and hasattr(redis_client, "eval"):
        redis_client.eval(COMPARE_AND_DELETE_LUA, 1, INVENTORY_WORKER_LOCK_KEY, token)


def _set_scheduler_marker(cache: Any, marker: str) -> None:
    setter = getattr(cache, "set_value", None)
    if callable(setter):
        current = now_datetime()
        if current.tzinfo is None:
            current = current.replace(tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
        setter(f"kopos:inventory-autopilot:scheduler:{marker}", current.isoformat(), expires_in_sec=7 * 24 * 60 * 60)
