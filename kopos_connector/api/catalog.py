from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime

from kopos_connector.api.devices import (
    KOPOS_DEVICE_API_ROLE,
    get_device_doc,
    get_session_roles,
)
CatalogPayload = dict[str, Any]
ERPRecord = dict[str, Any]


def build_catalog_payload(
    since: str | None = None,
    device_id: str | None = None,
    known_version: str | None = None,
) -> CatalogPayload:
    """Build a complete, versioned catalog payload consumed by KoPOS clients.

    ``since`` is accepted for compatibility with older tablets, but is
    intentionally not used as a delta cursor. Older implementations returned
    filtered arrays that a tablet could mistake for a complete snapshot and
    thereby remove valid cached products. KoPOS catalog sync is now explicitly
    full-or-unchanged until a tombstone-capable delta contract is introduced.
    """
    del since
    pos_profile = resolve_catalog_pos_profile(device_id=device_id)
    company = pos_profile.get("company") if pos_profile else None
    warehouse = pos_profile.get("warehouse") if pos_profile else None
    selling_price_list = pos_profile.get("selling_price_list") if pos_profile else None
    currency = (pos_profile or {}).get("currency") or (
        frappe.db.get_value("Company", company, "default_currency") if company else None
    )
    # The cashier catalog is deliberately limited to commercial Item data.
    # Recipe, modifier, and inventory configuration is optional during the
    # redesign and must never sit on the request path that lets a tablet sell.
    items = _without_optional_recipe_fields(
        get_items(
            warehouse=warehouse,
            selling_price_list=selling_price_list,
            pos_profile=pos_profile,
        )
    )
    category_ids = {
        cstr(item.get("category_id"))
        for item in items
        if cstr(item.get("category_id")).strip()
    }

    snapshot = {
        "categories": get_categories(category_ids=category_ids),
        "items": items,
        "modifier_groups": [],
        "modifier_options": [],
        "metadata": {
            "company": company,
            "pos_profile": (pos_profile or {}).get("name"),
            "warehouse": warehouse,
            "currency": currency,
            "tax_rate": get_tax_rate_value(device_id=device_id),
        },
    }
    validate_catalog_snapshot(snapshot)
    catalog_version = build_catalog_version(snapshot)
    timestamp = now_datetime().isoformat()
    normalized_known_version = cstr(known_version).strip()

    if normalized_known_version and normalized_known_version == catalog_version:
        return {
            "sync_mode": "unchanged",
            "unchanged": 1,
            "catalog_version": catalog_version,
            "timestamp": timestamp,
            "metadata": snapshot["metadata"],
        }

    payload = {
        "sync_mode": "full",
        "unchanged": 0,
        "catalog_version": catalog_version,
        "timestamp": timestamp,
        **snapshot,
    }

    frappe.logger("kopos_connector").info(
        "Catalog built with %s items and %s modifier groups",
        len(payload["items"]),
        len(payload["modifier_groups"]),
    )

    return payload


def _without_optional_recipe_fields(items: list[ERPRecord]) -> list[ERPRecord]:
    """Return the same commercial items without optional recipe enrichment."""

    return [
        {
            **item,
            "is_available": bool(cint(item.get("is_active", 1))),
            "stock_warning": None,
            "modifier_group_ids": [],
            "recipe_id": None,
            "recipe_version": None,
        }
        for item in items
    ]


