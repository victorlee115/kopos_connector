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
from frappe.utils import cstr, flt


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
    return identifier


def closed_shifts(company: str, limit: int = 50) -> list[dict[str, Any]]:
    return frappe.get_all(
        "FB Shift",
        filters={"company": company, "status": "Closed"},
        fields=["name", "staff_id", "closed_at", "expected_cash", "counted_cash", "cash_variance"],
        order_by="closed_at desc",
        limit_page_length=limit,
    )


def describe_difference(variance: float) -> str:
    """The direction in words, because a signed number is not an instruction."""
    amount = abs(flt(variance))
    if amount == 0:
        return "Matched"
    return f"RM {amount:,.2f} {'short' if flt(variance) < 0 else 'over'}"


def group_shifts(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Split shifts into the three answers a director actually wants."""
    matched: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    missing_count: list[dict[str, Any]] = []

    for row in rows:
        entry = {
            "shift": cstr(row.get("name")),
            "person": person_name(cstr(row.get("staff_id"))),
            "closed_at": cstr(row.get("closed_at")),
            "expected": flt(row.get("expected_cash")),
            "counted": flt(row.get("counted_cash")),
            "variance": flt(row.get("cash_variance")),
            "difference": describe_difference(flt(row.get("cash_variance"))),
        }
        if row.get("counted_cash") is None:
            missing_count.append(entry)
        elif flt(row.get("cash_variance")) != 0:
            needs_review.append(entry)
        else:
            matched.append(entry)

    return {"matched": matched, "needs_review": needs_review, "missing_count": missing_count}


@frappe.whitelist(methods=["GET"])
def get_shift_money(company: str | None = None) -> dict[str, Any]:
    """Summary first, then the exceptions, then one shift."""
    resolved = cstr(company or frappe.defaults.get_user_default("Company") or "")
    if not resolved:
        companies = frappe.get_all("Company", pluck="name", limit_page_length=2)
        resolved = cstr(companies[0]) if len(companies) == 1 else ""
    if not resolved:
        return {"status": "no_company"}

    grouped = group_shifts(closed_shifts(resolved))
    return {
        "status": "ok",
        "company": resolved,
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
        "report_route": ["query-report", "KoPOS Shift Reconciliation"],
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
    return {
        "title": "The cash is accounted for",
        "detail": "Every shift counted the same money JiJi expected.",
    }
