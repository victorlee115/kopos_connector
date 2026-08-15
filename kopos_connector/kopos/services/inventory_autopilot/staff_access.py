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
from kopos_connector.kopos.services.inventory_autopilot.staff_role_contract import (
    AUTHORIZED_DIRECTOR_ROLE,
    DEVICE_API_AUDITED_DOCTYPES,
    DEVICE_API_ROLE,
    LEGACY_MANAGER_ROLES,
    SENSITIVE_BUSINESS_DOCTYPES,
    TECHNICAL_ADMIN_ROLE,
)


ACCESS_LEVELS = {"Staff", "Manager"}


def outlet_erp_role_failures(*, company: str, warehouse: str) -> tuple[str, ...]:
    """Fail cutover while an outlet POS user still has business ERP access."""

    assignments = frappe.get_all(
        "KoPOS Staff Outlet",
        filters={"company": company, "warehouse": warehouse},
        fields=["parent"],
        limit_page_length=10_000,
    )
    parents = sorted(
        {
            cstr(row.get("parent")).strip()
            for row in assignments
            if cstr(row.get("parent")).strip()
        }
    )
    if not parents:
        return ("central_staff_access_not_configured",)
    rows = frappe.get_all(
        "KoPOS Staff Access",
        filters={"name": ["in", parents], "active": 1},
        fields=["user"],
        limit_page_length=10_000,
    )
    users = sorted(
        {
            cstr(row.get("user")).strip()
            for row in rows
            if cstr(row.get("user")).strip()
        }
    )
    if not users:
        return ("active_central_staff_access_not_configured",)

    failures: list[str] = []
    for user in users:
        roles = {cstr(role).strip() for role in frappe.get_roles(user)}
        if AUTHORIZED_DIRECTOR_ROLE in roles or TECHNICAL_ADMIN_ROLE in roles:
            continue
        legacy_roles = sorted(roles & LEGACY_MANAGER_ROLES)
        if legacy_roles:
            failures.append(
                "outlet_user_has_legacy_erp_role:{0}:{1}".format(
                    user, ",".join(legacy_roles)
                )
            )
        exposed = _sensitive_doctypes_for_roles(roles)
        if exposed:
            failures.append(
                "outlet_user_has_business_erp_permission:{0}:{1}".format(
                    user, ",".join(exposed)
                )
            )
    return tuple(sorted(set(failures)))


def _sensitive_doctypes_for_roles(roles: set[str]) -> tuple[str, ...]:
    if not roles:
        return ()
    exposed: set[str] = set()
    permission_fields = [
        "parent", "role", "read", "write", "create", "submit", "report",
        "export", "import", "print", "email", "share",
    ]
    for permission_doctype in ("DocPerm", "Custom DocPerm"):
        if not frappe.db.exists("DocType", permission_doctype):
            continue
        rows = frappe.get_all(
            permission_doctype,
            filters={
                "role": ["in", sorted(roles)],
                "parent": ["in", sorted(SENSITIVE_BUSINESS_DOCTYPES)],
            },
            fields=permission_fields,
            limit_page_length=10_000,
        )
        for row in rows:
            if any(cint(row.get(fieldname)) for fieldname in permission_fields[2:]):
                exposed.add(cstr(row.get("parent")).strip())
    return tuple(sorted(value for value in exposed if value))


def staff_identity_issue(*, user: str, employee: str) -> str | None:
    """Return the reason a central POS identity cannot be active.

    Frappe ``User`` and ``Employee`` are the person authorities. A central
    POS record is only usable when both links still exist, the User is
    enabled, the Employee is active, and the Employee points back to exactly
    that User. This read-only helper is also used by the device resolver so
    revoking a person cannot be bypassed by retaining an old POS role or PIN.

    Inactive ``KoPOS Staff Access`` documents are deliberately allowed to
    retain historical identity fields; callers must never serialize them as
    active access. The DocType validator applies this check when an access
    record is activated.
    """

    resolved_user = cstr(user).strip()
    resolved_employee = cstr(employee).strip()
    if not resolved_user or not resolved_employee:
        return "Central POS access requires both a User and Employee"
    if not frappe.db.exists("User", resolved_user):
        return "Central POS access User does not exist: {0}".format(resolved_user)
    if not cint(frappe.db.get_value("User", resolved_user, "enabled")):
        return "Central POS access User is disabled: {0}".format(resolved_user)
    if not frappe.db.exists("Employee", resolved_employee):
        return "Central POS access Employee does not exist: {0}".format(resolved_employee)
    employee_status = cstr(
        frappe.db.get_value("Employee", resolved_employee, "status")
    ).strip()
    if employee_status != "Active":
        return "Central POS access Employee is not active: {0}".format(
            resolved_employee
        )
    employee_user = cstr(
        frappe.db.get_value("Employee", resolved_employee, "user_id")
    ).strip()
    if employee_user != resolved_user:
        return (
            "Central POS access Employee {0} is linked to a different User"
        ).format(resolved_employee)
    return None