def build_catalog_version(snapshot: Mapping[str, Any]) -> str:
    """Return a deterministic content hash; request timestamps are excluded."""
    encoded = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def validate_catalog_snapshot(snapshot: Mapping[str, Any]) -> None:
    """Reject malformed or empty ERP data before publishing a content version."""
    categories = snapshot.get("categories")
    items = snapshot.get("items")
    modifier_groups = snapshot.get("modifier_groups")
    modifier_options = snapshot.get("modifier_options")
    if not isinstance(categories, list) or not categories:
        frappe.throw(
            _("KoPOS catalog requires at least one category"),
            frappe.ValidationError,
        )
    if not isinstance(items, list) or not items:
        frappe.throw(
            _("KoPOS catalog requires at least one saleable item"),
            frappe.ValidationError,
        )
    if not isinstance(modifier_groups, list) or not isinstance(
        modifier_options, list
    ):
        frappe.throw(
            _("KoPOS catalog modifier arrays are malformed"),
            frappe.ValidationError,
        )

    category_ids = _unique_catalog_ids(categories, "category")
    modifier_group_ids = _unique_catalog_ids(modifier_groups, "modifier group")
    _unique_catalog_ids(items, "item")
    modifier_option_ids = _unique_catalog_ids(modifier_options, "modifier option")

    barcodes: set[str] = set()
    for item in items:
        item_id = cstr(item.get("id")).strip()
        if not cstr(item.get("name")).strip():
            _throw_catalog_validation(f"Item {item_id} has no name")
        category_id = cstr(item.get("category_id")).strip()
        if category_id not in category_ids:
            _throw_catalog_validation(
                f"Item {item_id} references unknown category {category_id or '(missing)'}"
            )
        price_sen = item.get("price_sen")
        if isinstance(price_sen, bool) or not isinstance(price_sen, int):
            _throw_catalog_validation(f"Item {item_id} price_sen must be an integer")
        if price_sen < 0:
            _throw_catalog_validation(f"Item {item_id} price_sen cannot be negative")
        linked_groups = item.get("modifier_group_ids")
        if not isinstance(linked_groups, list):
            _throw_catalog_validation(
                f"Item {item_id} modifier_group_ids must be an array"
            )
        for group_id_value in linked_groups:
            group_id = cstr(group_id_value).strip()
            if group_id not in modifier_group_ids:
                _throw_catalog_validation(
                    f"Item {item_id} references unknown modifier group {group_id or '(missing)'}"
                )
        recipe_id = cstr(item.get("recipe_id")).strip()
        recipe_version = item.get("recipe_version")
        if bool(recipe_id) != (recipe_version is not None):
            _throw_catalog_validation(
                f"Item {item_id} recipe_id and recipe_version must be provided together"
            )
        if recipe_id and (
            isinstance(recipe_version, bool)
            or not isinstance(recipe_version, int)
            or recipe_version <= 0
        ):
            _throw_catalog_validation(
                f"Item {item_id} recipe_version must be a positive integer"
            )
        barcode = cstr(item.get("barcode")).strip()
        if barcode:
            if barcode in barcodes:
                _throw_catalog_validation(f"Duplicate catalog barcode {barcode}")
            barcodes.add(barcode)

    for option in modifier_options:
        option_id = cstr(option.get("id")).strip()
        group_id = cstr(option.get("group_id")).strip()
        if group_id not in modifier_group_ids:
            _throw_catalog_validation(
                f"Modifier option {option_id} references unknown group {group_id or '(missing)'}"
            )
        adjustment_sen = option.get("price_adjustment_sen")
        if isinstance(adjustment_sen, bool) or not isinstance(adjustment_sen, int):
            _throw_catalog_validation(
                f"Modifier option {option_id} price_adjustment_sen must be an integer"
            )

    options_by_group: dict[str, list[ERPRecord]] = {}
    for option in modifier_options:
        options_by_group.setdefault(cstr(option.get("group_id")).strip(), []).append(
            option
        )
    if modifier_groups:
        from kopos_connector.kopos.services.recipe.modifier_bounds import (
            ModifierBoundsError,
            validate_published_modifier_bounds,
        )
    for group in modifier_groups:
        group_id = cstr(group.get("id")).strip()
        if not cstr(group.get("name")).strip():
            _throw_catalog_validation(f"Modifier group {group_id} has no name")
        try:
            bounds = validate_published_modifier_bounds(
                selection_type=group.get("selection_type"),
                is_required=group.get("is_required"),
                min_selection=group.get("min_selections"),
                max_selection=group.get("max_selections"),
            )
        except ModifierBoundsError as error:
            _throw_catalog_validation(f"Modifier group {group_id}: {error}")
            raise AssertionError("frappe.throw must raise") from error
        active_option_count = sum(
            1
            for option in options_by_group.get(group_id, [])
            if cint(option.get("is_active"))
        )
        if active_option_count < bounds.min_selection:
            _throw_catalog_validation(
                f"Modifier group {group_id} has fewer active options than its minimum selection"
            )
        parent_option_id = cstr(group.get("parent_option_id")).strip()
        if parent_option_id and parent_option_id not in modifier_option_ids:
            _throw_catalog_validation(
                f"Modifier group {group_id} references unknown parent option {parent_option_id}"
            )


def _unique_catalog_ids(rows: list[ERPRecord], label: str) -> set[str]:
    identifiers: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            _throw_catalog_validation(f"Catalog {label} row must be an object")
        identifier = cstr(row.get("id")).strip()
        if not identifier:
            _throw_catalog_validation(f"Catalog {label} id is required")
        if identifier in identifiers:
            _throw_catalog_validation(f"Duplicate catalog {label} id {identifier}")
        identifiers.add(identifier)
    return identifiers


def _throw_catalog_validation(message: str) -> None:
    frappe.throw(_(message), frappe.ValidationError)


