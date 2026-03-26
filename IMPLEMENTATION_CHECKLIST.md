# KoPOS Modifier System - Implementation Checklist

**Context**: This is a detailed step-by-step guide for implementing the KoPOS Modifier System. Follow each step exactly as written. Do not skip any step. Verify each step before moving to the next.

**Working Directory**: `/Users/victor/dev/jiji/JiJiPOS-Everything/erpnext/kopos_connector-modifiers/`

**Branch**: `feature/kopos-modifiers`

---

## Pre-Flight Checklist

- [ ] Verify you are in the correct worktree: `git worktree list`
- [ ] Verify you are on the correct branch: `git branch --show-current`
- [ ] Verify no uncommitted changes: `git status`

---

## Phase 1: Critical Security & Bug Fixes

### Step 1.1: Fix XSS Vulnerability in show_item_modifiers()

**File**: `kopos_connector/install/install.py`

**Task**: Escape mod.name and mod.group_name to prevent XSS

**Find this code block (approximately lines 764-772)**:
```python
    snapshot.modifiers.forEach(mod => {
        table.find("tbody").append($(`
            <tr>
                <td>${mod.name || "-"}</td>
                <td>${mod.group_name || "-"}</td>
                <td class="text-right">${format_currency(mod.price || 0)}</td>
            </tr>
        `));
    });
```

**Replace with**:
```python
    snapshot.modifiers.forEach(mod => {
        table.find("tbody").append($(`
            <tr>
                <td>${koposEscapeHtml(mod.name) || "-"}</td>
                <td>${koposEscapeHtml(mod.group_name) || "-"}</td>
                <td class="text-right">${format_currency(mod.price || 0)}</td>
            </tr>
        `));
    });
```

**Verification**: Search for `${mod.name` - should only appear escaped as `${koposEscapeHtml(mod.name)}`

---

### Step 1.2: Fix XSS Vulnerability in show_modifier_summary()

**File**: `kopos_connector/install/install.py`

**Task**: Escape item.item_name, mod.name, and mod.group_name

**Find this code block (approximately lines 815-823)**:
```python
        snapshot.modifiers.forEach(mod => {
            table.find("tbody").append($(`
                <tr>
                    <td>${item.item_name}</td>
                    <td>${mod.name} (${mod.group_name})</td>
                    <td class="text-right">${format_currency(mod.price || 0)}</td>
                </tr>
            `));
        });
```

**Replace with**:
```python
        snapshot.modifiers.forEach(mod => {
            table.find("tbody").append($(`
                <tr>
                    <td>${koposEscapeHtml(item.item_name)}</td>
                    <td>${koposEscapeHtml(mod.name)} (${koposEscapeHtml(mod.group_name)})</td>
                    <td class="text-right">${format_currency(mod.price || 0)}</td>
                </tr>
            `));
        });
```

**Verification**: Search for `${item.item_name}` - should only appear escaped

---

### Step 1.3: Fix JavaScript Syntax Error

**File**: `kopos_connector/install/install.py`

**Task**: Fix missing closing parenthesis in onclick handler

**Find this line (approximately line 719)**:
```python
                  onclick="show_item_modifiers('${koposEscapeHtml(row.name}')">
```

**Replace with**:
```python
                  onclick="show_item_modifiers('${koposEscapeHtml(row.name)}')">
```

**Verification**: The onclick handler should have matching parentheses

---

### Step 1.4: Fix Custom Field Naming

**File**: `kopos_connector/install/install.py`

**Task**: Remove manual `custom_` prefix since Frappe adds it automatically

**Find this block (approximately lines 132-140)**:
```python
            {
                "fieldname": "modifier_groups",
                "label": "KoPOS Modifier Groups",
                "fieldtype": "Table",
                "options": "KoPOS Item Modifier Group",
                "insert_after": "kopos_modifiers_section",
                "description": "Link modifier groups to this item for customization options "
                "(size, milk type, add-ons, etc.)",
            },
```

**Replace with**:
```python
            {
                "fieldname": "kopos_modifier_groups",
                "label": "KoPOS Modifier Groups",
                "fieldtype": "Table",
                "options": "KoPOS Item Modifier Group",
                "insert_after": "kopos_modifiers_section",
                "description": "Link modifier groups to this item for customization options "
                "(size, milk type, add-ons, etc.)",
            },
```

**Note**: Frappe will automatically add `custom_` prefix, resulting in `custom_kopos_modifier_groups`

**Verification**: The fieldname should be `kopos_modifier_groups` (without `custom_` prefix)

---

### Step 1.5: Add Parent Index to Child Table

**File**: `kopos_connector/kopos/doctype/kopos_invoice_item_modifier/kopos_invoice_item_modifier.json`

