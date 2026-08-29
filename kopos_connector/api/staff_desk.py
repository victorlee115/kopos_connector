# pyright: reportMissingImports=false

"""The people who can use a tablet, and how a director gives one a PIN.

This is the screen with a real defect underneath it. ``KoPOS Staff Access``
carries a four-digit PIN at permlevel 1, and the doctype had no permlevel-1
permission row, so no role could set a staff PIN through the form at all --
which is why the field was missing from the screenshot everyone was reviewing.
The permission is fixed; this is what belongs on top of it.

The form showed an email as the page title, an employee code, a revision
counter, a grid with a Warehouse column, five red asterisks, and a Comments
feed on a security record. A director needs to know who can use a tablet, at
what level, and how to give them a PIN.

Two states the old form could not express are named here: "No PIN yet", which
is the whole reason a director opens this page, and "Not working here now",
which is what deactivating someone actually means. A checkbox labelled
``Active`` says neither.

The PIN itself is written by ``KoPOSStaffAccess``: it hashes the digits, bumps
the revision, and invalidates every bound tablet. This module hands the doctype
four digits and saves; it never touches ``pin_hash``, never stores the digits,
and never sends them back.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import add_days, cint, cstr, format_datetime, getdate, nowdate

from kopos_connector.api.director_scope import director_company
from kopos_connector.api.shift_money import person_name

# A tablet that has not spoken to JiJi for this long is not "quiet", it is
# something the director should look at.
SILENT_TABLET_DAYS = 3


def _outlet_names(access: str) -> list[str]:
    """The outlets someone works at, by the name a director uses for them.

    The row carries both an outlet name and the warehouse it maps to. The
    warehouse is the record; the outlet is what the café calls the place.
    """
    names = []
    for row in frappe.get_all(
        "KoPOS Staff Outlet",
        filters={"parent": access, "parenttype": "KoPOS Staff Access"},
        fields=["outlet", "warehouse"],
        order_by="idx asc",
        limit_page_length=0,
    ):
        name = cstr(row.get("outlet")).strip() or cstr(row.get("warehouse")).strip()
        if name:
            names.append(name)
    return names


def _people() -> list[dict[str, Any]]:
    rows = frappe.get_all(
        "KoPOS Staff Access",
        fields=["name", "user", "employee", "active", "access_level", "pin_hash"],
        order_by="active desc, modified desc",
        limit_page_length=0,
    )
    people = []
    for row in rows:
        active = bool(cint(row.get("active")))
        has_pin = bool(cstr(row.get("pin_hash")).strip())
        if not active:
            state, level = "Not working here now", ""
        elif has_pin:
            state, level = "PIN set", "ok"
        else:
            state, level = "No PIN yet", "attention"
        people.append({
            "access": cstr(row["name"]),
            "name": person_name(cstr(row.get("user"))),
            "outlets": " · ".join(_outlet_names(cstr(row["name"]))),
            "role": cstr(row.get("access_level")) or "Staff",
            "active": active,
            "has_pin": has_pin,
            "state": state,
            "state_level": level,
            # A person who has never had a PIN is the one thing this page
            # exists to fix, so it is the only primary action on the row.
            "action": "Change PIN" if has_pin else "Set tablet PIN",
            "route": ["Form", "KoPOS Staff Access", cstr(row["name"])],
        })
    return people


def _tablets(company: str) -> list[dict[str, Any]]:
    """Which tablets are working, and who is on them.

    A tablet is a place, not a person. Who is signed in comes from the open
    FB Shift, which is the record that already knows.
    """
    devices = frappe.get_all(
        "KoPOS Device",
        filters={"enabled": 1},
        fields=["name", "device_id", "device_name", "last_seen_at"],
        order_by="device_name asc",
        limit_page_length=0,
    )
    if not devices:
        return []

    open_shifts: dict[str, dict[str, Any]] = {}
    for shift in frappe.get_all(
        "FB Shift",
        filters={"company": company, "status": "Open"},
        fields=["name", "device_id", "staff_id", "opened_at"],
        limit_page_length=0,
    ):
        open_shifts[cstr(shift.get("device_id"))] = shift

    silent_before = getdate(add_days(nowdate(), -SILENT_TABLET_DAYS))
    tablets = []
    for device in devices:
        shift = open_shifts.get(cstr(device.get("device_id")))
        last_seen = device.get("last_seen_at")
        if shift:
            detail = format_datetime(shift.get("opened_at"), "HH:mm")
            tablets.append({
                "tablet": cstr(device.get("device_name") or device.get("device_id")),
                "detail": f"{person_name(cstr(shift.get('staff_id')))}, signed in since {detail}",
                "state": "Working",
                "state_level": "ok",
                "route": ["Form", "FB Shift", cstr(shift.get("name"))],
            })
        elif not last_seen or getdate(last_seen) < silent_before:
            tablets.append({
                "tablet": cstr(device.get("device_name") or device.get("device_id")),
                "detail": (
                    "Has not reached JiJi yet."
                    if not last_seen
                    else f"Has not reached JiJi since {format_datetime(last_seen, 'd MMM')}."
                ),
                "state": "Needs you",
                "state_level": "attention",
                "route": ["Form", "KoPOS Device", cstr(device["name"])],
            })
        else:
            tablets.append({
                "tablet": cstr(device.get("device_name") or device.get("device_id")),
                "detail": f"Nobody signed in. Last seen {format_datetime(last_seen, 'd MMM HH:mm')}.",
                "state": "Quiet",
                "state_level": "",
                "route": ["Form", "KoPOS Device", cstr(device["name"])],
            })
    return tablets


@frappe.whitelist(methods=["GET"])
def get_staff_desk() -> dict[str, Any]:
    """Who can use a tablet, at what level, and which tablet is working."""
    company = director_company()
    if not company:
        return {"status": "no_company"}
    people = _people()
    return {
        "status": "ok",
        "company": company,
        "people": people,
        "needs_pin": sum(1 for person in people if person["active"] and not person["has_pin"]),
        "tablets": _tablets(company),
        "records_route": ["List", "KoPOS Staff Access"],
    }


@frappe.whitelist(methods=["POST"])
def set_staff_pin(*, access: str, pin: str, confirm: str) -> dict[str, Any]:
    """Give one person a tablet PIN.

    Everything that makes a PIN safe already lives on the doctype: it hashes
    the digits, bumps the revision, and invalidates every bound tablet so a
    stale one cannot keep trusting the old verifier. This hands it four digits
    and saves through the normal permission check -- no ``ignore_permissions``,
    because a director's permlevel-1 write is exactly what is being tested.

    The digits are never stored here, never logged, and never returned.
    """
    name = cstr(access).strip()
    entered = cstr(pin).strip()
    repeated = cstr(confirm).strip()

    if not name:
        frappe.throw(frappe._("Choose who this PIN is for"))
    if not entered.isdigit() or len(entered) != 4:
        frappe.throw(frappe._("A tablet PIN is exactly four digits"))
    if entered != repeated:
        # Said before saving, because the second field exists to catch a typo
        # that would otherwise lock someone out of their own shift.
        frappe.throw(frappe._("The two PINs are different. Type the same four digits twice."))

    document = frappe.get_doc("KoPOS Staff Access", name)
    before = cstr(document.get("pin_hash")).strip()
    document.pin = entered
    document.save()

    # Frappe does not refuse a permlevel-1 write a role cannot make; it drops
    # the value and saves the rest. Without this check a director would be told
    # the PIN was set while the tablet kept asking for the old one. The salt is
    # random, so re-setting the same digits still changes the verifier.
    after = cstr(frappe.db.get_value("KoPOS Staff Access", name, "pin_hash")).strip()
    if not after or after == before:
        frappe.throw(
            frappe._("JiJi could not set this PIN. Your role cannot change a tablet PIN."),
            frappe.PermissionError,
        )

    return {
        "status": "ok",
        "person": person_name(cstr(document.user)),
        "detail": frappe._("Their tablets will ask for the new PIN within a minute."),
    }
