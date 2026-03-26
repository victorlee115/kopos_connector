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
            "width": 180,
        },
        {
            "label": _("Group"),
            "fieldname": "group_name",
            "fieldtype": "Data",
            "width": 120,
        },
        {
            "label": _("Selections"),
            "fieldname": "total_selections",
            "fieldtype": "Int",
            "width": 100,
        },
        {
            "label": _("Revenue"),
            "fieldname": "total_revenue",
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "label": _("Avg Price"),
            "fieldname": "avg_price",
            "fieldtype": "Currency",
            "width": 100,
        },
    ]


def get_data(filters):
    """Fetch report data using Query Builder."""
    Stats = DocType("KoPOS Modifier Stats")

    total_selections = frappe.qb.functions.Sum(Stats.selection_count).as_(
        "total_selections"
    )
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
            "datasets": [
                {
                    "name": _("Revenue"),
                    "values": [flt(d.total_revenue) for d in top_modifiers],
                }
            ],
        },
        "type": "bar",
        "colors": ["#F59E0B"],
        "fieldtype": "Currency",
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
