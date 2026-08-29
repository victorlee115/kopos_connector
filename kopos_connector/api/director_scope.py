# pyright: reportMissingImports=false

"""Which company, and which outlet, a director is looking at.

Every director page needs the same two answers before it can show a number,
and each page answering separately is how one page ends up showing Bangsar
while the one beside it shows the whole business.

This module reads defaults and falls back to the only answer there is. It
never asks the director to choose something a single-outlet café has only one
of, and it writes nothing.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr


def director_company() -> str:
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


def director_warehouse(company: str) -> str:
    """The outlet store this director is looking at.

    The stock page currently opens with an empty required filter, so a
    director sees nothing until they name a warehouse they did not choose.
    A café with one outlet store has already answered this question.
    """
    default = cstr(frappe.defaults.get_user_default("Warehouse") or "")
    if default:
        return default
    warehouses = frappe.get_all(
        "Warehouse",
        filters={"company": company, "is_group": 0, "disabled": 0},
        pluck="name",
        order_by="creation asc",
        limit_page_length=2,
    )
    return cstr(warehouses[0]) if len(warehouses) == 1 else ""


@frappe.whitelist(methods=["GET"])
def get_director_outlet() -> dict[str, str]:
    """The company and outlet store to open a page on, before anyone chooses.

    The stock page opens with a required outlet filter and shows nothing until
    it is filled. A café with one outlet store has already answered that
    question, and a director who set a default has answered it too. Returning
    an empty warehouse is a real answer -- it means "genuinely ambiguous, ask".
    """
    company = director_company()
    return {"company": company, "warehouse": director_warehouse(company) if company else ""}
