# pyright: reportMissingImports=false

"""Read-only composition for the director home page.

The director's first question is "is anything wrong, and what do I do about
it". Answering it previously meant reading a stock console, a query report and
two ERP forms, each of which reports its own area with equal weight, so three
areas could all claim to be the most urgent thing at once.

This module owns no business rule. It counts records and delegates every
calculation to the service that already owns it -- cash variance stays on
FB Shift, menu readiness stays in the menu authoring summary. It writes
nothing, and it returns one next step rather than a list of equals.
"""

from __future__ import annotations

from typing import Any, Callable

import frappe
from frappe import _
from frappe.utils import add_days, cint, cstr, getdate, nowdate

from kopos_connector.api.cafe_money import company_currency, money_sen, money_text
from kopos_connector.api.director_scope import DirectorContext, resolve_director_context
from kopos_connector.api.shift_money import closed_shifts, group_shifts

# Long enough to see a shape, short enough to still be this week.
TAKINGS_DAYS = 7
STAFF_SUMMARY_LIMIT = 500


def _recent_takings(company: str) -> dict[str, Any]:
    """Today, and the six days behind it, from the Sales Invoices that record them.

    Database aggregates keep the read proportional to days, outlets and hours,
    rather than to the number of sales made during a busy week.
    """
    today = getdate(nowdate())
    first = add_days(today, -(TAKINGS_DAYS - 1))
    currency = company_currency(company)
    daily_rows = frappe.db.sql(
        """
        SELECT invoice.posting_date, COUNT(*) AS order_count,
               COALESCE(SUM(invoice.grand_total), 0) AS total,
               COALESCE(SUM(CASE
                   WHEN fb_order.completion_kind = 'complimentary'
                   THEN fb_order.complimentary_value ELSE 0 END), 0) AS complimentary_value,
               SUM(CASE WHEN fb_order.completion_kind = 'complimentary' THEN 1 ELSE 0 END) AS complimentary_count
        FROM `tabSales Invoice` invoice
        LEFT JOIN `tabFB Order` fb_order
               ON fb_order.name = invoice.custom_fb_order
              AND fb_order.company = invoice.company
        WHERE invoice.company = %s AND invoice.docstatus = 1 AND invoice.posting_date >= %s
        GROUP BY invoice.posting_date
        ORDER BY invoice.posting_date ASC
        """,
        (company, first),
        as_dict=True,
    )

    days = [add_days(first, offset) for offset in range(TAKINGS_DAYS)]
    totals = {cstr(day): 0 for day in days}
    orders = {cstr(day): 0 for day in days}
    complimentary_values = {cstr(day): 0 for day in days}
    complimentary_orders = {cstr(day): 0 for day in days}
    for row in daily_rows:
        key = cstr(getdate(row.get("posting_date")))
        if key in totals:
            totals[key] = money_sen(row.get("total") or 0, "Sales Invoice daily total")
            orders[key] = cint(row.get("order_count"))
            complimentary_values[key] = money_sen(
                row.get("complimentary_value") or 0,
                "FB Order daily complimentary value",
            )
            complimentary_orders[key] = cint(row.get("complimentary_count"))

    # A café with one counter does not need its own total repeated back to it.
    # Two do, because "we took RM 1,240" hides one of them having a quiet day.
    outlet_rows = frappe.db.sql(
        """
        SELECT invoice.pos_profile, COUNT(*) AS order_count,
               COALESCE(SUM(invoice.grand_total), 0) AS total,
               COALESCE(SUM(CASE
                   WHEN fb_order.completion_kind = 'complimentary'
                   THEN fb_order.complimentary_value ELSE 0 END), 0) AS complimentary_value,
               SUM(CASE WHEN fb_order.completion_kind = 'complimentary' THEN 1 ELSE 0 END) AS complimentary_count
        FROM `tabSales Invoice` invoice
        LEFT JOIN `tabFB Order` fb_order
               ON fb_order.name = invoice.custom_fb_order
              AND fb_order.company = invoice.company
        WHERE invoice.company = %s AND invoice.docstatus = 1 AND invoice.posting_date = %s
        GROUP BY invoice.pos_profile
        ORDER BY total DESC
        """,
        (company, today),
        as_dict=True,
    )
    outlets = [
        {
            "outlet": cstr(row.get("pos_profile")).strip(),
            "total_sen": money_sen(row.get("total") or 0, "Sales Invoice outlet total"),
            "orders_count": cint(row.get("order_count")),
            "complimentary_value_sen": money_sen(
                row.get("complimentary_value") or 0,
                "FB Order outlet complimentary value",
            ),
            "complimentary_orders_count": cint(row.get("complimentary_count")),
        }
        for row in outlet_rows
        if cstr(row.get("pos_profile")).strip()
    ]
    profile_names = [row["outlet"] for row in outlets]
    profile_rows = (
        frappe.get_all(
            "POS Profile",
            filters={"name": ["in", profile_names], "company": company},
            fields=["name", "warehouse"],
            limit_page_length=len(profile_names),
        )
        if profile_names
        else []
    )
    profile_warehouses = {
        cstr(row.get("name")).strip(): cstr(row.get("warehouse")).strip()
        for row in profile_rows
    }
    warehouse_names = sorted(
        {name for name in profile_warehouses.values() if name}
    )
    warehouse_rows = (
        frappe.get_all(
            "Warehouse",
            filters={"name": ["in", warehouse_names]},
            fields=["name", "warehouse_name"],
            limit_page_length=len(warehouse_names),
        )
        if warehouse_names
        else []
    )
    outlet_labels = {
        profile: next(
            (
                cstr(row.get("warehouse_name")).strip()
                for row in warehouse_rows
                if cstr(row.get("name")).strip() == warehouse
            ),
            "",
        )
        or _("Outlet")
        for profile, warehouse in profile_warehouses.items()
    }

    # When the café is busiest, from the same invoices. A week's total says
    # how much; the hour says when to put someone on.
    hour_rows = frappe.db.sql(
        """
        SELECT HOUR(posting_time) AS sale_hour,
               COUNT(*) AS order_count,
               COALESCE(SUM(grand_total), 0) AS total
        FROM `tabSales Invoice`
        WHERE company = %s AND docstatus = 1 AND posting_date >= %s
              AND posting_time IS NOT NULL
        GROUP BY HOUR(posting_time)
        ORDER BY order_count DESC, total DESC
        LIMIT 1
        """,
        (company, first),
        as_dict=True,
    )
    busiest = cint(hour_rows[0].get("sale_hour")) if hour_rows else None
    today_total_sen = totals[cstr(today)]
    week_total_sen = sum(totals.values())
    today_complimentary_value_sen = complimentary_values[cstr(today)]
    week_complimentary_value_sen = sum(complimentary_values.values())
    today_complimentary_orders = complimentary_orders[cstr(today)]
    week_complimentary_orders = sum(complimentary_orders.values())

    return {
        "busiest_hour": (f"{busiest:02d}:00\u2013{(busiest + 1) % 24:02d}:00" if busiest is not None else ""),
        "orders": orders[cstr(today)],
        "paid_orders": orders[cstr(today)] - today_complimentary_orders,
        "complimentary_orders": today_complimentary_orders,
        "complimentary_value_sen": today_complimentary_value_sen,
        "complimentary_value": money_text(
            today_complimentary_value_sen,
            currency,
        ),
        "total_sen": today_total_sen,
        "total": money_text(today_total_sen, currency),
        "week_total_sen": week_total_sen,
        "week_total": money_text(week_total_sen, currency),
        "week_complimentary_orders": week_complimentary_orders,
        "week_complimentary_value_sen": week_complimentary_value_sen,
        "week_complimentary_value": money_text(
            week_complimentary_value_sen,
            currency,
        ),
        "outlets": [
            {
                **row,
                "total": money_text(row["total_sen"], currency),
                "orders": f"{row['orders_count']} order" + ("" if row["orders_count"] == 1 else "s"),
                "complimentary_value": money_text(
                    row["complimentary_value_sen"],
                    currency,
                ),
                "outlet": outlet_labels.get(row["outlet"], _("Outlet")),
            }
            for row in outlets
        ]
        if len(outlets) > 1
        else [],
        "days": [
            {
                "day": "Today" if day == today else getdate(day).strftime("%a"),
                "total_sen": totals[cstr(day)],
                "total": money_text(totals[cstr(day)], currency),
                "complimentary_value_sen": complimentary_values[cstr(day)],
                "complimentary_orders": complimentary_orders[cstr(day)],
                "today": day == today,
            }
            for day in days
        ],
    }


