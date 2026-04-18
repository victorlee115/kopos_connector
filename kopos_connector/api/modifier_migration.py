# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

from importlib import import_module
from typing import Mapping, Sequence, cast

import hashlib
import re
from decimal import Decimal, ROUND_HALF_UP

frappe = import_module("frappe")
frappe_utils = import_module("frappe.utils")

_ = frappe._
add_days = frappe_utils.add_days
cint = frappe_utils.cint
cstr = frappe_utils.cstr
flt = frappe_utils.flt
getdate = frappe_utils.getdate
now_datetime = frappe_utils.now_datetime
today = frappe_utils.today

FB_GROUP_CODE_PREFIX = "kopos-fb-group"
FB_MODIFIER_CODE_PREFIX = "kopos-fb-modifier"


def _get_field_value(source: object, fieldname: str) -> object:
    if isinstance(source, dict):
        return source.get(fieldname)
    return getattr(source, fieldname, None)


def _normalize_kopos_selection_type(selection_type: object) -> str:
    return (
        "Multiple" if cstr(selection_type).strip().lower() == "multiple" else "Single"
    )


def _stable_backfill_code(prefix: str, legacy_id: str) -> str:
    normalized_id = cstr(legacy_id).strip() or "legacy"
    canonical = re.sub(r"[^a-z0-9]+", "-", normalized_id.lower()).strip("-") or "legacy"
    slug = canonical
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{slug[:40]}-{digest}"


def _stable_fb_group_code(legacy_group_id: str) -> str:
    return _stable_backfill_code(FB_GROUP_CODE_PREFIX, legacy_group_id)


def _stable_fb_modifier_code(legacy_option_id: str) -> str:
    return _stable_backfill_code(FB_MODIFIER_CODE_PREFIX, legacy_option_id)


def _legacy_group_sort_key(group: Mapping[str, object]) -> tuple[int, str, str]:
    return (
        cint(group.get("display_order") or 0),
        cstr(group.get("group_name")).strip().lower(),
        cstr(group.get("legacy_id")).strip().lower(),
    )


def _legacy_option_sort_key(option: Mapping[str, object]) -> tuple[int, str, str]:
    return (
        cint(option.get("display_order") or 0),
        cstr(option.get("option_name")).strip().lower(),
        cstr(option.get("legacy_id")).strip().lower(),
    )


def _normalize_legacy_modifier_group(group_doc: object) -> dict[str, object]:
    selection_type = cstr(_get_field_value(group_doc, "selection_type")).strip().lower()
    option_rows = cast(list[object], _get_field_value(group_doc, "options") or [])
    options = [
        {
            "legacy_id": cstr(_get_field_value(option_row, "name")).strip(),
            "option_name": cstr(_get_field_value(option_row, "option_name")).strip(),
            "price_adjustment": flt(
                _get_field_value(option_row, "price_adjustment"), 2
            ),
            "is_default": cint(_get_field_value(option_row, "is_default")),
            "is_active": cint(_get_field_value(option_row, "is_active") or 0),
            "display_order": cint(_get_field_value(option_row, "display_order") or 0),
        }
        for option_row in option_rows
        if cstr(_get_field_value(option_row, "name")).strip()
    ]
    sorted_options = sorted(options, key=_legacy_option_sort_key)
    has_active_defaults = any(
        cint(option.get("is_default")) and cint(option.get("is_active"))
        for option in sorted_options
    )

    max_selection_default = 1 if selection_type != "multiple" else 0
    return {
        "legacy_id": cstr(_get_field_value(group_doc, "name")).strip(),
        "group_name": cstr(_get_field_value(group_doc, "group_name")).strip(),
        "selection_type": selection_type or "single",
        "is_required": cint(_get_field_value(group_doc, "is_required")),
        "min_selections": cint(_get_field_value(group_doc, "min_selections") or 0),
        "max_selections": cint(
            _get_field_value(group_doc, "max_selections") or max_selection_default
        ),
        "display_order": cint(_get_field_value(group_doc, "display_order") or 0),
        "is_active": cint(_get_field_value(group_doc, "is_active") or 0),
        "parent_option_id": cstr(
            _get_field_value(group_doc, "parent_option_id")
        ).strip()
        or None,
        "default_resolution_policy": (
            "Auto Apply Default"
            if has_active_defaults
            else "Require Explicit Selection"
        ),
        "options": sorted_options,
    }


