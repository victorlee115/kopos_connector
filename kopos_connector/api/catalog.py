from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime

from kopos_connector.api.devices import get_device_doc


CatalogPayload = dict[str, Any]
ERPRecord = dict[str, Any]


def build_catalog_payload(
    since: str | None = None, device_id: str | None = None
) -> CatalogPayload:
    """Build the catalog payload consumed by KoPOS clients."""
    pos_profile = resolve_catalog_pos_profile(device_id=device_id)
    company = pos_profile.get("company") if pos_profile else None
    warehouse = pos_profile.get("warehouse") if pos_profile else None
    selling_price_list = pos_profile.get("selling_price_list") if pos_profile else None
    currency = (pos_profile or {}).get("currency") or (
        frappe.db.get_value("Company", company, "default_currency") if company else None
    )
    items = get_items(
        warehouse=warehouse,
        selling_price_list=selling_price_list,
        since=since,
        pos_profile=pos_profile,
    )
    category_ids = {
        cstr(item.get("category_id"))
        for item in items
        if cstr(item.get("category_id")).strip()
    }

    payload = {
        "categories": get_categories(since, category_ids=category_ids),
        "items": items,
        "modifier_groups": get_modifier_groups(since),
        "modifier_options": get_modifier_options(since),
        "timestamp": now_datetime().isoformat(),
        "metadata": {
            "company": company,
            "pos_profile": (pos_profile or {}).get("name"),
            "warehouse": warehouse,
            "currency": currency,
            "tax_rate": get_tax_rate_value(device_id=device_id),
        },
    }

    frappe.logger("kopos_connector").info(
        "Catalog built with %s items and %s modifier groups",
        len(payload["items"]),
        len(payload["modifier_groups"]),
    )

    return payload


def resolve_catalog_pos_profile(device_id: str | None = None) -> ERPRecord | None:
    if cstr(device_id).strip():
        device_doc = get_device_doc(device_id=device_id)
        profile = frappe.get_cached_doc("POS Profile", device_doc.pos_profile)
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
    if since:
        filters["modified"] = [">=", since]

    rows = frappe.get_all(
        "Item",
        filters=filters,
        fields=[
            "name as id",
            "item_code",
            "item_name as name",
            "item_group as category_id",
            "standard_rate as price",
            "disabled",
            "custom_kopos_availability_mode",
            "custom_kopos_track_stock",
            "custom_kopos_min_qty",
            "custom_kopos_is_prep_item",
        ],
        order_by="item_name asc",
    )

    items: list[ERPRecord] = []
    for row in rows:
        item_id = cstr(row.get("id") or row.get("item_code"))
        price = get_item_price(
            item_code=item_id,
            standard_rate=flt(row.get("price")),
            selling_price_list=selling_price_list,
        )
        items.append(
            {
                "id": item_id,
                "item_code": cstr(row.get("item_code") or item_id),
                "name": cstr(row.get("name")),
                "category_id": cstr(row.get("category_id")),
                "price": price,
                "barcode": get_item_barcode(item_id),
                "is_available": get_item_availability(row, warehouse),
                "is_active": 0 if cint(row.get("disabled")) else 1,
                "is_prep_item": cint(row.get("custom_kopos_is_prep_item") or 0),
                "modifier_group_ids": get_item_modifier_groups(item_id),
            }
        )

    return items


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


def get_item_modifier_groups(item_code: str) -> list[str]:
    """Return modifier group ids linked to an Item's child table rows."""
    return frappe.get_all(
        "KoPOS Item Modifier Group",
        filters={
            "parent": item_code,
            "parenttype": "Item",
            "parentfield": "modifier_groups",
        },
        order_by="display_order asc, idx asc",
        pluck="modifier_group",
    )


def get_item_barcode(item_code: str) -> str | None:
    return frappe.db.get_value(
        "Item Barcode",
        {"parent": item_code, "parenttype": "Item", "parentfield": "barcodes"},
        "barcode",
    )


def get_item_availability(item: ERPRecord, warehouse: str | None = None) -> bool:
    """Resolve final item availability from override mode and stock."""
    if cint(item.get("disabled")):
        return False

    mode = cstr(item.get("custom_kopos_availability_mode") or "auto")
    if mode == "force_unavailable":
        return False
    if mode == "force_available":
        return True

    if not cint(item.get("custom_kopos_track_stock")) or not warehouse:
        return True

    bin_qty = flt(
        frappe.db.get_value(
            "Bin",
            {
                "item_code": item.get("item_code") or item.get("id"),
                "warehouse": warehouse,
            },
            "actual_qty",
        )
    )
    reserved_qty = get_pos_reserved_qty(
        item.get("item_code") or item.get("id"), warehouse
    )
    min_qty = flt(item.get("custom_kopos_min_qty") or 1)
    return (bin_qty - reserved_qty) >= min_qty