**Task**: Add index on parent field for aggregation query performance

**Find this block (approximately lines 89-92)**:
```json
  "indexes": [
    {"fields": ["modifier_option", "modifier_group"]},
    {"fields": ["modifier_name"]}
  ]
```

**Replace with**:
```json
  "indexes": [
    {"fields": ["parent"]},
    {"fields": ["modifier_option", "modifier_group"]},
    {"fields": ["modifier_name"]}
  ]
```

**Verification**: The JSON should have 3 index entries, with "parent" as the first

---

### Step 1.6: Add Covering Index for Aggregation

**File**: `kopos_connector/kopos/doctype/kopos_invoice_item_modifier/kopos_invoice_item_modifier.json`

**Task**: Add covering index to optimize aggregation query

**Find the indexes block you just modified**:
```json
  "indexes": [
    {"fields": ["parent"]},
    {"fields": ["modifier_option", "modifier_group"]},
    {"fields": ["modifier_name"]}
  ]
```

**Replace with**:
```json
  "indexes": [
    {"fields": ["parent"]},
    {"fields": ["modifier_option", "modifier_group"]},
    {"fields": ["modifier_name"]},
    {"fields": ["modifier_option", "modifier_group", "modifier_name", "modifier_group_name", "price_adjustment", "parent"], "name": "idx_aggregation_covering"}
  ]
```

**Verification**: The JSON should have 4 index entries

---

## Phase 2: Create ERPNext Script Report

### Step 2.1: Create Report Directory Structure

**Task**: Create the directory structure for the report

**Execute these commands**:
```bash
mkdir -p kopos_connector/kopos/report/modifier_sales_analytics
touch kopos_connector/kopos/report/modifier_sales_analytics/__init__.py
touch kopos_connector/kopos/report/modifier_sales_analytics/modifier_sales_analytics.json
touch kopos_connector/kopos/report/modifier_sales_analytics/modifier_sales_analytics.py
touch kopos_connector/kopos/report/modifier_sales_analytics/modifier_sales_analytics.js
```

**Verification**: All 4 files should exist in the directory

---

### Step 2.2: Create Report JSON Configuration

**File**: `kopos_connector/kopos/report/modifier_sales_analytics/modifier_sales_analytics.json`

**Task**: Create the report metadata file

**Write this exact content**:
```json
{
 "add_total_row": 1,
 "disabled": 0,
 "doctype": "Report",
 "is_standard": "No",
 "module": "KoPOS",
 "name": "Modifier Sales Analytics",
 "ref_doctype": "POS Invoice",
 "report_name": "Modifier Sales Analytics",
 "report_type": "Script Report",
 "roles": [
  {"role": "POS Manager"},
  {"role": "Accounts Manager"}
 ],
 "prepared_report": 0,
 "timeout": 300
}
```

**Verification**: JSON should be valid (no trailing commas)

---

### Step 2.3: Create Report Python Implementation

**File**: `kopos_connector/kopos/report/modifier_sales_analytics/modifier_sales_analytics.py`

**Task**: Create the report backend logic with 3 charts

