# pyright: reportMissingImports=false

"""Reading closed shifts and their cash difference.

FB Shift owns expected_cash, counted_cash and cash_variance; the shift close
path writes them. Nothing here recomputes any of that -- a second answer to
"how much cash was missing" is worse than no answer. This module only reads,
groups and words them.

Shared by JiJi Home and the Money page so the two cannot disagree about how
many shifts need a director.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cstr

from kopos_connector.api.cafe_money import company_currency, money_sen, money_text
from kopos_connector.api.director_scope import resolve_director_context


def person_name(staff_id: str) -> str:
    """A name a director can say out loud, never a login.

    User.full_name first, because that is the name the tablet shows its own
    staff when they sign in. Reading Employee first made the same person
    "Aisyah Rahman" on the tablet and something else on a director's screen.
    """
    identifier = cstr(staff_id).strip()
    if not identifier:
        return ""
    full_name = cstr(frappe.db.get_value("User", identifier, "full_name")).strip()
    if full_name:
        return full_name
    employee = frappe.db.get_value("KoPOS Staff Access", {"user": identifier}, "employee")
    if employee:
        name = cstr(frappe.db.get_value("Employee", employee, "employee_name")).strip()
        if name:
            return name
    return "Staff member"


def closed_shifts(company: str, limit: int = 50) -> list[dict[str, Any]]:
    return frappe.get_all(
        "FB Shift",
        filters={"company": company, "status": "Closed"},
        fields=["name", "staff_id", "closed_at", "expected_cash", "counted_cash", "cash_variance"],
        order_by="closed_at desc",
        limit_page_length=limit,
    )


def describe_difference(variance_sen: int, currency: str = "MYR") -> str:
    """The direction in words, because a signed number is not an instruction."""
    if variance_sen == 0:
        return "Matched"
    return f"{money_text(abs(variance_sen), currency)} {'short' if variance_sen < 0 else 'over'}"


def group_shifts(
    rows: list[dict[str, Any]], currency: str = "MYR"
) -> dict[str, list[dict[str, Any]]]:
    """Split shifts into the three answers a director actually wants."""
    matched: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    missing_count: list[dict[str, Any]] = []

    for row in rows:
        expected_sen = money_sen(row.get("expected_cash"), "FB Shift.expected_cash")
        counted_sen = (
            None
            if row.get("counted_cash") is None
            else money_sen(row.get("counted_cash"), "FB Shift.counted_cash")
        )
        variance_sen = (
            None
            if counted_sen is None
            else money_sen(row.get("cash_variance"), "FB Shift.cash_variance")
        )
        entry = {
            "person": person_name(cstr(row.get("staff_id"))),
            "closed_at": cstr(row.get("closed_at")),
            "expected_sen": expected_sen,
            "expected_display": money_text(expected_sen, currency),
            "counted_sen": counted_sen,
            "counted_display": (
                money_text(counted_sen, currency) if counted_sen is not None else "Not counted"
            ),
            "variance_sen": variance_sen,
            "difference": (
                describe_difference(variance_sen, currency)
                if variance_sen is not None
                else "Not compared"
            ),
        }
        if row.get("counted_cash") is None:
            missing_count.append(entry)
        elif variance_sen != 0:
            needs_review.append(entry)
        else:
            matched.append(entry)

    return {"matched": matched, "needs_review": needs_review, "missing_count": missing_count}


@frappe.whitelist(methods=["GET"])
def get_shift_money(company: str | None = None) -> dict[str, Any]:
    """Summary first, then the exceptions, then one shift."""
    context = resolve_director_context(company=cstr(company).strip() or None)
    if not context.company:
        return {
            "status": context.status,
            "companies": list(context.allowed_companies),
        }

    currency = company_currency(context.company)
    grouped = group_shifts(closed_shifts(context.company), currency)
    return {
        "status": "ok",
        "company": context.company,
        # Every other desk page leads with one statement. Money led with three
        # counters, so a café where nothing needs doing opened on a row of
        # zeros and buried the good news underneath them.
        "next_step": _money_next_step(grouped),
        "summary": [
            {"key": "matched", "label": "Matched", "count": len(grouped["matched"])},
            {"key": "needs_review", "label": "Did not match", "count": len(grouped["needs_review"]), "attention": bool(grouped["needs_review"])},
            {"key": "missing_count", "label": "Not counted", "count": len(grouped["missing_count"]), "attention": bool(grouped["missing_count"])},
        ],
        "needs_review": grouped["needs_review"],
        "missing_count": grouped["missing_count"],
    }


def _money_next_step(grouped: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """The one thing to do about the cash, or that there is nothing.

    Worst first: a shift that was counted and did not match is a discrepancy
    to explain; a shift never counted is a gap in the record. Neither is a
    list of three numbers.
    """
    unmatched = grouped["needs_review"]
    never = grouped["missing_count"]
    if unmatched:
        first = unmatched[0]
        return {
            "title": (
                "1 shift counted a different amount of cash"
                if len(unmatched) == 1
                else f"{len(unmatched)} shifts counted a different amount of cash"
            ),
            "detail": f"Start with {cstr(first.get('person'))}, {cstr(first.get('difference'))}.",
        }
    if never:
        return {
            "title": (
                "1 shift ended without counting the cash"
                if len(never) == 1
                else f"{len(never)} shifts ended without counting the cash"
            ),
            "detail": "There is nothing to compare against for these.",
        }
    if not grouped["matched"]:
        # Nothing has closed yet. "Accounted for" would be a positive result
        # drawn from an absence of shifts, which is not the same thing.
        return {
            "title": "No shift has been counted yet",
            "detail": "When a shift closes, JiJi compares the cash to what it expected and says so here.",
        }
    return {
        "title": "The cash is accounted for",
        "detail": "Every shift counted the same money JiJi expected.",
    }
