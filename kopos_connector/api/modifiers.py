"""
Legacy KoPOS modifier sales statistics for explicit migration and support runs.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, cint, getdate, add_days, today, now_datetime
from html import escape


def aggregate_modifier_stats(date: str | None = None) -> dict:
    """
    Aggregate legacy modifier stats for an explicit migration/support run.

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
        if not frappe.db.exists("FB Modifier Group", modifier_group):
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


def aggregate_modifier_stats_range(from_date: str, to_date: str) -> int:
    """
    Aggregate modifier stats for a date range.

    Args:
        from_date: Start date
        to_date: End date

    Returns:
        Total records processed
    """
    if not frappe.has_permission("KoPOS Modifier Stats", "write"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)

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