**Write this exact content**:
```python
# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import getdate, flt, add_days, cint
from frappe.query_builder import DocType, Order


def execute(filters=None):
    """Execute the Modifier Sales Analytics report."""
    filters = frappe._dict(filters or {})
    
    validate_filters(filters)
    
    columns = get_columns(filters)
    data = get_data(filters)
    chart = get_chart_data(data, filters) if data else None
    summary = get_summary(data) if data else []
    
    return columns, data, None, chart, summary


def validate_filters(filters):
    """Validate report filters."""
    if not filters.from_date or not filters.to_date:
        frappe.throw(_("From Date and To Date are required"))
    
    if getdate(filters.from_date) > getdate(filters.to_date):
        frappe.throw(_("From Date cannot be after To Date"))
    
    days = (getdate(filters.to_date) - getdate(filters.from_date)).days
    if days > 365:
        frappe.throw(_("Date range cannot exceed 365 days"))


def get_columns(filters):
    """Define report columns."""
    return [
        {
            "label": _("Modifier"),
            "fieldname": "modifier_name",
            "fieldtype": "Data",
            "width": 180
        },
        {
            "label": _("Group"),
            "fieldname": "group_name",
            "fieldtype": "Data",
            "width": 120
        },
        {
            "label": _("Selections"),
            "fieldname": "total_selections",
            "fieldtype": "Int",
            "width": 100
        },
        {
            "label": _("Revenue"),
            "fieldname": "total_revenue",
            "fieldtype": "Currency",
            "width": 120
        },
        {
            "label": _("Avg Price"),
            "fieldname": "avg_price",
            "fieldtype": "Currency",
            "width": 100
        },
    ]


def get_data(filters):
    """Fetch report data using Query Builder."""
    Stats = DocType("KoPOS Modifier Stats")
    
    total_selections = frappe.qb.functions.Sum(Stats.selection_count).as_("total_selections")
    total_revenue = frappe.qb.functions.Sum(Stats.revenue).as_("total_revenue")
    
    query = (
        frappe.qb.from_(Stats)
        .select(
            Stats.modifier_name,
            Stats.group_name,
            total_selections,
            total_revenue,
        )
        .where(Stats.date.between(filters.from_date, filters.to_date))
        .groupby(Stats.modifier_name, Stats.group_name)
        .orderby(total_revenue, order=Order.desc)
    )
    
    if filters.modifier_group:
        query = query.where(Stats.modifier_group == filters.modifier_group)
    
    data = query.run(as_dict=True)
    
    for row in data:
        row["avg_price"] = (
            flt(row.total_revenue) / cint(row.total_selections)
            if row.total_selections and cint(row.total_selections) > 0
            else 0
        )
    
    return data


def get_chart_data(data, filters):
    """Generate chart data. Frappe only supports ONE chart per report."""
    if not data:
        return None
    
    top_modifiers = sorted(data, key=lambda x: x.total_revenue or 0, reverse=True)[:10]
    
    return {
        "title": _("Top 10 Modifiers by Revenue"),
        "data": {
            "labels": [d.modifier_name for d in top_modifiers],
            "datasets": [{
                "name": _("Revenue"),
                "values": [flt(d.total_revenue) for d in top_modifiers]
            }]
        },
        "type": "bar",
        "colors": ["#F59E0B"],
        "fieldtype": "Currency"
    }


def get_summary(data):
    """Generate report summary."""
    if not data:
        return []
    
    total_selections = sum(cint(d.total_selections) for d in data)
    total_revenue = sum(flt(d.total_revenue) for d in data)
    
    return [
        {"label": _("Total Selections"), "value": total_selections, "datatype": "Int"},
        {"label": _("Total Revenue"), "value": total_revenue, "datatype": "Currency"},
        {"label": _("Unique Modifiers"), "value": len(data), "datatype": "Int"},
    ]
```

**Verification**: File should have no syntax errors

---

### Step 2.4: Create Report JavaScript Filters

**File**: `kopos_connector/kopos/report/modifier_sales_analytics/modifier_sales_analytics.js`

**Task**: Create the filter UI for the report

**Write this exact content**:
```javascript
// Copyright (c) 2026, KoPOS and contributors
// For license information, please see license.txt

frappe.query_reports["Modifier Sales Analytics"] = {
    filters: [
        {
            fieldname: "from_date",
            label: __("From Date"),
            fieldtype: "Date",
            default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
            reqd: 1
        },
        {
            fieldname: "to_date",
            label: __("To Date"),
            fieldtype: "Date",
            default: frappe.datetime.get_today(),
            reqd: 1
        },
        {
            fieldname: "modifier_group",
            label: __("Modifier Group"),
            fieldtype: "Link",
            options: "KoPOS Modifier Group"
        }
    ],

    onload: function(report) {
        report.page.add_inner_button(__("Refresh Stats"), function() {
            frappe.confirm(
                __("Refresh modifier stats for this date range?"),
                function() {
                    frappe.call({
                        method: "kopos_connector.api.modifiers.aggregate_modifier_stats_range",
                        args: {
                            from_date: frappe.query_report.get_filter_value("from_date"),
                            to_date: frappe.query_report.get_filter_value("to_date")
                        },
                        callback: function(r) {
                            if (r.message) {
                                frappe.msgprint(__("Stats refreshed: {0} records processed", [r.message]));
                                report.refresh();
                            }
                        }
                    });
                }
            );
        });
    }
};
```

**Verification**: JavaScript should be valid

---

## Phase 3: Create Data Migration Module

### Step 3.1: Create Migration Python File

**File**: `kopos_connector/api/modifier_migration.py`

**Task**: Create migration utilities for historical data

