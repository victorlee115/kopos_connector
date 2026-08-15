"""Central POS staff access resolution and legacy-device migration helpers.

``KoPOS Staff Access`` is the business authority for a person's POS level and
outlet assignments.  ``KoPOS Device User`` remains a compatibility source
while tablets are being upgraded; it is never allowed to override a central
record that exists for the device's outlet.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Iterable

import frappe
from frappe.utils import cint, cstr

from kopos_connector.utils.pin import is_supported_pin_hash


ACCESS_LEVELS = {"Staff", "Manager"}


def resolve_staff_access_for_device(
    device_doc: Any,
    *,
    profile_doc: Any | None = None,
    legacy_rows: Iterable[Any] | None = None,
) -> list[dict[str, Any]]:
    """Return the signed-in users allowed on one device's outlet.

    Central records are preferred whenever at least one matching assignment is
    present.  During the staged migration, a central record may temporarily
    reuse the legacy device row's verifier; this keeps a tablet usable while
    the director rotates/acknowledges the central access record.  No plaintext
    PIN crosses this boundary.
    """

    profile = profile_doc or frappe.get_cached_doc("POS Profile", device_doc.pos_profile)
    company = cstr(getattr(profile, "company", None)).strip()
    warehouse = cstr(getattr(profile, "warehouse", None)).strip()
    legacy = [row for row in (legacy_rows if legacy_rows is not None else getattr(device_doc, "device_users", None) or [])]
    legacy_by_user = {
        cstr(getattr(row, "user", None)).strip(): row
        for row in legacy
        if cstr(getattr(row, "user", None)).strip() and cint(getattr(row, "active", 0))
    }

    central = _central_rows(company=company, warehouse=warehouse)
    if central:
        return [_serialize_central_row(row, legacy_by_user.get(row["user"])) for row in central]

    # Compatibility mode is deliberately explicit.  It is reported in the
    # config so the release/preflight tools can require device acknowledgement
    # before the legacy source is removed.
    return [_serialize_legacy_row(row) for row in legacy_by_user.values()]


def find_staff_access_for_device(device_doc: Any, user: str, *, profile_doc: Any | None = None) -> dict[str, Any] | None:
    resolved_user = cstr(user).strip()
    if not resolved_user:
        return None
    rows = resolve_staff_access_for_device(device_doc, profile_doc=profile_doc)
    return next((row for row in rows if row["user"] == resolved_user), None)


def discover_legacy_staff_access() -> list[dict[str, Any]]:
    """Build a deterministic dry-run report from existing device-user rows."""

    if not frappe.db.exists("DocType", "KoPOS Device"):
        return []
    devices = frappe.get_all(
        "KoPOS Device",
        filters={"enabled": 1},
        fields=["name", "device_id", "pos_profile"],
        limit_page_length=10_000,
    )
    grouped: dict[str, dict[str, Any]] = {}
    conflicts: dict[str, set[str]] = defaultdict(set)
    for device in devices:
        profile = frappe.get_cached_doc("POS Profile", device["pos_profile"])
        device_doc = frappe.get_doc("KoPOS Device", device["name"])
        for row in device_doc.device_users or []:
            user = cstr(getattr(row, "user", None)).strip()
            if not user or not cint(getattr(row, "active", 0)):
                continue
            pin_hash = cstr(getattr(row, "pin_hash", None)).strip()
            conflicts[user].add(pin_hash)
            employee = ""
            if frappe.db.exists("DocType", "Employee"):
                employee = cstr(
                    frappe.db.get_value(
                        "Employee",
                        {"user_id": user, "status": "Active"},
                        "name",
                    )
                ).strip()
            access = grouped.setdefault(
                user,
                {
                    "user": user,
                    "employee": employee,
                    "access_level": "Staff",
                    "pin_hashes": set(),
                    "outlet_assignments": [],
                    "devices": [],
                },
            )
            access["pin_hashes"].add(pin_hash)
            if cint(getattr(row, "can_manager_override", 0)) or cint(getattr(row, "can_refund", 0)) or cint(getattr(row, "can_void", 0)):
                access["access_level"] = "Manager"
            assignment = {
                "company": cstr(getattr(profile, "company", None)).strip(),
                "outlet": cstr(getattr(profile, "name", None)).strip(),
                "warehouse": cstr(getattr(profile, "warehouse", None)).strip(),
            }
            if assignment not in access["outlet_assignments"]:
                access["outlet_assignments"].append(assignment)
            access["devices"].append(cstr(device["device_id"]).strip())

    report: list[dict[str, Any]] = []
    for user in sorted(grouped):
        row = grouped[user]
        hashes = sorted(value for value in row.pop("pin_hashes") if value)
        row["pin_conflict"] = len(set(conflicts[user])) > 1
        row["pin_hash_present"] = len(hashes) == 1 and is_supported_pin_hash(hashes[0])
        row["pin_hash_sha256"] = hashlib.sha256(hashes[0].encode("utf-8")).hexdigest() if len(hashes) == 1 else None
        row["devices"] = sorted(set(row["devices"]))
        row["outlet_assignments"] = sorted(row["outlet_assignments"], key=lambda value: (value["company"], value["warehouse"], value["outlet"]))
        report.append(row)
    return report


def migrate_legacy_staff_access(*, dry_run: bool = True, expected_digest: str | None = None) -> dict[str, Any]:
    """Create central records only when the dry-run has no PIN conflicts.

    This operation is intentionally director-run and idempotent.  It never
    deletes or edits the source device-user rows.
    """

    report = discover_legacy_staff_access()
    conflicts = [row["user"] for row in report if row["pin_conflict"] or not row["pin_hash_present"] or not row["employee"]]
    digest = hashlib.sha256(json.dumps(report, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()
    if expected_digest is not None and cstr(expected_digest).strip() != digest:
        frappe.throw("Staff access migration input digest does not match the reviewed dry run", frappe.ValidationError)
    if dry_run:
        return {"status": "dry_run", "input_digest": digest, "users": report, "blocked_users": conflicts}
    if conflicts:
        frappe.throw("Staff access migration is blocked for: {0}".format(", ".join(sorted(conflicts))), frappe.ValidationError)
    if not frappe.db.exists("DocType", "KoPOS Staff Access"):
        frappe.throw("KoPOS Staff Access is not installed", frappe.ValidationError)
    created = 0
    for row in report:
        existing = frappe.db.exists("KoPOS Staff Access", row["user"])
        doc = frappe.get_doc("KoPOS Staff Access", existing) if existing else frappe.new_doc("KoPOS Staff Access")
        doc.user = row["user"]
        doc.employee = row["employee"]
        doc.active = 1
        doc.access_level = row["access_level"]
        doc.pin_hash = _single_pin_hash(row["user"])
        doc.outlet_assignments = []
        for assignment in row["outlet_assignments"]:
            doc.append("outlet_assignments", assignment)
        if existing:
            doc.save(ignore_permissions=True)
        else:
            doc.insert(ignore_permissions=True)
            created += 1
    frappe.db.commit()
    return {"status": "applied", "input_digest": digest, "created": created, "updated": len(report) - created, "blocked_users": []}


def _central_rows(*, company: str, warehouse: str) -> list[dict[str, Any]]:
    if not company or not warehouse or not frappe.db.exists("DocType", "KoPOS Staff Access") or not frappe.db.exists("DocType", "KoPOS Staff Outlet"):
        return []
    assignments = frappe.get_all(
        "KoPOS Staff Outlet",
        filters={"company": company, "warehouse": warehouse},
        fields=["parent"],
        limit_page_length=10_000,
    )
    parents = sorted({cstr(row.get("parent")).strip() for row in assignments if cstr(row.get("parent")).strip()})
    if not parents:
        return []
    rows = frappe.get_all(
        "KoPOS Staff Access",
        filters={"name": ["in", parents], "active": 1},
        fields=["name", "user", "employee", "access_level", "revision", "pin_hash"],
        order_by="user asc",
        limit_page_length=10_000,
    )
    return [
        {
            "name": cstr(row.get("name")).strip(),
            "user": cstr(row.get("user")).strip(),
            "employee": cstr(row.get("employee")).strip(),
            "access_level": cstr(row.get("access_level")).strip() or "Staff",
            "revision": cint(row.get("revision") or 1),
            "pin_hash": cstr(row.get("pin_hash")).strip(),
        }
        for row in rows
        if cstr(row.get("user")).strip()
    ]


def _serialize_central_row(row: dict[str, Any], legacy: Any | None) -> dict[str, Any]:
    pin_hash = row["pin_hash"] or cstr(getattr(legacy, "pin_hash", None)).strip()
    if not is_supported_pin_hash(pin_hash):
        frappe.throw("Central POS access for {0} has no supported PIN verifier".format(row["user"]), frappe.ValidationError)
    level = row["access_level"] if row["access_level"] in ACCESS_LEVELS else "Staff"
    return {
        "user": row["user"],
        "display_name": cstr(frappe.db.get_value("User", row["user"], "full_name")).strip() or row["user"],
        "employee": row["employee"],
        "access_level": level,
        "revision": row["revision"],
        "pin_hash": pin_hash,
        "active": True,
        "can_manager_override": level == "Manager",
        "can_refund": level == "Manager",
        "can_void": level == "Manager",
        "can_open_shift": True,
        "can_close_shift": True,
        "default_cashier": bool(cint(getattr(legacy, "default_cashier", 0))) if legacy else False,
        "source": "central",
    }


def _serialize_legacy_row(row: Any) -> dict[str, Any]:
    return {
        "user": cstr(getattr(row, "user", None)).strip(),
        "display_name": cstr(getattr(row, "display_name", None)).strip() or cstr(getattr(row, "user", None)).strip(),
        "employee": "",
        "access_level": "Manager" if cint(getattr(row, "can_manager_override", 0)) else "Staff",
        "revision": 0,
        "pin_hash": cstr(getattr(row, "pin_hash", None)).strip(),
        "active": bool(cint(getattr(row, "active", 0))),
        "can_manager_override": bool(cint(getattr(row, "can_manager_override", 0))),
        "can_refund": bool(cint(getattr(row, "can_refund", 0))),
        "can_void": bool(cint(getattr(row, "can_void", 0))),
        "can_open_shift": bool(cint(getattr(row, "can_open_shift", 0))),
        "can_close_shift": bool(cint(getattr(row, "can_close_shift", 0))),
        "default_cashier": bool(cint(getattr(row, "default_cashier", 0))),
        "source": "legacy_device_user",
    }


def _single_pin_hash(user: str) -> str:
    for device in frappe.get_all("KoPOS Device", filters={"enabled": 1}, fields=["name"], limit_page_length=10_000):
        doc = frappe.get_doc("KoPOS Device", device["name"])
        for row in doc.device_users or []:
            if cstr(getattr(row, "user", None)).strip() == user and cint(getattr(row, "active", 0)):
                return cstr(getattr(row, "pin_hash", None)).strip()
    frappe.throw("No legacy PIN verifier found for {0}".format(user), frappe.ValidationError)
    raise AssertionError("frappe.throw must raise")
