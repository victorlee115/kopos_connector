"""Derive the physical stock an outlet needs from its active menu.

Nothing is persisted here. Recipes, current stock balances, availability rules,
and unfinished stock work remain the authorities; this module only resolves
their current union for the Stock, count, waste, and planning workflows.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

import frappe
from frappe.utils import cstr, now_datetime

from kopos_connector.kopos.services.inventory_autopilot.availability_capacity import (
    active_recipe_tracking_items,
)


MAX_TRACKING_CANDIDATES = 2000
OPEN_COUNT_STATES = ("Assigned", "Claimed", "In Progress", "Submitted", "Review")
OPEN_PLAN_STATES = ("Review First", "Ready", "Blocked")


@dataclass(frozen=True)
class TrackingScope:
    item_codes: frozenset[str]
    recipe_items: frozenset[str]
    residual_items: frozenset[str]
    operational_items: frozenset[str]


def merge_tracking_sources(*sources: Iterable[str]) -> frozenset[str]:
    return frozenset(
        cstr(item).strip()
        for source in sources
        for item in source
        if cstr(item).strip()
    )


def resolve_tracking_scope(
    *,
    company: str,
    warehouse: str,
    catalog_item_ids: set[str] | None,
    at_time: Any | None = None,
) -> TrackingScope:
    """Resolve physical products, ingredients, supplies, and prepared stock.

    ``catalog_item_ids=None`` means the caller cannot supply an outlet profile;
    current recipes for the café are then used as a safe fallback. An explicit
    empty set means the outlet menu is empty and must not inherit another
    outlet's products.
    """

    resolved_company = cstr(company).strip()
    resolved_warehouse = cstr(warehouse).strip()
    current_time = at_time or now_datetime()
    roots = (
        _current_recipe_products(resolved_company)
        if catalog_item_ids is None
        else {cstr(item).strip() for item in catalog_item_ids if cstr(item).strip()}
    )
    recipe_products = _current_recipe_products(resolved_company)
    recipe_items: set[str] = set()
    queue = sorted(roots & recipe_products)
    visited: set[str] = set()
    while queue:
        product = queue.pop(0)
        if product in visited:
            continue
        visited.add(product)
        components = active_recipe_tracking_items(
            item=product,
            company=resolved_company,
            at_time=current_time,
        )
        recipe_items.update(components)
        if len(recipe_items) > MAX_TRACKING_CANDIDATES:
            raise RuntimeError("The active menu references too many stock items to show safely")
        queue.extend(sorted((components & recipe_products) - visited - set(queue)))

    direct_items = _stock_items(roots)
    residual_items = _residual_bin_items(resolved_warehouse)
    operational_items = merge_tracking_sources(
        _availability_rule_items(resolved_company, resolved_warehouse),
        _open_count_items(resolved_warehouse),
        _open_plan_items(resolved_company, resolved_warehouse),
    )
    candidates = merge_tracking_sources(
        direct_items,
        recipe_items,
        residual_items,
        operational_items,
    )
    physical = _stock_items(candidates)
    return TrackingScope(
        item_codes=frozenset(physical),
        recipe_items=frozenset(recipe_items & physical),
        residual_items=frozenset(residual_items & physical),
        operational_items=frozenset(operational_items & physical),
    )


def _current_recipe_products(company: str) -> set[str]:
    if not company or not frappe.db.exists("DocType", "FB Recipe"):
        return set()
    rows = frappe.get_all(
        "FB Recipe",
        filters={"company": company, "status": "Active"},
        fields=["sellable_item"],
        limit_page_length=MAX_TRACKING_CANDIDATES + 1,
    )
    _require_bounded(rows, "active recipes")
    return {
        cstr(row.get("sellable_item")).strip()
        for row in rows
        if cstr(row.get("sellable_item")).strip()
    }


def _stock_items(item_codes: Iterable[str]) -> set[str]:
    selected = sorted({cstr(item).strip() for item in item_codes if cstr(item).strip()})
    if not selected or not frappe.db.exists("DocType", "Item"):
        return set()
    if len(selected) > MAX_TRACKING_CANDIDATES:
        raise RuntimeError("The stock setup references too many items to show safely")
    rows = frappe.get_all(
        "Item",
        filters={"name": ["in", selected], "is_stock_item": 1},
        fields=["name"],
        limit_page_length=len(selected),
    )
    return {cstr(row.get("name")).strip() for row in rows if cstr(row.get("name")).strip()}


def _residual_bin_items(warehouse: str) -> set[str]:
    if not warehouse or not frappe.db.exists("DocType", "Bin"):
        return set()
    meta = frappe.get_meta("Bin")
    quantity_fields = [
        fieldname
        for fieldname in ("actual_qty", "reserved_qty", "ordered_qty", "indented_qty")
        if meta.has_field(fieldname)
    ]
    rows = frappe.get_all(
        "Bin",
        filters={"warehouse": warehouse},
        fields=["item_code", *quantity_fields],
        limit_page_length=MAX_TRACKING_CANDIDATES + 1,
    )
    _require_bounded(rows, "outlet stock balances")
    return {
        cstr(row.get("item_code")).strip()
        for row in rows
        if cstr(row.get("item_code")).strip()
        and any(_nonzero(row.get(fieldname)) for fieldname in quantity_fields)
    }


def _availability_rule_items(company: str, warehouse: str) -> set[str]:
    if not frappe.db.exists("DocType", "FB Inventory Availability Rule"):
        return set()
    rows = frappe.get_all(
        "FB Inventory Availability Rule",
        filters={"company": company, "warehouse": warehouse, "target_type": "Item"},
        fields=["target_id"],
        limit_page_length=MAX_TRACKING_CANDIDATES + 1,
    )
    _require_bounded(rows, "stock availability rules")
    return {cstr(row.get("target_id")).strip() for row in rows if cstr(row.get("target_id")).strip()}


def _open_count_items(warehouse: str) -> set[str]:
    if not frappe.db.exists("DocType", "FB Inventory Count Task"):
        return set()
    parents = frappe.get_all(
        "FB Inventory Count Task",
        filters={"warehouse": warehouse, "status": ["in", OPEN_COUNT_STATES]},
        pluck="name",
        limit_page_length=MAX_TRACKING_CANDIDATES + 1,
    )
    _require_bounded(parents, "open stock counts")
    return _child_items("FB Inventory Count Task Line", parents, "item_id")


def _open_plan_items(company: str, warehouse: str) -> set[str]:
    if not frappe.db.exists("DocType", "FB Inventory Plan"):
        return set()
    parents = frappe.get_all(
        "FB Inventory Plan",
        filters={
            "company": company,
            "warehouse": warehouse,
            "status": ["in", OPEN_PLAN_STATES],
        },
        pluck="name",
        limit_page_length=MAX_TRACKING_CANDIDATES + 1,
    )
    _require_bounded(parents, "unfinished stock plans")
    return _child_items("FB Inventory Plan Line", parents, "item")


def _child_items(doctype: str, parents: Iterable[str], fieldname: str) -> set[str]:
    selected = sorted({cstr(parent).strip() for parent in parents if cstr(parent).strip()})
    if not selected or not frappe.db.exists("DocType", doctype):
        return set()
    rows = frappe.get_all(
        doctype,
        filters={"parent": ["in", selected]},
        fields=[fieldname],
        limit_page_length=MAX_TRACKING_CANDIDATES + 1,
    )
    _require_bounded(rows, "unfinished stock work")
    return {cstr(row.get(fieldname)).strip() for row in rows if cstr(row.get(fieldname)).strip()}


def _require_bounded(rows: Iterable[Any], label: str) -> None:
    if len(list(rows)) > MAX_TRACKING_CANDIDATES:
        raise RuntimeError(f"There are too many {label} to read safely")


def _nonzero(value: Any) -> bool:
    try:
        parsed = Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return parsed.is_finite() and parsed != 0