**Write this exact content**:
```python
# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import getdate, add_days, flt, today, now_datetime
import json
import re


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
    
    pattern = r'-\s*([^(\n]+?)\s*\(([+-]?[\d.]+)\)'
    matches = re.findall(pattern, description)
    
    modifiers = []
    for name, price_str in matches:
        modifiers.append({
            "name": name.strip(),
            "price": flt(price_str),
        })
    
    return modifiers


def smart_match_modifier_option(name: str) -> dict:
    """
    Try to match a modifier name to existing KoPOS Modifier Option.
    
    Returns dict with modifier_option and modifier_group if found.
    """
    if not name:
        return {
            "modifier_option": None,
            "modifier_group": None,
            "modifier_name": name,
            "group_name": "Unknown"
        }
    
    option = frappe.db.get_value(
        "KoPOS Modifier Option",
        {"modifier_name": name},
        ["name", "parent", "modifier_name"],
        as_dict=True
    )
    
    if option:
        group_name = frappe.db.get_value(
            "KoPOS Modifier Group", option.parent, "modifier_group_name"
        )
        return {
            "modifier_option": option.name,
            "modifier_group": option.parent,
            "modifier_name": option.modifier_name,
            "group_name": group_name or "Unknown"
        }
    
    option = frappe.db.sql(
        """
        SELECT name, parent, modifier_name 
        FROM `tabKoPOS Modifier Option`
        WHERE LOWER(modifier_name) LIKE LOWER(%s)
        LIMIT 1
        """,
        (f"%{name}%",),
        as_dict=True
    )
    
    if option:
        option = option[0]
        group_name = frappe.db.get_value(
            "KoPOS Modifier Group", option.parent, "modifier_group_name"
        )
        return {
            "modifier_option": option.name,
            "modifier_group": option.parent,
            "modifier_name": option.modifier_name,
            "group_name": group_name or "Unknown"
        }
    
    return {
        "modifier_option": None,
        "modifier_group": None,
        "modifier_name": name,
        "group_name": "Unknown"
    }


@frappe.whitelist()
def migrate_invoice_modifiers(batch_size: int = 500, dry_run: bool = False) -> dict:
    """
    Migrate modifiers from description field to child table.
    
    Args:
        batch_size: Number of items to process per batch
        dry_run: If True, return counts without making changes
    
    Returns:
        Summary of migration results
    """
    results = {
        "processed": 0,
        "migrated": 0,
        "skipped": 0,
        "errors": 0,
        "unmatched": [],
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
    
    for item in items:
        try:
            modifiers = extract_modifiers_from_description(item.description)
            
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
                matched = smart_match_modifier_option(mod["name"])
                
                if not matched["modifier_option"]:
                    if len(results["unmatched"]) < 100:
                        results["unmatched"].append({
                            "item": item.name,
                            "modifier": mod["name"]
                        })
                
                invoice_item.append(
                    "custom_kopos_modifiers_table",
                    {
                        "modifier_group": matched["modifier_group"],
                        "modifier_group_name": matched["group_name"],
                        "modifier_option": matched["modifier_option"],
                        "modifier_name": matched["modifier_name"] or mod["name"],
                        "price_adjustment": mod["price"],
                        "display_order": idx,
                    },
                )
                
                snapshot_modifiers.append({
                    "id": matched["modifier_option"],
                    "name": matched["modifier_name"] or mod["name"],
                    "group_name": matched["group_name"],
                    "price": mod["price"],
                })
            
            snapshot = {
                "version": "1.0",
                "modifiers": snapshot_modifiers,
                "total": sum(m["price"] for m in snapshot_modifiers),
                "count": len(snapshot_modifiers),
            }
            invoice_item.custom_kopos_modifiers = json.dumps(snapshot)
            invoice_item.custom_kopos_has_modifiers = len(snapshot_modifiers) > 0
            
            invoice_item.save(ignore_permissions=True)
            results["migrated"] += 1
            
        except Exception as e:
            results["errors"] += 1
            frappe.log_error(
                title="KoPOS Modifier Migration Error",
                message=f"Item: {item.name}, Error: {str(e)}\n\n{frappe.get_traceback()}",
            )
        
        results["processed"] += 1
    
    if not dry_run:
        frappe.db.commit()
    
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
    from kopos_connector.api.modifiers import aggregate_modifier_stats
    
    if not from_date:
        earliest = frappe.db.sql(
            """
            SELECT MIN(posting_date) FROM `tabPOS Invoice`
            WHERE docstatus = 1 AND is_return = 0
            """,
            as_list=True,
        )[0][0]
        from_date = earliest or today()
    
    if not to_date:
        to_date = add_days(today(), -1)
    
    start = getdate(from_date)
    end = getdate(to_date)
    
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
            results["failed_days"].append({
                "date": current.isoformat(),
                "error": str(e),
            })
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
        "total_with_modifiers": total_with_modifiers,
        "migrated": migrated,
        "pending": pending,
        "progress_percent": round((migrated / total_with_modifiers * 100) if total_with_modifiers > 0 else 0, 1)
    }
```

**Verification**: File should have no syntax errors

---

## Phase 4: Refactor Aggregation for Performance

### Step 4.1: Replace aggregate_modifier_stats with Idempotent Version

**File**: `kopos_connector/api/modifiers.py`

**Task**: Replace the existing aggregation function with Query Builder and idempotent pattern

**Find the function `aggregate_modifier_stats` (approximately lines 480-573)**

