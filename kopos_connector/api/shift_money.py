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


def closed_shifts(company: str, limit: int = 50, *, category: str = "needs_review", offset: int = 0) -> list[dict[str, Any]]:
    predicates = {
        "needs_review": "counted_cash IS NOT NULL AND (cash_variance IS NULL OR cash_variance != 0)",
        "missing_count": "counted_cash IS NULL",
    }
    if category not in predicates or not 1 <= limit <= 51 or offset < 0:
        raise ValueError("Invalid cash exception page")
    return frappe.db.sql(
        f"""SELECT name, staff_id, closed_at, expected_cash, counted_cash, cash_variance
        FROM `tabFB Shift` WHERE company = %s AND status = 'Closed'
        AND ({predicates[category]})
        ORDER BY closed_at DESC, name DESC LIMIT %s OFFSET %s""",
        (company, limit, offset), as_dict=True,
    )


def shift_money_overview(company: str, *, offset: int = 0) -> dict[str, Any]:
    """Complete counts with bounded exception detail, never a sampled all-clear."""
    totals = frappe.db.sql(
        """SELECT COUNT(*) AS total,
        COALESCE(SUM(CASE WHEN counted_cash IS NULL THEN 1 ELSE 0 END), 0) AS missing_count,
        COALESCE(SUM(CASE WHEN counted_cash IS NOT NULL AND cash_variance = 0 THEN 1 ELSE 0 END), 0) AS matched
        FROM `tabFB Shift` WHERE company = %s AND status = 'Closed'""",
        (company,), as_dict=True,
    )[0]
    counts = {key: int(totals[key]) for key in ("matched", "missing_count")}
    counts["needs_review"] = int(totals["total"]) - counts["matched"] - counts["missing_count"]
    currency = company_currency(company)
    grouped: dict[str, Any] = {"matched": [], "needs_review": [], "missing_count": [], "counts": counts}
    has_more = False
    for category in ("needs_review", "missing_count"):
        rows = closed_shifts(company, 51, category=category, offset=offset)
        has_more = has_more or len(rows) > 50
        grouped[category] = group_shifts(rows[:50], currency)[category]
    grouped["next_offset"] = offset + 50 if has_more else None
    return grouped


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
            if counted_sen is None or row.get("cash_variance") is None
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
def get_shift_money(company: str | None = None, offset: int = 0) -> dict[str, Any]:
    """Summary first, then the exceptions, then one shift."""
    context = resolve_director_context(company=cstr(company).strip() or None)
    if not context.company:
        return {
            "status": context.status,
            "companies": list(context.allowed_companies),
        }

    raw_offset = str(offset).strip()
    if not raw_offset.isascii() or not raw_offset.isdigit() or len(raw_offset) > 9:
        frappe.throw("Cash exception page must be a non-negative integer", frappe.ValidationError)
    grouped = shift_money_overview(context.company, offset=int(raw_offset))
    counts = grouped["counts"]
    return {
        "status": "ok",
        "company": context.company,
        "summary_scope": "all_closed_shifts",
        "offset": int(raw_offset),
        "next_offset": grouped["next_offset"],
        # Every other desk page leads with one statement. Money led with three
        # counters, so a café where nothing needs doing opened on a row of
        # zeros and buried the good news underneath them.
        "next_step": _money_next_step(grouped),
        "summary": [
            {"key": "matched", "label": "Matched", "count": counts["matched"]},
            {"key": "needs_review", "label": "Did not match", "count": counts["needs_review"], "attention": bool(counts["needs_review"])},
            {"key": "missing_count", "label": "Not counted", "count": counts["missing_count"], "attention": bool(counts["missing_count"])},
        ],
        "needs_review": grouped["needs_review"],
        "missing_count": grouped["missing_count"],
    }


def _money_next_step(grouped: dict[str, Any]) -> dict[str, Any]:
    """The one thing to do about the cash, or that there is nothing.

    Worst first: a shift that was counted and did not match is a discrepancy
    to explain; a shift never counted is a gap in the record. Neither is a
    list of three numbers.
    """
    unmatched = grouped["needs_review"]
    counts = grouped.get("counts") or {key: len(grouped[key]) for key in ("matched", "needs_review", "missing_count")}
    if counts["needs_review"]:
        first = unmatched[0] if unmatched else None
        return {
            "title": (
                "1 shift counted a different amount of cash"
                if counts["needs_review"] == 1
                else f"{counts['needs_review']} shifts counted a different amount of cash"
            ),
            "detail": f"Start with {cstr(first.get('person'))}, {cstr(first.get('difference'))}." if first else "Refresh to see the latest cash differences.",
        }
    if counts["missing_count"]:
        return {
            "title": (
                "1 shift ended without counting the cash"
                if counts["missing_count"] == 1
                else f"{counts['missing_count']} shifts ended without counting the cash"
            ),
            "detail": "There is nothing to compare against for these.",
        }
    if not counts["matched"]:
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
