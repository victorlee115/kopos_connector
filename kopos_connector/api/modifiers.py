"""
KoPOS Modifier API

Handles validation, sanitization, and processing of modifier data.
"""

import json
import frappe
from frappe import _
from frappe.utils import cstr, flt, cint, getdate, add_days, today
from typing import TypedDict, Any
from html import escape


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


MODIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "modifiers": {
            "type": "array",
            "maxItems": 50,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "maxLength": 140},
                    "group_id": {"type": "string", "maxLength": 140},
                    "name": {"type": "string", "maxLength": 255},
                    "group_name": {"type": "string", "maxLength": 255},
                    "price": {"type": "number", "minimum": -999999, "maximum": 999999},
                    "base_price": {
                        "type": "number",
                        "minimum": -999999,
                        "maximum": 999999,
                    },
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
        jsonschema.validate({"modifiers": raw_modifiers}, MODIFIER_SCHEMA)
    except jsonschema.ValidationError as e:
        frappe.throw(
            _("Invalid modifier data: {0}").format(e.message), frappe.ValidationError
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

    sanitized = escape(str(value))
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
    if not isinstance(raw_item, dict):
        raise TypeError(f"Expected dict, got {type(raw_item).__name__}")

    raw_modifiers = raw_item.get("modifiers") or []

    if not isinstance(raw_modifiers, list):
        frappe.log_error(
            title="KoPOS Modifier Warning",
            message=f"Expected list for modifiers, got {type(raw_modifiers).__name__}",
        )
        raw_modifiers = []

    validated = validate_modifier_data(raw_modifiers)

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


def populate_modifiers_on_item(invoice_item: Any, raw_item: dict) -> None:
    """
    Populate modifier fields on POS Invoice Item synchronously.

    This is called BEFORE invoice.submit() to ensure atomicity.

    Args:
        invoice_item: POS Invoice Item document
        raw_item: Raw item data from POS client
    """
    if not raw_item.get("modifiers"):
        return

    snapshot = build_modifiers_snapshot(raw_item)

    invoice_item.custom_kopos_modifiers = serialize_json_compact(snapshot)
    invoice_item.custom_kopos_modifier_total = snapshot["total"]
    invoice_item.custom_kopos_has_modifiers = 1 if snapshot["count"] > 0 else 0

    _populate_modifiers_table(invoice_item, snapshot)


def _populate_modifiers_table(invoice_item: Any, snapshot: dict) -> None:
    """
    Populate child table with modifier data.

    Args:
        invoice_item: POS Invoice Item document
        snapshot: Sanitized modifier snapshot
    """
    modifiers = snapshot.get("modifiers", [])

    try:
        for idx, mod in enumerate(modifiers):
            modifier_group = _resolve_link(mod.get("group_id"), "KoPOS Modifier Group")
            modifier_option = _resolve_link(mod.get("id"), "KoPOS Modifier Option")

            invoice_item.append(
                "custom_kopos_modifiers_table",
                {
                    "modifier_group": modifier_group,
                    "modifier_group_name": mod.get("group_name", ""),
                    "modifier_option": modifier_option,
                    "modifier_name": mod.get("name", ""),
                    "price_adjustment": flt(mod.get("price")),
                    "base_price": flt(mod.get("base_price")),
                    "is_default": bool(mod.get("is_default")),
                    "display_order": idx,
                },
            )
    except Exception as e:
        frappe.log_error(
            title="KoPOS Modifier Population Error",
            message=f"Failed to populate modifiers for {invoice_item.name}: {str(e)}\n\n"
            f"Snapshot: {json.dumps(snapshot, indent=2)}",
        )
        raise


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


def copy_modifiers_to_refund(
    credit_item: Any, original_item: Any, refund_qty: float | None = None
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

    try:
        snapshot = frappe.parse_json(original_item.custom_kopos_modifiers or "{}")
    except (json.JSONDecodeError, TypeError):
        return

    if not snapshot.get("modifiers"):
        return

    original_qty = abs(flt(getattr(original_item, "qty", 0)))
    if refund_qty is None:
        refund_qty = original_qty

    if original_qty <= 0:
        return

    ratio = min(1.0, max(0.0, flt(refund_qty) / original_qty))

    credit_item.custom_kopos_modifiers = original_item.custom_kopos_modifiers
    credit_item.custom_kopos_modifier_total = (
        flt(original_item.custom_kopos_modifier_total or 0) * ratio
    )
    credit_item.custom_kopos_has_modifiers = original_item.custom_kopos_has_modifiers

    if hasattr(original_item, "custom_kopos_modifiers_table"):
        for mod_entry in original_item.custom_kopos_modifiers_table:
            credit_item.append(
                "custom_kopos_modifiers_table",
                {
                    "modifier_group": mod_entry.modifier_group,
                    "modifier_group_name": mod_entry.modifier_group_name,
                    "modifier_option": mod_entry.modifier_option,
                    "modifier_name": mod_entry.modifier_name,
                    "price_adjustment": flt(mod_entry.price_adjustment) * ratio,
                    "base_price": mod_entry.base_price,
                    "is_default": mod_entry.is_default,
                    "display_order": mod_entry.display_order,
                },
            )


def aggregate_modifier_stats(date: str | None = None) -> int:
    """
    Aggregate modifier stats for a given date.

    Called nightly by scheduler to pre-compute analytics.

    Args:
        date: Date to aggregate (default: yesterday)

    Returns:
        Number of stat records created/updated
    """
    if not date:
        date = add_days(today(), -1)

    date = getdate(date).isoformat()

    stats = frappe.db.sql(
        """
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
        INNER JOIN `tabPOS Invoice` inv ON ii.parent = inv.name
        WHERE inv.posting_date = %s
          AND inv.docstatus = 1
          AND inv.is_return = 0
        GROUP BY m.modifier_option, m.modifier_group, m.modifier_name, m.modifier_group_name
    """,
        (date, date),
        as_dict=True,
    )

    count = 0
    for stat in stats:
        existing = frappe.db.exists(
            "KoPOS Modifier Stats",
            {
                "date": date,
                "modifier_option": stat.modifier_option,
            },
        )

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


@frappe.whitelist()
def get_modifier_sales_report(
    from_date: str, to_date: str, modifier_group: str | None = None
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
    if not frappe.has_permission("POS Invoice", "read"):
        frappe.throw(_("Not permitted to view sales reports"), frappe.PermissionError)

    from_date = getdate(from_date).isoformat()
    to_date = getdate(to_date).isoformat()

    conditions = ["date BETWEEN %s AND %s"]
    params = [from_date, to_date]

    if modifier_group:
        if not frappe.db.exists("KoPOS Modifier Group", modifier_group):
            frappe.throw(_("Invalid modifier group"), frappe.ValidationError)
        conditions.append("modifier_group = %s")
        params.append(modifier_group)

    return frappe.db.sql(
        f"""
        SELECT 
            modifier_name,
            group_name,
            SUM(selection_count) as total_selections,
            SUM(revenue) as total_revenue
        FROM `tabKoPOS Modifier Stats`
        WHERE {" AND ".join(conditions)}
        GROUP BY modifier_name, group_name
        ORDER BY total_selections DESC
    """,
        tuple(params),
        as_dict=True,
    )
