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

from typing import Any

import frappe
from frappe.utils import add_days, cint, cstr, flt, getdate, nowdate

from kopos_connector.api.director_scope import director_company
from kopos_connector.api.shift_money import closed_shifts, group_shifts

# Long enough to see a shape, short enough to still be this week.
TAKINGS_DAYS = 7


# Routes are returned as Frappe route segments, not URLs. The desk is served
# at /app on some deployments and /desk on others, and its router intercepts
# clicks rather than following an href, so a hard-coded prefix either 404s or
# does nothing when clicked. frappe.set_route(...segments) is deployment
# agnostic; the page builds an href from the same segments for middle-click.
def _recent_takings(company: str) -> dict[str, Any]:
    """Today, and the six days behind it, from the Sales Invoices that record them.

    One query answers both. A director asking "did we make money today" almost
    always means "compared with what", and a single number cannot say.
    """
    today = getdate(nowdate())
    first = add_days(today, -(TAKINGS_DAYS - 1))
    rows = frappe.get_all(
        "Sales Invoice",
        filters={"company": company, "docstatus": 1, "posting_date": [">=", first]},
        fields=["posting_date", "posting_time", "grand_total", "pos_profile"],
        limit_page_length=0,
    )

    days = [add_days(first, offset) for offset in range(TAKINGS_DAYS)]
    totals = {cstr(day): 0.0 for day in days}
    orders = {cstr(day): 0 for day in days}
    for row in rows:
        key = cstr(getdate(row.get("posting_date")))
        if key in totals:
            totals[key] += flt(row.get("grand_total"))
            orders[key] += 1

    # A café with one counter does not need its own total repeated back to it.
    # Two do, because "we took RM 1,240" hides one of them having a quiet day.
    outlets: dict[str, dict[str, Any]] = {}
    for row in rows:
        if cstr(getdate(row.get("posting_date"))) != cstr(today):
            continue
        outlet = cstr(row.get("pos_profile")).strip()
        if not outlet:
            continue
        entry = outlets.setdefault(outlet, {"total": 0.0, "orders": 0})
        entry["total"] += flt(row.get("grand_total"))
        entry["orders"] += 1

    # When the café is busiest, from the same invoices. A week's total says
    # how much; the hour says when to put someone on.
    by_hour: dict[int, float] = {}
    for row in rows:
        stamp = cstr(row.get("posting_time"))
        if len(stamp) < 2 or not stamp[:2].isdigit():
            continue
        hour = int(stamp[:2])
        by_hour[hour] = by_hour.get(hour, 0.0) + flt(row.get("grand_total"))
    busiest = max(by_hour, key=lambda hour: by_hour[hour]) if by_hour else None

    return {
        "busiest_hour": (f"{busiest:02d}:00\u2013{(busiest + 1) % 24:02d}:00" if busiest is not None else ""),
        "orders": orders[cstr(today)],
        "total": flt(totals[cstr(today)], 2),
        "week_total": flt(sum(totals.values()), 2),
        "outlets": [
            {
                "outlet": outlet,
                "total": f"RM {flt(entry['total'], 2):,.2f}",
                "orders": f"{entry['orders']} order" + ("" if entry["orders"] == 1 else "s"),
                "route": ["List", "Sales Invoice", {"pos_profile": outlet}],
            }
            for outlet, entry in sorted(outlets.items(), key=lambda pair: -pair[1]["total"])
        ]
        if len(outlets) > 1
        else [],
        "days": [
            {
                "day": "Today" if day == today else getdate(day).strftime("%a"),
                "total": flt(totals[cstr(day)], 2),
                "today": day == today,
            }
            for day in days
        ],
    }


def _shifts_needing_review(company: str) -> list[dict[str, Any]]:
    """Closed shifts whose counted cash did not match.

    Delegated to shift_money so Home and the Money page cannot disagree about
    how many shifts need a director. cash_variance is written by the shift
    close path and is never recomputed.
    """
    return group_shifts(closed_shifts(company))["needs_review"]


def _staff_access() -> dict[str, int]:
    """Who can use a tablet, and who is still waiting for a PIN.

    Someone active without a verifier cannot sign in, which is the one thing
    this area ever needs a director for.
    """
    rows = frappe.get_all(
        "KoPOS Staff Access",
        filters={"active": 1},
        fields=["pin_hash"],
        limit_page_length=0,
    )
    return {
        "people": len(rows),
        "needs_pin": sum(1 for row in rows if not cstr(row.get("pin_hash")).strip()),
    }


def _draft_supplier_orders(company: str) -> int:
    return cint(frappe.db.count("Purchase Order", {"company": company, "docstatus": 0}))


@frappe.whitelist(methods=["GET"])
def get_director_home() -> dict[str, Any]:
    """One next step, five tiles, and whatever is queued behind them."""
    company = director_company()
    if not company:
        return {"status": "no_company"}

    takings = _recent_takings(company)
    unmatched_shifts = _shifts_needing_review(company)
    drafts = _draft_supplier_orders(company)
    staff = _staff_access()

    try:
        from kopos_connector.api.inventory import get_menu_authoring_summary

        menu = get_menu_authoring_summary(company) or {}
    except Exception:
        # The home page must still render when one area cannot report.
        menu = {}

    items_ready = cint(menu.get("items_ready"))
    items_missing_recipe = cint(menu.get("items_missing_recipe"))

    # Worst first, and only one of them is the next step.
    if unmatched_shifts:
        first = unmatched_shifts[0]
        next_step = {
            "area": "money",
            "title": (
                "1 shift counted a different amount of cash"
                if len(unmatched_shifts) == 1
                else f"{len(unmatched_shifts)} shifts counted a different amount of cash"
            ),
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
    else:
        next_step = {
            "area": "none",
            "title": "Nothing needs you right now",
            "detail": "Your sales and your stock both look normal.",
            "action": "",
            "route": [],
        }

    waiting = []
    if unmatched_shifts and drafts:
        waiting.append({
            "title": (
                "1 supplier order waiting for you"
                if drafts == 1
                else f"{drafts} supplier orders waiting for you"
            ),
            "route": ["jiji_supplies"],
        })
    if staff["needs_pin"]:
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

    return {
        "status": "ok",
        "company": company,
        "next_step": next_step,
        "waiting": waiting,
        "week": {
            "total": f"RM {takings['week_total']:,.2f}",
            "days": takings["days"],
            "busiest": takings["busiest_hour"],
        },
        "outlets": takings["outlets"],
        "tiles": [
            {"key": "today", "label": "Today", "value": f"RM {takings['total']:,.2f}", "detail": f"{takings['orders']} orders", "route": ["List", "Sales Invoice"]},
            {"key": "menu", "label": "Menu", "value": str(items_ready), "detail": "things on sale", "route": ["jiji_menu_recipes"]},
            {"key": "supplies", "label": "Supplies", "value": str(drafts), "detail": "orders waiting for you", "route": ["jiji_supplies"], "attention": drafts > 0},
            {
                "key": "staff",
                "label": "Staff",
                "value": str(staff["people"]),
                "detail": (
                    "can use tablets"
                    if not staff["needs_pin"]
                    else f"{staff['needs_pin']} waiting for a PIN"
                ),
                "route": ["jiji_staff"],
            },
            {"key": "money", "label": "Money", "value": str(len(unmatched_shifts)), "detail": "to check", "route": ["jiji_money"], "attention": bool(unmatched_shifts)},
        ],
    }
