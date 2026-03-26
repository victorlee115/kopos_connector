# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import getdate, add_days, flt, today, now_datetime, cint
import json
import re

MAX_BATCH_SIZE = 1000


def extract_modifiers_from_description(description: str) -> list[dict]:
    """
    Parse modifiers from legacy description field.

    Expected format:
    Item Name

    Modifiers:
    - Oat Milk (+0.50)
    - Large (+1.00)
    """
    if not description or "Modifiers:" not in description:
        return []

    # Scope regex to only text after "Modifiers:" to avoid false positives
    modifiers_section = description.split("Modifiers:", 1)[1]

    pattern = r"-\s*([^(\n]+?)\s*\(([+-]?[\d.]+)\)"
    matches = re.findall(pattern, modifiers_section)

    modifiers = []
    for name, price_str in matches:
        modifiers.append(
            {
                "name": name.strip(),
                "price": flt(price_str),
            }
        )

    return modifiers


def smart_match_modifier_option(name: str) -> dict:
    """
    Try to match a modifier name to existing KoPOS Modifier Option.

    Returns dict with modifier_option and modifier_group if found.
    """
    if not name or len(name) < 2:
        return {
            "modifier_option": None,
            "modifier_group": None,
            "modifier_name": name,
            "group_name": "Unknown",
        }

    # Exact match first
    option = frappe.db.get_value(
        "KoPOS Modifier Option",
        {"modifier_name": name},
        ["name", "parent", "modifier_name"],
        as_dict=True,
    )

    if option:
        group_name = frappe.db.get_value(
            "KoPOS Modifier Group", option.parent, "group_name"
        )
        return {
            "modifier_option": option.name,
            "modifier_group": option.parent,
            "modifier_name": option.modifier_name,
            "group_name": group_name or "Unknown",
        }

    # Case-insensitive exact match
    escaped_name = name.replace("%", r"\%").replace("_", r"\_")
    option = frappe.db.sql(
        """
        SELECT name, parent, modifier_name 
        FROM `tabKoPOS Modifier Option`
        WHERE LOWER(modifier_name) = LOWER(%s)
        ORDER BY name ASC
        LIMIT 1
        """,
        (escaped_name,),
        as_dict=True,
    )

    if option:
        option = option[0]
        group_name = frappe.db.get_value(
            "KoPOS Modifier Group", option.parent, "group_name"
        )
        return {
            "modifier_option": option.name,
            "modifier_group": option.parent,
            "modifier_name": option.modifier_name,
            "group_name": group_name or "Unknown",
        }

    return {
        "modifier_option": None,
        "modifier_group": None,
        "modifier_name": name,
        "group_name": "Unknown",
    }


def _batch_match_modifiers(modifier_names: list[str]) -> dict[str, dict]:
    """
    Batch-resolve all modifier names in a single query.
    Returns dict mapping modifier_name -> option record.
    """
    if not modifier_names:
        return {}

    # Exact match
    options = frappe.db.sql(
        """
        SELECT opt.name, opt.parent, opt.modifier_name, grp.group_name
        FROM `tabKoPOS Modifier Option` opt
        INNER JOIN `tabKoPOS Modifier Group` grp ON grp.name = opt.parent
        WHERE opt.modifier_name IN %s
        """,
        (tuple(modifier_names),),
        as_dict=True,
    )

    result = {}
    for opt in options:
        result[opt.modifier_name] = {
            "modifier_option": opt.name,
            "modifier_group": opt.parent,
            "modifier_name": opt.modifier_name,
            "group_name": opt.group_name or "Unknown",
        }

    return result


def _backup_migration_json_fields(items: list[dict]) -> int:
    """
    Backup existing custom_kopos_modifiers values before migration overwrites them.
    Returns count of backed-up records.
    """
    backed_up = 0
    for item in items:
        existing = item.get("custom_kopos_modifiers")
        if existing and existing.strip() and existing.strip() != "{}":
            frappe.db.set_value(
                "POS Invoice Item",
                item["name"],
                "_kopos_modifiers_backup",
                existing,
                update_modified=False,
            )
            backed_up += 1

    # Commit backup before processing
    if backed_up > 0:
        frappe.db.commit()

    return backed_up


