"""
KoPOS Modifier API

Handles validation, sanitization, and processing of modifier data.
"""

from __future__ import annotations

import json
import re
import unicodedata
import frappe
from frappe import _
from frappe.utils import cstr, flt, cint, getdate, add_days, today, now_datetime
from typing import TypedDict, Any
from html import escape


SNAPSHOT_VERSION = "1.0"
MAX_MODIFIERS_PER_ITEM = 50
MAX_MODIFIER_ID_LENGTH = 140
MAX_MODIFIER_NAME_LENGTH = 255
MAX_MODIFIER_PRICE = 999999
MIN_MODIFIER_PRICE = -999999

DANGEROUS_PATTERNS = re.compile(
    r"(javascript|vbscript|data|blob):|"
    r"expression\s*\(|"
    r"@import|"
    r"behavior\s*:",
    re.IGNORECASE,
)


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

    version: str
    modifiers: list[ModifierDict]
    total: float
    count: int


MODIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "modifiers": {
            "type": "array",
            "maxItems": MAX_MODIFIERS_PER_ITEM,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "maxLength": MAX_MODIFIER_ID_LENGTH},
                    "group_id": {"type": "string", "maxLength": MAX_MODIFIER_ID_LENGTH},
                    "name": {"type": "string", "maxLength": MAX_MODIFIER_NAME_LENGTH},
                    "group_name": {
                        "type": "string",
                        "maxLength": MAX_MODIFIER_NAME_LENGTH,
                    },
                    "price": {
                        "type": "number",
                        "minimum": MIN_MODIFIER_PRICE,
                        "maximum": MAX_MODIFIER_PRICE,
                    },
                    "base_price": {
                        "type": "number",
                        "minimum": MIN_MODIFIER_PRICE,
                        "maximum": MAX_MODIFIER_PRICE,
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
        Validated and normalized modifier list

    Raises:
        frappe.ValidationError: If validation fails
    """
    try:
        import jsonschema
    except ImportError:
        frappe.throw(
            _(
                "Modifier validation requires jsonschema package. Install via: pip install jsonschema"
            ),
            frappe.ValidationError,
        )

    try:
        jsonschema.validate({"modifiers": raw_modifiers}, MODIFIER_SCHEMA)
    except jsonschema.ValidationError as e:
        frappe.throw(
            _("Invalid modifier data: {0}").format(e.message), frappe.ValidationError
        )

    normalized = []
    for mod in raw_modifiers:
        if isinstance(mod, dict):
            normalized.append(
                {
                    "id": cstr(mod.get("id", "")),
                    "group_id": cstr(mod.get("group_id", "")),
                    "name": mod.get("name", ""),
                    "group_name": mod.get("group_name", ""),
                    "price": flt(mod.get("price")),
                    "base_price": flt(mod.get("base_price")),
                    "is_default": bool(mod.get("is_default")),
                }
            )
    return normalized


def sanitize_modifier_text(value: str) -> str:
    """
    Sanitize text for safe storage and display.
    Prevents XSS attacks by escaping HTML entities and removing dangerous patterns.

    Args:
        value: Raw text input

    Returns:
        Sanitized text (max 255 chars)
    """
    if not value:
        return ""

    text = str(value)

    normalized = unicodedata.normalize("NFKC", text)

    sanitized = "".join(c for c in normalized if ord(c) >= 32 or c in "\n\r\t")

    if DANGEROUS_PATTERNS.search(sanitized):
        sanitized = DANGEROUS_PATTERNS.sub("[removed]", sanitized)

    sanitized = escape(sanitized, quote=True)

    if len(sanitized) > MAX_MODIFIER_NAME_LENGTH:
        last_amp = sanitized.rfind("&", 0, MAX_MODIFIER_NAME_LENGTH)
        if last_amp > MAX_MODIFIER_NAME_LENGTH - 6:
            sanitized = sanitized[:last_amp]
        else:
            sanitized = sanitized[:MAX_MODIFIER_NAME_LENGTH]

    return sanitized


def validate_modifier_totals(snapshot: dict) -> dict:
    """
    Validate and correct modifier totals.

    Args:
        snapshot: Modifier snapshot dictionary

    Returns:
        Snapshot with validated/corrected totals
    """
    modifiers = snapshot.get("modifiers", [])
    calculated_total = sum(flt(m.get("price", 0)) for m in modifiers)
    reported_total = flt(snapshot.get("total", 0))

    if abs(calculated_total - reported_total) > 0.01:
        frappe.log_error(
            title="KoPOS Modifier Total Mismatch",
            message=f"Calculated: {calculated_total}, Reported: {reported_total}, "
            f"Modifier count: {len(modifiers)}",
        )
        snapshot["total"] = calculated_total

    return snapshot


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
        raise TypeError(
            f"build_modifiers_snapshot expected dict, got {type(raw_item).__name__}"
        )

    raw_modifiers = raw_item.get("modifiers")

    if raw_modifiers is None:
        raw_modifiers = []
    elif not isinstance(raw_modifiers, list):
        frappe.log_error(
            title="KoPOS Modifier Type Error",
            message=json.dumps(
                {
                    "error": "modifiers field has invalid type",
                    "expected": "list",
                    "actual": type(raw_modifiers).__name__,
                    "raw_item_keys": list(raw_item.keys())
                    if isinstance(raw_item, dict)
                    else None,
                }
            ),
        )
        frappe.throw(
            _("Invalid modifiers data type: expected list, got {0}").format(
                type(raw_modifiers).__name__
            ),
            frappe.ValidationError,
        )

    validated = validate_modifier_data(raw_modifiers)

    sanitized_modifiers = []
    for idx, mod in enumerate(validated):
        if not isinstance(mod, dict):
            frappe.log_error(
                title="KoPOS Modifier Warning",
                message=f"Skipping non-dict modifier at index {idx}: {type(mod).__name__}",
            )
            continue

        sanitized_modifiers.append(
            {
                "id": cstr(mod.get("id", "")),
                "group_id": cstr(mod.get("group_id", "")),
                "name": sanitize_modifier_text(mod.get("name", "")),
                "group_name": sanitize_modifier_text(mod.get("group_name", "")),
                "price": flt(mod.get("price"), 2),
                "base_price": flt(mod.get("base_price"), 2),
                "is_default": bool(mod.get("is_default")),
            }
        )

    snapshot: ModifierSnapshot = {
        "version": SNAPSHOT_VERSION,
        "modifiers": sanitized_modifiers,
        "total": flt(raw_item.get("modifier_total"), 2),
        "count": len(sanitized_modifiers),
    }

    return validate_modifier_totals(snapshot)


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


def _batch_resolve_links(modifiers: list[dict]) -> dict[str, set[str]]:
    """
    Resolve all links in 2 queries instead of 2N queries.

    Args:
        modifiers: List of modifier dictionaries

    Returns:
        Dictionary with 'groups' and 'options' sets of existing names
    """
    group_ids = {m.get("group_id") for m in modifiers if m.get("group_id")}
    option_ids = {m.get("id") for m in modifiers if m.get("id")}

    existing_groups = set()
    existing_options = set()

    if group_ids:
        existing_groups = set(
            frappe.db.get_all(
                "KoPOS Modifier Group",
                filters={"name": ("in", list(group_ids))},
                pluck="name",
            )
        )

    if option_ids:
        existing_options = set(
            frappe.db.get_all(
                "KoPOS Modifier Option",
                filters={"name": ("in", list(option_ids))},
                pluck="name",
            )
        )

    return {"groups": existing_groups, "options": existing_options}


def _populate_modifiers_table(invoice_item: Any, snapshot: dict) -> None:
    """
    Populate child table with modifier data.
    Uses batch link resolution for performance.

    Args:
        invoice_item: POS Invoice Item document
        snapshot: Sanitized modifier snapshot
    """
    modifiers = snapshot.get("modifiers", [])

    if not modifiers:
        return

    try:
        existing_links = _batch_resolve_links(modifiers)

        for idx, mod in enumerate(modifiers):
            group_id = mod.get("group_id", "")
            option_id = mod.get("id", "")

            modifier_group = group_id if group_id in existing_links["groups"] else None
            modifier_option = (
                option_id if option_id in existing_links["options"] else None
            )

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
            message=json.dumps(
                {
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                    "invoice_item": getattr(invoice_item, "name", "unknown"),
                    "modifier_count": len(modifiers),
                }
            ),
        )
        raise


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
    if not hasattr(credit_item, "custom_kopos_modifiers"):
        return

    if not hasattr(original_item, "custom_kopos_modifiers"):
        return

    if not getattr(original_item, "custom_kopos_has_modifiers", 0):
        return

    try:
        snapshot = frappe.parse_json(original_item.custom_kopos_modifiers or "{}")
    except (json.JSONDecodeError, TypeError) as e:
        frappe.log_error(
            title="KoPOS Modifier Parse Error",
            message=f"Failed to parse modifiers JSON for {getattr(original_item, 'name', 'unknown')}: {e}",
        )
        return

    if not snapshot or not snapshot.get("modifiers"):
        return

    original_qty = abs(flt(getattr(original_item, "qty", 0)))
    if refund_qty is None:
        refund_qty = original_qty

    if original_qty <= 0:
        frappe.log_error(
            title="KoPOS Modifier Warning",
            message=f"Original item {getattr(original_item, 'name', 'unknown')} "
            f"has invalid qty: {original_qty}",
        )
        return

    refund_qty_val = flt(refund_qty)
    if refund_qty_val < 0:
        return

    ratio = min(1.0, max(0.0, refund_qty_val / original_qty))

    credit_item.custom_kopos_modifiers = original_item.custom_kopos_modifiers
    credit_item.custom_kopos_modifier_total = round(
        flt(original_item.custom_kopos_modifier_total or 0) * ratio, 2
    )
    credit_item.custom_kopos_has_modifiers = original_item.custom_kopos_has_modifiers

    if hasattr(original_item, "custom_kopos_modifiers_table") and hasattr(
        credit_item, "custom_kopos_modifiers_table"
    ):
        for mod_entry in original_item.custom_kopos_modifiers_table:
            credit_item.append(
                "custom_kopos_modifiers_table",
                {
                    "modifier_group": mod_entry.modifier_group,
                    "modifier_group_name": mod_entry.modifier_group_name,
                    "modifier_option": mod_entry.modifier_option,
                    "modifier_name": mod_entry.modifier_name,
                    "price_adjustment": round(
                        flt(mod_entry.price_adjustment) * ratio, 2
                    ),
                    "base_price": mod_entry.base_price,
                    "is_default": mod_entry.is_default,
                    "display_order": mod_entry.display_order,
                },
            )


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
            result["errors"].append(
                {
                    "modifier_option": stat.modifier_option,
                    "error": str(e),
                }
            )

    frappe.db.commit()

    frappe.logger("kopos").info(
        f"Modifier stats aggregated: {result['processed']} records for {date_str}"
    )

    return result


def _notify_aggregation_failure(date: str, error: str) -> None:
    """Send email notification for aggregation failures."""
    from html import escape

    recipients = frappe.get_all(
        "User", filters={"role": "System Manager", "enabled": 1}, pluck="email"
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


@frappe.whitelist()
@frappe.rate_limit(limit=10, seconds=60)
def get_modifier_sales_report(
    from_date: str,
    to_date: str,
    modifier_group: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """
    Get modifier sales report with permission check.

    Args:
        from_date: Start date
        to_date: End date
        modifier_group: Optional filter by group
        limit: Maximum results to return (default 100, max 1000)

    Returns:
        List of modifier sales data
    """
    if not frappe.has_permission("POS Invoice", "read"):
        frappe.throw(_("Not permitted to view sales reports"), frappe.PermissionError)

    try:
        from_date_obj = getdate(from_date)
        to_date_obj = getdate(to_date)
        from_date = from_date_obj.isoformat()
        to_date = to_date_obj.isoformat()
    except (ValueError, TypeError):
        frappe.throw(_("Invalid date format"), frappe.ValidationError)

    if (to_date_obj - from_date_obj).days > 365:
        frappe.throw(_("Date range cannot exceed 365 days"), frappe.ValidationError)

    limit = min(cint(limit), 1000)

    filters = {
        "date": ["between", [from_date, to_date]],
    }

    if modifier_group:
        if not frappe.db.exists("KoPOS Modifier Group", modifier_group):
            frappe.throw(_("Invalid modifier group"), frappe.ValidationError)
        filters["modifier_group"] = modifier_group

    return frappe.get_all(
        "KoPOS Modifier Stats",
        filters=filters,
        fields=[
            "modifier_name",
            "group_name",
            "SUM(selection_count) as total_selections",
            "SUM(revenue) as total_revenue",
        ],
        group_by="modifier_name, group_name",
        order_by="total_selections DESC",
        limit_page_length=limit,
    )


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
    start = getdate(from_date)
    end = getdate(to_date)

    if start > end:
        frappe.throw(_("From Date cannot be after To Date"))

    if (end - start).days > 365:
        frappe.throw(_("Date range cannot exceed 365 days"))

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

    start = getdate(from_date)
    end = getdate(to_date)

    if (end - start).days > 365:
        frappe.throw(_("Date range cannot exceed 365 days"))

    results = []
    current = start

    while current <= end:
        result = aggregate_modifier_stats(current.isoformat())
        results.append({"date": current.isoformat(), **result})
        current = add_days(current, 1)

    return {"retries": results}
