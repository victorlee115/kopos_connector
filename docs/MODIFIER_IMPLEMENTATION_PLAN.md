# KoPOS Modifier System: Implementation Plan

**Version:** 1.0  
**Date:** 2026-03-14  
**Status:** Approved  
**Architecture:** Synchronous Child Table with JSON Snapshot

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Architecture Overview](#architecture-overview)
3. [Data Model](#data-model)
4. [Implementation Phases](#implementation-phases)
   - [Phase 1: Backend Implementation](#phase-1-backend-implementation)
   - [Phase 2: Security Implementation](#phase-2-security-implementation)
   - [Phase 3: Mobile UI Updates](#phase-3-mobile-ui-updates)
   - [Phase 4: ERPNext UI](#phase-4-erpnext-ui)
   - [Phase 5: Testing & Deployment](#phase-5-testing--deployment)
5. [Timeline & Effort](#timeline--effort)
6. [Risk Mitigation](#risk-mitigation)
7. [Success Metrics](#success-metrics)

---

## Executive Summary

### Problem Statement
Item modifiers in KoPOS POS system are currently stored as plain text in the `description` field, making them:
- Not queryable for analytics
- Not structured for reporting
- Difficult to track in refunds
- Impossible to aggregate for business insights

### Solution
Implement a **hybrid storage architecture** with:
1. **JSON snapshot** for display, refunds, and audit trail
2. **Child DocType** for structured analytics and reporting
3. **Nightly aggregation** for fast dashboard queries

### Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Population strategy | **Synchronous** | Atomic transactions, data integrity, follows ERPNext patterns |
| Storage approach | **Hybrid (JSON + Child Table)** | JSON for display/refunds, child table for analytics |
| Analytics | **Nightly aggregation** | Pre-computed stats for fast queries |
| Mobile UI | **8.7" optimized** | Touch targets 48-52px, 700px breakpoint |
| Security | **Input validation + XSS prevention** | Sanitize all user input before storage |

---

## Architecture Overview

### System Flow

```
┌─────────────────────────────────────────────────────────────┐
│                    POS CLIENT (Mobile App)                  │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ Order with Modifiers                                  │   │
│  │  Item: Iced Latte                                     │   │
│  │  Modifiers:                                           │   │
│  │   - Large (+$1.00)                                    │   │
│  │   - Oat Milk (+$0.50)                                 │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼ JSON Payload
┌─────────────────────────────────────────────────────────────┐
│                    ERPNEXT BACKEND                          │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ 1. VALIDATE INPUT                                     │   │
│  │    • Schema validation (max 50 modifiers)            │   │
│  │    • Type checking                                    │   │
│  │    • XSS sanitization                                 │   │
│  └─────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ 2. BUILD SNAPSHOT                                     │   │
│  │    • Sanitize modifier names                          │   │
│  │    • Calculate totals                                 │   │
│  │    • Create JSON snapshot                             │   │
│  └─────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ 3. POPULATE CHILD TABLE (SYNC)                        │   │
│  │    • Before invoice.submit()                          │   │
│  │    • Atomic transaction                               │   │
│  │    • Resolve FK links                                 │   │
│  └─────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ 4. STORE INVOICE                                      │   │
│  │    • JSON snapshot (display/refunds)                 │   │
│  │    • Child table (analytics)                          │   │
│  │    • Single transaction                               │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼ Nightly (Scheduler)
┌─────────────────────────────────────────────────────────────┐
│                    ANALYTICS LAYER                          │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ KoPOS Modifier Stats                                  │   │
│  │  • Pre-aggregated daily stats                         │   │
│  │  • Fast dashboard queries                             │   │
│  │  • Historical trends                                  │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### Why Synchronous Population?

| Aspect | Synchronous ✅ | Async ❌ |
|--------|---------------|----------|
| **Data integrity** | Atomic with invoice | Eventual consistency |
| **Error handling** | Immediate rollback | Silent failures possible |
| **Code complexity** | Simple, debuggable | Requires reconciliation |
| **ERPNext patterns** | Follows child table patterns | Non-standard |
| **This codebase** | Consistent with existing | No `frappe.enqueue` usage |
| **Performance** | < 200ms overhead | ~50ms overhead |

---

## Data Model

### Entity Relationship Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    POS Invoice                              │
│─────────────────────────────────────────────────────────────│
│ + name: VARCHAR(140) PK                                     │
│ + posting_date: DATE                                        │
│ + customer: VARCHAR(140)                                    │
│ + grand_total: DECIMAL(18,2)                               │
│ + docstatus: INT                                            │
│ + is_return: INT                                            │
└─────────────────────────────────────────────────────────────┘
                           │
                           │ 1:N
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    POS Invoice Item                         │
│─────────────────────────────────────────────────────────────│
│ + name: VARCHAR(140) PK                                     │
│ + parent: VARCHAR(140) FK → POS Invoice                     │
│ + item_code: VARCHAR(140)                                   │
│ + item_name: VARCHAR(255)                                   │
│ + qty: DECIMAL(18,2)                                        │
│ + rate: DECIMAL(18,2)                                       │
│ + amount: DECIMAL(18,2)                                     │
│ + custom_kopos_modifiers: LONGTEXT (JSON)                   │
│ + custom_kopos_modifier_total: DECIMAL(18,2)               │
│ + custom_kopos_has_modifiers: TINYINT(1)                   │
└─────────────────────────────────────────────────────────────┘
                           │
                           │ 1:N
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              KoPOS Invoice Item Modifier                    │
│─────────────────────────────────────────────────────────────│
│ + name: VARCHAR(140) PK                                     │
│ + parent: VARCHAR(140) FK → POS Invoice Item                │
│ + modifier_group: VARCHAR(140) FK (nullable)                │
│ + modifier_group_name: VARCHAR(255)                         │
│ + modifier_option: VARCHAR(140) FK (nullable)               │
│ + modifier_name: VARCHAR(255)                               │
│ + price_adjustment: DECIMAL(18,2)                           │
│ + base_price: DECIMAL(18,2)                                 │
│ + is_default: TINYINT(1)                                    │
│ + display_order: INT                                        │
│─────────────────────────────────────────────────────────────│
│ INDEXES:                                                    │
│   idx_modifier_parent (parent)                              │
│   idx_modifier_group_option (modifier_group, modifier_option)│
│   idx_unique_parent_option UNIQUE (parent, modifier_option) │
└─────────────────────────────────────────────────────────────┘

                           ▲
                           │ Nightly Aggregation
                           │
┌─────────────────────────────────────────────────────────────┐
│                  KoPOS Modifier Stats                       │
│─────────────────────────────────────────────────────────────│
│ + name: VARCHAR(140) PK                                     │
│ + date: DATE                                                │
│ + modifier_option: VARCHAR(140)                             │
│ + modifier_group: VARCHAR(140)                              │
│ + modifier_name: VARCHAR(255)                               │
│ + group_name: VARCHAR(255)                                  │
│ + selection_count: INT                                      │
│ + revenue: DECIMAL(18,2)                                    │
│─────────────────────────────────────────────────────────────│
│ INDEXES:                                                    │
│   uk_date_option UNIQUE (date, modifier_option)             │
│   idx_group_date (modifier_group, date)                     │
│   idx_covering (date, modifier_group, selection_count, revenue)│
└─────────────────────────────────────────────────────────────┘
```

### JSON Snapshot Structure

```json
{
  "modifiers": [
    {
      "id": "OPT-LARGE-001",
      "group_id": "GRP-SIZE",
      "name": "Large",
      "group_name": "Size",
      "price": 1.00,
      "base_price": 1.00,
      "is_default": false
    },
    {
      "id": "OPT-OAT-MILK",
      "group_id": "GRP-MILK",
      "name": "Oat Milk",
      "group_name": "Milk Type",
      "price": 0.50,
      "base_price": 0.50,
      "is_default": false
    }
  ],
  "total": 1.50,
  "count": 2
}
```

---

## Implementation Phases

### Phase 1: Backend Implementation (Days 1-4, 24 hours)

#### Task 1.1: Create Child DocType (Day 1, 2 hours)

**File:** `kopos_connector/kopos/doctype/kopos_invoice_item_modifier/kopos_invoice_item_modifier.json`

**Key Design Decisions:**

| Aspect | Decision | Rationale |
|--------|----------|-----------|
| Autoname | `KOPOS-INVOICE-ITEM-MOD-.#####` | Follows codebase pattern |
| Field name | `display_order` (not `sort_order`) | Matches codebase conventions |
| Precision | `"2"` | Consistent with ERPNext currency fields |
| Permissions | Empty array `[]` | Child table inherits from parent |
| `istable` | `1` | Standard child table pattern |

**DocType Definition:**

```json
{
    "name": "KoPOS Invoice Item Modifier",
    "module": "KoPOS",
    "istable": 1,
    "editable_grid": 0,
    "autoname": "KOPOS-INVOICE-ITEM-MOD-.#####",
    "document_type": "Setup",
    "engine": "InnoDB",
    "sort_field": "idx",
    "sort_order": "ASC",
    "permissions": [],
    "fields": [
        {
            "fieldname": "modifier_group",
            "fieldtype": "Link",
            "options": "KoPOS Modifier Group",
            "label": "Modifier Group",
            "in_list_view": 1
        },
        {
            "fieldname": "modifier_group_name",
            "fieldtype": "Data",
            "label": "Group Name",
            "in_list_view": 1,
            "reqd": 1
        },
        {
            "fieldname": "modifier_option",
            "fieldtype": "Link",
            "options": "KoPOS Modifier Option",
            "label": "Modifier Option"
        },
        {
            "fieldname": "modifier_name",
            "fieldtype": "Data",
            "label": "Option Name",
            "in_list_view": 1,
            "reqd": 1
        },
        {
            "fieldname": "price_adjustment",
            "fieldtype": "Currency",
            "label": "Price Adjustment",
            "in_list_view": 1,
            "precision": "2",
            "reqd": 1
        },
        {
            "fieldname": "base_price",
            "fieldtype": "Currency",
            "label": "Base Price",
            "precision": "2"
        },
        {
            "fieldname": "is_default",
            "fieldtype": "Check",
            "label": "Is Default",
            "default": "0"
        },
        {
            "fieldname": "display_order",
            "fieldtype": "Int",
            "label": "Display Order",
            "default": "0"
        }
    ],
    "indexes": [
        {"fields": ["parent"]},
        {"fields": ["modifier_group", "modifier_option"]},
        {"fields": ["parent", "modifier_option"], "unique": true}
    ]
}
```

**Create via:**
```bash
cd /Users/victor/dev/jiji/JiJiPOS/erpnext/kopos_connector
bench new-doctype KoPOS Invoice Item Modifier
```

#### Task 1.2: Add Custom Fields (Day 1, 1 hour)

**File:** `kopos_connector/kopos_connector/install/install.py`

```python
"POS Invoice Item": [
    # JSON snapshot for display, refunds, and audit
    {
        "fieldname": "custom_kopos_modifiers",
        "label": "KoPOS Modifiers JSON",
        "fieldtype": "Long Text",
        "insert_after": "pricing_rules",
        "read_only": 1,
        "hidden": 1,
        "no_copy": 0,  # Allow copy to credit notes
    },
    # Quick access for queries
    {
        "fieldname": "custom_kopos_modifier_total",
        "label": "KoPOS Modifier Total",
        "fieldtype": "Currency",
        "insert_after": "custom_kopos_modifiers",
        "read_only": 1,
        "precision": "2",
    },
    # Filter flag
    {
        "fieldname": "custom_kopos_has_modifiers",
        "label": "Has Modifiers",
        "fieldtype": "Check",
        "insert_after": "custom_kopos_modifier_total",
        "read_only": 1,
    },
    # Child table for analytics
    {
        "fieldname": "custom_kopos_modifiers_table",
        "label": "KoPOS Modifiers",
        "fieldtype": "Table",
        "options": "KoPOS Invoice Item Modifier",
        "insert_after": "custom_kopos_has_modifiers",
    },
]
```

**Run migration:**
```bash
bench migrate
```

#### Task 1.3: Input Validation & Sanitization (Day 2, 3 hours)

**File:** `kopos_connector/kopos_connector/api/modifiers.py` (NEW)

```python
"""
KoPOS Modifier API

Handles validation, sanitization, and processing of modifier data.
"""

import json
import frappe
from frappe import _
from frappe.utils import cstr, flt, cint
from typing import TypedDict, NotRequired, Any
from html import escape


# Type definitions for better code clarity
class ModifierDict(TypedDict):
    """Type definition for a single modifier."""
    id: str
    group_id: str
    name: str
    group_name: str
    price: float
    base_price: float
    is_default: bool


class ModifierSnapshot(TypedDict):
    """Type definition for modifier snapshot."""
    modifiers: list[ModifierDict]
    total: float
    count: int


# JSON Schema for validation
MODIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "modifiers": {
            "type": "array",
            "maxItems": 50,  # Reasonable limit
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "maxLength": 140},
                    "group_id": {"type": "string", "maxLength": 140},
                    "name": {"type": "string", "maxLength": 255},
                    "group_name": {"type": "string", "maxLength": 255},
                    "price": {"type": "number", "minimum": -999999, "maximum": 999999},
                    "base_price": {"type": "number", "minimum": -999999, "maximum": 999999},
                    "is_default": {"type": "boolean"},
                },
                "required": ["id", "name"],
            },
        },
        "modifier_total": {"type": "number"},
    },
    "required": ["modifiers"],
}


def validate_modifier_data(raw_modifiers: list) -> list[dict]:
    """
    Validate modifier data against JSON schema.
    
    Args:
        raw_modifiers: Raw modifier data from POS client
        
    Returns:
        Validated modifier list
        
    Raises:
        frappe.ValidationError: If validation fails
    """
    import jsonschema
    
    try:
        jsonschema.validate(
            {"modifiers": raw_modifiers},
            MODIFIER_SCHEMA
        )
    except jsonschema.ValidationError as e:
        frappe.throw(
            _("Invalid modifier data: {0}").format(str(e.message)),
            frappe.ValidationError
        )
    
    return raw_modifiers


def sanitize_modifier_text(value: str) -> str:
    """
    Sanitize text for safe storage and display.
    Prevents XSS attacks by escaping HTML entities.
    
    Args:
        value: Raw text input
        
    Returns:
        Sanitized text (max 255 chars)
    """
    if not value:
        return ""
    
    # Escape HTML entities to prevent XSS
    sanitized = escape(str(value))
    
    # Enforce length limit
    return sanitized[:255]


def build_modifiers_snapshot(raw_item: dict) -> ModifierSnapshot:
    """
    Build sanitized JSON snapshot from raw POS data.
    
    Args:
        raw_item: Raw item data from POS client containing:
            - modifiers: List of modifier dictionaries
            - modifier_total: Total modifier price
            
    Returns:
        Sanitized snapshot dictionary
        
    Raises:
        TypeError: If raw_item is not a dictionary
        frappe.ValidationError: If validation fails
    """
    # Type check
    if not isinstance(raw_item, dict):
        raise TypeError(f"Expected dict, got {type(raw_item).__name__}")
    
    # Extract modifiers
    raw_modifiers = raw_item.get("modifiers") or []
    
    # Handle various falsy values
    if not isinstance(raw_modifiers, list):
        frappe.log_error(
            title="KoPOS Modifier Warning",
            message=f"Expected list for modifiers, got {type(raw_modifiers).__name__}"
        )
        raw_modifiers = []
    
    # Validate against schema
    validated = validate_modifier_data(raw_modifiers)
    
    # Sanitize all text fields
    sanitized_modifiers = [
        {
            "id": cstr(mod.get("id", "")),
            "group_id": cstr(mod.get("group_id", "")),
            "name": sanitize_modifier_text(mod.get("name", "")),
            "group_name": sanitize_modifier_text(mod.get("group_name", "")),
            "price": flt(mod.get("price"), precision=2),
            "base_price": flt(mod.get("base_price"), precision=2),
            "is_default": bool(mod.get("is_default")),
        }
        for mod in validated
        if isinstance(mod, dict)
    ]
    
    return {
        "modifiers": sanitized_modifiers,
        "total": flt(raw_item.get("modifier_total"), precision=2),
        "count": len(sanitized_modifiers),
    }


def serialize_json_compact(payload: Any) -> str:
    """
    Serialize JSON with compact format.
    
    Args:
        payload: Data to serialize
        
    Returns:
        Compact JSON string
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
```

#### Task 1.4: Synchronous Modifier Population (Day 2-3, 22 hours)

**File:** `kopos_connector/kopos_connector/api/orders.py`

**Add imports:**
```python
from kopos_connector.api.modifiers import (
    build_modifiers_snapshot,
    serialize_json_compact,
)
```

**Update `build_pos_invoice()` function:**

```python
def build_pos_invoice(payload: dict, pos_profile_doc):
    """Build a draft POS Invoice document from the payload."""
    from erpnext.accounts.doctype.sales_invoice.sales_invoice import (
        get_mode_of_payment_info,
    )

    order = payload["order"]
    created_at = get_datetime(order["created_at"] or None)
    warehouse = payload.get("warehouse") or pos_profile_doc.get("warehouse")
    customer = pos_profile_doc.get("customer")

    if not customer:
        frappe.throw(
            _("POS Profile {0} must define a default Customer").format(
                pos_profile_doc.name
            ),
            frappe.ValidationError,
        )

    invoice = frappe.new_doc("POS Invoice")
    invoice.is_pos = 1
    invoice.pos_profile = pos_profile_doc.name
    invoice.company = payload.get("company") or pos_profile_doc.get("company")
    invoice.customer = customer
    invoice.currency = payload.get("currency") or pos_profile_doc.get("currency")
    invoice.posting_date = created_at.date().isoformat()
    invoice.posting_time = created_at.time().strftime("%H:%M:%S")
    invoice.set_posting_time = 1
    invoice.custom_kopos_idempotency_key = payload["idempotency_key"]
    invoice.custom_kopos_device_id = payload["device_id"]
    set_invoice_promotion_metadata(invoice, payload)
    invoice.remarks = build_invoice_remarks(payload)
    invoice.ignore_pricing_rule = 1

    for raw_item in order["items"]:
        item_doc = frappe.get_cached_doc("Item", raw_item["item_code"])
        row = {
            "item_code": item_doc.name,
            "qty": raw_item["qty"],
            "uom": item_doc.stock_uom,
            "stock_uom": item_doc.stock_uom,
            "conversion_factor": 1,
        }
        if warehouse:
            row["warehouse"] = warehouse
        invoice.append("items", row)

    invoice.set_missing_values()

    for invoice_item, raw_item in zip(invoice.items, order["items"], strict=False):
        effective_rate = raw_item["amount"] / raw_item["qty"] if raw_item["qty"] else 0
        invoice_item.item_name = raw_item["item_name"] or invoice_item.item_name
        invoice_item.description = build_item_description(
            raw_item, invoice_item.description
        )
        invoice_item.rate = effective_rate
        invoice_item.price_list_rate = (
            raw_item["base_rate"] or raw_item["rate"] or effective_rate
        )
        
        # === NEW: Populate modifier fields ===
        if raw_item.get("modifiers"):
            _populate_modifiers_on_item(invoice_item, raw_item)
        
        if hasattr(invoice_item, "custom_kopos_promotion_allocation"):
            invoice_item.custom_kopos_promotion_allocation = serialize_json_compact(
                {
                    "base_amount": raw_item.get("base_amount"),
                    "discount_amount": raw_item.get("discount_amount"),
                    "promotion_allocations": raw_item.get("promotion_allocations")
                    or [],
                }
            )
        if warehouse:
            invoice_item.warehouse = warehouse

    # ... rest of function ...
```

**Add helper functions:**

```python
def _populate_modifiers_on_item(invoice_item, raw_item: dict) -> None:
    """
    Populate modifier fields on POS Invoice Item synchronously.
    
    This is called BEFORE invoice.submit() to ensure atomicity.
    
    Args:
        invoice_item: POS Invoice Item document
        raw_item: Raw item data from POS client
    """
    # Build sanitized snapshot
    snapshot = build_modifiers_snapshot(raw_item)
    
    # Store JSON snapshot
    invoice_item.custom_kopos_modifiers = serialize_json_compact(snapshot)
    invoice_item.custom_kopos_modifier_total = snapshot["total"]
    invoice_item.custom_kopos_has_modifiers = 1 if snapshot["count"] > 0 else 0
    
    # Populate child table synchronously
    _populate_modifiers_table(invoice_item, snapshot)


def _populate_modifiers_table(invoice_item, snapshot: dict) -> None:
    """
    Populate child table with modifier data.
    
    Args:
        invoice_item: POS Invoice Item document
        snapshot: Sanitized modifier snapshot
    """
    modifiers = snapshot.get("modifiers", [])
    
    for idx, mod in enumerate(modifiers):
        # Resolve FK links safely
        modifier_group = _resolve_link(mod.get("group_id"), "KoPOS Modifier Group")
        modifier_option = _resolve_link(mod.get("id"), "KoPOS Modifier Option")
        
        invoice_item.append("custom_kopos_modifiers_table", {
            "modifier_group": modifier_group,
            "modifier_group_name": mod.get("group_name", ""),
            "modifier_option": modifier_option,
            "modifier_name": mod.get("name", ""),
            "price_adjustment": flt(mod.get("price")),
            "base_price": flt(mod.get("base_price")),
            "is_default": bool(mod.get("is_default")),
            "display_order": idx,
        })


def _resolve_link(value: str, doctype: str) -> str | None:
    """
    Safely resolve link field, returning None if not exists.
    
    Args:
        value: Link value to resolve
        doctype: Target DocType
        
    Returns:
        Link value if exists, None otherwise
    """
    if not value:
        return None
    
    if frappe.db.exists(doctype, value):
        return value
    
    return None
```

#### Task 1.5: Analytics DocType & Aggregation (Day 3, 3 hours)

**Create Analytics DocType:**

**File:** `kopos_connector/kopos/doctype/kopos_modifier_stats/kopos_modifier_stats.json`

```json
{
    "name": "KoPOS Modifier Stats",
    "module": "KoPOS",
    "document_type": "Setup",
    "autoname": "KOPOS-MOD-STATS-.#####",
    "fields": [
        {
            "fieldname": "date",
            "fieldtype": "Date",
            "in_list_view": 1,
            "reqd": 1,
            "label": "Date"
        },
        {
            "fieldname": "modifier_option",
            "fieldtype": "Link",
            "options": "KoPOS Modifier Option",
            "reqd": 1,
            "label": "Modifier Option"
        },
        {
            "fieldname": "modifier_group",
            "fieldtype": "Link",
            "options": "KoPOS Modifier Group",
            "label": "Modifier Group"
        },
        {
            "fieldname": "modifier_name",
            "fieldtype": "Data",
            "in_list_view": 1,
            "label": "Modifier Name"
        },
        {
            "fieldname": "group_name",
            "fieldtype": "Data",
            "label": "Group Name"
        },
        {
            "fieldname": "selection_count",
            "fieldtype": "Int",
            "in_list_view": 1,
            "default": 0,
            "label": "Selection Count"
        },
        {
            "fieldname": "revenue",
            "fieldtype": "Currency",
            "in_list_view": 1,
            "precision": "2",
            "default": 0,
            "label": "Revenue"
        }
    ],
    "indexes": [
        {"fields": ["date", "modifier_option"], "unique": true},
        {"fields": ["modifier_group", "date"]},
        {"fields": ["modifier_option"]}
    ]
}
```

**Add aggregation function to `modifiers.py`:**

```python
def aggregate_modifier_stats(date: str | None = None) -> int:
    """
    Aggregate modifier stats for a given date.
    
    Called nightly by scheduler to pre-compute analytics.
    
    Args:
        date: Date to aggregate (default: yesterday)
        
    Returns:
        Number of stat records created/updated
    """
    from frappe.utils import add_days, today, getdate
    
    if not date:
        date = add_days(today(), -1)
    
    # Validate date format
    date = getdate(date).isoformat()
    
    # Aggregate from child table
    stats = frappe.db.sql("""
        SELECT 
            %s as date,
            m.modifier_option,
            m.modifier_group,
            m.modifier_name,
            m.modifier_group_name as group_name,
            COUNT(*) as selection_count,
            SUM(m.price_adjustment * i.qty) as revenue
        FROM `tabKoPOS Invoice Item Modifier` m
        INNER JOIN `tabPOS Invoice Item` ii ON m.parent = ii.name
        INNER JOIN `tabPOS Invoice` i ON ii.parent = i.name
        WHERE i.posting_date = %s
          AND i.docstatus = 1
          AND i.is_return = 0
        GROUP BY m.modifier_option, m.modifier_group, m.modifier_name, m.modifier_group_name
    """, (date, date), as_dict=True)
    
    count = 0
    for stat in stats:
        # Use upsert pattern
        existing = frappe.db.exists("KoPOS Modifier Stats", {
            "date": date,
            "modifier_option": stat.modifier_option,
        })
        
        if existing:
            doc = frappe.get_doc("KoPOS Modifier Stats", existing)
            doc.selection_count = stat.selection_count
            doc.revenue = flt(stat.revenue) or 0
            doc.save(ignore_permissions=True)
        else:
            doc = frappe.new_doc("KoPOS Modifier Stats")
            doc.date = date
            doc.modifier_option = stat.modifier_option
            doc.modifier_group = stat.modifier_group
            doc.modifier_name = stat.modifier_name
            doc.group_name = stat.group_name
            doc.selection_count = stat.selection_count
            doc.revenue = flt(stat.revenue) or 0
            doc.insert(ignore_permissions=True)
        
        count += 1
    
    frappe.db.commit()
    return count


def backfill_modifier_stats(days: int = 30) -> dict:
    """
    Backfill stats for historical data.
    
    Args:
        days: Number of days to backfill
        
    Returns:
        Summary of records created
    """
    from frappe.utils import add_days, getdate
    
    results = {"success": 0, "errors": []}
    
    for i in range(days):
        date = add_days(getdate(), -i)
        try:
            count = aggregate_modifier_stats(date.isoformat())
            results["success"] += count
        except Exception as e:
            results["errors"].append({"date": str(date), "error": str(e)})
    
    return results
```

**Add scheduler hook:**

**File:** `kopos_connector/kopos_connector/hooks.py`

```python
scheduler_events = {
    "daily": [
        "kopos_connector.api.modifiers.aggregate_modifier_stats",
    ],
    "daily_long": [
        "kopos_connector.api.modifiers.backfill_modifier_stats",
    ],
}
```

#### Task 1.6: Refund Support (Day 3-4, 2 hours)

**File:** `kopos_connector/kopos_connector/api/orders.py`

**Update `build_credit_note()` function:**

```python
def build_credit_note(
    validated: dict[str, Any],
    original_invoice: Any,
    pos_profile: Any,
) -> Any:
    """Build Credit Note from validated refund payload."""
    from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return

    pos_profile_name = pos_profile.name
    company = pos_profile.company

    credit_note = make_sales_return(original_invoice.name)
    original_timestamp = get_datetime(
        f"{original_invoice.posting_date} {original_invoice.posting_time}"
    )
    refund_timestamp = max(now_datetime(), add_to_date(original_timestamp, minutes=1))
    credit_note.customer = original_invoice.customer
    credit_note.company = company
    credit_note.pos_profile = pos_profile_name
    credit_note.set_posting_time = 1
    credit_note.posting_date = refund_timestamp.date().isoformat()
    credit_note.posting_time = refund_timestamp.time().strftime("%H:%M:%S")
    credit_note.custom_kopos_idempotency_key = validated["idempotency_key"]
    credit_note.custom_kopos_device_id = validated["device_id"]
    if hasattr(credit_note, "custom_kopos_refund_reason_code"):
        credit_note.custom_kopos_refund_reason_code = validated["refund_reason_code"]
    if hasattr(credit_note, "custom_kopos_refund_reason"):
        credit_note.custom_kopos_refund_reason = validated["refund_reason"]
    if hasattr(credit_note, "update_stock") and not validated["return_to_stock"]:
        credit_note.update_stock = 0
    
    # Copy promotion fields
    for fieldname in (
        "custom_kopos_promotion_snapshot_version",
        "custom_kopos_pricing_mode",
        "custom_kopos_promotion_payload",
        "custom_kopos_promotion_reconciliation_status",
    ):
        if hasattr(credit_note, fieldname) and hasattr(original_invoice, fieldname):
            setattr(credit_note, fieldname, getattr(original_invoice, fieldname, None))

    if validated["refund_type"] == "full":
        for item in credit_note.items:
            original_item = next(
                (
                    source_item
                    for source_item in original_invoice.items
                    if source_item.item_code == item.item_code
                ),
                None,
            )
            if original_item:
                item.rate = get_original_refund_rate(original_item)
            if not validated["return_to_stock"]:
                item.warehouse = None
            if hasattr(item, "custom_kopos_promotion_allocation"):
                item.custom_kopos_promotion_allocation = (
                    build_refund_promotion_allocation(original_item, abs(flt(item.qty)))
                )
            
            # === NEW: Copy modifier data ===
            if original_item and hasattr(item, "custom_kopos_modifiers_table"):
                _copy_modifiers_to_refund(item, original_item)
    else:
        # Partial refund logic...
        requested = {item["item_code"]: item for item in validated["items"]}
        kept_items = []
        for credit_item in credit_note.items:
            refund_item = requested.get(credit_item.item_code)
            if not refund_item:
                continue

            original_item = next(
                (
                    item
                    for item in original_invoice.items
                    if item.item_code == credit_item.item_code
                ),
                None,
            )
            if not original_item:
                frappe.throw(
                    _("Item {0} not found in original invoice").format(
                        credit_item.item_code
                    ),
                    frappe.ValidationError,
                )

            qty = flt(refund_item["qty"])
            if qty > original_item.qty:
                frappe.throw(
                    _("Cannot refund {0} units of {1}; only {2} were purchased").format(
                        qty, credit_item.item_code, original_item.qty
                    ),
                    frappe.ValidationError,
                )

            credit_item.qty = -abs(qty)
            credit_item.rate = get_original_refund_rate(original_item)
            if not validated["return_to_stock"]:
                credit_item.warehouse = None
            if hasattr(credit_item, "custom_kopos_promotion_allocation"):
                credit_item.custom_kopos_promotion_allocation = (
                    build_refund_promotion_allocation(original_item, qty)
                )
            
            # === NEW: Copy modifier data for partial refund ===
            if hasattr(credit_item, "custom_kopos_modifiers_table"):
                _copy_modifiers_to_refund(credit_item, original_item, qty)
            
            kept_items.append(credit_item)

        credit_note.set("items", kept_items)

    # ... rest of function ...
```

**Add helper functions:**

```python
def _copy_modifiers_to_refund(
    credit_item: Any,
    original_item: Any,
    refund_qty: float | None = None
) -> None:
    """
    Copy modifier data to refund item with proportional amounts.
    
    Args:
        credit_item: Credit note item
        original_item: Original invoice item
        refund_qty: Quantity being refunded (None = full refund)
    """
    if not hasattr(original_item, "custom_kopos_modifiers"):
        return
    
    if not getattr(original_item, "custom_kopos_has_modifiers", 0):
        return
    
    # Parse original snapshot
    try:
        snapshot = frappe.parse_json(original_item.custom_kopos_modifiers or "{}")
    except (json.JSONDecodeError, TypeError):
        return
    
    if not snapshot.get("modifiers"):
        return
    
    # Calculate ratio for partial refunds
    original_qty = abs(flt(getattr(original_item, "qty", 0)))
    if refund_qty is None:
        refund_qty = original_qty
    
    if original_qty <= 0:
        return
    
    ratio = min(1.0, max(0.0, flt(refund_qty) / original_qty))
    
    # Copy JSON snapshot
    credit_item.custom_kopos_modifiers = original_item.custom_kopos_modifiers
    credit_item.custom_kopos_modifier_total = flt(
        original_item.custom_kopos_modifier_total or 0
    ) * ratio
    credit_item.custom_kopos_has_modifiers = original_item.custom_kopos_has_modifiers
    
    # Copy child table entries with proportional amounts
    if hasattr(original_item, "custom_kopos_modifiers_table"):
        for mod_entry in original_item.custom_kopos_modifiers_table:
            credit_item.append("custom_kopos_modifiers_table", {
                "modifier_group": mod_entry.modifier_group,
                "modifier_group_name": mod_entry.modifier_group_name,
                "modifier_option": mod_entry.modifier_option,
                "modifier_name": mod_entry.modifier_name,
                "price_adjustment": flt(mod_entry.price_adjustment) * ratio,
                "base_price": mod_entry.base_price,
                "is_default": mod_entry.is_default,
                "display_order": mod_entry.display_order,
            })
```

#### Task 1.7: Unit Tests (Day 4, 4 hours)

**File:** `kopos_connector/tests/test_modifiers.py`

```python
"""
Unit tests for KoPOS Modifier System
"""

import unittest
import frappe
from frappe.tests.utils import FrappeTestCase


class TestModifierSnapshot(FrappeTestCase):
    """Test suite for modifier snapshot functionality."""
    
    @classmethod
    def setUpClass(cls):
        """Set up test data."""
        cls.test_data = {
            "valid_item": {
                "modifiers": [
                    {
                        "id": "m1",
                        "group_id": "g1",
                        "name": "Extra Cheese",
                        "group_name": "Toppings",
                        "price": 1.50,
                        "base_price": 1.50,
                        "is_default": False
                    }
                ],
                "modifier_total": 1.50
            },
            "empty_item": {},
            "null_modifiers": {"modifiers": None},
            "invalid_modifiers": {"modifiers": "not a list"},
            "unicode_item": {
                "modifiers": [
                    {
                        "id": "m2",
                        "name": "Extra Spicy 🌶️",
                        "group_name": "香辣程度"
                    }
                ]
            },
            "xss_item": {
                "modifiers": [
                    {
                        "id": "m3",
                        "name": '<script>alert("xss")</script>',
                        "group_name": "Test"
                    }
                ]
            },
        }
    
    def test_build_snapshot_valid_item(self):
        """Test snapshot creation with valid data."""
        from kopos_connector.api.modifiers import build_modifiers_snapshot
        
        result = build_modifiers_snapshot(self.test_data["valid_item"])
        
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["total"], 1.50)
        self.assertEqual(len(result["modifiers"]), 1)
        self.assertEqual(result["modifiers"][0]["name"], "Extra Cheese")
    
    def test_build_snapshot_empty_item(self):
        """Test snapshot creation with empty item."""
        from kopos_connector.api.modifiers import build_modifiers_snapshot
        
        result = build_modifiers_snapshot(self.test_data["empty_item"])
        
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["modifiers"], [])
    
    def test_build_snapshot_null_modifiers(self):
        """Test snapshot handles null modifiers gracefully."""
        from kopos_connector.api.modifiers import build_modifiers_snapshot
        
        result = build_modifiers_snapshot(self.test_data["null_modifiers"])
        
        self.assertEqual(result["modifiers"], [])
        self.assertEqual(result["count"], 0)
    
    def test_build_snapshot_invalid_modifiers_type(self):
        """Test snapshot handles invalid modifier type."""
        from kopos_connector.api.modifiers import build_modifiers_snapshot
        
        result = build_modifiers_snapshot(self.test_data["invalid_modifiers"])
        
        self.assertEqual(result["modifiers"], [])
    
    def test_build_snapshot_non_dict_input(self):
        """Test snapshot raises TypeError for non-dict input."""
        from kopos_connector.api.modifiers import build_modifiers_snapshot
        
        with self.assertRaises(TypeError):
            build_modifiers_snapshot("not a dict")
    
    def test_sanitize_xss(self):
        """Test XSS prevention in modifier names."""
        from kopos_connector.api.modifiers import sanitize_modifier_text
        
        malicious = '<script>alert("xss")</script>'
        result = sanitize_modifier_text(malicious)
        
        self.assertNotIn('<script>', result)
        self.assertIn('&lt;script&gt;', result)
    
    def test_unicode_names(self):
        """Test handling of unicode characters in names."""
        from kopos_connector.api.modifiers import build_modifiers_snapshot
        
        result = build_modifiers_snapshot(self.test_data["unicode_item"])
        
        self.assertIn("🌶️", result["modifiers"][0]["name"])
        self.assertIn("香辣程度", result["modifiers"][0]["group_name"])
    
    def test_modifier_price_precision(self):
        """Test price values maintain proper precision."""
        from kopos_connector.api.modifiers import build_modifiers_snapshot
        
        item = {
            "modifiers": [{"price": 1.23456789}],
            "modifier_total": 9.87654321
        }
        result = build_modifiers_snapshot(item)
        
        # Should be rounded to 2 decimal places
        self.assertEqual(result["modifiers"][0]["price"], 1.23)
        self.assertEqual(result["total"], 9.88)
    
    def test_very_long_modifier_list(self):
        """Test performance with many modifiers."""
        from kopos_connector.api.modifiers import build_modifiers_snapshot
        import time
        
        item = {
            "modifiers": [
                {"id": f"m{i}", "name": f"Modifier {i}"}
                for i in range(50)
            ]
        }
        
        start = time.time()
        result = build_modifiers_snapshot(item)
        elapsed = time.time() - start
        
        self.assertEqual(result["count"], 50)
        self.assertLess(elapsed, 0.1, "Should process 50 modifiers in < 100ms")


class TestModifierValidation(FrappeTestCase):
    """Test modifier validation."""
    
    def test_max_modifiers_limit(self):
        """Test that max 50 modifiers is enforced."""
        from kopos_connector.api.modifiers import validate_modifier_data
        
        # 51 modifiers should fail
        too_many = [{"id": f"m{i}", "name": f"Mod {i}"} for i in range(51)]
        
        with self.assertRaises(frappe.ValidationError):
            validate_modifier_data(too_many)
    
    def test_required_fields(self):
        """Test that required fields are enforced."""
        from kopos_connector.api.modifiers import validate_modifier_data
        
        # Missing 'id' and 'name' should fail
        invalid = [{"price": 1.00}]
        
        with self.assertRaises(frappe.ValidationError):
            validate_modifier_data(invalid)


class TestModifierStats(FrappeTestCase):
    """Test modifier analytics aggregation."""
    
    def test_aggregate_empty_date(self):
        """Test aggregation with no data."""
        from kopos_connector.api.modifiers import aggregate_modifier_stats
        from frappe.utils import add_days, today
        
        # Future date should have no data
        future_date = add_days(today(), 10)
        count = aggregate_modifier_stats(future_date)
        
        self.assertEqual(count, 0)
```

**Run tests:**
```bash
bench run-tests --module kopos_connector.tests.test_modifiers
```

#### Task 1.8: Migration (Day 4, 2 hours)

**Create migration patch:**

**File:** `kopos_connector/patches/v1_0/add_modifier_fields.py`

```python
"""
Add modifier custom fields to POS Invoice Item
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    """Run migration to add modifier fields."""
    
    custom_fields = {
        "POS Invoice Item": [
            {
                "fieldname": "custom_kopos_modifiers",
                "label": "KoPOS Modifiers JSON",
                "fieldtype": "Long Text",
                "insert_after": "pricing_rules",
                "read_only": 1,
                "hidden": 1,
                "no_copy": 0,
            },
            {
                "fieldname": "custom_kopos_modifier_total",
                "label": "KoPOS Modifier Total",
                "fieldtype": "Currency",
                "insert_after": "custom_kopos_modifiers",
                "read_only": 1,
                "precision": "2",
            },
            {
                "fieldname": "custom_kopos_has_modifiers",
                "label": "Has Modifiers",
                "fieldtype": "Check",
                "insert_after": "custom_kopos_modifier_total",
                "read_only": 1,
            },
            {
                "fieldname": "custom_kopos_modifiers_table",
                "label": "KoPOS Modifiers",
                "fieldtype": "Table",
                "options": "KoPOS Invoice Item Modifier",
                "insert_after": "custom_kopos_has_modifiers",
            },
        ]
    }
    
    create_custom_fields(custom_fields, update=True)
    frappe.db.commit()
    
    print("✓ Modifier fields added to POS Invoice Item")
```

**Run migration:**
```bash
bench migrate
```

---

### Phase 2: Security Implementation (Days 1-2, 2 hours)

#### Task 2.1: Add Permission Checks (Day 1, 1 hour)

**File:** `kopos_connector/kopos_connector/api/modifiers.py`

```python
@frappe.whitelist()
def get_modifier_sales_report(
    from_date: str,
    to_date: str,
    modifier_group: str | None = None
) -> list[dict]:
    """
    Get modifier sales report with permission check.
    
    Args:
        from_date: Start date
        to_date: End date
        modifier_group: Optional filter by group
        
    Returns:
        List of modifier sales data
    """
    # Permission check
    if not frappe.has_permission("POS Invoice", "read"):
        frappe.throw(
            _("Not permitted to view sales reports"),
            frappe.PermissionError
        )
    
    # Validate inputs
    from_date = getdate(from_date).isoformat()
    to_date = getdate(to_date).isoformat()
    
    conditions = ["date BETWEEN %s AND %s"]
    params = [from_date, to_date]
    
    if modifier_group:
        if not frappe.db.exists("KoPOS Modifier Group", modifier_group):
            frappe.throw(_("Invalid modifier group"), frappe.ValidationError)
        conditions.append("modifier_group = %s")
        params.append(modifier_group)
    
    return frappe.db.sql(f"""
        SELECT 
            modifier_name,
            group_name,
            SUM(selection_count) as total_selections,
            SUM(revenue) as total_revenue
        FROM `tabKoPOS Modifier Stats`
        WHERE {' AND '.join(conditions)}
        GROUP BY modifier_name, group_name
        ORDER BY total_selections DESC
    """, tuple(params), as_dict=True)
```

#### Task 2.2: Error Handling (Day 2, 1 hour)

**Update `modifiers.py` with comprehensive error handling:**

```python
def _populate_modifiers_table(invoice_item, snapshot: dict) -> None:
    """
    Populate child table with modifier data and error handling.
    
    Args:
        invoice_item: POS Invoice Item document
        snapshot: Sanitized modifier snapshot
        
    Raises:
        Exception: If population fails (logged and re-raised)
    """
    modifiers = snapshot.get("modifiers", [])
    
    try:
        for idx, mod in enumerate(modifiers):
            # Resolve FK links safely
            modifier_group = _resolve_link(mod.get("group_id"), "KoPOS Modifier Group")
            modifier_option = _resolve_link(mod.get("id"), "KoPOS Modifier Option")
            
            invoice_item.append("custom_kopos_modifiers_table", {
                "modifier_group": modifier_group,
                "modifier_group_name": mod.get("group_name", ""),
                "modifier_option": modifier_option,
                "modifier_name": mod.get("name", ""),
                "price_adjustment": flt(mod.get("price")),
                "base_price": flt(mod.get("base_price")),
                "is_default": bool(mod.get("is_default")),
                "display_order": idx,
            })
    except Exception as e:
        frappe.log_error(
            title="KoPOS Modifier Population Error",
            message=f"Failed to populate modifiers for {invoice_item.name}: {str(e)}\n\n"
                    f"Snapshot: {json.dumps(snapshot, indent=2)}"
        )
        raise
```

---

### Phase 3: Mobile UI Updates (Days 2-3, 10 hours)

#### Task 3.1: Update Breakpoints (Day 2, 1 hour)

**File:** `mobile/src/theme/breakpoints.ts`

```typescript
/**
 * Responsive breakpoints for KoPOS mobile app
 */

export const BREAKPOINTS = {
  phone: 480,
  tabletSmall: 700,    // NEW: For 8.7" tablets
  tablet: 900,         // 10" tablets
  tabletLarge: 1024,   // 11"+ tablets
} as const;

export type DeviceType = 
  | 'phone' 
  | 'phoneLarge' 
  | 'tabletSmall' 
  | 'tablet' 
  | 'tabletLarge';

export const getDeviceType = (width: number): DeviceType => {
  if (width < BREAKPOINTS.phone) return 'phone';
  if (width < BREAKPOINTS.tabletSmall) return 'phoneLarge';
  if (width < BREAKPOINTS.tablet) return 'tabletSmall';  // 8.7"
  if (width < BREAKPOINTS.tabletLarge) return 'tablet';  // 10"
  return 'tabletLarge';
};

export const isTablet = (width: number): boolean => {
  return width >= BREAKPOINTS.tabletSmall;
};

export const useResponsiveValue = <T>(
  values: Partial<Record<DeviceType, T>>
): T | undefined => {
  const { width } = Dimensions.get('window');
  const deviceType = getDeviceType(width);
  return values[deviceType];
};
```

#### Task 3.2: Update Touch Targets (Day 2, 1 hour)

**File:** `mobile/src/theme/sizing.ts`

```typescript
/**
 * Sizing constants optimized for touch targets
 */

import { DeviceType } from './breakpoints';

export interface TouchTargets {
  option: number;        // Modifier option button height
  button: number;        // Action button height
  buttonMinWidth: number; // Minimum button width
}

export const TOUCH_TARGETS: Record<DeviceType, TouchTargets> = {
  phone: {
    option: 44,
    button: 44,
    buttonMinWidth: 88,
  },
  phoneLarge: {
    option: 46,
    button: 46,
    buttonMinWidth: 100,
  },
  tabletSmall: {  // 8.7" tablets
    option: 50,   // Up from 40
    button: 48,   // Up from 40
    buttonMinWidth: 120, // Up from 88
  },
  tablet: {
    option: 52,
    button: 52,
    buttonMinWidth: 140,
  },
  tabletLarge: {
    option: 52,
    button: 52,
    buttonMinWidth: 140,
  },
};

export const TYPOGRAPHY = {
  phone: {
    optionText: 13,
    priceText: 12,
    groupTitle: 13,
  },
  phoneLarge: {
    optionText: 13,
    priceText: 12,
    groupTitle: 13,
  },
  tabletSmall: {  // 8.7" tablets
    optionText: 14,  // Up from 13
    priceText: 13,   // Up from 12
    groupTitle: 14,  // Up from 13
  },
  tablet: {
    optionText: 15,
    priceText: 14,
    groupTitle: 15,
  },
  tabletLarge: {
    optionText: 15,
    priceText: 14,
    groupTitle: 15,
  },
};
```

#### Task 3.3: Update ModifierSheet Styles (Day 3, 4 hours)

**File:** `mobile/src/components/ModifierSheet/styles.ts`

```typescript
import { StyleSheet, Dimensions } from 'react-native';
import { BREAKPOINTS, getDeviceType } from '../../theme/breakpoints';
import { TOUCH_TARGETS, TYPOGRAPHY } from '../../theme';
import { colors, typography, radius, spacing } from '../../theme';

export const useStyles = () => {
  const screenWidth = Dimensions.get('window').width;
  const deviceType = getDeviceType(screenWidth);
  const targets = TOUCH_TARGETS[deviceType];
  const typo = TYPOGRAPHY[deviceType];
  const isTablet = screenWidth >= BREAKPOINTS.tabletSmall;
  
  return StyleSheet.create({
    // Modal overlay
    modalOverlay: {
      flex: 1,
      backgroundColor: 'rgba(0, 0, 0, 0.5)',
      justifyContent: 'center',
      alignItems: 'center',
    },
    
    // Sheet container
    modifierSheet: {
      width: isTablet ? '96%' : '94%',
      maxWidth: isTablet 
        ? Math.min(760, screenWidth * 0.92)  // Down from 980 for 8.7"
        : 680,  // Up from 640
      maxHeight: isTablet ? '88%' : '86%',
      minHeight: isTablet ? 400 : 360,
      backgroundColor: colors.surface,
      borderRadius: radius.lg,
      borderWidth: 1,
      borderColor: colors.border,
      padding: isTablet ? 16 : 14,
      gap: isTablet ? 12 : 10,
    },
    
    // Header
    sheetHeader: {
      flexDirection: 'row',
      justifyContent: 'space-between',
      alignItems: 'center',
      paddingBottom: spacing.sm,
      borderBottomWidth: 1,
      borderBottomColor: colors.border,
    },
    
    sheetTitle: {
      fontFamily: typography.heading,
      fontSize: 18,
      color: colors.textPrimary,
    },
    
    runningTotal: {
      fontFamily: typography.mono,
      fontSize: 16,
      color: colors.accent,
    },
    
    // Groups container
    groupsContainer: {
      flexDirection: isTablet ? 'row' : 'column',
      flexWrap: 'wrap',
      gap: isTablet ? 12 : 10,
    },
    
    // Group card
    modifierGroupCard: {
      borderWidth: 1,
      borderColor: colors.border,
      borderRadius: radius.md,
      backgroundColor: colors.raised,
      padding: isTablet ? 12 : 10,  // Up from 8
      gap: isTablet ? 10 : 8,
    },
    
    modifierGroupCardWide: {
      width: isTablet ? '48%' : '100%',
      minWidth: isTablet ? 300 : 0,  // Down from 360 for 8.7"
    },
    
    groupTitle: {
      fontFamily: typography.subheading,
      fontSize: typo.groupTitle,
      color: colors.textPrimary,
      marginBottom: spacing.xs,
    },
    
    requiredIndicator: {
      color: colors.error,
      marginLeft: 4,
    },
    
    // Options grid
    optionsGrid: {
      gap: spacing.sm,
    },
    
    optionsGridMultiColumn: {
      flexDirection: 'row',
      flexWrap: 'wrap',
    },
    
    // Modifier option
    modifierOption: {
      minHeight: targets.option,  // 50px on 8.7"
      borderRadius: radius.sm,
      borderWidth: 1,
      borderColor: colors.border,
      backgroundColor: colors.surface,
      paddingHorizontal: isTablet ? 14 : 12,
      flexDirection: 'row',
      alignItems: 'center',
      justifyContent: 'space-between',
    },
    
    modifierOptionSelected: {
      borderColor: colors.accent,
      backgroundColor: colors.accentLight,
      borderWidth: 2,
    },
    
    modifierOptionText: {
      fontFamily: typography.body,
      fontSize: typo.optionText,  // 14px on 8.7"
      color: colors.textPrimary,
      flex: 1,
      marginRight: spacing.sm,
    },
    
    modifierOptionPrice: {
      fontFamily: typography.mono,
      fontSize: typo.priceText,  // 13px on 8.7"
      color: colors.textSecondary,
    },
    
    // Actions
    modifierActions: {
      flexDirection: 'row',
      justifyContent: 'flex-end',
      gap: isTablet ? 12 : 10,
      paddingTop: spacing.sm,
      borderTopWidth: 1,
      borderTopColor: colors.border,
    },
    
    modifierCancel: {
      height: targets.button,  // 48px on 8.7"
      minWidth: targets.buttonMinWidth,  // 120px on 8.7"
      borderRadius: radius.md,
      borderWidth: 1,
      borderColor: colors.border,
      backgroundColor: colors.raised,
      alignItems: 'center',
      justifyContent: 'center',
      paddingHorizontal: isTablet ? 20 : 16,
    },
    
    modifierConfirm: {
      height: targets.button,  // 48px on 8.7"
      minWidth: targets.buttonMinWidth + 20,  // 140px on 8.7"
      borderRadius: radius.md,
      backgroundColor: colors.accent,
      alignItems: 'center',
      justifyContent: 'center',
      paddingHorizontal: isTablet ? 24 : 20,
    },
    
    actionButtonText: {
      fontFamily: typography.body,
      fontSize: 14,
      color: colors.textPrimary,
    },
    
    confirmButtonText: {
      fontFamily: typography.bodyBold,
      fontSize: 14,
      color: colors.white,
    },
  });
};
```

#### Task 3.4: Testing on Device (Day 3, 4 hours)

**Test checklist for Samsung Tab A11 (8.7"):**

- [ ] Modifier sheet opens smoothly
- [ ] Touch targets are easily tappable (50px options)
- [ ] Text is readable at arm's length (14px)
- [ ] 2-column layout works properly
- [ ] Scrolling is smooth with multiple modifier groups
- [ ] Confirm button shows total ("Add · RM14.00")
- [ ] Cancel button works correctly
- [ ] Landscape mode works
- [ ] Performance is acceptable (< 200ms to open)

---

### Phase 4: ERPNext UI (Days 2-3, 10 hours)

#### Task 4.1: Client Script (Day 2, 4 hours)

**File:** Create via `ensure_pos_invoice_modifier_script()` in `install.py`

```python
def ensure_pos_invoice_modifier_script():
    """Create client script for modifier display in POS Invoice."""
    
    script_name = "KoPOS POS Invoice Modifier Display"
    script_body = """
/**
 * KoPOS Modifier Display for POS Invoice
 * Shows modifier badges and expandable details
 */

frappe.ui.form.on("POS Invoice Item", {
    custom_kopos_has_modifiers: function(frm, cdt, cdn) {
        try {
            const row = frappe.get_doc(cdt, cdn);
            
            if (!row) {
                console.warn(`Row not found: ${cdt}/${cdn}`);
                return;
            }
            
            ModifierBadgeManager.toggle(frm, row);
            
        } catch (error) {
            frappe.show_alert({
                message: __("Error updating modifier display"),
                indicator: "red"
            }, 5);
            console.error("Modifier handler error:", error);
        }
    },
    
    items_remove: function(frm, cdt, cdn) {
        // Cleanup when row is removed
        ModifierBadgeManager.cleanup(cdn);
    }
});


frappe.ui.form.on("POS Invoice", {
    refresh: function(frm) {
        // Add modifier summary button
        if (frm.doc.docstatus === 1) {
            const modifierCount = frm.doc.items.reduce((sum, item) => 
                sum + (item.custom_kopos_has_modifiers || 0), 0);
            
            if (modifierCount > 0) {
                frm.add_custom_button(__("Modifier Summary"), () => {
                    show_modifier_summary(frm);
                }, __("View"));
            }
        }
        
        // Add badges to existing items
        frm.doc.items.forEach(item => {
            if (item.custom_kopos_has_modifiers) {
                ModifierBadgeManager.show(frm, item);
            }
        });
    }
});


/**
 * Manages modifier badge display on POS items
 */
const ModifierBadgeManager = {
    _cache: new Map(),
    
    /**
     * Toggle modifier badge visibility
     */
    toggle: function(frm, row) {
        const hasModifiers = Boolean(row.custom_kopos_has_modifiers);
        const rowName = row.name;
        
        if (hasModifiers) {
            this._show(frm, row);
            this._cache.set(rowName, true);
        } else {
            this._hide(frm, row);
            this._cache.delete(rowName);
        }
    },
    
    /**
     * Show modifier badge
     */
    show: function(frm, row) {
        this._show(frm, row);
    },
    
    _show: function(frm, row) {
        const $row = this._getRowElement(row.name);
        if (!$row) return;
        
        // Remove existing badge first
        this._hide(frm, row);
        
        const badge = this._createBadge(row);
        $row.find(".col-name").append(badge);
    },
    
    /**
     * Hide modifier badge
     */
    _hide: function(frm, row) {
        const $row = this._getRowElement(row.name);
        if ($row) {
            $row.find(".modifier-badge").remove();
        }
    },
    
    /**
     * Get jQuery element for grid row
     */
    _getRowElement: function(rowName) {
        const $grid = cur_frm.fields_dict.items?.grid;
        return $grid?.wrapper?.find(`[data-name="${rowName}"]`);
    },
    
    /**
     * Create modifier badge HTML
     */
    _createBadge: function(row) {
        const count = this._getModifierCount(row);
        return $(`
            <span class="modifier-badge label label-info" 
                  style="margin-left: 8px; cursor: pointer;"
                  onclick="show_item_modifiers('${row.name}')">
                <i class="fa fa-plus-circle"></i> ${count} ${__("modifiers")}
            </span>
        `);
    },
    
    /**
     * Extract modifier count from row data
     */
    _getModifierCount: function(row) {
        try {
            const snapshot = JSON.parse(row.custom_kopos_modifiers || "{}");
            return snapshot.count || snapshot.modifiers?.length || 0;
        } catch {
            return 0;
        }
    },
    
    /**
     * Cleanup resources for removed row
     */
    cleanup: function(rowName) {
        this._cache.delete(rowName);
    }
};


/**
 * Show modifier details for an item
 */
window.show_item_modifiers = function(itemName) {
    const item = cur_frm.doc.items.find(i => i.name === itemName);
    if (!item) return;
    
    let snapshot = {};
    try {
        snapshot = JSON.parse(item.custom_kopos_modifiers || "{}");
    } catch (e) {
        console.warn("Invalid modifier JSON for item:", itemName);
        return;
    }
    
    const modifiers = snapshot.modifiers || [];
    
    if (modifiers.length === 0) {
        frappe.msgprint(__("No modifiers for this item"));
        return;
    }
    
    // Build safe HTML
    const rows = modifiers.map(m => {
        const name = frappe.utils.escape_html(m.name || '');
        const group = frappe.utils.escape_html(m.group_name || '');
        const price = parseFloat(m.price || 0).toFixed(2);
        
        return `
            <tr>
                <td>${group}</td>
                <td>${name}</td>
                <td class="text-right">+${price}</td>
            </tr>
        `;
    }).join('');
    
    const total = parseFloat(snapshot.total || 0).toFixed(2);
    
    const html = `
        <div>
            <h5>${frappe.utils.escape_html(item.item_name || '')}</h5>
            <table class="table table-bordered">
                <thead>
                    <tr>
                        <th>${__('Group')}</th>
                        <th>${__('Modifier')}</th>
                        <th class="text-right">${__('Price')}</th>
                    </tr>
                </thead>
                <tbody>
                    ${rows}
                </tbody>
                <tfoot>
                    <tr>
                        <td colspan="2"><strong>${__('Total')}</strong></td>
                        <td class="text-right"><strong>+${total}</strong></td>
                    </tr>
                </tfoot>
            </table>
        </div>
    `;
    
    frappe.msgprint({
        title: __("Modifier Details"),
        message: html,
        wide: true
    });
};


/**
 * Show modifier summary for entire invoice
 */
function show_modifier_summary(frm) {
    let rows = [];
    
    frm.doc.items.forEach(item => {
        if (item.custom_kopos_has_modifiers) {
            let snapshot = {};
            try {
                snapshot = JSON.parse(item.custom_kopos_modifiers || "{}");
            } catch (e) {
                console.warn("Invalid modifier JSON for item:", item.name);
                return;
            }
            
            const mods = (snapshot.modifiers || []).map(m => 
                frappe.utils.escape_html(m.name || '') + 
                ' (+' + parseFloat(m.price || 0).toFixed(2) + ')'
            ).join(", ");
            
            rows.push({
                item_name: frappe.utils.escape_html(item.item_name || ''),
                modifiers: mods,
                total: parseFloat(item.custom_kopos_modifier_total || 0).toFixed(2)
            });
        }
    });
    
    const tbody = rows.map(row => `
        <tr>
            <td>${row.item_name}</td>
            <td>${row.modifiers}</td>
            <td class="text-right">${row.total}</td>
        </tr>
    `).join('');
    
    const html = `
        <table class="table table-bordered">
            <thead>
                <tr>
                    <th>${__('Item')}</th>
                    <th>${__('Modifiers')}</th>
                    <th class="text-right">${__('Total')}</th>
                </tr>
            </thead>
            <tbody>
                ${tbody}
            </tbody>
        </table>
    `;
    
    frappe.msgprint({
        title: __("Modifier Summary"),
        message: html,
        wide: true
    });
}
""".strip()
    
    existing_name = frappe.db.exists("Client Script", script_name)
    if existing_name:
        doc = frappe.get_doc("Client Script", existing_name)
        doc.dt = "POS Invoice"
        doc.view = "Form"
        doc.enabled = 1
        doc.script = script_body
        doc.save(ignore_permissions=True)
    else:
        frappe.get_doc({
            "doctype": "Client Script",
            "name": script_name,
            "dt": "POS Invoice",
            "view": "Form",
            "enabled": 1,
            "script": script_body,
        }).insert(ignore_permissions=True)
```

#### Task 4.2: Modifier Sales Report (Day 3, 4 hours)

**File:** `kopos_connector/kopos/report/modifier_sales_analysis/modifier_sales_analysis.py`

```python
"""
KoPOS Modifier Sales Analysis Report
"""

import frappe
from frappe import _


def get_columns():
    """Define report columns."""
    return [
        {
            "fieldname": "modifier_name",
            "label": _("Modifier"),
            "fieldtype": "Data",
            "width": 200
        },
        {
            "fieldname": "group_name",
            "label": _("Group"),
            "fieldtype": "Data",
            "width": 120
        },
        {
            "fieldname": "selection_count",
            "label": _("Count"),
            "fieldtype": "Int",
            "width": 80
        },
        {
            "fieldname": "revenue",
            "label": _("Revenue"),
            "fieldtype": "Currency",
            "width": 100
        },
        {
            "fieldname": "avg_price",
            "label": _("Avg Price"),
            "fieldtype": "Currency",
            "width": 100
        },
    ]


def get_data(filters):
    """Get report data."""
    conditions = []
    params = []
    
    if filters.get("from_date"):
        conditions.append("date >= %s")
        params.append(filters.from_date)
    
    if filters.get("to_date"):
        conditions.append("date <= %s")
        params.append(filters.to_date)
    
    if filters.get("modifier_group"):
        conditions.append("modifier_group = %s")
        params.append(filters.modifier_group)
    
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    
    return frappe.db.sql(f"""
        SELECT 
            modifier_name,
            group_name,
            SUM(selection_count) as selection_count,
            SUM(revenue) as revenue,
            SUM(revenue) / NULLIF(SUM(selection_count), 0) as avg_price
        FROM `tabKoPOS Modifier Stats`
        {where}
        GROUP BY modifier_name, group_name
        ORDER BY selection_count DESC
    """, tuple(params), as_dict=True)


def get_filters():
    """Define report filters."""
    return [
        {
            "fieldname": "from_date",
            "label": _("From Date"),
            "fieldtype": "Date",
            "default": frappe.utils.add_months(frappe.utils.today(), -1),
        },
        {
            "fieldname": "to_date",
            "label": _("To Date"),
            "fieldtype": "Date",
            "default": frappe.utils.today(),
        },
        {
            "fieldname": "modifier_group",
            "label": _("Modifier Group"),
            "fieldtype": "Link",
            "options": "KoPOS Modifier Group",
        },
    ]
```

#### Task 4.3: Print Format (Day 3, 2 hours)

**Update print format to show modifiers:**

```html
{%- for item in doc.items -%}
  <div class="item-row">
    <span>{{ item.qty }}x {{ item.item_name }}</span>
    <span class="text-right">{{ frappe.format_value(item.amount, currency=doc.currency) }}</span>
  </div>
  
  {%- if item.custom_kopos_has_modifiers -%}
    {%- set snapshot = item.custom_kopos_modifiers | parse_json -%}
    {%- for mod in (snapshot.modifiers or []) -%}
    <div class="modifier-line" style="padding-left: 20px; font-size: 11px; color: #666;">
      <span>+ {{ mod.name }}</span>
      <span class="text-right">+{{ frappe.format_value(mod.price, currency=doc.currency) }}</span>
    </div>
    {%- endfor -%}
  {%- endif -%}
{%- endfor -%}
```

---

### Phase 5: Testing & Deployment (Days 1-2, 12 hours)

#### Task 5.1: Integration Tests (Day 1, 4 hours)

**Test scenarios:**

1. **Order submission with modifiers**
   - Submit order with 3 items, each with 2-3 modifiers
   - Verify JSON snapshot stored correctly
   - Verify child table populated correctly
   - Verify totals match

2. **Refund flow**
   - Create full refund
   - Verify modifiers copied correctly
   - Verify proportional amounts calculated

3. **Analytics aggregation**
   - Submit multiple orders
   - Run aggregation job
   - Verify stats are correct

4. **Reports**
   - Run modifier sales report
   - Verify data accuracy

#### Task 5.2: Performance Testing (Day 1, 4 hours)

**Load test scenarios:**

1. **Order submission (100 orders/minute)**
   - Verify latency < 200ms (P95)
   - Check for errors

2. **Analytics query (1000 rows)**
   - Verify latency < 500ms
   - Check index usage

3. **Dashboard query (daily stats)**
   - Verify latency < 100ms

#### Task 5.3: Documentation (Day 2, 4 hours)

**Create documentation:**

1. **API documentation**
   - Modifier payload format
   - Validation rules
   - Error codes

2. **User guide**
   - How to view modifiers in POS Invoice
   - How to run modifier reports
   - How to interpret analytics

3. **Developer documentation**
   - Data model
   - Integration points
   - Customization options

---

## Timeline & Effort

### Summary

| Phase | Description | Duration | Effort |
|-------|-------------|----------|--------|
| **Phase 1** | Backend Implementation | 3-4 days | 24 hours |
| **Phase 2** | Security Implementation | 1-2 days | 2 hours |
| **Phase 3** | Mobile UI Updates | 2-3 days | 10 hours |
| **Phase 4** | ERPNext UI | 2-3 days | 10 hours |
| **Phase 5** | Testing & Deployment | 1-2 days | 12 hours |
| **TOTAL** | | **8-12 days** | **56 hours** |

### Detailed Timeline

```
Week 1:
├── Day 1: Create DocTypes + Custom Fields (3h)
├── Day 2: Validation + Sanitization (3h)
├── Day 3: Synchronous Population (8h)
├── Day 4: Analytics + Tests (7h)
└── Day 5: Security + Error Handling (3h)

Week 2:
├── Day 6: Mobile Breakpoints + Touch Targets (2h)
├── Day 7: ModifierSheet Styles (4h)
├── Day 8: Mobile Testing (4h)
├── Day 9: Client Script + Report (8h)
└── Day 10: Testing + Documentation (8h)
```

---

## Risk Mitigation

### Identified Risks

| Risk | Impact | Probability | Mitigation | Status |
|------|--------|-------------|------------|--------|
| **Data inconsistency** | High | Medium | Synchronous population, JSON as source of truth | ✅ Addressed |
| **XSS attacks** | High | Medium | HTML escaping in `sanitize_modifier_text()` | ✅ Addressed |
| **Invalid input** | Medium | High | JSON schema validation | ✅ Addressed |
| **Missing permissions** | High | Low | Permission checks in all whitelisted methods | ✅ Addressed |
| **Performance on 8.7" tablets** | Medium | Medium | Optimized breakpoints and touch targets | ✅ Addressed |
| **Query performance** | Medium | Low | Proper indexes on child table | ✅ Addressed |
| **Migration failures** | Medium | Low | Test migration in staging first | ⚠️ To verify |
| **Async job failures** | Low | N/A | Using synchronous approach | ✅ Addressed |

### Monitoring Points

```python
# Add to monitoring endpoint
def get_modifier_system_health():
    """Health check for modifier system."""
    
    # Check for missing modifier data (should be 0)
    missing = frappe.db.sql("""
        SELECT COUNT(*) FROM `tabPOS Invoice Item` ii
        WHERE ii.custom_kopos_has_modifiers = 1
          AND NOT EXISTS (
              SELECT 1 FROM `tabKoPOS Invoice Item Modifier` m 
              WHERE m.parent = ii.name
          )
    """)[0][0]
    
    # Check analytics lag
    latest = frappe.db.get_value("KoPOS Modifier Stats", {}, "MAX(date)")
    
    # Check data consistency
    inconsistent = frappe.db.sql("""
        SELECT COUNT(*) FROM `tabPOS Invoice Item` ii
        WHERE ii.custom_kopos_has_modifiers = 1
          AND ii.custom_kopos_modifier_total != (
              SELECT COALESCE(SUM(price_adjustment), 0)
              FROM `tabKoPOS Invoice Item Modifier` m
              WHERE m.parent = ii.name
          )
    """)[0][0]
    
    return {
        "missing_modifiers": missing,
        "latest_analytics": latest,
        "inconsistent_totals": inconsistent,
        "status": "healthy" if missing == 0 and inconsistent == 0 else "degraded"
    }
```

---

## Success Metrics

### Performance Targets

| Metric | Target | Measurement |
|--------|--------|-------------|
| Order submission latency | < 200ms (P95) | Load testing |
| Modifier report query | < 500ms | Query profiling |
| Dashboard stats query | < 100ms | Query profiling |
| Mobile UI response | < 100ms | User testing |

### Quality Targets

| Metric | Target | Measurement |
|--------|--------|-------------|
| Test coverage | 100% modifier functions | Unit tests |
| Data consistency | 0 inconsistencies | Health check |
| XSS vulnerabilities | 0 vulnerabilities | Security audit |
| Mobile usability | 48-52px touch targets | Device testing |

### Business Targets

| Metric | Target | Measurement |
|--------|--------|-------------|
| Modifier visibility | 100% invoices with modifiers | Query |
| Analytics accuracy | 100% match with transactions | Reconciliation |
| Report availability | < 1s load time | Performance monitoring |

---

## Appendix

### File Structure

```
kopos_connector/
├── kopos/
│   ├── doctype/
│   │   ├── kopos_invoice_item_modifier/
│   │   │   ├── __init__.py
│   │   │   ├── kopos_invoice_item_modifier.json
│   │   │   └── kopos_invoice_item_modifier.py
│   │   └── kopos_modifier_stats/
│   │       ├── __init__.py
│   │       ├── kopos_modifier_stats.json
│   │       └── kopos_modifier_stats.py
│   └── report/
│       └── modifier_sales_analysis/
│           ├── __init__.py
│           ├── modifier_sales_analysis.js
│           └── modifier_sales_analysis.py
├── kopos_connector/
│   ├── api/
│   │   ├── __init__.py
│   │   ├── orders.py (modified)
│   │   └── modifiers.py (new)
│   ├── install/
│   │   └── install.py (modified)
│   └── patches/
│       └── v1_0/
│           └── add_modifier_fields.py (new)
├── tests/
│   └── test_modifiers.py (new)
└── docs/
    └── MODIFIER_IMPLEMENTATION_PLAN.md (this file)

mobile/
├── src/
│   ├── theme/
│   │   ├── breakpoints.ts (modified)
│   │   └── sizing.ts (modified)
│   └── components/
│       └── ModifierSheet/
│           └── styles.ts (modified)
```

### Dependencies

- **ERPNext**: v15+ (for custom field support)
- **Frappe**: v15+ (for JSON schema validation)
- **Python**: 3.10+ (for TypedDict)
- **React Native**: 0.72+ (for mobile app)

### References

- [ERPNext Custom Fields](https://frappeframework.com/docs/user/en/guides/basics/how-to-make-custom-fields)
- [Frappe Child Tables](https://frappeframework.com/docs/user/en/guides/basics/how-to-make-child-tables)
- [Frappe Scheduler](https://frappeframework.com/docs/user/en/guides/integration/scheduled_tasks)
- [React Native Responsive](https://reactnative.dev/docs/dimensions)

---

**Document Status:** Final  
**Last Updated:** 2026-03-14  
**Next Review:** After Phase 1 completion
