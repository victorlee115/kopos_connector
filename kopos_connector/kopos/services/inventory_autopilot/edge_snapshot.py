"""Bounded, warehouse-scoped operational data for a KoPOS device.

The catalog and the standard ERPNext stock documents remain the authorities.
This module only derives a small response at request time.  It deliberately
does not persist a device snapshot, copy valuation data, or calculate a second
stock ledger.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import cint, cstr, get_datetime, now_datetime

from kopos_connector.kopos.services.inventory_autopilot.holds import (
    active_holds,
    choose_availability,
)
from kopos_connector.kopos.services.inventory_autopilot.availability_capacity import (
    target_capacity,
)


EDGE_SCHEMA_VERSION = "inventory-edge-v1"
DEFAULT_ITEM_LIMIT = 50
MAX_ITEM_LIMIT = 100
MAX_SEARCH_LENGTH = 80
MAX_HOLDS_PER_TARGET = 20

_AVAILABILITY_MODES = {"Off", "Warn", "Ask Manager", "Auto Pause & Restore"}


def normalize_edge_query(search: Any = None, limit: Any = None) -> tuple[str, int]:
    """Validate the small query surface exposed by ``get_edge_snapshot``."""

    normalized_search = cstr(search).strip()
    if len(normalized_search) > MAX_SEARCH_LENGTH:
        frappe.throw(
            _("Stock search must be {0} characters or fewer").format(MAX_SEARCH_LENGTH),
            frappe.ValidationError,
        )
    if limit in (None, ""):
        return normalized_search, DEFAULT_ITEM_LIMIT
    try:
        parsed_limit = int(str(limit))
    except (TypeError, ValueError) as error:
        frappe.throw(_("Stock result limit must be a whole number"), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if isinstance(limit, bool) or parsed_limit < 1 or parsed_limit > MAX_ITEM_LIMIT:
        frappe.throw(
            _("Stock result limit must be between 1 and {0}").format(MAX_ITEM_LIMIT),
            frappe.ValidationError,
        )
    return normalized_search, parsed_limit


def build_edge_inventory_snapshot(
    *,
    company: str,
    warehouse: str,
    search: Any = None,
    limit: Any = None,
    now: Any | None = None,
    catalog_item_ids: set[str] | None = None,
    catalog_modifier_ids: set[str] | None = None,
    pos_profile: str | None = None,
) -> dict[str, Any]:
    """Build one device-safe operational stock read model.

    ``company`` and ``warehouse`` must already have been resolved from the
    authenticated device's POS Profile.  The function intentionally accepts no
    caller-selected warehouse and never writes a snapshot or a health row.
    """

    resolved_company = cstr(company).strip()
    resolved_warehouse = cstr(warehouse).strip()
    if not resolved_company or not resolved_warehouse:
        frappe.throw(
            _("The authenticated device has no company and warehouse binding"),
            frappe.ValidationError,
        )
    normalized_search, result_limit = normalize_edge_query(search, limit)
    current_time = now or now_datetime()
    policy = _policy(resolved_company, resolved_warehouse)
    max_age_minutes = _max_source_age(policy)
    bin_rows: dict[str, dict[str, Any]] = {}
    modifier_rows = _load_modifier_rows(normalized_search, result_limit)
    modifier_stock_codes = {
        cstr(row.get("new_item") or row.get("target_item")).strip()
        for row in modifier_rows
        if cstr(row.get("new_item") or row.get("target_item")).strip()
    }
    item_rows, item_truncated = _load_item_rows(
        normalized_search,
        result_limit,
        extra_codes=modifier_stock_codes | set(catalog_item_ids or set()),
    )
    item_codes = {
        cstr(row.get("name")).strip()
        for row in item_rows
        if cstr(row.get("name")).strip()
    }
    item_codes.update(modifier_stock_codes)
    bin_rows.update(_load_bin_rows(resolved_warehouse, item_codes=item_codes))
    rule_map = _load_rule_map(
        resolved_company,
        resolved_warehouse,
        item_codes=item_codes,
        modifier_ids={cstr(row.get("name")).strip() for row in modifier_rows},
    )
    item_result: list[dict[str, Any]] = []
    for row in item_rows[:result_limit]:
        item_code = cstr(row.get("name")).strip()
        item_result.append(
            _operational_target(
                target_type="Item",
                target_id=item_code,
                label=cstr(row.get("item_name")).strip() or item_code,
                stock_item=item_code,
                active=not cint(row.get("disabled")),
                is_stock_item=bool(cint(row.get("is_stock_item"))),
                stock_uom=cstr(row.get("stock_uom")).strip(),
                bin_row=bin_rows.get(item_code),
                rule_mode=rule_map.get(("Item", item_code), "Off"),
                is_catalog_target=item_code in (catalog_item_ids or set()),
                company=resolved_company,
                warehouse=resolved_warehouse,
                policy=policy,
                max_age_minutes=max_age_minutes,
                current_time=current_time,
                pos_profile=pos_profile,
            )
        )
        item_result[-1].update({"item_code": item_code, "item_name": item_result[-1]["label"]})

    item_by_code = {
        cstr(row.get("name")).strip(): row
        for row in item_rows
        if cstr(row.get("name")).strip()
    }
    modifier_result: list[dict[str, Any]] = []
    for row in modifier_rows[:result_limit]:
        modifier_id = cstr(row.get("name")).strip()
        stock_item = cstr(row.get("new_item") or row.get("target_item")).strip()
        affects_stock = bool(cint(row.get("affects_stock")))
        stock_row = item_by_code.get(stock_item, {})
        target = _operational_target(
            target_type="Modifier",
            target_id=modifier_id,
            label=cstr(row.get("modifier_name")).strip() or modifier_id,
            stock_item=stock_item,
            active=bool(cint(row.get("active", 1))),
            is_stock_item=True,
            stock_uom=cstr(stock_row.get("stock_uom")).strip(),
            bin_row=bin_rows.get(stock_item),
            rule_mode=rule_map.get(("Modifier", modifier_id), "Off"),
            is_catalog_target=modifier_id in (catalog_modifier_ids or set()),
            company=resolved_company,
            warehouse=resolved_warehouse,
            policy=policy,
            max_age_minutes=max_age_minutes,
            current_time=current_time,
            pos_profile=pos_profile,
        )
        target.update(
            {
                "modifier_id": modifier_id,
                "modifier_name": target["label"],
                "modifier_group": cstr(row.get("modifier_group")).strip(),
                "affects_stock": affects_stock,
            }
        )
        if not stock_item:
            if affects_stock:
                target.update(
                    {
                        "actual_qty": None,
                        "usable_qty": None,
                        "incoming_qty": None,
                        "stock_uom": None,
                        "freshness": "stale",
                        "reliability": "Not ready",
                        "reliability_reason": "This modifier has no stocked Item mapping",
                        "availability": "warning" if target["availability"] == "available" else target["availability"],
                        "runout": None,
                        "runout_reason": "Runout is unavailable until the modifier is mapped to a stocked Item",
                    }
                )
            else:
                instruction_ready = bool(
                    policy
                    and cstr(policy.get("cutover_token")).strip()
                    and policy.get("cutover_at")
                )
                target.update(
                    {
                        "actual_qty": None,
                        "usable_qty": None,
                        "incoming_qty": None,
                        "stock_uom": None,
                        "freshness": "current" if instruction_ready else "stale",
                        "reliability": "Reliable" if instruction_ready else "Not ready",
                        "reliability_reason": (
                            "This modifier is instruction-only and does not consume stock"
                            if instruction_ready
                            else target["reliability_reason"]
                        ),
                        "runout": None,
                        "runout_reason": "Runout does not apply to an instruction-only modifier",
                    }
                )
        modifier_result.append(target)

    forecast_map, forecast_reason = _reliable_forecasts(
        resolved_company,
        resolved_warehouse,
        policy=policy,
        now=current_time,
        max_age_minutes=max_age_minutes,
    )
    for target in [*item_result, *modifier_result]:
        _attach_runout(
            target,
            forecast_map=forecast_map,
            forecast_reason=forecast_reason,
        )

    global_freshness, global_reliability, global_reason = _overall_reliability(
        policy=policy,
        item_targets=[*item_result, *modifier_result],
        warehouse=resolved_warehouse,
    )
    return {
        "schema_version": EDGE_SCHEMA_VERSION,
        "status": "ok",
        "company": resolved_company,
        "warehouse": resolved_warehouse,
        "automation_state": cstr((policy or {}).get("automation_state")).strip() or None,
        "generated_at": _iso_with_offset(current_time),
        "search": normalized_search or None,
        "limit": result_limit,
        "freshness": global_freshness,
        "reliability": global_reliability,
        "reliability_reason": global_reason,
        "items": item_result,
        "modifier_options": modifier_result,
        "truncated": {
            "items": item_truncated,
            "modifier_options": len(modifier_rows) > result_limit,
        },
        "tasks": [],
        "count_task": None,
    }


def attach_bounded_tasks(
    snapshot: dict[str, Any],
    task_response: Mapping[str, Any] | None,
    *,
    max_tasks: int = 100,
    max_lines_per_task: int = 100,
) -> dict[str, Any]:
    """Attach only the fixed, operational task vocabulary to a snapshot."""

    raw_tasks = task_response.get("tasks", []) if isinstance(task_response, Mapping) else []
    if not isinstance(raw_tasks, list):
        raw_tasks = []
    snapshot["tasks"] = [
        _safe_task(task, max_lines=max_lines_per_task)
        for task in raw_tasks[:max_tasks]
        if isinstance(task, Mapping) and cstr(task.get("kind")).strip()
        in {"count", "preparation", "receiving", "transfer_dispatch", "transfer_receipt"}
    ]
    snapshot["tasks_truncated"] = len(raw_tasks) > max_tasks
    raw_count_task = task_response.get("count_task") if isinstance(task_response, Mapping) else None
    if isinstance(raw_count_task, Mapping):
        snapshot["count_task"] = _safe_count_task(
            raw_count_task,
            max_lines=max_lines_per_task,
        )
    else:
        # Keep the edge contract compatible with older task readers while the
        # server and clients roll out together.  The count task is duplicated
        # in ``tasks`` for the task list, but the dedicated field is the
        # authority consumed by the count screen and offline cache.
        count_task = next(
            (
                task for task in snapshot["tasks"]
                if isinstance(task, Mapping) and task.get("kind") == "count"
            ),
            None,
        )
        snapshot["count_task"] = _safe_count_task(
            {
                "name": count_task.get("document"),
                "revision": count_task.get("revision"),
                "warehouse": count_task.get("warehouse"),
                "assignee": count_task.get("assignee"),
                "stock_watermark": count_task.get("stock_watermark"),
                "lines": count_task.get("lines", []),
            },
            max_lines=max_lines_per_task,
        ) if count_task else None
    return snapshot


def _safe_task(task: Mapping[str, Any], *, max_lines: int) -> dict[str, Any]:
    allowed = {
        "kind", "document", "title", "revision", "stock_watermark", "assignee", "warehouse", "status", "docstatus",
        "item_code", "item_name", "qty", "produced_qty", "bom_no",
        "preparation_instructions", "blocked_reason", "preparation_alert", "preparation_fingerprint",
        "batch_qty", "min_ready_qty", "trigger_qty", "current_qty", "lead_minutes",
    }
    result = {key: _safe_value(task.get(key)) for key in allowed if key in task}
    lines = task.get("lines")
    if isinstance(lines, list):
        result["lines"] = [
            _safe_task_line(line)
            for line in lines[:max_lines]
            if isinstance(line, Mapping)
        ]
        if len(lines) > max_lines:
            result["lines_truncated"] = True
    else:
        result["lines"] = []
    # Supplier names, rates, value thresholds, and other fields not in the
    # allow-list are intentionally omitted at this device boundary.
    return result


def _safe_count_task(task: Mapping[str, Any], *, max_lines: int) -> dict[str, Any]:
    """Bound the dedicated count assignment without exposing expected stock."""

    allowed = {"name", "revision", "warehouse", "assignee", "stock_watermark"}
    result = {key: _safe_value(task.get(key)) for key in allowed if key in task}
    lines = task.get("lines")
    if isinstance(lines, list):
        result["lines"] = [
            _safe_count_task_line(line)
            for line in lines[:max_lines]
            if isinstance(line, Mapping)
        ]
        if len(lines) > max_lines:
            result["lines_truncated"] = True
    else:
        result["lines"] = []
    review = task.get("review")
    if isinstance(review, Mapping):
        review_result = {
            key: _safe_value(review.get(key))
            for key in {"observation_id", "status"}
            if key in review
        }
        review_lines = review.get("lines")
        if isinstance(review_lines, list):
            review_result["lines"] = [
                _safe_count_review_line(line)
                for line in review_lines[:max_lines]
                if isinstance(line, Mapping)
            ]
        result["review"] = review_result
    return result


def _safe_count_task_line(line: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "item_id", "item_name", "uom", "stock_uom", "purchase_uom", "conversion_factor",
    }
    return {key: _safe_value(line.get(key)) for key in allowed if key in line}


def _safe_count_review_line(line: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "item_id", "item_name", "uom", "counted_quantity", "variance_percent",
        "stock_uom", "purchase_uom", "conversion_factor", "full_packs",
        "loose_quantity", "total_quantity",
    }
    return {key: _safe_value(line.get(key)) for key in allowed if key in line}


def _safe_task_line(line: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "item_id", "item_code", "item_name", "qty", "remaining_qty", "requested_qty", "dispatched_qty", "received_qty",
        "stock_qty", "stock_dispatched_qty", "stock_received_qty",
        "uom", "warehouse", "source_warehouse", "destination_warehouse", "transit_warehouse",
        "stock_uom", "purchase_uom", "conversion_factor", "purchase_order_item", "material_request_item",
    }
    return {key: _safe_value(line.get(key)) for key in allowed if key in line}


def _safe_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_string(value)
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, Mapping):
        return {cstr(key): _safe_value(item) for key, item in value.items()}
    return value


def _load_item_rows(
    search: str,
    limit: int,
    *,
    extra_codes: set[str],
) -> tuple[list[dict[str, Any]], bool]:
    if not frappe.db.exists("DocType", "Item"):
        return [], False
    filters: dict[str, Any] = {"is_stock_item": 1, "disabled": 0}
    or_filters = None
    if search:
        pattern = f"%{search.replace('%', '').replace('_', '')}%"
        or_filters = [{"name": ["like", pattern]}, {"item_name": ["like", pattern]}]
    query_kwargs: dict[str, Any] = {
        "filters": filters,
        "fields": ["name", "item_name", "stock_uom", "is_stock_item", "disabled"],
        "order_by": "item_name asc, name asc",
        "limit_page_length": limit + 1,
    }
    if or_filters:
        query_kwargs["or_filters"] = or_filters
    rows = frappe.get_all("Item", **query_kwargs)
    normalized = [dict(row) for row in rows if cstr(row.get("name")).strip()]
    selected_codes = {
        cstr(row.get("name")).strip()
        for row in normalized
        if cstr(row.get("name")).strip()
    }
    missing_extra_codes = sorted(extra_codes - selected_codes)
    if missing_extra_codes:
        extra_rows = frappe.get_all(
            "Item",
            filters={"name": ["in", missing_extra_codes], "disabled": 0},
            fields=["name", "item_name", "stock_uom", "is_stock_item", "disabled"],
            limit_page_length=len(missing_extra_codes),
        )
        normalized.extend(dict(row) for row in extra_rows if cstr(row.get("name")).strip())
    normalized.sort(key=lambda row: (cstr(row.get("item_name")).lower(), cstr(row.get("name"))))
    return normalized, len(rows) > limit


def _load_bin_rows(warehouse: str, *, item_codes: set[str] | None = None) -> dict[str, dict[str, Any]]:
    if not frappe.db.exists("DocType", "Bin"):
        return {}
    meta = frappe.get_meta("Bin")
    fields = [
        fieldname
        for fieldname in ("item_code", "actual_qty", "reserved_qty", "ordered_qty", "indented_qty", "modified")
        if fieldname == "item_code" or meta.has_field(fieldname)
    ]
    filters: dict[str, Any] = {"warehouse": warehouse}
    if item_codes:
        filters["item_code"] = ["in", sorted(item_codes)]
    rows = frappe.get_all(
        "Bin",
        filters=filters,
        fields=fields,
        limit_page_length=max(MAX_ITEM_LIMIT * 2, len(item_codes or set())),
    )
    return {
        cstr(row.get("item_code")).strip(): dict(row)
        for row in rows
        if cstr(row.get("item_code")).strip()
    }


def _load_modifier_rows(search: str, limit: int) -> list[dict[str, Any]]:
    if not frappe.db.exists("DocType", "FB Modifier") or not frappe.db.exists("DocType", "FB Modifier Group"):
        return []
    groups = frappe.get_all(
        "FB Modifier Group",
        filters={"active": 1},
        fields=["name"],
        limit_page_length=MAX_ITEM_LIMIT * 10,
    )
    group_ids = [cstr(row.get("name")).strip() for row in groups if cstr(row.get("name")).strip()]
    if not group_ids:
        return []
    rows = frappe.get_all(
        "FB Modifier",
        filters={"active": 1, "modifier_group": ["in", group_ids]},
        fields=["name", "modifier_group", "modifier_name", "target_item", "new_item", "affects_stock", "active"],
        order_by="modifier_group asc, modifier_name asc, name asc",
        limit_page_length=MAX_ITEM_LIMIT * 5,
    )
    if search:
        folded = search.casefold()
        rows = [
            row
            for row in rows
            if folded in " ".join(
                cstr(row.get(field)).casefold()
                for field in ("name", "modifier_name", "target_item", "new_item")
            )
        ]
    return [dict(row) for row in rows[: limit + 1]]


def _load_rule_map(
    company: str,
    warehouse: str,
    *,
    item_codes: set[str],
    modifier_ids: set[str],
) -> dict[tuple[str, str], str]:
    if not frappe.db.exists("DocType", "FB Inventory Availability Rule"):
        return {}
    target_ids = sorted(item_codes | modifier_ids)
    if not target_ids:
        return {}
    rows = frappe.get_all(
        "FB Inventory Availability Rule",
        filters={
            "company": company,
            "warehouse": warehouse,
            "target_id": ["in", target_ids],
        },
        fields=["target_type", "target_id", "mode"],
        limit_page_length=MAX_ITEM_LIMIT * 2,
    )
    return {
        (cstr(row.get("target_type")).strip(), cstr(row.get("target_id")).strip()): (
            cstr(row.get("mode")).strip() if cstr(row.get("mode")).strip() in _AVAILABILITY_MODES else "Off"
        )
        for row in rows
        if cstr(row.get("target_type")).strip() and cstr(row.get("target_id")).strip()
    }


def _operational_target(
    *,
    target_type: str,
    target_id: str,
    label: str,
    stock_item: str,
    active: bool,
    is_stock_item: bool,
    stock_uom: str,
    bin_row: Mapping[str, Any] | None,
    rule_mode: str,
    is_catalog_target: bool,
    company: str,
    warehouse: str,
    policy: Mapping[str, Any] | None,
    max_age_minutes: int,
    current_time: Any,
    pos_profile: str | None,
) -> dict[str, Any]:
    raw_actual = _decimal_value((bin_row or {}).get("actual_qty"))
    raw_reserved = _decimal_value((bin_row or {}).get("reserved_qty"))
    raw_ordered = _decimal_value((bin_row or {}).get("ordered_qty"))
    raw_indented = _decimal_value((bin_row or {}).get("indented_qty"))
    actual = raw_actual or Decimal("0")
    reserved = max(raw_reserved or Decimal("0"), Decimal("0"))
    incoming = max((raw_ordered or Decimal("0")) + (raw_indented or Decimal("0")), Decimal("0"))
    usable = max(actual - reserved, Decimal("0"))
    capacity = (
        target_capacity(
            target_type=target_type,
            target_id=target_id,
            company=company,
            warehouse=warehouse,
            at_time=current_time,
        )
        if is_catalog_target
        else None
    )
    # ``Bin.modified`` is the last stock movement, not the age of this read.
    # A quiet but valid Item must not become stale merely because nobody moved
    # it during the freshness window.  The query itself is the source read;
    # the request timestamp is emitted as ``generated_at``.  Keep the movement
    # timestamp as context only, without using it as a source-age gate.
    modified = (bin_row or {}).get("modified")
    freshness = "current" if bin_row is not None else "stale"
    holds = _safe_holds(
        target_type=target_type,
        target_id=target_id,
        warehouse=warehouse,
        pos_profile=pos_profile,
    )
    warning = rule_mode in {"Warn", "Ask Manager", "Auto Pause & Restore"} and (
        (
            capacity is not None
            and (
                not capacity.reliable
                or (capacity.capacity is not None and capacity.capacity <= Decimal("0"))
            )
        )
        if is_catalog_target
        else usable <= Decimal("0")
    )
    availability = choose_availability(
        commercially_enabled=active,
        holds=holds,
        warning=warning,
    )
    reasons: list[str] = []
    reliability = "Reliable"
    reliability_reason = "Current ERPNext stock evidence is available"
    if not policy:
        reliability = "Not ready"
        reliability_reason = "Inventory policy is not configured for this warehouse"
    elif not cstr(policy.get("cutover_token")).strip() or not policy.get("cutover_at"):
        reliability = "Not ready"
        reliability_reason = "Inventory cutover is not active for this warehouse"
    elif not is_catalog_target and not stock_item:
        reliability = "Not ready"
        reliability_reason = "This target has no stocked Item mapping"
    elif (not is_catalog_target or is_stock_item) and not stock_uom:
        reliability = "Not ready"
        reliability_reason = "The stocked Item has no stock UOM"
    elif (not is_catalog_target or is_stock_item) and bin_row is None:
        reliability = "Not ready"
        reliability_reason = "No ERPNext warehouse stock record exists yet"
    elif freshness == "stale":
        reliability = "Please check"
        reliability_reason = "The warehouse stock evidence is older than the configured freshness limit"
    elif any(bool(hold.get("stale")) for hold in holds):
        freshness = "stale"
        reliability = "Please check"
        reliability_reason = "An automation hold needs a fresh stock check"
    if is_catalog_target and capacity is not None and not capacity.reliable:
        freshness = "stale"
        reliability = "Not ready"
        reliability_reason = capacity.reason
    elif is_catalog_target and capacity is not None and capacity.reliable:
        freshness = "current"
        reliability = "Reliable"
        reliability_reason = capacity.reason
    if raw_actual is None and not (is_catalog_target and capacity is not None and capacity.reliable):
        reliability = "Not ready"
        reliability_reason = "ERPNext did not return complete stock quantities"
    if warning:
        reasons.append("Stock is at or below the usable quantity threshold")
    if is_catalog_target and capacity is not None and not capacity.reliable:
        reasons.append(capacity.reason)
    if any(bool(hold.get("stale")) for hold in holds):
        reasons.append("Stock check overdue; selling remains held until evidence is refreshed")
    return {
        "target_id": target_id,
        "label": label,
        "is_catalog_target": bool(is_catalog_target),
        "stock_item": stock_item if (not is_catalog_target or is_stock_item) and stock_item else None,
        "stock_uom": stock_uom if (not is_catalog_target or is_stock_item) and stock_uom else None,
        "actual_qty": _decimal_string(actual) if ((not is_catalog_target or is_stock_item) and stock_item) else None,
        "usable_qty": _decimal_string(usable) if ((not is_catalog_target or is_stock_item) and stock_item) else None,
        "incoming_qty": _decimal_string(incoming) if ((not is_catalog_target or is_stock_item) and stock_item) else None,
        "sellable_capacity": (
            _decimal_string(capacity.capacity)
            if capacity is not None and capacity.reliable and capacity.capacity is not None
            else None
        ),
        "last_stock_movement_at": _iso_with_offset(modified) if modified else None,
        "availability": availability,
        "freshness": freshness,
        "reliability": reliability,
        "reliability_reason": reliability_reason,
        "holds": holds,
        "reasons": reasons,
        "runout": None,
        "runout_reason": "Runout is available only after a current reliable forecast is proven",
    }


def _safe_holds(
    *, target_type: str, target_id: str, warehouse: str, pos_profile: str | None = None
) -> list[dict[str, Any]]:
    if not target_id or not frappe.db.exists("DocType", "FB Availability Hold"):
        return []
    rows = active_holds(target_type=target_type, target_id=target_id, warehouse=warehouse)
    return [
        {
            "hold_id": cstr(row.get("name")),
            "source": cstr(row.get("source")),
            "reason_code": cstr(row.get("reason_code")),
            "reason_label": cstr(row.get("reason_label")),
            "expires_at": _iso_with_offset(row.get("expires_at")) if row.get("expires_at") else None,
            "stale": bool(row.get("stale")),
            "manager_owned": (
                cstr(row.get("source")).strip() == "manual"
                and cstr(row.get("reason_code")).strip() == "manual_manager_pause"
                and bool(cstr(pos_profile).strip())
                and cstr(row.get("pos_profile")).strip() == cstr(pos_profile).strip()
            ),
        }
        for row in rows[:MAX_HOLDS_PER_TARGET]
    ]


def _policy(company: str, warehouse: str) -> dict[str, Any] | None:
    if not frappe.db.exists("DocType", "FB Inventory Policy"):
        return None
    rows = frappe.get_all(
        "FB Inventory Policy",
        filters={"company": company, "warehouse": warehouse},
        fields=["name", "automation_state", "max_source_age_minutes", "cutover_token", "cutover_at"],
        order_by="modified desc",
        limit_page_length=1,
    )
    return dict(rows[0]) if rows else None


def _max_source_age(policy: Mapping[str, Any] | None) -> int:
    try:
        return max(1, int((policy or {}).get("max_source_age_minutes") or 30))
    except (TypeError, ValueError):
        return 30


def _reliable_forecasts(
    company: str,
    warehouse: str,
    *,
    policy: Mapping[str, Any] | None,
    now: Any,
    max_age_minutes: int,
) -> tuple[dict[str, Decimal], str]:
    if not policy or not cstr(policy.get("cutover_token")).strip() or not policy.get("cutover_at"):
        return {}, "Runout is unavailable until this warehouse has an active inventory cutover"
    if _critical_exception_exists(warehouse):
        return {}, "Runout is unavailable while a critical inventory exception is open"
    if _projection_backlog_exists(warehouse):
        return {}, "Runout is unavailable while ingredient projection work is pending"
    if not frappe.db.exists("DocType", "FB Inventory Plan"):
        return {}, "Runout is unavailable because no inventory plan is installed"
    rows = frappe.get_all(
        "FB Inventory Plan",
        filters={
            "company": company,
            "warehouse": warehouse,
            "forecast_state": "Reliable",
            "status": ["in", ["Ready", "Executed"]],
        },
        fields=["name", "modified", "forecast_evidence"],
        order_by="modified desc",
        limit_page_length=1,
    )
    if not rows:
        return {}, "Runout is unavailable until a reliable forecast is available"
    age = _source_age_minutes(rows[0].get("modified"), now)
    if age is None or age > max_age_minutes:
        return {}, "Runout is unavailable because the reliable forecast is stale"
    raw_evidence = rows[0].get("forecast_evidence")
    try:
        evidence = json.loads(raw_evidence) if isinstance(raw_evidence, str) else raw_evidence
    except (TypeError, ValueError):
        evidence = None
    if not isinstance(evidence, Mapping) or not isinstance(evidence.get("items"), list):
        return {}, "Runout is unavailable because forecast arithmetic is incomplete"
    forecast: dict[str, Decimal] = {}
    for row in evidence["items"]:
        if not isinstance(row, Mapping) or cstr(row.get("forecast_state")).strip() != "Reliable":
            continue
        item = cstr(row.get("item")).strip()
        value = _decimal_value(row.get("forecast"))
        if item and value is not None and value > Decimal("0"):
            forecast[item] = value
    if not forecast:
        return {}, "Runout is unavailable because no Item has reliable positive demand"
    return forecast, ""


def _attach_runout(
    target: dict[str, Any],
    *,
    forecast_map: Mapping[str, Decimal],
    forecast_reason: str,
) -> None:
    item = cstr(target.get("stock_item")).strip()
    if not item or target.get("reliability") != "Reliable":
        target["runout"] = None
        target["runout_reason"] = target.get("reliability_reason") or forecast_reason
        return
    demand = forecast_map.get(item)
    if demand is None:
        target["runout"] = None
        target["runout_reason"] = forecast_reason or (
            "Runout is unavailable for this Item until its forecast is reliable"
        )
        return
    usable = _decimal_value(target.get("usable_qty"))
    if usable is None:
        target["runout"] = None
        target["runout_reason"] = "Runout is unavailable because usable stock is incomplete"
        return
    target["runout"] = {
        "days": _decimal_string(usable / demand),
        "basis": "latest reliable post-cutover forecast",
    }
    target["runout_reason"] = None


def _overall_reliability(
    *,
    policy: Mapping[str, Any] | None,
    item_targets: list[dict[str, Any]],
    warehouse: str,
) -> tuple[str, str, str]:
    if not policy:
        return "stale", "Not ready", "Inventory policy is not configured for this warehouse"
    if not cstr(policy.get("cutover_token")).strip() or not policy.get("cutover_at"):
        return "current", "Not ready", "Inventory cutover is not active for this warehouse"
    if not item_targets:
        return "stale", "Not ready", "No stock Items are configured for this warehouse"
    if cstr(policy.get("automation_state")).strip() == "Paused":
        return "current", "Please check", "Inventory automation is paused for this warehouse"
    if _critical_exception_exists(warehouse):
        return "stale", "Please check", "A critical inventory exception requires attention"
    if _projection_backlog_exists(warehouse):
        return "stale", "Please check", "Inventory projection work is still pending"
    if any(target.get("freshness") == "stale" for target in item_targets):
        return "stale", "Please check", "One or more stock sources are older than the freshness limit"
    if any(target.get("reliability") == "Not ready" for target in item_targets):
        return "current", "Not ready", "One or more stock targets are missing required setup"
    return "current", "Reliable", "Current warehouse stock evidence is available"


def _critical_exception_exists(warehouse: str) -> bool:
    return bool(
        frappe.db.exists(
            "FB Inventory Exception",
            {"warehouse": warehouse, "status": "Open", "severity": "Critical"},
        )
    ) if frappe.db.exists("DocType", "FB Inventory Exception") else False


def _projection_backlog_exists(warehouse: str) -> bool:
    if not frappe.db.exists("DocType", "FB Projection Log") or not frappe.db.exists("DocType", "FB Order"):
        return False
    rows = frappe.db.sql(
        """
        SELECT pl.name
        FROM `tabFB Projection Log` pl
        INNER JOIN `tabFB Order` o ON o.name = pl.source_name
        WHERE pl.projection_type = 'Stock Issue'
          AND pl.source_doctype = 'FB Order'
          AND o.booth_warehouse = %s
          AND pl.state IN ('Pending', 'Processing', 'Failed', 'Dead Letter')
        LIMIT 1
        """,
        (warehouse,),
    )
    return bool(rows)


def _decimal_value(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _decimal_string(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized else "0"


def _source_age_minutes(value: Any, now: Any) -> int | None:
    if not value:
        return None
    try:
        observed = get_datetime(value)
        observed_tz = getattr(observed, "tzinfo", None)
        now_tz = getattr(now, "tzinfo", None)
        if observed_tz is not None and now_tz is None:
            now = now.replace(tzinfo=observed_tz)
        elif observed_tz is None and now_tz is not None:
            observed = observed.replace(tzinfo=now_tz)
        return max(0, int((now - observed).total_seconds() // 60))
    except (TypeError, ValueError, OverflowError):
        return None


def _iso_with_offset(value: Any) -> str:
    current = get_datetime(value) if isinstance(value, str) else value
    if getattr(current, "tzinfo", None) is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
    return current.isoformat()