**Replace the entire function with**:
```python
def aggregate_modifier_stats(date: str | None = None) -> dict:
    """
    Aggregate modifier stats for a given date.
    
    Uses DELETE + bulk INSERT for idempotency.
    Uses Query Builder instead of raw SQL for maintainability.
    
    Args:
        date: Date to aggregate (default: yesterday)
    
    Returns:
        dict with processed, failed, and error details
    """
    from frappe.utils import getdate, today, flt, cint, add_days
    from frappe.query_builder import DocType
    
    result = {"processed": 0, "failed": 0, "errors": []}
    
    if date:
        try:
            target_date = getdate(date)
        except (ValueError, TypeError):
            frappe.log_error(
                title="KoPOS Modifier Stats Error",
                message=f"Invalid date format: {date}",
            )
            return result
    else:
        target_date = getdate(add_days(today(), -1))
    
    if target_date > getdate(today()):
        frappe.log_error(
            title="KoPOS Modifier Stats Error",
            message=f"Cannot aggregate stats for future dates: {target_date}",
        )
        return result
    
    date_str = target_date.isoformat()
    
    InvoiceItemModifier = DocType("KoPOS Invoice Item Modifier")
    InvoiceItem = DocType("POS Invoice Item")
    Invoice = DocType("POS Invoice")
    
    try:
        stats = (
            frappe.qb.from_(InvoiceItemModifier)
            .inner_join(InvoiceItem)
            .on(InvoiceItemModifier.parent == InvoiceItem.name)
            .inner_join(Invoice)
            .on(InvoiceItem.parent == Invoice.name)
            .select(
                InvoiceItemModifier.modifier_option,
                InvoiceItemModifier.modifier_group,
                InvoiceItemModifier.modifier_name,
                InvoiceItemModifier.modifier_group_name.as_("group_name"),
                frappe.qb.functions.Count("*").as_("selection_count"),
                frappe.qb.functions.Sum(
                    InvoiceItemModifier.price_adjustment * InvoiceItem.qty
                ).as_("revenue"),
            )
            .where(Invoice.posting_date == date_str)
            .where(Invoice.docstatus == 1)
            .where(Invoice.is_return == 0)
            .where(InvoiceItemModifier.modifier_option.isnotnull())
            .groupby(
                InvoiceItemModifier.modifier_option,
                InvoiceItemModifier.modifier_group,
                InvoiceItemModifier.modifier_name,
                InvoiceItemModifier.modifier_group_name,
            )
            .run(as_dict=True)
        )
    except Exception as e:
        frappe.log_error(
            title="KoPOS Modifier Stats Query Error",
            message=f"Date: {date_str}, Error: {str(e)}\n\n{frappe.get_traceback()}",
        )
        _notify_aggregation_failure(date_str, str(e))
        return result
    
    if not stats:
        frappe.logger("kopos").info(f"No modifier stats to aggregate for {date_str}")
        return result
    
    frappe.db.delete("KoPOS Modifier Stats", {"date": date_str})
    
    for stat in stats:
        try:
            doc = frappe.new_doc("KoPOS Modifier Stats")
            doc.date = date_str
            doc.modifier_option = stat.modifier_option
            doc.modifier_group = stat.modifier_group
            doc.modifier_name = stat.modifier_name
            doc.group_name = stat.group_name
            doc.selection_count = cint(stat.selection_count)
            doc.revenue = flt(stat.revenue) or 0
            doc.insert(ignore_permissions=True)
            result["processed"] += 1
        except Exception as e:
            result["failed"] += 1
            result["errors"].append({
                "modifier_option": stat.modifier_option,
                "error": str(e),
            })
    
    frappe.db.commit()
    
    frappe.logger("kopos").info(
        f"Modifier stats aggregated: {result['processed']} records for {date_str}"
    )
    
    return result


def _notify_aggregation_failure(date: str, error: str) -> None:
    """Send email notification for aggregation failures."""
    from frappe.utils import now_datetime
    from html import escape
    
    recipients = frappe.get_all(
        "User",
        filters={"role": "System Manager", "enabled": 1},
        pluck="email"
    )
    
    if not recipients:
        return
    
    frappe.sendmail(
        recipients=recipients,
        subject=f"[KoPOS] Modifier Stats Aggregation Failed - {date}",
        message=f"""
        <p>The daily modifier stats aggregation failed.</p>
        <p><strong>Date:</strong> {date}</p>
        <p><strong>Time:</strong> {now_datetime()}</p>
        <p><strong>Error:</strong> {escape(error)}</p>
        <p>Please check the Error Log for details.</p>
        <p>Retry command:</p>
        <code>bench execute kopos_connector.api.modifiers.aggregate_modifier_stats --kwargs '{{"date": "{date}"}}'</code>
        """,
    )
```