def resolve_catalog_pos_profile(device_id: str | None = None) -> ERPRecord | None:
    if cstr(device_id).strip():
        device_doc = get_device_doc(device_id=device_id)
        profile_name = cstr(getattr(device_doc, "pos_profile", None)).strip()
        if not profile_name:
            frappe.throw(
                _("KoPOS Device {0} has no POS Profile configured").format(
                    cstr(device_id).strip()
                ),
                frappe.ValidationError,
            )
        profile = frappe.get_cached_doc("POS Profile", profile_name)
        return profile.as_dict()
    return get_default_pos_profile()


def get_default_pos_profile(company: str | None = None) -> ERPRecord | None:
    """Return the most recently updated enabled POS Profile."""
    filters: dict[str, Any] = {"disabled": 0}
    if company:
        filters["company"] = company

    profiles = frappe.get_all(
        "POS Profile",
        filters=filters,
        fields=[
            "name",
            "company",
            "warehouse",
            "selling_price_list",
            "currency",
            "customer",
            "custom_kopos_enable_sst",
            "custom_kopos_sst_rate",
        ],
        order_by="modified desc",
        limit=1,
    )

    if profiles:
        return dict(profiles[0])

    default_company = company or frappe.defaults.get_user_default("Company")
    if default_company and default_company != company:
        return get_default_pos_profile(default_company)

    return None


def get_categories(
    since: str | None = None, category_ids: set[str] | None = None
) -> list[ERPRecord]:
    """Return catalog categories from leaf Item Groups."""
    filters: dict[str, Any] = {"is_group": 0}
    if since:
        filters["modified"] = [">=", since]

    rows = frappe.get_all(
        "Item Group",
        filters=filters,
        fields=["name as id", "item_group_name as name", "lft"],
        order_by="lft asc, name asc",
    )

    allowed_ids = {
        cstr(category_id).strip()
        for category_id in (category_ids or set())
        if cstr(category_id).strip()
    }

    return [
        {
            "id": row.get("id") or row.get("name"),
            "name": row.get("name"),
            "display_order": index,
            "is_active": 1,
        }
        for index, row in enumerate(rows, start=1)
        if not allowed_ids
        or cstr(row.get("id") or row.get("name")).strip() in allowed_ids
    ]


def get_items(
    warehouse: str | None = None,
    selling_price_list: str | None = None,
    since: str | None = None,
    pos_profile: ERPRecord | None = None,
) -> list[ERPRecord]:
    """Return saleable Item records for KoPOS."""
    filters: dict[str, Any] = {"is_sales_item": 1, "disabled": 0}
    allowed_item_groups = get_allowed_item_groups(pos_profile)
    if allowed_item_groups:
        filters["item_group"] = ["in", sorted(allowed_item_groups)]
    row_by_item_id: dict[str, ERPRecord] = {}
    for row in get_saleable_item_rows(filters=filters, since=since):
        item_id = cstr(row.get("id") or row.get("item_code")).strip()
        if item_id:
            row_by_item_id[item_id] = row

    rows = sorted(
        row_by_item_id.values(),
        key=lambda row: (
            cstr(row.get("name") or row.get("item_code")).lower(),
            cstr(row.get("id") or row.get("item_code")),
        ),
    )
    item_ids = [
        cstr(row.get("id") or row.get("item_code")).strip()
        for row in rows
        if cstr(row.get("id") or row.get("item_code")).strip()
    ]
    prices_by_item = get_item_prices_map(
        item_codes=item_ids,
        selling_price_list=selling_price_list,
        item_uoms={
            cstr(row.get("id") or row.get("item_code")).strip(): cstr(
                row.get("stock_uom")
            ).strip()
            for row in rows
            if cstr(row.get("id") or row.get("item_code")).strip()
        },
    )
    barcodes_by_item = get_item_barcodes_map(item_ids)
    items: list[ERPRecord] = []
    for row in rows:
        item_id = cstr(row.get("id") or row.get("item_code"))
        price = prices_by_item.get(item_id, flt(row.get("price")))
        items.append(
            {
                "id": item_id,
                "item_code": cstr(row.get("item_code") or item_id),
                "name": cstr(row.get("name")),
                "category_id": cstr(row.get("category_id")),
                "price": price,
                "price_sen": money_to_sen(price),
                "barcode": barcodes_by_item.get(item_id),
                "is_available": not bool(cint(row.get("disabled"))),
                "stock_warning": None,
                "is_active": 0 if cint(row.get("disabled")) else 1,
                "is_prep_item": cint(row.get("custom_kopos_is_prep_item") or 0),
                "modifier_group_ids": [],
                "recipe_id": None,
                "recipe_version": None,
            }
        )

    return items


