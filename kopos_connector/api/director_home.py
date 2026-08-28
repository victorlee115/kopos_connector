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
from frappe.utils import cint, cstr, flt, nowdate

from kopos_connector.api.shift_money import closed_shifts, group_shifts


def _company() -> str:
    """The company this director is looking at.

    Most JiJi sites hold exactly one company, and a director who never set a
    user default would otherwise meet an empty page. Fall back to the only
    company there is, and ask only when the answer is genuinely ambiguous.
    """
    default = cstr(frappe.defaults.get_user_default("Company") or "")
    if default:
        return default
    companies = frappe.get_all("Company", pluck="name", limit_page_length=2)
    return cstr(companies[0]) if len(companies) == 1 else ""


def _today_takings(company: str) -> dict[str, Any]:
    """Today's completed sales, from the Sales Invoice that already records them."""
    rows = frappe.get_all(
        "Sales Invoice",
        filters={"company": company, "docstatus": 1, "posting_date": nowdate()},
        fields=["name", "grand_total"],
        limit_page_length=0,
    )
    return {
        "orders": len(rows),
        "total": flt(sum(flt(row.get("grand_total")) for row in rows), 2),
    }


def _shifts_needing_review(company: str) -> list[dict[str, Any]]:
    """Closed shifts whose counted cash did not match.

    Delegated to shift_money so Home and the Money page cannot disagree about
    how many shifts need a director. cash_variance is written by the shift
    close path and is never recomputed.
    """
    return group_shifts(closed_shifts(company))["needs_review"]


def _staff_count() -> int:
    return cint(frappe.db.count("KoPOS Staff Access", {"active": 1}))


def _draft_supplier_orders(company: str) -> int:
    return cint(frappe.db.count("Purchase Order", {"company": company, "docstatus": 0}))


@frappe.whitelist(methods=["GET"])
def get_director_home() -> dict[str, Any]:
    """One next step, five tiles, and whatever is queued behind them."""
    company = _company()
    if not company:
        return {"status": "no_company"}

    takings = _today_takings(company)
    unmatched_shifts = _shifts_needing_review(company)
    drafts = _draft_supplier_orders(company)
    staff = _staff_count()

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
            "action": "Review money",
            "route": "/app/query-report/KoPOS Shift Reconciliation",
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
            "action": "Review supplies",
            "route": "/app/purchase-order?docstatus=0",
        }
    elif items_missing_recipe:
        next_step = {
            "area": "menu",
            "title": (
                "1 menu item cannot go on sale yet"
                if items_missing_recipe == 1
                else f"{items_missing_recipe} menu items cannot go on sale yet"
            ),
            "detail": "Each one needs a recipe, or a reason it does not need one.",
            "action": "Open Menu & Recipes",
            "route": "/app/jiji-menu-recipes",
        }
    else:
        next_step = {
            "area": "none",
            "title": "Nothing needs you right now",
            "detail": "Today's sales and stock are behaving.",
            "action": "",
            "route": "",
        }

    waiting = []
    if unmatched_shifts and drafts:
        waiting.append({
            "title": (
                "1 supplier order to review"
                if drafts == 1
                else f"{drafts} supplier orders to review"
            ),
            "route": "/app/purchase-order?docstatus=0",
        })
    if next_step["area"] != "menu" and items_missing_recipe:
        waiting.append({
            "title": (
                "1 menu item has no recipe yet"
                if items_missing_recipe == 1
                else f"{items_missing_recipe} menu items have no recipe yet"
            ),
            "route": "/app/jiji-menu-recipes",
        })

    return {
        "status": "ok",
        "company": company,
        "next_step": next_step,
        "waiting": waiting,
        "tiles": [
            {"key": "today", "label": "Today", "value": f"RM {takings['total']:,.2f}", "detail": f"{takings['orders']} orders", "route": "/app/sales-invoice"},
            {"key": "menu", "label": "Menu", "value": str(items_ready), "detail": "on sale", "route": "/app/jiji-menu-recipes"},
            {"key": "stock", "label": "Stock", "value": str(drafts), "detail": "orders to review", "route": "/app/jiji-stock-autopilot", "attention": drafts > 0},
            {"key": "staff", "label": "Staff", "value": str(staff), "detail": "can use tablets", "route": "/app/kopos-staff-access"},
            {"key": "money", "label": "Money", "value": str(len(unmatched_shifts)), "detail": "shifts to review", "route": "/app/query-report/KoPOS Shift Reconciliation", "attention": bool(unmatched_shifts)},
        ],
    }