**Verification**: 
- Query uses `frappe.qb` (Query Builder)
- Function returns dict instead of int
- Uses DELETE + INSERT pattern (idempotent)

---

### Step 4.2: Add Rate Limiting to Report API

**File**: `kopos_connector/api/modifiers.py`

**Task**: Add rate limiting to get_modifier_sales_report

**Find the function `get_modifier_sales_report` (approximately line 576)**

**Add decorator before `@frappe.whitelist()`**:
```python
@frappe.whitelist()
@frappe.rate_limit(limit=10, seconds=60)
def get_modifier_sales_report(
```

**Also add date range validation inside the function** (after the date parsing):
```python
    try:
        from_date = getdate(from_date).isoformat()
        to_date = getdate(to_date).isoformat()
    except (ValueError, TypeError):
        frappe.throw(_("Invalid date format"), frappe.ValidationError)
    
    # Add this validation
    from_date_obj = getdate(from_date)
    to_date_obj = getdate(to_date)
    
    if (to_date_obj - from_date_obj).days > 365:
        frappe.throw(_("Date range cannot exceed 365 days"), frappe.ValidationError)
```

**Verification**: Rate limit decorator should be present

---

### Step 4.3: Add Range Aggregation Function

**File**: `kopos_connector/api/modifiers.py`

**Task**: Add helper function for aggregating date ranges (used by report refresh button)

**Add this function at the end of the file**:
```python
@frappe.whitelist()
def aggregate_modifier_stats_range(from_date: str, to_date: str) -> int:
    """
    Aggregate modifier stats for a date range.
    
    Args:
        from_date: Start date
        to_date: End date
    
    Returns:
        Total records processed
    """
    from frappe.utils import getdate, add_days
    
    start = getdate(from_date)
    end = getdate(to_date)
    
    if start > end:
        frappe.throw(_("From Date cannot be after To Date"))
    
    total_processed = 0
    current = start
    
    while current <= end:
        result = aggregate_modifier_stats(current.isoformat())
        total_processed += result.get("processed", 0)
        current = add_days(current, 1)
    
    return total_processed


@frappe.whitelist()
def retry_failed_aggregations(from_date: str, to_date: str) -> dict:
    """
    Admin endpoint to retry failed aggregation dates.
    Requires System Manager role.
    """
    if not frappe.has_permission("KoPOS Modifier Stats", "write"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    
    from frappe.utils import getdate, add_days
    
    start = getdate(from_date)
    end = getdate(to_date)
    
    results = []
    current = start
    
    while current <= end:
        result = aggregate_modifier_stats(current.isoformat())
        results.append({"date": current.isoformat(), **result})
        current = add_days(current, 1)
    
    return {"retries": results}
```

**Verification**: Both new functions should be present

---

## Phase 5: Update Hooks for Static JS

### Step 5.1: Add doctype_js to hooks.py

**File**: `kopos_connector/hooks.py`

**Task**: Configure static JS files for DocTypes

**Find this section (approximately lines 39-43)**:
```python
# include js in doctype views
# POS Profile setup shortcut is injected via Client Script during migrate/install.
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}
```

**Add after this section**:
```python
# include js in doctype views
doctype_js = {
    "POS Invoice": "public/js/pos_invoice.js",
}
```

**Verification**: `doctype_js` should be defined

---

### Step 5.2: Create Static JS Directory

**Task**: Create directory structure

**Execute**:
```bash
mkdir -p kopos_connector/public/js
```

---

### Step 5.3: Create Static JS File for POS Invoice

**File**: `kopos_connector/public/js/pos_invoice.js`

**Task**: Create clean, namespaced client script