def get_saleable_item_rows(
    filters: dict[str, Any],
    since: str | None = None,
    item_codes: set[str] | None = None,
) -> list[ERPRecord]:
    query_filters = dict(filters)
    normalized_item_codes = sorted(
        {
            cstr(item_code).strip()
            for item_code in (item_codes or set())
            if cstr(item_code).strip()
        }
    )
    if since:
        query_filters["modified"] = [">=", since]
    if normalized_item_codes:
        query_filters["name"] = ["in", normalized_item_codes]
    if item_codes is not None and not normalized_item_codes:
        return []

    fields = [
        "name as id",
        "item_code",
        "item_name as name",
        "item_group as category_id",
        "standard_rate as price",
        "disabled",
        "stock_uom",
    ]
    try:
        item_meta = frappe.get_meta("Item")
        if item_meta.has_field("custom_kopos_is_prep_item"):
            fields.append("custom_kopos_is_prep_item")
    except Exception:
        # A missing custom prep flag defaults to a normal sale item. It must
        # not make the catalog query invalid during staged deployments.
        pass

    try:
        return frappe.get_all(
            "Item",
            filters=query_filters,
            fields=fields,
            order_by="item_name asc",
        )
    except Exception:
        if "custom_kopos_is_prep_item" not in fields:
            raise
        # During a rolling migration, cached metadata can briefly advertise a
        # custom column before this database has it. Retry once using only
        # standard Item columns so the menu remains saleable.
        return frappe.get_all(
            "Item",
            filters=query_filters,
            fields=[
                field
                for field in fields
                if field != "custom_kopos_is_prep_item"
            ],
            order_by="item_name asc",
        )


def get_recipe_changed_item_codes(
    company: str | None = None, since: str | None = None
) -> set[str]:
    if not since:
        return set()

    filters: dict[str, Any] = {"status": "Active", "modified": [">=", since]}
    if company:
        filters["company"] = company

    try:
        changed_items = frappe.get_all(
            "FB Recipe", filters=filters, pluck="sellable_item"
        )
    except Exception:
        return set()
    return {
        cstr(item_code).strip()
        for item_code in changed_items
        if cstr(item_code).strip()
    }


def get_allowed_item_groups(pos_profile: ERPRecord | None) -> set[str]:
    if not pos_profile:
        return set()

    child_rows = (
        (pos_profile.get("item_groups") or [])
        if isinstance(pos_profile, dict)
        else (getattr(pos_profile, "item_groups", []) or [])
    )
    selected_groups = {
        cstr(
            row.get("item_group")
            if isinstance(row, dict)
            else getattr(row, "item_group", None)
        ).strip()
        for row in child_rows
        if cstr(
            row.get("item_group")
            if isinstance(row, dict)
            else getattr(row, "item_group", None)
        ).strip()
    }
    if not selected_groups:
        return set()

    rows = frappe.get_all(
        "Item Group",
        filters={"name": ["in", sorted(selected_groups)]},
        fields=["name", "lft", "rgt"],
    )
    if not rows:
        return selected_groups

    conditions = []
    values: list[Any] = []
    for row in rows:
        conditions.append("(lft >= %s AND rgt <= %s)")
        values.extend([row.get("lft"), row.get("rgt")])

    descendants = frappe.db.sql(
        f"""
            SELECT name
            FROM `tabItem Group`
            WHERE {" OR ".join(conditions)}
        """,
        tuple(values),
        as_dict=True,
    )
    return {
        cstr(row.get("name")).strip()
        for row in descendants
        if cstr(row.get("name")).strip()
    } or selected_groups


def get_item_modifier_groups(item_code: str, company: str | None = None) -> list[str]:
    item_id = cstr(item_code).strip()
    if not item_id:
        return []

    return get_item_modifier_groups_map(
        [{"id": item_id, "item_code": item_id}],
        company=company,
    ).get(item_id, [])