def legacy_device_user_signature(document_or_rows: Any) -> tuple[tuple[Any, ...], ...]:
    """Return the persisted, non-secret shape of legacy device-user rows.

    The child rows remain in the database during the staged migration.  This
    signature is deliberately small and deterministic so the device
    controller can reject an attempted edit without treating a PIN verifier
    as an authority of its own.
    """

    rows = (
        getattr(document_or_rows, "device_users", None)
        if not isinstance(document_or_rows, (list, tuple))
        else document_or_rows
    ) or []
    return tuple(
        sorted(
            (
                cstr(getattr(row, "user", None)).strip(),
                cstr(getattr(row, "display_name", None)).strip(),
                cstr(getattr(row, "pin_hash", None)).strip(),
                cint(getattr(row, "active", 0)),
                cint(getattr(row, "can_manager_override", 0)),
                cint(getattr(row, "can_refund", 0)),
                cint(getattr(row, "can_void", 0)),
                cint(getattr(row, "can_open_shift", 0)),
                cint(getattr(row, "can_close_shift", 0)),
                cint(getattr(row, "default_cashier", 0)),
            )
            for row in rows
        )
    )


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
    outlet = cstr(getattr(profile, "name", None)).strip()
    legacy = [row for row in (legacy_rows if legacy_rows is not None else getattr(device_doc, "device_users", None) or [])]
    legacy_by_user = {
        cstr(getattr(row, "user", None)).strip(): row
        for row in legacy
        if cstr(getattr(row, "user", None)).strip() and cint(getattr(row, "active", 0))
    }

    central_authority_exists = central_staff_access_exists_for_device(
        device_doc, profile_doc=profile
    )
    central = _central_rows(company=company, warehouse=warehouse, outlet=outlet)
    if central_authority_exists:
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