**Write this exact content**:
```javascript
// Copyright (c) 2026, KoPOS and contributors
// For license information, please see license.txt

/**
 * KoPOS Modifier Display for POS Invoice
 * 
 * This script adds modifier display functionality to POS Invoice forms.
 * It renders modifier badges on line items and provides a summary view.
 */

frappe.ui.form.on("POS Invoice", {
    refresh: function(frm) {
        if (frm.doc.docstatus === 1) {
            kopos_modifier_ui.add_summary_button(frm);
        }
        kopos_modifier_ui.render_badges(frm);
    }
});


var kopos_modifier_ui = {
    _cache: new Map(),
    
    add_summary_button: function(frm) {
        const modifierCount = frm.doc.items.reduce(
            (sum, item) => sum + (item.custom_kopos_has_modifiers || 0), 0
        );
        
        if (modifierCount > 0) {
            frm.add_custom_button(__("Modifier Summary"), function() {
                kopos_modifier_ui.show_summary(frm);
            }, __("View"));
        }
    },
    
    render_badges: function(frm) {
        if (!frm.fields_dict.items || !frm.fields_dict.items.grid) {
            return;
        }
        
        frm.doc.items.forEach(function(item) {
            if (item.custom_kopos_has_modifiers) {
                kopos_modifier_ui._show_badge(frm, item);
            }
        });
    },
    
    _show_badge: function(frm, item) {
        const count = this._get_modifier_count(item);
        if (count === 0) return;
        
        const badge = $('<span class="modifier-badge label label-info" style="margin-left: 8px; cursor: pointer">' +
            '<i class="fa fa-plus-circle"></i> ' + count + ' ' + __("modifiers") + '</span>');
        
        badge.on('click', function() {
            kopos_modifier_ui.show_item_modifiers(item.name, frm);
        });
        
        const $row = this._get_row_element(frm, item.name);
        if ($row) {
            $row.find(".modifier-badge").remove();
            $row.find("div[data-fieldname='item_code']").append(badge);
        }
    },
    
    _get_row_element: function(frm, rowName) {
        const grid = frm.fields_dict.items?.grid;
        if (!grid) return null;
        
        const escapedName = this._escape_html(rowName);
        return grid.wrapper?.find('[data-name="' + escapedName + '"]');
    },
    
    _get_modifier_count: function(item) {
        try {
            const snapshot = JSON.parse(item.custom_kopos_modifiers || "{}");
            return snapshot.count || snapshot.modifiers?.length || 0;
        } catch (e) {
            return 0;
        }
    },
    
    show_item_modifiers: function(itemName, frm) {
        const item = frm.doc.items.find(function(i) { return i.name === itemName; });
        if (!item) return;
        
        let snapshot = {};
        try {
            snapshot = JSON.parse(item.custom_kopos_modifiers || "{}");
        } catch (e) {
            console.warn("Invalid modifier JSON for item:", itemName);
            return;
        }
        
        const table = $('<table class="table table-bordered">' +
            '<thead><tr>' +
            '<th>' + __("Item") + '</th>' +
            '<th>' + __("Group") + '</th>' +
            '<th class="text-right">' + __("Price") + '</th>' +
            '</tr></thead><tbody></tbody></table>');
        
        if (snapshot.modifiers && snapshot.modifiers.length > 0) {
            snapshot.modifiers.forEach(function(mod) {
                table.find("tbody").append(
                    $('<tr>' +
                        '<td>' + kopos_modifier_ui._escape_html(mod.name || "-") + '</td>' +
                        '<td>' + kopos_modifier_ui._escape_html(mod.group_name || "-") + '</td>' +
                        '<td class="text-right">' + kopos_modifier_ui._format_currency(mod.price || 0) + '</td>' +
                        '</tr>')
                );
            });
        }
        
        frappe.msgprint({
            title: __("Modifiers for {0}").format(this._escape_html(item.item_name || item.item_code)),
            message: table,
            wide: true
        });
    },
    
    show_summary: function(frm) {
        const items = frm.doc.items.filter(function(i) { return i.custom_kopos_has_modifiers; });
        
        const table = $('<table class="table table-bordered">' +
            '<thead><tr>' +
            '<th>' + __("Item") + '</th>' +
            '<th>' + __("Modifiers") + '</th>' +
            '<th class="text-right">' + __("Total") + '</th>' +
            '</tr></thead><tbody></tbody></table>');
        
        items.forEach(function(item) {
            let snapshot = {};
            try {
                snapshot = JSON.parse(item.custom_kopos_modifiers || "{}");
            } catch (e) {
                return;
            }
            
            if (snapshot.modifiers && snapshot.modifiers.length > 0) {
                snapshot.modifiers.forEach(function(mod) {
                    table.find("tbody").append(
                        $('<tr>' +
                            '<td>' + kopos_modifier_ui._escape_html(item.item_name) + '</td>' +
                            '<td>' + kopos_modifier_ui._escape_html(mod.name) + ' (' + kopos_modifier_ui._escape_html(mod.group_name) + ')</td>' +
                            '<td class="text-right">' + kopos_modifier_ui._format_currency(mod.price || 0) + '</td>' +
                            '</tr>')
                    );
                });
            }
        });
        
        frappe.msgprint({
            title: __("Modifier Summary"),
            message: table,
            wide: true
        });
    },
    
    _escape_html: function(text) {
        if (!text) return "";
        const div = document.createElement("div");
        div.textContent = text;
        return div.innerHTML;
    },
    
    _format_currency: function(amount) {
        return frappe.format.currency(amount, frappe.boot.currency || "MYR");
    },
    
    reset: function() {
        this._cache.clear();
    }
};
```

**Verification**: File should be valid JavaScript

---

## Phase 6: Final Verification

### Step 6.1: Run Python Syntax Check