def _shift_money(company: str) -> dict[str, list[dict[str, Any]]]:
    """Closed shifts, split the way the Money page splits them.

    Delegated to shift_money so Home and the Money page cannot disagree about
    how many shifts need a director. cash_variance is written by the shift
    close path and is never recomputed.
    """
    return group_shifts(closed_shifts(company), company_currency(company))


def _staff_access(context: DirectorContext) -> dict[str, int]:
    """Who can use a tablet, and who is still waiting for a PIN.

    Someone active without a verifier cannot sign in, which is the one thing
    this area ever needs a director for.
    """
    if not context.allowed_companies:
        return {"people": 0, "needs_pin": 0}
    placeholders = ", ".join(["%s"] * len(context.allowed_companies))
    rows = frappe.db.sql(
        f"""
        SELECT staff.name, staff.pin_hash
        FROM `tabKoPOS Staff Access` staff
        WHERE staff.active = 1
          AND EXISTS (
              SELECT 1 FROM `tabKoPOS Staff Outlet` selected_outlet
              WHERE selected_outlet.parent = staff.name
                AND selected_outlet.parenttype = 'KoPOS Staff Access'
                AND selected_outlet.company = %s
          )
          AND NOT EXISTS (
              SELECT 1 FROM `tabKoPOS Staff Outlet` outside_outlet
              WHERE outside_outlet.parent = staff.name
                AND outside_outlet.parenttype = 'KoPOS Staff Access'
                AND outside_outlet.company NOT IN ({placeholders})
          )
        ORDER BY staff.modified DESC
        LIMIT %s
        """,
        (context.company, *context.allowed_companies, STAFF_SUMMARY_LIMIT + 1),
        as_dict=True,
    )
    if len(rows) > STAFF_SUMMARY_LIMIT:
        raise RuntimeError("Staff summary is larger than its safe page")
    return {
        "people": len(rows),
        "needs_pin": sum(1 for row in rows if not cstr(row.get("pin_hash")).strip()),
    }


