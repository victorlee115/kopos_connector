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


def _user_default(key: str) -> str:
    """One director's default for a key, or "" when there is no single answer.

    ``frappe.defaults.get_user_default`` is the obvious reader and the wrong
    one here. For a key that also scopes User Permissions -- Company and
    Warehouse both do -- it only returns a stored string when that string
    arrived as a single-item list, and otherwise looks for a lower-cased key
    that is usually absent. Reading it returned an empty warehouse on a site
    whose Administrator had one set, which is how this was found.

    The merged defaults hold the value directly. A list means the director is
    permitted several, which is a genuinely ambiguous answer, not a default.
    """
    value = frappe.defaults.get_defaults().get(key)
    if isinstance(value, (list, tuple)):
        return cstr(value[0]).strip() if len(value) == 1 else ""
    return cstr(value or "").strip()


def director_company() -> str:
    """The company this director is looking at.

    Most JiJi sites hold exactly one company, and a director who never set a
    user default would otherwise meet an empty page. Fall back to the only
    company there is, and ask only when the answer is genuinely ambiguous.
    """
    default = _user_default("Company")
    if default:
        return default
    companies = frappe.get_all("Company", pluck="name", limit_page_length=2)
    return cstr(companies[0]) if len(companies) == 1 else ""


def director_warehouse(company: str) -> str:
    """The outlet store this director is looking at.

    The stock page opens with an empty required filter, so a director sees
    nothing until they name a warehouse they did not choose.

    The answer already exists: a POS Profile names the warehouse its tablets
    sell from, which is the outlet store the café means by "stock". Reading it
    here means the desk and the tablet cannot disagree about which stock the
    numbers describe.

    Counting warehouses would not work -- ERPNext creates Stores, Work In
    Progress, Finished Goods and Goods In Transit for every company, so a
    single-outlet café already has four and never looks like one.

    An empty answer is a real answer: it means two outlets sell from different
    stores, and choosing one for the director would show them the wrong café
    without saying so.
    """
    default = _user_default("Warehouse")
    if default:
        return default
    warehouses = {
        cstr(name).strip()
        for name in frappe.get_all(
            "POS Profile",
            filters={"company": company, "disabled": 0},
            pluck="warehouse",
            limit_page_length=0,
        )
        if cstr(name).strip()
    }
    return warehouses.pop() if len(warehouses) == 1 else ""


def director_price_list(company: str) -> str:
    """The price list this café actually sells at.

    A recipe's cost only answers a director's question next to the price the
    drink sells for, and a POS Profile already names where that price comes
    from. Reading it means the desk quotes the price the tablet charges.

    A café builds its menu before it sets up a counter, though, so a company
    with no POS Profile yet falls back to the site's default selling price
    list. Two profiles on two price lists stays a real ambiguity rather than a
    coin toss between them.
    """
    price_lists = {
        cstr(name).strip()
        for name in frappe.get_all(
            "POS Profile",
            filters={"company": company, "disabled": 0},
            pluck="selling_price_list",
            limit_page_length=0,
        )
        if cstr(name).strip()
    }
    if len(price_lists) == 1:
        return price_lists.pop()
    if price_lists:
        return ""
    return cstr(frappe.db.get_single_value("Selling Settings", "selling_price_list") or "").strip()


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