@frappe.whitelist()
def migrate_invoice_modifiers(batch_size: int = 500, dry_run: bool = False) -> dict:
    """
    Migrate modifiers from description field to child table.

    Args:
        batch_size: Number of items to process per batch (max 1000)
        dry_run: If True, return counts without making changes

    Returns:
        Summary of migration results
    """
    if not frappe.has_permission("POS Invoice Item", "write"):
        frappe.throw(_("Not permitted to run migrations"), frappe.PermissionError)

    batch_size = min(cint(batch_size), MAX_BATCH_SIZE)
    if batch_size <= 0:
        batch_size = 500

    results = {
        "processed": 0,
        "migrated": 0,
        "skipped": 0,
        "errors": 0,
        "unmatched": [],
        "failed_items": [],
        "backed_up": 0,
    }

    items = frappe.db.sql(
        """
        SELECT ii.name, ii.parent, ii.description, ii.custom_kopos_modifiers
        FROM `tabPOS Invoice Item` ii
        INNER JOIN `tabPOS Invoice` inv ON ii.parent = inv.name
        WHERE inv.docstatus = 1
          AND ii.description LIKE '%Modifiers:%'
          AND NOT EXISTS (
              SELECT 1 FROM `tabKoPOS Invoice Item Modifier` m 
              WHERE m.parent = ii.name
          )
        ORDER BY inv.posting_date DESC
        LIMIT %s
        """,
        (batch_size,),
        as_dict=True,
    )

    if not items:
        return results

    if not dry_run:
        results["backed_up"] = _backup_migration_json_fields(items)

    # Batch-resolve all modifier names
    all_names = set()
    for item in items:
        modifiers = extract_modifiers_from_description(item.get("description", ""))
        for mod in modifiers:
            all_names.add(mod["name"])

    batch_matches = _batch_match_modifiers(list(all_names)) if all_names else {}

    for item in items:
        try:
            modifiers = extract_modifiers_from_description(item.get("description", ""))

            if not modifiers:
                results["skipped"] += 1
                results["processed"] += 1
                continue

            if dry_run:
                results["migrated"] += 1
                results["processed"] += 1
                continue

            snapshot_modifiers = []
            invoice_item = frappe.get_doc("POS Invoice Item", item.name)

            for idx, mod in enumerate(modifiers):
                matched = batch_matches.get(mod["name"])

                if not matched:
                    matched = smart_match_modifier_option(mod["name"])

                if not matched["modifier_option"]:
                    if len(results["unmatched"]) < 100:
                        results["unmatched"].append(
                            {"item": item.name, "modifier": mod["name"]}
                        )

                invoice_item.append(
                    "custom_kopos_modifiers_table",
                    {
                        "modifier_group": matched["modifier_group"],
                        "modifier_group_name": matched["group_name"],
                        "modifier_option": matched["modifier_option"],
                        "modifier_name": matched["modifier_name"] or mod["name"],
                        "price_adjustment": flt(mod["price"], 2),
                        "base_price": flt(mod["price"], 2),
                        "is_default": 0,
                        "display_order": idx,
                    },
                )

                snapshot_modifiers.append(
                    {
                        "id": matched["modifier_option"] or "",
                        "group_id": matched["modifier_group"] or "",
                        "name": matched["modifier_name"] or mod["name"],
                        "group_name": matched["group_name"],
                        "price": flt(mod["price"], 2),
                        "base_price": flt(mod["price"], 2),
                        "is_default": False,
                    }
                )

            total = round(sum(m["price"] for m in snapshot_modifiers), 2)
            snapshot = {
                "version": "1.0",
                "modifiers": snapshot_modifiers,
                "total": total,
                "count": len(snapshot_modifiers),
            }
            invoice_item.custom_kopos_modifiers = json.dumps(
                snapshot, sort_keys=True, separators=(",", ":")
            )
            invoice_item.custom_kopos_modifier_total = total
            invoice_item.custom_kopos_has_modifiers = 1 if snapshot_modifiers else 0

            invoice_item.save(ignore_permissions=True)
            frappe.db.commit()
            results["migrated"] += 1

        except Exception as e:
            frappe.db.rollback()
            results["errors"] += 1
            results["failed_items"].append(item.name)
            frappe.log_error(
                title="KoPOS Modifier Migration Error",
                message=f"Item: {item.name}, Error: {str(e)}\n\n{frappe.get_traceback()}",
            )

        results["processed"] += 1

    return results


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