def get_pos_reserved_qty(item_code: str | None, warehouse: str | None) -> float:
    if not item_code or not warehouse:
        return 0

    from erpnext.accounts.doctype.pos_invoice.pos_invoice import (
        get_pos_reserved_qty as impl,
    )

    return flt(impl(item_code, warehouse) or 0)


def get_item_price(
    item_code: str, standard_rate: float, selling_price_list: str | None = None
) -> float:
    """Return price list rate when available, otherwise Item.standard_selling_rate."""
    if not selling_price_list:
        return standard_rate or 0

    price = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "selling": 1, "price_list": selling_price_list},
        "price_list_rate",
    )
    return flt(price) if price is not None else (standard_rate or 0)


def get_modifier_groups(since: str | None = None) -> list[ERPRecord]:
    """Return active modifier groups."""
    filters: dict[str, Any] = {"is_active": 1}
    if since:
        filters["modified"] = [">=", since]

    rows = frappe.get_all(
        "KoPOS Modifier Group",
        filters=filters,
        fields=[
            "name as id",
            "group_name as name",
            "selection_type",
            "is_required",
            "min_selections",
            "max_selections",
            "display_order",
            "parent_option_id",
        ],
        order_by="display_order asc, group_name asc",
    )

    return [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "selection_type": row.get("selection_type") or "single",
            "is_required": cint(row.get("is_required")),
            "min_selections": cint(row.get("min_selections") or 0),
            "max_selections": cint(row.get("max_selections") or 1),
            "display_order": cint(row.get("display_order") or 0),
            "parent_option_id": row.get("parent_option_id"),
        }
        for row in rows
    ]


def get_modifier_options(since: str | None = None) -> list[ERPRecord]:
    """Return active modifier options joined to active parent groups."""
    conditions = ["opt.is_active = 1", "grp.is_active = 1"]
    values: list[Any] = []
    if since:
        conditions.append("(opt.modified >= %s OR grp.modified >= %s)")
        values.extend([since, since])

    rows = frappe.db.sql(
        f"""
			SELECT
				opt.name AS id,
				opt.parent AS group_id,
				opt.option_name AS name,
				opt.price_adjustment,
				opt.is_default,
				opt.is_active,
				opt.display_order
			FROM `tabKoPOS Modifier Option` opt
			INNER JOIN `tabKoPOS Modifier Group` grp ON grp.name = opt.parent
			WHERE {" AND ".join(conditions)}
			ORDER BY opt.parent ASC, opt.display_order ASC, opt.option_name ASC
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
            "is_default": cint(row.get("is_default")),
            "is_active": cint(
                row.get("is_active") if row.get("is_active") is not None else 1
            ),
            "display_order": cint(row.get("display_order") or 0),
        }
        for row in rows
    ]


def get_tax_rate_value(
    pos_profile_name: str | None = None, device_id: str | None = None
) -> float:
    """Return the KoPOS SST rate as a decimal."""
    profile_data = None
    if cstr(device_id).strip():
        device_doc = get_device_doc(device_id=device_id)
        profile = frappe.get_doc("POS Profile", device_doc.pos_profile)
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


def get_item_modifiers_payload(item_code: str) -> list[ERPRecord]:
    """Return modifier groups with active options for a single item."""
    group_ids = get_item_modifier_groups(item_code)
    if not group_ids:
        return []

    options_by_group: dict[str, list[ERPRecord]] = {}
    for option in get_modifier_options():
        options_by_group.setdefault(cstr(option.get("group_id")), []).append(option)

    groups = []
    for group in get_modifier_groups():
        group_id = cstr(group.get("id"))
        if group_id not in group_ids:
            continue
        groups.append({**group, "options": options_by_group.get(group_id, [])})

    return groups


def cint(value: Any) -> int:
    return frappe.utils.cint(value)


def cstr(value: Any) -> str:
    return frappe.utils.cstr(value)


def flt(value: Any) -> float:
    return frappe.utils.flt(value)