def get_item_modifier_groups_map(
    item_rows: list[ERPRecord],
    company: str | None = None,
    recipe_snapshots_by_item: dict[str, ERPRecord] | None = None,
) -> dict[str, list[str]]:
    item_codes = sorted(
        {
            cstr(row.get("id") or row.get("item_code")).strip()
            for row in item_rows
            if cstr(row.get("id") or row.get("item_code")).strip()
        }
    )
    if not item_codes:
        return {}

    snapshots = (
        recipe_snapshots_by_item
        if recipe_snapshots_by_item is not None
        else get_item_recipe_snapshots_map(item_rows, company=company)
    )
    recipe_names = sorted(
        {
            cstr(snapshot.get("recipe_id")).strip()
            for snapshot in snapshots.values()
            if cstr(snapshot.get("recipe_id")).strip()
        }
    )
    if not recipe_names:
        return {}

    allowed_group_rows = frappe.get_all(
        "FB Allowed Modifier Group",
        filters={
            "parent": ["in", recipe_names],
            "parenttype": "FB Recipe",
            "parentfield": "allowed_modifier_groups",
        },
        fields=[
            "parent",
            "modifier_group",
            "required",
            "override_min_selection",
            "override_max_selection",
            "display_order",
            "idx",
        ],
        order_by="parent asc, display_order asc, idx asc",
    )
    allowed_group_ids = sorted(
        {
            cstr(row.get("modifier_group")).strip()
            for row in allowed_group_rows
            if cstr(row.get("modifier_group")).strip()
        }
    )
    if not allowed_group_ids:
        return {}
    active_group_rows = frappe.get_all(
        "FB Modifier Group",
        filters={"active": 1, "name": ["in", allowed_group_ids]},
        fields=[
            "name",
            "selection_type",
            "is_required",
            "min_selection",
            "max_selection",
        ],
    )
    active_groups_by_id = {
        cstr(row.get("name")).strip(): row
        for row in active_group_rows
        if cstr(row.get("name")).strip()
    }

    item_code_by_recipe_name = {
        cstr(snapshot.get("recipe_id")).strip(): item_code
        for item_code, snapshot in snapshots.items()
        if cstr(snapshot.get("recipe_id")).strip()
    }
    group_ids_by_item: dict[str, list[str]] = {}
    for row in allowed_group_rows:
        recipe_name = cstr(row.get("parent")).strip()
        modifier_group = cstr(row.get("modifier_group")).strip()
        item_code = item_code_by_recipe_name.get(recipe_name)
        if (
            not item_code
            or not modifier_group
            or modifier_group not in active_groups_by_id
        ):
            continue

        item_group_ids = group_ids_by_item.setdefault(item_code, [])
        if modifier_group not in item_group_ids:
            item_group_ids.append(modifier_group)

    return group_ids_by_item


def _validate_recipe_modifier_bounds_parity(
    *,
    recipe_name: str,
    modifier_group: str,
    group: Mapping[str, Any],
    recipe_group: Mapping[str, Any],
) -> None:
    """Reject item-specific bounds that the current catalog wire cannot express."""
    from kopos_connector.kopos.services.recipe.modifier_bounds import (
        ModifierBoundsError,
        resolve_effective_modifier_bounds,
    )

    try:
        published_bounds = resolve_effective_modifier_bounds(
            selection_type=group.get("selection_type"),
            group_is_required=group.get("is_required"),
            group_min_selection=group.get("min_selection"),
            group_max_selection=group.get("max_selection"),
        )
        recipe_bounds = resolve_effective_modifier_bounds(
            selection_type=group.get("selection_type"),
            group_is_required=group.get("is_required"),
            group_min_selection=group.get("min_selection"),
            group_max_selection=group.get("max_selection"),
            recipe_required=recipe_group.get("required"),
            override_min_selection=recipe_group.get("override_min_selection"),
            override_max_selection=recipe_group.get("override_max_selection"),
        )
    except ModifierBoundsError as error:
        _throw_catalog_validation(
            f"Recipe {recipe_name} modifier group {modifier_group}: {error}"
        )
        raise AssertionError("frappe.throw must raise") from error

    if recipe_bounds != published_bounds:
        _throw_catalog_validation(
            "Recipe {0} changes the selection rules for modifier group {1}; "
            "use a separate modifier group so every tablet enforces the same rules".format(
                recipe_name,
                modifier_group,
            )
        )


def get_item_recipe_snapshots_map(
    item_rows: list[ERPRecord], company: str | None = None
) -> dict[str, ERPRecord]:
    item_codes = sorted(
        {
            cstr(row.get("id") or row.get("item_code")).strip()
            for row in item_rows
            if cstr(row.get("id") or row.get("item_code")).strip()
        }
    )
    if not item_codes:
        return {}

    recipe_filters: dict[str, Any] = {
        "sellable_item": ["in", item_codes],
        "status": "Active",
    }
    if company:
        recipe_filters["company"] = company
    recipe_rows = frappe.get_all(
        "FB Recipe",
        filters=recipe_filters,
        fields=[
            "name",
            "sellable_item",
            "effective_from",
            "effective_to",
            "version_no",
            "modified",
        ],
        order_by="sellable_item asc, version_no desc, modified desc",
    )

    snapshots: dict[str, ERPRecord] = {}
    current_time = now_datetime()
    for row in recipe_rows:
        item_code = cstr(row.get("sellable_item")).strip()
        recipe_name = cstr(row.get("name")).strip()
        if not item_code or not recipe_name or not is_effective_recipe_row(
            row, current_time
        ):
            continue
        existing = snapshots.get(item_code)
        if existing:
            # Rows are sorted newest-first. A stale duplicate must not take
            # down the whole menu; keep the deterministic first snapshot.
            continue
        version_no = cint(row.get("version_no"))
        if version_no <= 0:
            continue
        snapshots[item_code] = {
            "recipe_id": recipe_name,
            "recipe_version": version_no,
        }

    return snapshots


