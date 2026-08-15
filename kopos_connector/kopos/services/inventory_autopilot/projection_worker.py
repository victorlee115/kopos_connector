from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import cint, cstr, now_datetime

try:
    from frappe.utils import add_to_date
except ImportError:
    # Mocked commercial-contract tests intentionally provide only the small
    # Frappe surface they exercise.  Keep the inventory worker optional at
    # import time while preserving the real Frappe implementation in runtime.
    def add_to_date(value: Any = None, **kwargs: Any) -> Any:
        base = value if value is not None else now_datetime()
        if isinstance(base, str):
            from datetime import datetime

            base = datetime.fromisoformat(base.replace("Z", "+00:00"))
        return base + timedelta(**kwargs)

from kopos_connector.kopos.services.inventory_autopilot.exceptions import (
    resolve_inventory_exception,
    upsert_inventory_exception,
)
from kopos_connector.kopos.services.inventory_autopilot.recipe_compiler import (
    compile_recipe_vector,
)
from kopos_connector.kopos.services.inventory_autopilot.holds import (
    create_reliable_automation_holds,
    record_stale_automation_hold_exceptions,
    restore_automation_holds,
)
from kopos_connector.kopos.services.projection.log_service import (
    create_projection_log,
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
        record_stale_automation_hold_exceptions()
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
    if not _is_post_cutover_order(order, policy):
        return {"order": resolved_name, "state": "Not Evaluated"}

    identity = {
        "order": resolved_name,
        "projector_role": "inventory_material_issue",
        "inventory_contract_version": cstr(policy.inventory_contract_version),
        "cutover_token": cstr(policy.cutover_token),
    }
    idempotency_key = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    # The log must exist *before* recipe compilation.  A missing or malformed
    # snapshot is a normal post-commit inventory failure, not a queue crash
    # that disappears without a retry count or an actionable exception.  The
    # payload is derived only from the immutable sale snapshot and policy
    # identity; the canonical recipe hash plus the frozen modifier selections
    # fully determine the component vector without reading current menu data.
    payload_hash = _inventory_projection_payload_hash(order, identity)
    try:
        log_name = create_projection_log(
            source_doctype="FB Order",
            source_name=resolved_name,
            projection_type=INVENTORY_PROJECTION_TYPE,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
    except Exception as error:
        upsert_inventory_exception(
            reason_code="inventory_projection_log_failed",
            summary="Ingredient stock projection could not record its work",
            next_action="Review the inventory projection log and retry after resolving the evidence conflict",
            severity="Critical",
            company=cstr(getattr(order, "company", None)),
            warehouse=cstr(getattr(order, "booth_warehouse", None)),
            source_doctype="FB Order",
            source_name=resolved_name,
        )
        return {
            "order": resolved_name,
            "state": "Failed",
            "error": "inventory projection could not record its work; see FB Inventory Exception",
        }
    lease_token = _claim_log(log_name)
    if not lease_token:
        return {
            "order": resolved_name,
            "projection_log": log_name,
            "state": cstr(frappe.db.get_value("FB Projection Log", log_name, "state"))
            or "Pending",
        }
    try:
        # Re-run the idempotent preparation step on every delivery. It fills a
        # partially prepared order after a process crash instead of silently
        # projecting only the lines which happened to finish first.
        resolved_sales = _prepare_frozen_resolved_sales(order)
        if not resolved_sales:
            _finalize_success(log_name, lease_token, None)
            _resolve_projection_exceptions(order)
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
        _finalize_success(log_name, lease_token, target_name)
        _resolve_projection_exceptions(order)
        return {
            "order": resolved_name,
            "projection_log": log_name,
            "target_name": target_name,
            "state": "Succeeded",
        }
    except Exception as error:
        frappe.db.rollback()
        _record_failure(order, log_name, policy, error, lease_token)
        return {
            "order": resolved_name,
            "projection_log": log_name,
            "state": "Failed",
            "error": "inventory projection failed; see FB Inventory Exception",
        }


def _inventory_projection_payload_hash(order: Any, identity: dict[str, str]) -> str:
    """Hash the immutable post-cutover projection input before any worker I/O.

    A stock vector is intentionally not used here: it is produced only after
    this evidence record is durable.  The recipe hash, selected-modifier
    snapshot, and line quantity make a later vector deterministic while still
    allowing recipe-data failures to be retried and dead-lettered safely.
    """

    lines = [
        {
            "backend_line_uuid": cstr(getattr(line, "backend_line_uuid", None)),
            "commercial_modifier_snapshot_json": cstr(
                getattr(line, "commercial_modifier_snapshot_json", None)
            ),
            "item": cstr(getattr(line, "item", None)),
            "line_id": cstr(getattr(line, "line_id", None)),
            "qty": cstr(getattr(line, "qty", None)),
            "recipe": cstr(getattr(line, "recipe", None)),
            "recipe_hash": cstr(getattr(line, "recipe_hash", None)).lower(),
            "recipe_version": cstr(getattr(line, "recipe_version", None)),
        }
        for line in list(getattr(order, "items", None) or [])
    ]
    encoded = json.dumps(
        {"identity": identity, "lines": lines},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolve_projection_exceptions(order: Any) -> None:
    identity = {
        "company": cstr(getattr(order, "company", None)),
        "warehouse": cstr(getattr(order, "booth_warehouse", None)),
        "source_doctype": "FB Order",
        "source_name": cstr(getattr(order, "name", None)),
    }
    for reason_code in (
        "inventory_projection_log_failed",
        "inventory_projection_failed",
        "inventory_projection_dead_letter",
    ):
        resolve_inventory_exception(reason_code=reason_code, **identity)


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
            if _item_requires_inventory_recipe(cstr(getattr(line, "item", None))):
                raise ValueError(
                    "Order line {0} has no valid sale-time recipe snapshot for an inventory-required Item".format(
                        line_index
                    )
                )
            resolutions.append({
                "line": line,
                "line_index": line_index,
                "recipe_doc": None,
                "selected_modifiers": [],
                "resolved_components": [],
            })
            continue
        _validate_sale_time_recipe_snapshot(line, line_index)
        recipe_doc = order.resolve_recipe_for_line(line_index, line)
        selected_modifiers = order.validate_modifier_selections(line_index, line, recipe_doc)
        _replace_with_frozen_modifier_effects(
            recipe_doc=recipe_doc,
            selected_modifiers=selected_modifiers,
            line_index=line_index,
        )
        resolved_components = _compile_frozen_components(
            recipe_doc=recipe_doc,
            selected_modifiers=selected_modifiers,
            sale_qty=getattr(line, "qty", None),
            warehouse=cstr(getattr(order, "booth_warehouse", None)).strip(),
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


def _is_post_cutover_order(order: Any, policy: Any | None) -> bool:
    """Return true only when immutable sale evidence is at or after cutover.

    Inventory policies are created after historical commercial orders already
    exist.  Looking only for a current policy would silently reinterpret those
    orders and backfill ingredient movements, so malformed or pre-cutover
    evidence must remain explicitly ``Not Evaluated``.  Sale registration is
    intentionally uninvolved: this guard runs only in the asynchronous worker.
    """

    if policy is None or not cstr(getattr(policy, "cutover_token", None)).strip():
        return False
    sale_time = _as_kuala_lumpur_naive_datetime(
        getattr(order, "sale_datetime", None)
    )
    cutover_time = _as_kuala_lumpur_naive_datetime(
        getattr(policy, "cutover_at", None)
    )
    return bool(sale_time and cutover_time and sale_time >= cutover_time)


def _as_kuala_lumpur_naive_datetime(value: Any) -> datetime | None:
    """Normalize ERP timestamps without making malformed history projectable."""

    if value in (None, ""):
        return None
    candidate: Any = value
    if not isinstance(candidate, datetime):
        try:
            parser = getattr(getattr(frappe, "utils", None), "get_datetime", None)
            candidate = parser(value) if callable(parser) else datetime.fromisoformat(
                cstr(value).replace("Z", "+00:00")
            )
        except (TypeError, ValueError, OverflowError):
            return None
    if not isinstance(candidate, datetime):
        return None
    if candidate.tzinfo is not None:
        return candidate.astimezone(ZoneInfo("Asia/Kuala_Lumpur")).replace(tzinfo=None)
    return candidate


def _item_requires_inventory_recipe(item_code: str) -> bool:
    """Missing optional inventory setup must not make checkout fail.

    A configured required Item is the positive proof that a recipe snapshot is
    expected. Explicit exclusions remain sellable but never become a silent
    stock projection.
    """

    if not item_code:
        return False
    try:
        values = frappe.db.get_value(
            "Item",
            item_code,
            ["custom_fb_recipe_required", "custom_fb_inventory_excluded"],
            as_dict=True,
        ) or {}
    except Exception:
        return False
    required = cint(values.get("custom_fb_recipe_required"))
    excluded = cint(values.get("custom_fb_inventory_excluded"))
    return bool(required and not excluded)


def _validate_sale_time_recipe_snapshot(line: Any, line_index: int) -> None:
    recipe = cstr(getattr(line, "recipe", None)).strip()
    recipe_hash = cstr(getattr(line, "recipe_hash", None)).strip().lower()
    try:
        recipe_version = int(getattr(line, "recipe_version", None))
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Order line {line_index} has an invalid sale-time recipe version"
        ) from error
    if (
        not recipe
        or recipe_version <= 0
        or len(recipe_hash) != 64
        or any(character not in "0123456789abcdef" for character in recipe_hash)
    ):
        raise ValueError(
            f"Order line {line_index} has no valid sale-time recipe identity"
        )
    recipe_doc = frappe.get_cached_doc("FB Recipe", recipe)
    canonical_hash = cstr(getattr(recipe_doc, "canonical_hash", None)).strip().lower()
    if not canonical_hash or canonical_hash != recipe_hash:
        raise ValueError(
            "Order line {0} sale-time recipe hash does not match immutable recipe {1}".format(
                line_index, recipe
            )
        )
    if int(getattr(recipe_doc, "version_no", 0) or 0) != recipe_version:
        raise ValueError(
            "Order line {0} sale-time recipe version does not match immutable recipe {1}".format(
                line_index, recipe
            )
        )


def _replace_with_frozen_modifier_effects(
    *, recipe_doc: Any, selected_modifiers: list[dict[str, Any]], line_index: int
) -> None:
    effects = {
        (
            cstr(getattr(effect, "modifier_group", None)).strip(),
            cstr(getattr(effect, "modifier", None)).strip(),
        ): effect
        for effect in list(getattr(recipe_doc, "recipe_modifier_effects", None) or [])
        if cstr(getattr(effect, "modifier_group", None)).strip()
        and cstr(getattr(effect, "modifier", None)).strip()
    }
    for selected in selected_modifiers:
        row = selected["row"]
        key = (
            cstr(getattr(row, "modifier_group", None)).strip(),
            cstr(getattr(row, "modifier", None)).strip(),
        )
        effect = effects.get(key)
        if effect is None:
            raise ValueError(
                "Order line {0} modifier {1} is not frozen in recipe {2}".format(
                    line_index, key[1] or "(missing)", recipe_doc.name
                )
            )
        selected["modifier_doc"] = frappe._dict(
            {
                "name": key[1],
                "modifier_group": key[0],
                "kind": getattr(effect, "kind", None),
                "target_substitution_key": getattr(effect, "target_substitution_key", None),
                "target_item": getattr(effect, "target_item", None),
                "new_item": getattr(effect, "new_item", None),
                "qty_delta": getattr(effect, "qty_delta", None),
                "qty_delta_decimal": getattr(effect, "qty_delta_decimal", None),
                "qty_uom": getattr(effect, "qty_uom", None),
                "stock_qty_delta": getattr(effect, "stock_qty_delta", None),
                "stock_qty_delta_decimal": getattr(effect, "stock_qty_delta_decimal", None),
                "stock_uom": getattr(effect, "stock_uom", None),
                "stock_conversion_factor": getattr(effect, "stock_conversion_factor", None),
                "stock_conversion_factor_decimal": getattr(effect, "stock_conversion_factor_decimal", None),
                "scale_percent": getattr(effect, "scale_percent", None),
                "scale_percent_decimal": getattr(effect, "scale_percent_decimal", None),
                "affects_stock": getattr(effect, "affects_stock", None),
                "affects_recipe": getattr(effect, "affects_recipe", None),
            }
        )


def _compile_frozen_components(
    *, recipe_doc: Any, selected_modifiers: list[dict[str, Any]], sale_qty: Any, warehouse: str
) -> list[dict[str, Any]]:
    if not warehouse:
        raise ValueError("Inventory projection requires an outlet warehouse")
    recipe_snapshot = {
        "yield_qty": getattr(recipe_doc, "yield_qty", None),
        "yield_qty_decimal": getattr(recipe_doc, "yield_qty_decimal", None),
        "default_serving_qty": getattr(recipe_doc, "default_serving_qty", None),
        "default_serving_qty_decimal": getattr(recipe_doc, "default_serving_qty_decimal", None),
        "components": [
            {
                "item": getattr(row, "item", None),
                "qty": getattr(row, "qty", None),
                "stock_qty": getattr(row, "stock_qty", None),
                "qty_decimal": getattr(row, "qty_decimal", None),
                "stock_qty_decimal": getattr(row, "stock_qty_decimal", None),
                "stock_uom": getattr(row, "stock_uom", None),
                "stock_conversion_factor": getattr(row, "stock_conversion_factor", None),
                "stock_conversion_factor_decimal": getattr(row, "stock_conversion_factor_decimal", None),
                "loss_factor_pct": getattr(row, "loss_factor_pct", None),
                "loss_factor_pct_decimal": getattr(row, "loss_factor_pct_decimal", None),
                "affects_stock": bool(cint(getattr(row, "affects_stock", 0))),
                "substitution_key": getattr(row, "substitution_key", None),
                "name": getattr(row, "name", None),
            }
            for row in list(getattr(recipe_doc, "components", None) or [])
        ],
    }
    modifier_effects = [
        {
            "modifier": getattr(entry["modifier_doc"], "name", None),
            "kind": getattr(entry["modifier_doc"], "kind", None),
            "target_substitution_key": getattr(entry["modifier_doc"], "target_substitution_key", None),
            "target_item": getattr(entry["modifier_doc"], "target_item", None),
            "new_item": getattr(entry["modifier_doc"], "new_item", None),
            "qty_delta": getattr(entry["modifier_doc"], "qty_delta", None),
            "qty_delta_decimal": getattr(entry["modifier_doc"], "qty_delta_decimal", None),
            "stock_qty_delta": getattr(entry["modifier_doc"], "stock_qty_delta", None),
            "stock_qty_delta_decimal": getattr(entry["modifier_doc"], "stock_qty_delta_decimal", None),
            "stock_uom": getattr(entry["modifier_doc"], "stock_uom", None),
            "stock_conversion_factor": getattr(entry["modifier_doc"], "stock_conversion_factor", None),
            "stock_conversion_factor_decimal": getattr(entry["modifier_doc"], "stock_conversion_factor_decimal", None),
            "scale_percent": getattr(entry["modifier_doc"], "scale_percent", None),
            "scale_percent_decimal": getattr(entry["modifier_doc"], "scale_percent_decimal", None),
            "affects_stock": bool(cint(getattr(entry["modifier_doc"], "affects_stock", 0))),
            "affects_recipe": bool(cint(getattr(entry["modifier_doc"], "affects_recipe", 0))),
        }
        for entry in selected_modifiers
    ]
    vector = compile_recipe_vector(
        recipe_snapshot,
        servings=sale_qty,
        modifiers=modifier_effects,
    )
    if not vector:
        raise ValueError("Frozen recipe resolved to no stock components")
    return [
        {
            "item": row["item"],
            "source_type": row["source_type"],
            "qty": row["stock_qty"],
            "qty_decimal": row["stock_qty"],
            "uom": row["stock_uom"],
            "stock_qty": row["stock_qty"],
            "stock_qty_decimal": row["stock_qty"],
            "stock_uom": row["stock_uom"],
            "warehouse": warehouse,
            "source_reference": row["source_reference"],
            "affects_stock": 1,
            "affects_cogs": 1,
            "remarks": None,
        }
        for row in vector
    ]


def _claim_log(log_name: str) -> str | None:
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
    return lease_token if claimed else None


def _finalize_success(log_name: str, lease_token: str, target_name: str | None) -> None:
    """Finish only the projection attempt that still owns the durable lease."""

    now = now_datetime()
    _lock_owned_log(log_name, lease_token)
    frappe.db.sql(
        """
        UPDATE `tabFB Projection Log`
        SET state = 'Succeeded',
            target_doctype = %s,
            target_name = %s,
            last_error = NULL,
            last_attempt_at = %s,
            next_retry_at = NULL,
            dead_lettered_at = NULL,
            lease_token = NULL,
            lease_expires_at = NULL
        WHERE name = %s
        """,
        ("Stock Entry" if target_name else None, target_name, now, log_name),
    )
    frappe.db.commit()


def _record_failure(
    order: Any,
    log_name: str,
    policy: Any,
    error: Exception,
    lease_token: str,
) -> None:
    locked_log = _lock_owned_log(log_name, lease_token)
    retry_count = cint(_row_value(locked_log, "retry_count"))
    next_retry_count = retry_count + 1
    maximum = _configured_retry_limit()
    state = "Dead Letter" if next_retry_count >= maximum else "Failed"
    now = now_datetime()
    next_retry_at = (
        None
        if state == "Dead Letter"
        else add_to_date(
            now,
            seconds=min(60 * 60, 60 * (2 ** max(0, next_retry_count - 1))),
        )
    )
    frappe.db.sql(
        """
        UPDATE `tabFB Projection Log`
        SET state = %s,
            retry_count = %s,
            target_doctype = NULL,
            target_name = NULL,
            last_error = %s,
            last_attempt_at = %s,
            next_retry_at = %s,
            dead_lettered_at = %s,
            lease_token = NULL,
            lease_expires_at = NULL
        WHERE name = %s
        """,
        (
            state,
            next_retry_count,
            str(error)[:1000],
            now,
            next_retry_at,
            now if state == "Dead Letter" else None,
            log_name,
        ),
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
    if state == "Dead Letter":
        _pause_policy_for_integrity_failure(policy)
    frappe.db.commit()


def _pause_policy_for_integrity_failure(policy: Any) -> None:
    """Contain a warehouse after its projection retry budget is exhausted.

    The policy already owns the automation lifecycle.  The open critical
    inventory exception remains the reason authority surfaced by health, so a
    second mutable pause-reason record is deliberately not introduced.
    """

    policy_name = cstr(getattr(policy, "name", None)).strip()
    if not policy_name or cstr(getattr(policy, "automation_state", None)).strip() == "Paused":
        return
    frappe.db.set_value(
        "FB Inventory Policy",
        policy_name,
        "automation_state",
        "Paused",
        update_modified=False,
    )
    policy.automation_state = "Paused"


def _lock_owned_log(log_name: str, lease_token: str) -> Any:
    """Lock and verify ownership before a terminal inventory-log mutation."""

    rows = frappe.db.sql(
        """
        SELECT name, state, retry_count, lease_token, lease_expires_at
        FROM `tabFB Projection Log`
        WHERE name = %s
        LIMIT 1
        FOR UPDATE
        """,
        (log_name,),
        as_dict=True,
    ) or []
    if not rows or cstr(_row_value(rows[0], "lease_token")) != lease_token:
        frappe.db.rollback()
        raise RuntimeError(
            "Inventory projection lease was lost while finalizing {0}".format(log_name)
        )
    return rows[0]


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, dict):
        return row.get(fieldname)
    getter = getattr(row, "get", None)
    if callable(getter):
        return getter(fieldname)
    return getattr(row, fieldname, None)


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