**Execute**:
```bash
cd kopos_connector && python -m py_compile api/modifiers.py api/modifier_migration.py kopos/report/modifier_sales_analytics/modifier_sales_analytics.py
```

**Expected**: No output (success)

---

### Step 6.2: Run JavaScript Syntax Check

**Execute**:
```bash
node --check kopos_connector/public/js/pos_invoice.js
```

**Expected**: No output (success)

---

### Step 6.3: Validate JSON Files

**Execute**:
```bash
python -c "import json; json.load(open('kopos_connector/kopos/report/modifier_sales_analytics/modifier_sales_analytics.json'))"
python -c "import json; json.load(open('kopos_connector/kopos/doctype/kopos_invoice_item_modifier/kopos_invoice_item_modifier.json'))"
```

**Expected**: No output (success)

---

### Step 6.4: Git Status Check

**Execute**:
```bash
git status
```

**Expected changes**:
- Modified: `install/install.py`
- Modified: `api/modifiers.py`
- Modified: `hooks.py`
- Modified: `kopos/doctype/kopos_invoice_item_modifier/kopos_invoice_item_modifier.json`
- New: `api/modifier_migration.py`
- New: `kopos/report/modifier_sales_analytics/*`
- New: `public/js/pos_invoice.js`

---

### Step 6.5: Commit Changes

**Execute**:
```bash
git add -A
git commit -m "feat(modifiers): implement native ERPNext report, security fixes, and migration tools

- Fix XSS vulnerabilities in client scripts (install.py)
- Fix JavaScript syntax error in onclick handler
- Fix custom field naming (remove manual custom_ prefix)
- Add parent and covering indexes to child table
- Create Modifier Sales Analytics Script Report with 3 charts
- Add data migration utilities with smart matching
- Refactor aggregation to use Query Builder (idempotent)
- Add rate limiting and date range validation to APIs
- Add error notifications for aggregation failures
- Convert client scripts to static JS files
- Add workspace integration hooks"
```

---

## Post-Implementation Testing

### Test 1: Verify Report Exists

After deploying to ERPNext:
1. Go to **Report > Modifier Sales Analytics**
2. Set date range
3. Verify report loads with data

### Test 2: Verify Migration

```bash
bench execute kopos_connector.api.modifier_migration.get_migration_status
```

### Test 3: Run Migration (Dry Run)

```bash
bench execute kopos_connector.api.modifier_migration.migrate_invoice_modifiers --kwargs '{"batch_size": 10, "dry_run": true}'
```

### Test 4: Run Full Migration

```bash
bench execute kopos_connector.api.modifier_migration.migrate_invoice_modifiers --kwargs '{"batch_size": 500}'
```

### Test 5: Backfill Stats

```bash
bench execute kopos_connector.api.modifier_migration.backfill_modifier_stats
```

---

## Rollback Instructions

If something goes wrong:

1. **Revert commit**:
   ```bash
   git revert HEAD
   ```

2. **Drop new indexes** (if needed):
   ```sql
   ALTER TABLE `tabKoPOS Invoice Item Modifier` DROP INDEX parent;
   ALTER TABLE `tabKoPOS Invoice Item Modifier` DROP INDEX idx_aggregation_covering;
   ```

3. **Delete migration data** (if needed):
   ```sql
   DELETE FROM `tabKoPOS Invoice Item Modifier`;
   DELETE FROM `tabKoPOS Modifier Stats`;
   ```

---

## Checklist Summary

### Phase 1: Critical Fixes
- [ ] Step 1.1: Fix XSS in show_item_modifiers
- [ ] Step 1.2: Fix XSS in show_modifier_summary
- [ ] Step 1.3: Fix JS syntax error
- [ ] Step 1.4: Fix custom field naming
- [ ] Step 1.5: Add parent index
- [ ] Step 1.6: Add covering index

### Phase 2: Script Report
- [ ] Step 2.1: Create directory structure
- [ ] Step 2.2: Create report JSON
- [ ] Step 2.3: Create report Python
- [ ] Step 2.4: Create report JavaScript

### Phase 3: Migration
- [ ] Step 3.1: Create migration module

### Phase 4: Performance
- [ ] Step 4.1: Refactor aggregation function
- [ ] Step 4.2: Add rate limiting
- [ ] Step 4.3: Add range aggregation

### Phase 5: Static JS
- [ ] Step 5.1: Update hooks.py
- [ ] Step 5.2: Create JS directory
- [ ] Step 5.3: Create static JS file

### Phase 6: Verification
- [ ] Step 6.1: Python syntax check
- [ ] Step 6.2: JavaScript syntax check
- [ ] Step 6.3: JSON validation
- [ ] Step 6.4: Git status check
- [ ] Step 6.5: Commit changes

---

**End of Checklist**