def is_effective_recipe_row(row: ERPRecord, current_time: Any) -> bool:
    effective_from = row.get("effective_from")
    effective_to = row.get("effective_to")
    if effective_from and get_datetime(effective_from) > current_time:
        return False
    if effective_to and get_datetime(effective_to) < current_time:
        return False
    return True


def get_datetime(value: Any) -> Any:
    return frappe.utils.get_datetime(value)


def get_item_barcodes_map(item_codes: list[str]) -> dict[str, str]:
    normalized_item_codes = sorted(
        {cstr(item_code).strip() for item_code in item_codes if cstr(item_code).strip()}
    )
    if not normalized_item_codes:
        return {}

    rows = frappe.get_all(
        "Item Barcode",
        filters={
            "parent": ["in", normalized_item_codes],
            "parenttype": "Item",
            "parentfield": "barcodes",
        },
        fields=["parent", "barcode", "idx"],
        order_by="parent asc, idx asc",
    )
    result: dict[str, str] = {}
    for row in rows:
        item_code = cstr(row.get("parent")).strip()
        barcode = cstr(row.get("barcode")).strip()
        if item_code and barcode and item_code not in result:
            result[item_code] = barcode
    return result


def get_item_barcode(item_code: str) -> str | None:
    """Compatibility helper for callers outside the bulk catalog builder."""
    return get_item_barcodes_map([item_code]).get(cstr(item_code).strip())


def get_item_availability(
    item: ERPRecord,
    warehouse: str | None = None,
    *,
    bin_qty_by_item: Mapping[str, float] | None = None,
    reserved_qty_by_item: Mapping[str, float] | None = None,
) -> ERPRecord:
    """Resolve cashier availability without reading optional inventory state."""
    del warehouse, bin_qty_by_item, reserved_qty_by_item
    if cint(item.get("disabled")):
        return {"is_available": False, "stock_warning": None}

    return {"is_available": True, "stock_warning": None}


def get_bin_qty_map(
    item_codes: list[str], warehouse: str | None
) -> dict[str, float]:
    normalized_item_codes = sorted(
        {cstr(item_code).strip() for item_code in item_codes if cstr(item_code).strip()}
    )
    normalized_warehouse = cstr(warehouse).strip()
    if not normalized_item_codes or not normalized_warehouse:
        return {}

    rows = frappe.get_all(
        "Bin",
        filters={
            "item_code": ["in", normalized_item_codes],
            "warehouse": normalized_warehouse,
        },
        fields=["item_code", "actual_qty"],
    )
    return {
        cstr(row.get("item_code")).strip(): flt(row.get("actual_qty"))
        for row in rows
        if cstr(row.get("item_code")).strip()
    }


def get_fb_pending_reserved_qty_map(
    item_codes: list[str], warehouse: str | None
) -> dict[str, float]:
    """Return quantities awaiting the canonical FB Order stock projection.

    A submitted FB Order with ``stock_status = Pending`` has committed a sale
    locally/financially but has not yet reduced Bin.actual_qty. Treating those
    rows as reservations prevents that projection delay from overstating stock.
    """
    normalized_item_codes = sorted(
        {cstr(item_code).strip() for item_code in item_codes if cstr(item_code).strip()}
    )
    normalized_warehouse = cstr(warehouse).strip()
    if not normalized_item_codes or not normalized_warehouse:
        return {}

    placeholders = ", ".join(["%s"] * len(normalized_item_codes))
    rows = frappe.db.sql(
        f"""
            SELECT
                line.item AS item_code,
                SUM(GREATEST(line.qty - COALESCE(line.refunded_qty, 0), 0)) AS reserved_qty
            FROM `tabFB Order Line` line
            INNER JOIN `tabFB Order` fb_order ON fb_order.name = line.parent
            WHERE line.parenttype = 'FB Order'
              AND line.parentfield = 'items'
              AND fb_order.docstatus = 1
              AND fb_order.status = 'Submitted'
              AND fb_order.stock_status = 'Pending'
              AND fb_order.booth_warehouse = %s
              AND line.item IN ({placeholders})
            GROUP BY line.item
        """,
        tuple([normalized_warehouse, *normalized_item_codes]),
        as_dict=True,
    )
    return {
        cstr(row.get("item_code")).strip(): flt(row.get("reserved_qty"))
        for row in rows
        if cstr(row.get("item_code")).strip()
    }