def _draft_supplier_orders(company: str) -> int:
    return cint(frappe.db.count("Purchase Order", {"company": company, "docstatus": 0}))


def _read_area(name: str, reader: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"status": "ready", "value": reader()}
    except Exception:
        frappe.log_error(title=f"JiJi Home could not check {name}")
        return {"status": "unavailable", "value": None}


@frappe.whitelist(methods=["GET"])
def get_director_home(*, company: str | None = None) -> dict[str, Any]:
    """One next step, five tiles, and whatever is queued behind them."""
    context = resolve_director_context(company=company)
    if not context.company:
        return {
            "status": context.status,
            "companies": list(context.allowed_companies),
        }

    areas = {
        "sales": _read_area("sales", lambda: _recent_takings(context.company)),
        "money": _read_area("cash", lambda: _shift_money(context.company)),
        "supplies": _read_area("supplier orders", lambda: _draft_supplier_orders(context.company)),
        "staff": _read_area("staff access", lambda: _staff_access(context)),
        "menu": _read_area("menu readiness", lambda: _menu_summary(context.company)),
    }
    takings = areas["sales"]["value"] or {}
    shift_money = areas["money"]["value"] or {
        "matched": [], "needs_review": [], "missing_count": []
    }
    unmatched_shifts = shift_money["needs_review"]
    uncounted_shifts = shift_money["missing_count"]
    money_issues = [*uncounted_shifts, *unmatched_shifts]
    # A café that has never closed a shift has nothing to check, which is
    # not the same as having checked everything. The Money page says so;
    # the tile beside it must not say the opposite.
    any_shift = bool(shift_money["matched"] or unmatched_shifts or shift_money["missing_count"])
    drafts = cint(areas["supplies"]["value"] or 0)
    staff = areas["staff"]["value"] or {"people": 0, "needs_pin": 0}
    menu = areas["menu"]["value"] or {}

    items_ready = cint(menu.get("items_ready"))
    items_missing_recipe = cint(menu.get("items_missing_recipe"))

    # Worst first, and only one of them is the next step.
    if money_issues:
        first = money_issues[0]
        missing_count = len(uncounted_shifts)
        different_count = len(unmatched_shifts)
        if missing_count and different_count:
            missing_verb = "needs" if missing_count == 1 else "need"
            money_title = (
                f"{missing_count} shift{'s' if missing_count != 1 else ''} still {missing_verb} a cash count · "
                f"{different_count} counted a different amount"
            )
        elif missing_count:
            money_title = (
                "1 shift still needs a cash count"
                if missing_count == 1
                else f"{missing_count} shifts still need a cash count"
            )
        else:
            money_title = (
                "1 shift counted a different amount of cash"
                if different_count == 1
                else f"{different_count} shifts counted a different amount of cash"
            )
        next_step = {
            "area": "money",
            "title": money_title,
            "detail": f"Start with {cstr(first.get('person'))}.",
            "action": "See the shifts",
            "route": ["jiji_money"],
        }
    elif drafts:
        next_step = {
            "area": "buying",
            "title": (
                "1 supplier order is waiting for you"
                if drafts == 1
                else f"{drafts} supplier orders are waiting for you"
            ),
            "detail": "Nothing is sent until you say so.",
            "action": "See the order",
            "route": ["jiji_supplies"],
        }
    elif items_missing_recipe:
        next_step = {
            "area": "menu",
            "title": (
                "1 menu item cannot go on sale yet"
                if items_missing_recipe == 1
                else f"{items_missing_recipe} menu items cannot go on sale yet"
            ),
            "detail": "JiJi needs to know what goes into it before it can go on sale.",
            "action": "See the drink",
            "route": ["jiji_menu_recipes"],
        }
    elif staff["needs_pin"]:
        next_step = {
            "area": "staff",
            "title": (
                "1 person needs a PIN before they can sign in"
                if staff["needs_pin"] == 1
                else f"{staff['needs_pin']} people need a PIN before they can sign in"
            ),
            "detail": "Set a private four-digit PIN for each person.",
            "action": "See the people",
            "route": ["jiji_staff"],
        }
    elif any(area["status"] != "ready" for area in areas.values()):
        next_step = {
            "area": "unavailable",
            "title": "JiJi could not check everything just now",
            "detail": "The areas shown with a dash have not been confirmed. Try Refresh.",
            "action": "",
            "route": [],
        }
    else:
        next_step = {
            "area": "none",
            "title": "Nothing needs you right now",
            "detail": "There is no money, menu, supplies, or staff action waiting on this page.",
            "action": "",
            "route": [],
        }

    waiting = []
    if money_issues and drafts:
        waiting.append({
            "title": (
                "1 supplier order waiting for you"
                if drafts == 1
                else f"{drafts} supplier orders waiting for you"
            ),
            "route": ["jiji_supplies"],
        })
    if next_step["area"] != "staff" and staff["needs_pin"]:
        waiting.append({
            "title": (
                "1 person cannot sign in until you set a PIN"
                if staff["needs_pin"] == 1
                else f"{staff['needs_pin']} people cannot sign in until you set a PIN"
            ),
            "route": ["jiji_staff"],
        })
    if next_step["area"] != "menu" and items_missing_recipe:
        waiting.append({
            "title": (
                "1 menu item has no recipe yet"
                if items_missing_recipe == 1
                else f"{items_missing_recipe} menu items have no recipe yet"
            ),
            "route": ["jiji_menu_recipes"],
        })

    paid_orders = cint(takings.get("paid_orders"))
    complimentary_orders = cint(takings.get("complimentary_orders"))
    today_order_parts = []
    if paid_orders:
        today_order_parts.append(
            f"{paid_orders} paid order" + ("" if paid_orders == 1 else "s")
        )
    if complimentary_orders:
        today_order_parts.append(
            f"{complimentary_orders} given free"
        )
    today_order_detail = " · ".join(today_order_parts) or "No orders yet"

    return {
        "status": "ok",
        "company": context.company,
        "unavailable_areas": [key for key, area in areas.items() if area["status"] != "ready"],
        "next_step": next_step,
        "waiting": waiting,
        "week": {
            "total_sen": takings.get("week_total_sen"),
            "total": takings.get("week_total", ""),
            "days": takings.get("days", []),
            "busiest": takings.get("busiest_hour", ""),
            "complimentary_orders": takings.get("week_complimentary_orders"),
            "complimentary_value_sen": takings.get(
                "week_complimentary_value_sen"
            ),
            "complimentary_value": takings.get(
                "week_complimentary_value", ""
            ),
        },
        "outlets": takings.get("outlets", []),
        "tiles": [
            {"key": "today", "label": "Money in today", "value": takings.get("total", "—"), "value_sen": takings.get("total_sen"), "detail": today_order_detail if areas["sales"]["status"] == "ready" else "Could not check", "route": []},
            {
                "key": "menu",
                "label": "Menu",
                "value": str(items_ready) if areas["menu"]["status"] == "ready" else "—",
                "detail": (
                    ("thing on sale" if items_ready == 1 else "things on sale")
                    if areas["menu"]["status"] == "ready"
                    else "Could not check"
                ),
                "route": ["jiji_menu_recipes"],
            },
            {"key": "supplies", "label": "Supplies", "value": str(drafts) if areas["supplies"]["status"] == "ready" else "—", "detail": "orders waiting for you" if areas["supplies"]["status"] == "ready" else "Could not check", "route": ["jiji_supplies"], "attention": areas["supplies"]["status"] == "ready" and drafts > 0},
            {
                "key": "staff",
                "label": "Staff",
                "value": str(staff["people"]) if areas["staff"]["status"] == "ready" else "—",
                "detail": (
                    "Could not check"
                    if areas["staff"]["status"] != "ready"
                    else "can use tablets"
                    if not staff["needs_pin"]
                    else f"{staff['needs_pin']} waiting for a PIN"
                ),
                "route": ["jiji_staff"],
            },
            {
                "key": "money",
                "label": "Cash checks",
                "value": str(len(money_issues)) if any_shift else "\u2014",
                "detail": "Could not check" if areas["money"]["status"] != "ready" else ("to check" if money_issues else "all counted and matched" if any_shift else "no shift counted yet"),
                "route": ["jiji_money"],
                "attention": bool(money_issues),
            },
        ],
    }


def _menu_summary(company: str) -> dict[str, Any]:
    from kopos_connector.api.inventory import get_menu_authoring_summary

    return get_menu_authoring_summary(company) or {}