def central_staff_access_exists_for_device(
    device_doc: Any, *, profile_doc: Any | None = None
) -> bool:
    """Return whether central authority has ever been assigned to this outlet.

    Inactive central records still count.  Falling back to legacy child rows
    after a director deactivates a central user would silently restore an old
    PIN and defeat the migration lock.
    """

    if cint(getattr(device_doc, "central_staff_access_locked", 0)):
        return True

    profile = profile_doc
    if profile is None:
        profile_name = cstr(getattr(device_doc, "pos_profile", None)).strip()
        if not profile_name:
            return False
        profile = frappe.get_cached_doc("POS Profile", profile_name)
    company = cstr(getattr(profile, "company", None)).strip()
    warehouse = cstr(getattr(profile, "warehouse", None)).strip()
    outlet = cstr(getattr(profile, "name", None)).strip()
    return bool(
        _central_parent_names(
            company=company, warehouse=warehouse, outlet=outlet
        )
    )


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
                    "identity_issue": None,
                    "access_level": "Staff",
                    "pin_hashes": set(),
                    "outlet_assignments": [],
                    "devices": [],
                },
            )
            if employee:
                access["identity_issue"] = staff_identity_issue(
                    user=user,
                    employee=employee,
                )
            else:
                access["identity_issue"] = (
                    "Central POS access requires an active Employee linked to the User"
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
    conflicts = [
        row["user"]
        for row in report
        if row["pin_conflict"]
        or not row["pin_hash_present"]
        or not row["employee"]
        or row.get("identity_issue")
    ]
    digest = hashlib.sha256(json.dumps(report, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()
    if expected_digest is not None and cstr(expected_digest).strip() != digest:
        frappe.throw("Staff access migration input digest does not match the reviewed dry run", frappe.ValidationError)
    if dry_run:
        readiness = staff_access_acknowledgement_readiness(
            device_ids=sorted({device for row in report for device in row["devices"]})
        )
        return {
            "status": "dry_run",
            "input_digest": digest,
            "users": report,
            "blocked_users": conflicts,
            "acknowledgements": readiness,
            "cleanup_eligible": readiness["cleanup_eligible"] and not conflicts,
            "legacy_rows_preserved": True,
            "deletion_performed": False,
        }
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
        # Existing central records may predate this migration command.  The
        # device-level lock makes this reconciliation idempotent while still
        # invalidating any tablet that has not yet acknowledged central access.
        invalidate_devices_for_staff_access(doc)
    frappe.db.commit()
    readiness = staff_access_acknowledgement_readiness(
        device_ids=sorted({device for row in report for device in row["devices"]})
    )
    return {
        "status": "applied",
        "input_digest": digest,
        "created": created,
        "updated": len(report) - created,
        "blocked_users": [],
        "acknowledgements": readiness,
        "cleanup_eligible": readiness["cleanup_eligible"],
        "legacy_rows_preserved": True,
        "deletion_performed": False,
    }


def _central_rows(
    *, company: str, warehouse: str, outlet: str | None = None
) -> list[dict[str, Any]]:
    parents = _central_parent_names(
        company=company, warehouse=warehouse, outlet=outlet
    )
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
        and staff_identity_issue(
            user=cstr(row.get("user")).strip(),
            employee=cstr(row.get("employee")).strip(),
        )
        is None
    ]


def _central_parent_names(
    *, company: str, warehouse: str, outlet: str | None = None
) -> list[str]:
    if (
        not company
        or not warehouse
        or not frappe.db.exists("DocType", "KoPOS Staff Access")
        or not frappe.db.exists("DocType", "KoPOS Staff Outlet")
    ):
        return []
    filters: dict[str, Any] = {"company": company, "warehouse": warehouse}
    resolved_outlet = cstr(outlet).strip()
    if resolved_outlet:
        filters["outlet"] = resolved_outlet
    assignments = frappe.get_all(
        "KoPOS Staff Outlet",
        filters=filters,
        fields=["parent"],
        limit_page_length=10_000,
    )
    return sorted(
        {
            cstr(row.get("parent")).strip()
            for row in assignments
            if cstr(row.get("parent")).strip()
        }
    )


def _serialize_central_row(row: dict[str, Any], legacy: Any | None) -> dict[str, Any]:
    pin_hash = row["pin_hash"] or _unambiguous_legacy_pin_hash(
        row["user"], legacy
    )
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


def _unambiguous_legacy_pin_hash(user: str, fallback_row: Any | None) -> str:
    """Use a legacy verifier only when every known active copy agrees."""

    values: set[str] = set()
    if fallback_row is not None:
        values.add(cstr(getattr(fallback_row, "pin_hash", None)).strip())

    # Older sites may not have the device DocType during migrations.  The
    # supplied row is still safe to use in that compatibility-only case; once
    # the DocType exists, inspect every active copy before accepting it.
    if frappe.db.exists("DocType", "KoPOS Device"):
        for device in frappe.get_all(
            "KoPOS Device", fields=["name"], limit_page_length=10_000
        ):
            device_doc = frappe.get_doc("KoPOS Device", device["name"])
            for row in getattr(device_doc, "device_users", None) or []:
                if (
                    cstr(getattr(row, "user", None)).strip() == user
                    and cint(getattr(row, "active", 0))
                ):
                    values.add(cstr(getattr(row, "pin_hash", None)).strip())

    if len(values) != 1:
        frappe.throw(
            "Central POS access for {0} cannot select an ambiguous legacy PIN verifier".format(user),
            frappe.ValidationError,
        )
    pin_hash = next(iter(values))
    if not is_supported_pin_hash(pin_hash):
        frappe.throw(
            "Central POS access for {0} has no supported PIN verifier".format(user),
            frappe.ValidationError,
        )
    return pin_hash


def central_staff_access_signature(document: Any) -> tuple[Any, ...]:
    """Stable central-access fingerprint used to invalidate device configs."""

    assignments = tuple(
        sorted(
            (
                cstr(getattr(row, "company", None)).strip(),
                cstr(getattr(row, "outlet", None)).strip(),
                cstr(getattr(row, "warehouse", None)).strip(),
            )
            for row in (getattr(document, "outlet_assignments", None) or [])
        )
    )
    return (
        cstr(getattr(document, "user", None)).strip(),
        cstr(getattr(document, "employee", None)).strip(),
        cint(getattr(document, "active", 0)),
        cstr(getattr(document, "access_level", None)).strip(),
        cstr(getattr(document, "pin_hash", None)).strip(),
        assignments,
    )


def invalidate_devices_for_staff_access(
    access_doc: Any, *, previous: Any | None = None, force: bool = False
) -> dict[str, Any]:
    """Bump every device bound to an access assignment and clear old reports."""

    device_names: set[str] = set()
    documents = [access_doc] + ([previous] if previous is not None else [])
    for document in documents:
        for assignment in getattr(document, "outlet_assignments", None) or []:
            company = cstr(getattr(assignment, "company", None)).strip()
            warehouse = cstr(getattr(assignment, "warehouse", None)).strip()
            if not company or not warehouse:
                continue
            outlet = cstr(getattr(assignment, "outlet", None)).strip()
            profile_filters: dict[str, Any] = {
                "company": company,
                "warehouse": warehouse,
            }
            if outlet:
                profile_filters["name"] = outlet
            profiles = frappe.get_all(
                "POS Profile",
                filters=profile_filters,
                fields=["name"],
                limit_page_length=10_000,
            )
            profile_names = [
                cstr(row.get("name")).strip()
                for row in profiles
                if cstr(row.get("name")).strip()
            ]
            if not profile_names:
                continue
            devices = frappe.get_all(
                "KoPOS Device",
                filters={"pos_profile": ["in", profile_names]},
                fields=["name"],
                limit_page_length=10_000,
            )
            device_names.update(
                cstr(row.get("name")).strip()
                for row in devices
                if cstr(row.get("name")).strip()
            )

    bumped: list[dict[str, Any]] = []
    skipped: list[str] = []
    for name in sorted(device_names):
        device = frappe.get_doc("KoPOS Device", name)
        if cint(getattr(device, "central_staff_access_locked", 0)) and not force:
            skipped.append(cstr(getattr(device, "device_id", None)).strip() or name)
            continue
        invalidator = getattr(device, "invalidate_for_staff_access", None)
        if callable(invalidator):
            next_version = invalidator()
        else:
            next_version = max(1, cint(getattr(device, "config_version", 0))) + 1
            setattr(device, "config_version", next_version)
        saver = getattr(device, "save", None)
        if callable(saver):
            saver(ignore_permissions=True)
        bumped.append(
            {
                "device": cstr(getattr(device, "device_id", None)).strip() or name,
                "config_version": next_version,
            }
        )
    return {
        "devices": bumped,
        "count": len(bumped),
        "skipped_already_locked": sorted(skipped),
    }


def staff_access_acknowledgement_readiness(
    *, device_ids: Iterable[str]
) -> dict[str, Any]:
    """Report exact-config acknowledgement without deleting legacy rows."""

    requested = sorted({cstr(value).strip() for value in device_ids if cstr(value).strip()})
    if not requested:
        return {
            "affected_devices": [],
            "all_affected_devices_acknowledged": False,
            "cleanup_eligible": False,
            "legacy_rows_preserved": True,
            "deletion_performed": False,
        }
    rows = frappe.get_all(
        "KoPOS Device",
        filters={"device_id": ["in", requested]},
        fields=[
            "device_id",
            "config_version",
            "inventory_config_version",
            "inventory_report_received_at",
            "inventory_report_revision",
            "central_staff_access_locked",
        ],
        limit_page_length=10_000,
    )
    by_id = {
        cstr(row.get("device_id")).strip(): row
        for row in rows
        if cstr(row.get("device_id")).strip()
    }
    statuses: list[dict[str, Any]] = []
    for device_id in requested:
        row = by_id.get(device_id)
        if row is None:
            statuses.append(
                {
                    "device_id": device_id,
                    "acknowledged": False,
                    "cleanup_eligible": False,
                    "reason": "device_not_found",
                }
            )
            continue
        expected = cint(row.get("config_version"))
        reported = cint(row.get("inventory_config_version"))
        received = bool(cstr(row.get("inventory_report_received_at")).strip())
        revision = cint(row.get("inventory_report_revision"))
        central_locked = bool(cint(row.get("central_staff_access_locked")))
        acknowledged = (
            central_locked
            and expected > 0
            and reported == expected
            and received
            and revision > 0
        )
        statuses.append(
            {
                "device_id": device_id,
                "config_version": expected,
                "reported_config_version": reported,
                "report_revision": revision,
                "report_received": received,
                "central_staff_access_locked": central_locked,
                "acknowledged": acknowledged,
                "cleanup_eligible": acknowledged,
                "reason": (
                    None
                    if acknowledged
                    else (
                        "central_access_not_applied"
                        if not central_locked
                        else "exact_config_report_required"
                    )
                ),
            }
        )
    all_acknowledged = bool(statuses) and all(row["acknowledged"] for row in statuses)
    return {
        "affected_devices": statuses,
        "all_affected_devices_acknowledged": all_acknowledged,
        "cleanup_eligible": all_acknowledged,
        "legacy_rows_preserved": True,
        "deletion_performed": False,
    }