def get_item_price(
    item_code: str,
    standard_rate: float,
    selling_price_list: str | None = None,
    item_uom: str | None = None,
) -> float:
    """Return price list rate when available, otherwise Item.standard_selling_rate."""
    item_id = cstr(item_code).strip()
    return get_item_prices_map(
        [item_code],
        selling_price_list,
        item_uoms={item_id: cstr(item_uom).strip()} if item_uom else None,
    ).get(
        item_id,
        standard_rate or 0,
    )


def get_item_prices_map(
    item_codes: list[str],
    selling_price_list: str | None = None,
    item_uoms: Mapping[str, str] | None = None,
) -> dict[str, float]:
    normalized_item_codes = sorted(
        {cstr(item_code).strip() for item_code in item_codes if cstr(item_code).strip()}
    )
    normalized_price_list = cstr(selling_price_list).strip()
    if not normalized_item_codes or not normalized_price_list:
        return {}

    rows = frappe.get_all(
        "Item Price",
        filters={
            "item_code": ["in", normalized_item_codes],
            "selling": 1,
            "price_list": normalized_price_list,
        },
        fields=[
            "name",
            "item_code",
            "price_list_rate",
            "uom",
            "valid_from",
            "valid_upto",
            "modified",
        ],
        order_by="item_code asc, uom asc, valid_from desc, modified desc, name asc",
    )
    current_date = now_datetime().date()
    selected: dict[str, tuple[tuple[int, date, str, str], float]] = {}
    for row in rows:
        item_code = cstr(row.get("item_code")).strip()
        if not item_code or row.get("price_list_rate") is None:
            continue
        valid_from = _catalog_date(row.get("valid_from"))
        valid_upto = _catalog_date(row.get("valid_upto"))
        if valid_from and valid_from > current_date:
            continue
        if valid_upto and valid_upto < current_date:
            continue

        expected_uom = cstr((item_uoms or {}).get(item_code)).strip()
        price_uom = cstr(row.get("uom")).strip()
        if expected_uom and price_uom and price_uom != expected_uom:
            continue
        uom_priority = 1 if expected_uom and price_uom == expected_uom else 0
        rank = (
            uom_priority,
            valid_from or date.min,
            cstr(row.get("modified")),
            cstr(row.get("name")),
        )
        existing = selected.get(item_code)
        if existing is None or rank > existing[0]:
            selected[item_code] = (rank, flt(row.get("price_list_rate")))
    return {item_code: value for item_code, (_, value) in selected.items()}


def _catalog_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    parsed = get_datetime(value)
    return parsed.date()


def money_to_sen(value: Any) -> int:
    """Convert ERP currency values to integer sen using explicit half-up rounding."""
    try:
        amount = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError):
        frappe.throw(_("Catalog money value is invalid"), frappe.ValidationError)
        return 0
    if not amount.is_finite():
        frappe.throw(_("Catalog money value must be finite"), frappe.ValidationError)
    return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def get_modifier_groups(
    since: str | None = None,
    group_ids: Collection[str] | None = None,
) -> list[ERPRecord]:
    filters: dict[str, Any] = {"active": 1}
    if since:
        filters["modified"] = [">=", since]
    normalized_group_ids = _normalized_catalog_group_ids(group_ids)
    if group_ids is not None:
        if not normalized_group_ids:
            return []
        filters["name"] = ["in", normalized_group_ids]

    from kopos_connector.kopos.services.recipe.modifier_bounds import (
        ModifierBoundsError,
        resolve_effective_modifier_bounds,
    )

    rows = frappe.get_all(
        "FB Modifier Group",
        filters=filters,
        fields=[
            "name as id",
            "group_name as name",
            "selection_type",
            "is_required",
            "min_selection",
            "max_selection",
            "display_order",
            "parent_modifier",
        ],
        order_by="display_order asc, group_name asc",
    )

    groups: list[ERPRecord] = []
    for row in rows:
        group_id = cstr(row.get("id")).strip()
        try:
            bounds = resolve_effective_modifier_bounds(
                selection_type=row.get("selection_type"),
                group_is_required=row.get("is_required"),
                group_min_selection=row.get("min_selection"),
                group_max_selection=row.get("max_selection"),
            )
        except ModifierBoundsError as error:
            _throw_catalog_validation(f"Modifier group {group_id}: {error}")
            raise AssertionError("frappe.throw must raise") from error
        groups.append(
            {
                "id": group_id,
                "name": row.get("name"),
                "selection_type": bounds.selection_type,
                "is_required": int(bounds.is_required),
                "min_selections": bounds.min_selection,
                "max_selections": bounds.max_selection,
                "display_order": cint(row.get("display_order") or 0),
                "parent_option_id": cstr(row.get("parent_modifier")).strip()
                or None,
            }
        )
    return groups