def load_legacy_modifier_groups() -> list[dict[str, object]]:
    group_rows = frappe.get_all(
        "KoPOS Modifier Group",
        fields=["name"],
        order_by="display_order asc, group_name asc, name asc",
    )
    normalized_groups: list[dict[str, object]] = []
    for group_row in group_rows:
        group_name = cstr(_get_field_value(group_row, "name")).strip()
        if not group_name:
            continue
        normalized_groups.append(
            _normalize_legacy_modifier_group(
                frappe.get_doc("KoPOS Modifier Group", group_name)
            )
        )
    return sorted(normalized_groups, key=_legacy_group_sort_key)


def build_fb_modifier_backfill_plan(
    legacy_groups: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    normalized_groups = sorted(legacy_groups, key=_legacy_group_sort_key)
    group_codes_by_legacy_group: dict[str, str] = {}
    modifier_codes_by_legacy_option: dict[str, str] = {}
    fb_groups: list[dict[str, object]] = []
    fb_modifiers: list[dict[str, object]] = []
    pending_parent_links: list[dict[str, str]] = []
    unresolved_parent_links: list[dict[str, str]] = []

    for legacy_group in normalized_groups:
        legacy_group_id = cstr(legacy_group.get("legacy_id")).strip()
        group_code = _stable_fb_group_code(legacy_group_id)
        group_codes_by_legacy_group[legacy_group_id] = group_code

        fb_group = {
            "group_code": group_code,
            "group_name": cstr(legacy_group.get("group_name")).strip()
            or legacy_group_id,
            "selection_type": _normalize_kopos_selection_type(
                legacy_group.get("selection_type")
            ),
            "is_required": cint(legacy_group.get("is_required")),
            "min_selection": cint(legacy_group.get("min_selections") or 0),
            "max_selection": cint(
                legacy_group.get("max_selections")
                or (1 if legacy_group.get("selection_type") != "multiple" else 0)
            ),
            "display_order": cint(legacy_group.get("display_order") or 0),
            "active": cint(legacy_group.get("is_active") or 0),
            "parent_modifier": None,
            "default_resolution_policy": cstr(
                legacy_group.get("default_resolution_policy")
            ).strip()
            or "Require Explicit Selection",
        }

        legacy_options = cast(
            list[dict[str, object]], legacy_group.get("options") or []
        )
        for legacy_option in sorted(legacy_options, key=_legacy_option_sort_key):
            legacy_option_id = cstr(legacy_option.get("legacy_id")).strip()
            modifier_code = _stable_fb_modifier_code(legacy_option_id)
            modifier_codes_by_legacy_option[legacy_option_id] = modifier_code
            fb_modifiers.append(
                {
                    "modifier_code": modifier_code,
                    "modifier_name": cstr(legacy_option.get("option_name")).strip()
                    or legacy_option_id,
                    "modifier_group": group_code,
                    "kind": "Instruction Only",
                    "price_adjustment": float(
                        Decimal(
                            str(flt(legacy_option.get("price_adjustment") or 0))
                        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    ),
                    "is_default": cint(legacy_option.get("is_default")),
                    "display_order": cint(legacy_option.get("display_order") or 0),
                    "active": cint(legacy_option.get("is_active") or 0),
                }
            )

        parent_option_id = cstr(legacy_group.get("parent_option_id")).strip()
        if parent_option_id:
            pending_parent_links.append(
                {
                    "group_code": group_code,
                    "group_name": cstr(legacy_group.get("group_name")).strip()
                    or legacy_group_id,
                    "parent_option_id": parent_option_id,
                }
            )

        fb_groups.append(fb_group)

    groups_by_code = {group["group_code"]: group for group in fb_groups}
    for pending_link in pending_parent_links:
        parent_modifier = modifier_codes_by_legacy_option.get(
            pending_link["parent_option_id"]
        )
        if not parent_modifier:
            unresolved_parent_links.append(
                {
                    "group_name": pending_link["group_name"],
                    "parent_option_id": pending_link["parent_option_id"],
                }
            )
            continue
        groups_by_code[pending_link["group_code"]]["parent_modifier"] = parent_modifier

    if unresolved_parent_links:
        details = ", ".join(
            f"{row['group_name']} -> {row['parent_option_id']}"
            for row in unresolved_parent_links
        )
        frappe.throw(
            _(
                "Cannot backfill FB modifiers because the following parent_option_id references were not found: {0}"
            ).format(details),
            frappe.ValidationError,
        )

    return {
        "groups": fb_groups,
        "modifiers": fb_modifiers,
        "group_codes_by_legacy_group": group_codes_by_legacy_group,
        "modifier_codes_by_legacy_option": modifier_codes_by_legacy_option,
        "resolved_parent_links": len(
            [group for group in fb_groups if group.get("parent_modifier")]
        ),
    }


def _get_backfill_doc_name(
    doctype: str, code_field: str, code_value: str
) -> str | None:
    if not code_value:
        return None
    existing_name = frappe.db.get_value(doctype, {code_field: code_value}, "name")
    if existing_name:
        return cstr(existing_name).strip()
    if frappe.db.exists(doctype, code_value):
        return code_value
    return None


def _apply_backfill_payload(doc: object, payload: dict[str, object]) -> bool:
    changed = False
    for fieldname, expected_value in payload.items():
        if getattr(doc, fieldname, None) != expected_value:
            setattr(doc, fieldname, expected_value)
            changed = True
    return changed


def _upsert_backfill_doc(
    doctype: str, code_field: str, payload: dict[str, object]
) -> str:
    code_value = cstr(payload.get(code_field)).strip()
    existing_name = _get_backfill_doc_name(doctype, code_field, code_value)
    if existing_name:
        doc = frappe.get_doc(doctype, existing_name)
        if not _apply_backfill_payload(doc, payload):
            return "unchanged"
        doc.save(ignore_permissions=True)
        return "updated"

    doc = frappe.new_doc(doctype)
    _apply_backfill_payload(doc, payload)
    doc.insert(ignore_permissions=True)
    return "created"


def _update_backfill_counts(
    results: dict[str, object], prefix: str, action: str
) -> None:
    key = f"{prefix}_{action}"
    results[key] = cint(results.get(key) or 0) + 1


@frappe.whitelist()
def backfill_kopos_modifiers_to_fb(dry_run: bool = False) -> dict[str, object]:
    enforce_permissions = not cint(getattr(frappe.flags, "in_migrate", 0))
    if enforce_permissions and not frappe.has_permission("FB Modifier Group", "write"):
        frappe.throw(
            _("Not permitted to backfill FB Modifier Groups"), frappe.PermissionError
        )
    if enforce_permissions and not frappe.has_permission("FB Modifier", "write"):
        frappe.throw(
            _("Not permitted to backfill FB Modifiers"), frappe.PermissionError
        )

    legacy_groups = load_legacy_modifier_groups()
    plan = build_fb_modifier_backfill_plan(legacy_groups)
    planned_groups = cast(list[dict[str, object]], plan["groups"])
    planned_modifiers = cast(list[dict[str, object]], plan["modifiers"])
    results: dict[str, object] = {
        "dry_run": bool(dry_run),
        "legacy_groups": len(legacy_groups),
        "legacy_options": len(planned_modifiers),
        "resolved_parent_links": cint(plan["resolved_parent_links"]),
        "groups_created": 0,
        "groups_updated": 0,
        "groups_unchanged": 0,
        "modifiers_created": 0,
        "modifiers_updated": 0,
        "modifiers_unchanged": 0,
        "parent_links_updated": 0,
        "parent_links_unchanged": 0,
    }

    if dry_run or not legacy_groups:
        return results

    try:
        for group_payload in planned_groups:
            base_group_payload = dict(group_payload)
            base_group_payload.pop("parent_modifier", None)
            action = _upsert_backfill_doc(
                "FB Modifier Group", "group_code", base_group_payload
            )
            _update_backfill_counts(results, "groups", action)

        for modifier_payload in planned_modifiers:
            action = _upsert_backfill_doc(
                "FB Modifier", "modifier_code", modifier_payload
            )
            _update_backfill_counts(results, "modifiers", action)

        for group_payload in planned_groups:
            action = _upsert_backfill_doc(
                "FB Modifier Group",
                "group_code",
                {
                    "group_code": group_payload["group_code"],
                    "parent_modifier": group_payload.get("parent_modifier"),
                },
            )
            _update_backfill_counts(results, "parent_links", action)

        frappe.db.commit()
        return results
    except Exception as error:
        frappe.db.rollback()
        frappe.log_error(
            title="KoPOS FB Modifier Backfill Error",
            message=f"Error: {str(error)}\n\n{frappe.get_traceback()}",
        )
        raise


@frappe.whitelist()
def backfill_modifier_stats(
    from_date: str | None = None,
    to_date: str | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Backfill modifier stats for historical data after migration.
    """
    if not frappe.has_permission("KoPOS Modifier Stats", "write"):
        frappe.throw(_("Not permitted to run backfill"), frappe.PermissionError)

    from kopos_connector.api.modifiers import aggregate_modifier_stats

    if not from_date:
        earliest = frappe.db.sql(
            """
            SELECT MIN(posting_date) FROM `tabPOS Invoice`
            WHERE docstatus = 1 AND is_return = 0
            """,
            as_list=True,
        )[0][0]
        if not earliest:
            return {
                "total_days": 0,
                "successful_days": 0,
                "failed_days": [],
                "total_records": 0,
            }
        from_date = earliest

    if not to_date:
        to_date = add_days(today(), -1)

    start = getdate(from_date)
    end = getdate(to_date)

    if (end - start).days > 365:
        frappe.throw(_("Date range cannot exceed 365 days"))

    results = {
        "total_days": 0,
        "successful_days": 0,
        "failed_days": [],
        "total_records": 0,
    }

    current = start
    while current <= end:
        try:
            if dry_run:
                count = frappe.db.sql(
                    """
                    SELECT COUNT(DISTINCT m.modifier_option)
                    FROM `tabKoPOS Invoice Item Modifier` m
                    INNER JOIN `tabPOS Invoice Item` ii ON m.parent = ii.name
                    INNER JOIN `tabPOS Invoice` inv ON ii.parent = inv.name
                    WHERE inv.posting_date = %s
                      AND inv.docstatus = 1
                      AND inv.is_return = 0
                      AND m.modifier_option IS NOT NULL
                    """,
                    (current.isoformat(),),
                )[0][0]
                results["total_records"] += count or 0
            else:
                result = aggregate_modifier_stats(current.isoformat())
                results["total_records"] += result.get("processed", 0)
                results["successful_days"] += 1
        except Exception as e:
            results["failed_days"].append(
                {
                    "date": current.isoformat(),
                    "error": str(e),
                }
            )
            frappe.log_error(
                title="KoPOS Modifier Stats Backfill Error",
                message=f"Date: {current}, Error: {str(e)}\n\n{frappe.get_traceback()}",
            )

        results["total_days"] += 1
        current = add_days(current, 1)

    return results


@frappe.whitelist()
def get_migration_status() -> dict:
    """
    Get status of migration - how many invoices need migration.
    """
    if not frappe.has_permission("POS Invoice Item", "read"):
        frappe.throw(
            _("Not permitted to view migration status"), frappe.PermissionError
        )

    total_with_modifiers = frappe.db.sql(
        """
        SELECT COUNT(*)
        FROM `tabPOS Invoice Item` ii
        INNER JOIN `tabPOS Invoice` inv ON ii.parent = inv.name
        WHERE inv.docstatus = 1
          AND ii.description LIKE '%Modifiers:%'
        """,
        as_list=True,
    )[0][0]

    migrated = frappe.db.sql(
        """
        SELECT COUNT(DISTINCT m.parent)
        FROM `tabKoPOS Invoice Item Modifier` m
        """,
        as_list=True,
    )[0][0]

    pending = frappe.db.sql(
        """
        SELECT COUNT(*)
        FROM `tabPOS Invoice Item` ii
        INNER JOIN `tabPOS Invoice` inv ON ii.parent = inv.name
        WHERE inv.docstatus = 1
          AND ii.description LIKE '%Modifiers:%'
          AND NOT EXISTS (
              SELECT 1 FROM `tabKoPOS Invoice Item Modifier` m 
              WHERE m.parent = ii.name
          )
        """,
        as_list=True,
    )[0][0]

    return {
        "total_with_modifiers": cint(total_with_modifiers),
        "migrated": cint(migrated),
        "pending": cint(pending),
        "progress_percent": round(
            (cint(migrated) / cint(total_with_modifiers) * 100)
            if cint(total_with_modifiers) > 0
            else 0,
            1,
        ),
    }
