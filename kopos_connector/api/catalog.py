from __future__ import annotations

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
        profile_name = cstr(getattr(device_doc, "pos_profile", None)).strip()
        if not profile_name:
            return get_default_pos_profile()
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
    company = cstr((pos_profile or {}).get("company")).strip() or None

    row_by_item_id: dict[str, ERPRecord] = {}
    for row in get_saleable_item_rows(filters=filters, since=since):
        item_id = cstr(row.get("id") or row.get("item_code")).strip()
        if item_id:
            row_by_item_id[item_id] = row

    for row in get_saleable_item_rows(
        filters=filters,
        item_codes=get_recipe_changed_item_codes(company=company, since=since),
    ):
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
    modifier_groups_by_item = get_item_modifier_groups_map(rows, company=company)

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
                "modifier_group_ids": modifier_groups_by_item.get(item_id, []),
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

    return frappe.get_all(
        "Item",
        filters=query_filters,
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
            "custom_fb_recipe_required",
            "custom_fb_default_recipe",
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

    return {
        cstr(item_code).strip()
        for item_code in frappe.get_all(
            "FB Recipe", filters=filters, pluck="sellable_item"
        )
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
    item_rows: list[ERPRecord], company: str | None = None
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

    effective_recipe_by_item: dict[str, str] = {}
    current_time = now_datetime()
    for row in recipe_rows:
        item_code = cstr(row.get("sellable_item")).strip()
        recipe_name = cstr(row.get("name")).strip()
        if not item_code or not recipe_name:
            continue
        if not is_effective_recipe_row(row, current_time):
            continue
        existing_recipe_name = effective_recipe_by_item.get(item_code)
        if existing_recipe_name and existing_recipe_name != recipe_name:
            frappe.throw(
                "Multiple active FB Recipes were found for item {0}: {1}, {2}".format(
                    item_code,
                    existing_recipe_name,
                    recipe_name,
                ),
                frappe.ValidationError,
            )
        effective_recipe_by_item[item_code] = recipe_name

    required_recipe_items = sorted(
        {
            cstr(row.get("id") or row.get("item_code")).strip()
            for row in item_rows
            if cstr(row.get("id") or row.get("item_code")).strip()
            and (
                cint(row.get("custom_fb_recipe_required"))
                or cstr(row.get("custom_fb_default_recipe")).strip()
            )
        }
    )
    missing_recipe_items = [
        item_code
        for item_code in required_recipe_items
        if item_code not in effective_recipe_by_item
    ]
    if missing_recipe_items:
        frappe.throw(
            "No active FB Recipe was found for item(s): {0}".format(
                ", ".join(missing_recipe_items)
            ),
            frappe.ValidationError,
        )

    recipe_names = sorted(set(effective_recipe_by_item.values()))
    if not recipe_names:
        return {}

    allowed_group_rows = frappe.get_all(
        "FB Allowed Modifier Group",
        filters={
            "parent": ["in", recipe_names],
            "parenttype": "FB Recipe",
            "parentfield": "allowed_modifier_groups",
        },
        fields=["parent", "modifier_group", "display_order", "idx"],
        order_by="parent asc, display_order asc, idx asc",
    )
    allowed_group_ids = sorted(
        {
            cstr(row.get("modifier_group")).strip()
            for row in allowed_group_rows
            if cstr(row.get("modifier_group")).strip()
        }
    )
    active_group_ids = set(
        frappe.get_all(
            "FB Modifier Group",
            filters={"active": 1, "name": ["in", allowed_group_ids]},
            pluck="name",
        )
    )

    item_code_by_recipe_name = {
        recipe_name: item_code
        for item_code, recipe_name in effective_recipe_by_item.items()
    }
    group_ids_by_item: dict[str, list[str]] = {}
    for row in allowed_group_rows:
        recipe_name = cstr(row.get("parent")).strip()
        modifier_group = cstr(row.get("modifier_group")).strip()
        item_code = item_code_by_recipe_name.get(recipe_name)
        if (
            not item_code
            or not modifier_group
            or modifier_group not in active_group_ids
        ):
            continue

        item_group_ids = group_ids_by_item.setdefault(item_code, [])
        if modifier_group not in item_group_ids:
            item_group_ids.append(modifier_group)

    return group_ids_by_item


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
    filters: dict[str, Any] = {"active": 1}
    if since:
        filters["modified"] = [">=", since]

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

    return [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "selection_type": "multiple"
            if cstr(row.get("selection_type")).strip().lower() == "multiple"
            else "single",
            "is_required": cint(row.get("is_required")),
            "min_selections": cint(row.get("min_selection") or 0),
            "max_selections": cint(row.get("max_selection") or 1),
            "display_order": cint(row.get("display_order") or 0),
            "parent_option_id": cstr(row.get("parent_modifier")).strip() or None,
        }
        for row in rows
    ]


def get_modifier_options(since: str | None = None) -> list[ERPRecord]:
    conditions = ["opt.active = 1", "grp.active = 1"]
    values: list[Any] = []
    if since:
        conditions.append("(opt.modified >= %s OR grp.modified >= %s)")
        values.extend([since, since])

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
        profile_name = cstr(getattr(device_doc, "pos_profile", None)).strip()
        if not profile_name:
            return 0.08
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