def get_modifier_options(
    since: str | None = None,
    group_ids: Collection[str] | None = None,
) -> list[ERPRecord]:
    conditions = ["opt.active = 1", "grp.active = 1"]
    values: list[Any] = []
    if since:
        conditions.append("(opt.modified >= %s OR grp.modified >= %s)")
        values.extend([since, since])
    normalized_group_ids = _normalized_catalog_group_ids(group_ids)
    if group_ids is not None:
        if not normalized_group_ids:
            return []
        conditions.append(
            "opt.modifier_group IN ({0})".format(
                ", ".join(["%s"] * len(normalized_group_ids))
            )
        )
        values.extend(normalized_group_ids)

    rows = frappe.db.sql(
        f"""
			SELECT
				opt.name AS id,
				opt.modifier_group AS group_id,
				opt.modifier_name AS name,
				opt.price_adjustment,
				opt.is_default,
				opt.active AS is_active,
				opt.display_order
			FROM `tabFB Modifier` opt
			INNER JOIN `tabFB Modifier Group` grp ON grp.name = opt.modifier_group
			WHERE {" AND ".join(conditions)}
			ORDER BY opt.modifier_group ASC, opt.display_order ASC, opt.modifier_name ASC
		""",
        tuple(values),
        as_dict=True,
    )

    return [
        {
            "id": row.get("id"),
            "group_id": row.get("group_id"),
            "name": row.get("name"),
            "price_adjustment": flt(row.get("price_adjustment")),
            "price_adjustment_sen": money_to_sen(row.get("price_adjustment")),
            "is_default": cint(row.get("is_default")),
            "is_active": cint(
                row.get("is_active") if row.get("is_active") is not None else 1
            ),
            "display_order": cint(row.get("display_order") or 0),
        }
        for row in rows
    ]


def _normalized_catalog_group_ids(
    group_ids: Collection[str] | None,
) -> list[str]:
    if group_ids is None:
        return []
    source_group_ids = [group_ids] if isinstance(group_ids, str) else group_ids
    return sorted(
        {
            cstr(group_id).strip()
            for group_id in source_group_ids
            if cstr(group_id).strip()
        }
    )


def get_tax_rate_value(
    pos_profile_name: str | None = None, device_id: str | None = None
) -> float:
    """Return the KoPOS SST rate as a decimal."""
    profile_data = None
    if cstr(device_id).strip():
        device_doc = get_device_doc(device_id=device_id)
        profile_name = cstr(getattr(device_doc, "pos_profile", None)).strip()
        if not profile_name:
            frappe.throw(
                _("KoPOS Device {0} has no POS Profile configured").format(
                    cstr(device_id).strip()
                ),
                frappe.ValidationError,
            )
        profile = frappe.get_doc("POS Profile", profile_name)
        profile_data = profile.as_dict()
    elif pos_profile_name:
        profile = frappe.get_doc("POS Profile", pos_profile_name)
        profile_data = profile.as_dict()
    else:
        profile_data = get_default_pos_profile()

    if not profile_data:
        return 0.08

    if not cint(profile_data.get("custom_kopos_enable_sst", 1)):
        return 0.0

    return flt(profile_data.get("custom_kopos_sst_rate") or 8) / 100


def get_item_modifiers_payload(
    item_code: str, company: str | None = None
) -> list[ERPRecord]:
    """Return no optional configuration on the cashier-critical API path."""
    del item_code, company
    return []


def cint(value: Any) -> int:
    return frappe.utils.cint(value)


def cstr(value: Any) -> str:
    return frappe.utils.cstr(value)


def flt(value: Any) -> float:
    return frappe.utils.flt(value)


@frappe.whitelist()
def list_modifier_option_choices() -> list[ERPRecord]:
    roles = get_session_roles()
    if (
        "System Manager" not in roles
        and "Item Manager" not in roles
        and KOPOS_DEVICE_API_ROLE not in roles
    ):
        frappe.throw(
            _("User {0} is not allowed to access KoPOS modifier configuration").format(
                cstr(getattr(frappe.session, "user", None)).strip() or _("Guest")
            ),
            frappe.ValidationError,
        )

    return [
        {
            "value": cstr(option.get("id")).strip(),
            "label": "{0} ({1})".format(
                cstr(option.get("name")).strip(),
                cstr(option.get("group_id")).strip(),
            ),
        }
        for option in get_modifier_options()
        if cstr(option.get("id")).strip()
    ]
