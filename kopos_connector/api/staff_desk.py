# pyright: reportMissingImports=false

"""The people who can use a tablet, and how a director gives one a PIN.

Business users never open the security record underneath this page. The API
checks café scope, then lets that record hash the four-digit PIN and invalidate
bound tablets without returning the digits.

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
from frappe import _
from frappe.utils import add_days, cint, cstr, format_datetime, getdate, nowdate

from kopos_connector.api.director_scope import (
    DirectorContext,
    can_manage_staff_access,
    director_pos_profiles,
    resolve_director_context,
)
from kopos_connector.api.shift_money import person_name

# A tablet that has not spoken to JiJi for this long is not "quiet", it is
# something the director should look at.
SILENT_TABLET_DAYS = 3
PEOPLE_PAGE_SIZE = 200
TABLET_PAGE_SIZE = 100


def _people(context: DirectorContext) -> tuple[list[dict[str, Any]], bool]:
    """People fully inside this director's scope, with this café's outlets."""
    assignments = frappe.get_all(
        "KoPOS Staff Outlet",
        filters={
            "parenttype": "KoPOS Staff Access",
            "company": context.company,
        },
        fields=["parent", "outlet", "warehouse", "idx"],
        order_by="parent asc, idx asc",
        limit_page_length=(PEOPLE_PAGE_SIZE + 1) * 10,
    )
    candidate_names = list(
        dict.fromkeys(
            cstr(row.get("parent")).strip()
            for row in assignments
            if cstr(row.get("parent")).strip()
        )
    )
    if not candidate_names:
        return [], False
    rows = frappe.get_all(
        "KoPOS Staff Access",
        filters={"name": ["in", candidate_names[: PEOPLE_PAGE_SIZE + 1]]},
        fields=["name", "user", "employee", "active", "access_level", "pin_hash"],
        order_by="active desc, modified desc",
        limit_page_length=PEOPLE_PAGE_SIZE + 1,
    )
    has_more = len(candidate_names) > PEOPLE_PAGE_SIZE or len(rows) > PEOPLE_PAGE_SIZE
    rows = rows[:PEOPLE_PAGE_SIZE]
    access_names = [cstr(row.get("name")).strip() for row in rows]
    outlet_names: dict[str, list[str]] = {name: [] for name in access_names}
    for assignment in assignments:
        name = cstr(assignment.get("outlet")).strip() or cstr(
            assignment.get("warehouse")
        ).strip()
        parent = cstr(assignment.get("parent")).strip()
        if parent in outlet_names and name:
            outlet_names[parent].append(name)
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
            "outlets": " · ".join(outlet_names.get(cstr(row["name"]), ())),
            "role": cstr(row.get("access_level")) or "Staff",
            "active": active,
            "has_pin": has_pin,
            "state": state,
            "state_level": level,
            # A person who has never had a PIN is the one thing this page
            # exists to fix, so it is the only primary action on the row.
            "action": "Change PIN" if has_pin else "Set tablet PIN",
        })
    return people, has_more


def _tablets(context: DirectorContext) -> tuple[list[dict[str, Any]], bool]:
    """Which tablets are working, and who is on them.

    A tablet is a place, not a person. Who is signed in comes from the open
    FB Shift, which is the record that already knows.
    """
    profiles = director_pos_profiles(context)
    if not profiles:
        return [], False
    devices = frappe.get_all(
        "KoPOS Device",
        filters={"enabled": 1, "pos_profile": ["in", list(profiles)]},
        fields=["name", "device_id", "device_name", "last_seen_at"],
        order_by="device_name asc",
        limit_page_length=TABLET_PAGE_SIZE + 1,
    )
    has_more = len(devices) > TABLET_PAGE_SIZE
    devices = devices[:TABLET_PAGE_SIZE]
    if not devices:
        return [], has_more

    open_shifts: dict[str, dict[str, Any]] = {}
    for shift in frappe.get_all(
        "FB Shift",
        filters={
            "company": context.company,
            "status": "Open",
            "device_id": ["in", [cstr(device.get("device_id")) for device in devices]],
        },
        fields=["name", "device_id", "staff_id", "opened_at"],
        limit_page_length=TABLET_PAGE_SIZE,
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
            })
        else:
            tablets.append({
                "tablet": cstr(device.get("device_name") or device.get("device_id")),
                "detail": f"Nobody signed in. Last seen {format_datetime(last_seen, 'd MMM HH:mm')}.",
                "state": "Quiet",
                "state_level": "",
            })
    return tablets, has_more


@frappe.whitelist(methods=["GET"])
def get_staff_desk(*, company: str | None = None) -> dict[str, Any]:
    """Who can use a tablet, at what level, and which tablet is working."""
    context = resolve_director_context(company=company)
    if not context.company:
        return {
            "status": context.status,
            "companies": list(context.allowed_companies),
            "company": context.company,
        }
    people, people_has_more = _people(context)
    tablets, tablets_have_more = _tablets(context)
    return {
        "status": "ok",
        "company": context.company,
        "people": people,
        "people_have_more": people_has_more,
        "needs_pin": sum(1 for person in people if person["active"] and not person["has_pin"]),
        "tablets": tablets,
        "tablets_have_more": tablets_have_more,
    }


@frappe.whitelist(methods=["GET"])
def get_staff_setup_choices(*, company: str | None = None) -> dict[str, Any]:
    """Friendly people and outlet choices for the JiJi Staff dialog."""

    context = resolve_director_context(company=company)
    if not context.company:
        return {
            "status": context.status,
            "cafes": list(context.allowed_companies),
            "people": [],
            "outlets": [],
        }
    employees = frappe.get_all(
        "Employee",
        filters={"company": context.company, "status": "Active"},
        fields=["name", "employee_name", "user_id"],
        order_by="employee_name asc, name asc",
        limit_page_length=201,
    )
    if len(employees) > 200:
        frappe.throw(_("This café has too many people to show safely"), frappe.ValidationError)
    users = [cstr(row.get("user_id")).strip() for row in employees if cstr(row.get("user_id")).strip()]
    existing = {
        cstr(value).strip()
        for value in (
            frappe.get_all(
                "KoPOS Staff Access",
                filters={"user": ["in", users]},
                pluck="user",
                limit_page_length=201,
            )
            if users
            else []
        )
        if cstr(value).strip()
    }
    profile_names = director_pos_profiles(context)
    profiles = (
        frappe.get_all(
            "POS Profile",
            filters={"name": ["in", list(profile_names)]},
            fields=["name", "warehouse"],
            order_by="name asc",
            limit_page_length=100,
        )
        if profile_names
        else []
    )
    warehouses = {
        cstr(row.get("warehouse")).strip()
        for row in profiles
        if cstr(row.get("warehouse")).strip()
    }
    warehouse_rows = (
        frappe.get_all(
            "Warehouse",
            filters={"name": ["in", sorted(warehouses)]},
            fields=["name", "warehouse_name"],
            limit_page_length=100,
        )
        if warehouses
        else []
    )
    warehouse_labels = {
        cstr(row.get("name")).strip(): (
            cstr(row.get("warehouse_name")).strip() or cstr(row.get("name")).strip()
        )
        for row in warehouse_rows
    }
    return {
        "status": "ok",
        "company": context.company,
        "people": [
            {
                "value": cstr(row.get("name")).strip(),
                "label": cstr(row.get("employee_name")).strip()
                or person_name(cstr(row.get("user_id"))),
            }
            for row in employees
            if cstr(row.get("user_id")).strip() and cstr(row.get("user_id")).strip() not in existing
        ],
        "people_missing_sign_in": sum(
            1 for row in employees if not cstr(row.get("user_id")).strip()
        ),
        "outlets": [
            {
                "value": cstr(row.get("name")).strip(),
                "label": warehouse_labels.get(
                    cstr(row.get("warehouse")).strip(),
                    cstr(row.get("name")).strip(),
                ),
            }
            for row in profiles
            if cstr(row.get("name")).strip()
        ],
    }


@frappe.whitelist(methods=["POST"])
def add_staff_access(
    *,
    company: str,
    employee: str,
    outlet: str,
    access_level: str,
    pin: str,
    confirm: str,
) -> dict[str, Any]:
    """Give an existing café employee tablet access without the raw form."""

    context = resolve_director_context(company=company)
    employee_name = cstr(employee).strip()
    row = frappe.db.get_value(
        "Employee",
        employee_name,
        ["employee_name", "user_id", "status", "company"],
        as_dict=True,
    )
    if (
        not row
        or cstr(row.get("company")).strip() != context.company
        or cstr(row.get("status")).strip() != "Active"
    ):
        frappe.throw(_("Choose someone who currently works at this café"), frappe.ValidationError)
    user = cstr(row.get("user_id")).strip()
    if not user:
        frappe.throw(_("Ask Support to add a sign-in email for this person first"), frappe.ValidationError)
    if frappe.db.exists("KoPOS Staff Access", user):
        frappe.throw(_("This person is already on the tablet staff list"), frappe.ValidationError)
    profile_name = cstr(outlet).strip()
    if profile_name not in set(director_pos_profiles(context)):
        frappe.throw(_("Choose an outlet from this café"), frappe.PermissionError)
    profile = frappe.db.get_value(
        "POS Profile", profile_name, ["company", "warehouse"], as_dict=True
    )
    if not profile or cstr(profile.get("company")).strip() != context.company:
        frappe.throw(_("Choose an outlet from this café"), frappe.PermissionError)
    level = cstr(access_level).strip()
    if level not in {"Staff", "Manager"}:
        frappe.throw(_("Choose Staff or Manager"), frappe.ValidationError)
    entered = _checked_pin(pin, confirm)
    document = frappe.new_doc("KoPOS Staff Access")
    document.user = user
    document.employee = employee_name
    document.active = 1
    document.access_level = level
    document.pin = entered
    document.append(
        "outlet_assignments",
        {
            "company": context.company,
            "outlet": profile_name,
            "warehouse": cstr(profile.get("warehouse")).strip(),
        },
    )
    document.insert(ignore_permissions=True)
    if not cstr(frappe.db.get_value("KoPOS Staff Access", user, "pin_hash")).strip():
        frappe.throw(_("JiJi could not save the tablet PIN. Nothing was added."), frappe.ValidationError)
    return {
        "status": "ok",
        "person": cstr(row.get("employee_name")).strip() or person_name(user),
        "detail": _("They can sign in on the tablet now."),
    }


@frappe.whitelist(methods=["POST"])
def set_staff_active(*, access: str, active: int) -> dict[str, Any]:
    """Remove or restore tablet access through the scoped Staff page."""

    name = cstr(access).strip()
    if not name or not can_manage_staff_access(name):
        frappe.throw(_("You do not have access to this person."), frappe.PermissionError)
    document = frappe.get_doc("KoPOS Staff Access", name)
    document.active = 1 if cint(active) else 0
    document.save(ignore_permissions=True)
    return {
        "status": "ok",
        "person": person_name(cstr(document.user)),
        "active": bool(document.active),
        "detail": _("They can use the tablet again.")
        if document.active
        else _("They can no longer sign in on the tablet."),
    }


def _checked_pin(pin: str, confirm: str) -> str:
    entered = cstr(pin).strip()
    repeated = cstr(confirm).strip()
    if not entered.isdigit() or len(entered) != 4:
        frappe.throw(frappe._("A tablet PIN is exactly four digits"))
    if entered != repeated:
        frappe.throw(frappe._("The two PINs are different. Type the same four digits twice."))
    return entered


@frappe.whitelist(methods=["POST"])
def set_staff_pin(*, access: str, pin: str, confirm: str) -> dict[str, Any]:
    """Give one person a tablet PIN.

    Everything that makes a PIN safe already lives on the doctype: it hashes
    the digits, bumps the revision, and invalidates every bound tablet so a
    stale one cannot keep trusting the old verifier. This hands it four digits
    only after the director and café scope have been checked.

    The digits are never stored here, never logged, and never returned.
    """
    name = cstr(access).strip()
    if not name:
        frappe.throw(frappe._("Choose who this PIN is for"))
    entered = _checked_pin(pin, confirm)

    if not can_manage_staff_access(name):
        frappe.throw(
            frappe._("You do not have access to this person."),
            frappe.PermissionError,
        )

    document = frappe.get_doc("KoPOS Staff Access", name)
    before = cstr(document.get("pin_hash")).strip()
    document.pin = entered
    document.save(ignore_permissions=True)

    # Verify the domain hook actually rotated the verifier. Without this check
    # a director could be told the PIN changed while the tablet kept asking for
    # the old one. The salt is random, so the same digits still change it.
    after = cstr(frappe.db.get_value("KoPOS Staff Access", name, "pin_hash")).strip()
    if not after or after == before:
        frappe.throw(
            frappe._("JiJi could not set this PIN. Nothing was changed."),
            frappe.PermissionError,
        )

    return {
        "status": "ok",
        "person": person_name(cstr(document.user)),
        "detail": frappe._("Their tablets will ask for the new PIN within a minute."),
    }
