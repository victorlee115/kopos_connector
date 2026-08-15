from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.acceptance.restored_outlet_matrix import _people
from kopos_connector.kopos.doctype.kopos_staff_access.kopos_staff_access import (
    KoPOSStaffAccess,
)
from kopos_connector.kopos.services.inventory_autopilot.staff_access import (
    _central_rows,
    resolve_staff_access_for_device,
    staff_identity_issue,
)
from kopos_connector.kopos.services.inventory_autopilot.staff_role_contract import (
    SENSITIVE_BUSINESS_DOCTYPES,
)


ROOT = Path(__file__).resolve().parents[1]
DEVICE_ROLE = "KoPOS Device API"


def _permissions(path: Path) -> list[dict[str, object]]:
    return json.loads(path.read_text(encoding="utf8")).get("permissions", [])


def test_inventory_records_have_no_generic_device_resource_permissions() -> None:
    paths = sorted(
        list((ROOT / "kopos" / "doctype").glob("fb_inventory_*/*.json"))
        + [ROOT / "kopos" / "doctype" / "fb_availability_hold" / "fb_availability_hold.json"]
    )
    assert paths
    for path in paths:
        assert all(permission.get("role") != DEVICE_ROLE for permission in _permissions(path)), path


def test_sensitive_role_boundary_includes_authoritative_inventory_records() -> None:
    assert {
        "FB Availability Hold",
        "FB Inventory Availability Rule",
        "FB Inventory Count Observation",
        "FB Inventory Count Task",
        "FB Inventory Exception",
        "FB Inventory Plan",
        "FB Inventory Policy",
    }.issubset(SENSITIVE_BUSINESS_DOCTYPES)


def test_device_role_permissions_are_limited_to_audited_live_read_paths() -> None:
    expected = {
        "KoPOS Item Modifier Group",
        "KoPOS Modifier Group",
        "KoPOS Modifier Option",
        "KoPOS Promotion",
        "KoPOS Promotion Snapshot",
        "Maybank QR Transaction",
    }
    actual: set[str] = set()
    for path in (ROOT / "kopos" / "doctype").glob("*/*.json"):
        payload = json.loads(path.read_text(encoding="utf8"))
        if any(permission.get("role") == DEVICE_ROLE for permission in payload.get("permissions", [])):
            actual.add(str(payload.get("name")))
            for permission in _permissions(path):
                if permission.get("role") == DEVICE_ROLE:
                    assert permission.get("read") == 1
                    for fieldname in ("write", "create", "submit", "cancel", "delete", "share"):
                        assert not permission.get(fieldname), (path, fieldname)
    assert actual == expected


def _identity_values(*, enabled: int = 1, employee_status: str = "Active", employee_user: str = "staff@example.com"):
    def exists(doctype: str, name: str) -> bool:
        return (doctype, name) in {
            ("User", "staff@example.com"),
            ("Employee", "EMP-001"),
        }

    def get_value(doctype: str, _name: str, fieldname: str):
        if doctype == "User" and fieldname == "enabled":
            return enabled
        if doctype == "Employee" and fieldname == "status":
            return employee_status
        if doctype == "Employee" and fieldname == "user_id":
            return employee_user
        return None

    return exists, get_value


def _staff_access(*, active: int = 1) -> KoPOSStaffAccess:
    access = KoPOSStaffAccess()
    access.user = "staff@example.com"
    access.employee = "EMP-001"
    access.active = active
    access.access_level = "Staff"
    access.pin_hash = "scrypt$256$" + ("a" * 32) + "$" + ("b" * 64)
    access.outlet_assignments = [
        SimpleNamespace(company="JiJi", outlet="Outlet POS", warehouse="Outlet - JiJi")
    ]
    return access


def test_active_staff_access_requires_enabled_user_active_matching_employee() -> None:
    cases = (
        ("User is disabled", _identity_values(enabled=0)),
        ("Employee does not exist", _identity_values()),
        ("Employee is not active", _identity_values(employee_status="Inactive")),
        ("linked to a different User", _identity_values(employee_user="other@example.com")),
    )
    for message, (exists, get_value) in cases:
        if message == "Employee does not exist":
            exists = lambda doctype, name: doctype == "User" and name == "staff@example.com"
        with patch.object(frappe.db, "exists", side_effect=exists), patch.object(
            frappe.db, "get_value", side_effect=get_value
        ):
            access = _staff_access()
            try:
                access.validate()
            except frappe.ValidationError as error:
                assert message in str(error), str(error)
            else:
                raise AssertionError(f"expected active identity failure: {message}")


def test_active_staff_access_accepts_enabled_matching_identity() -> None:
    exists, get_value = _identity_values()
    with patch.object(frappe.db, "exists", side_effect=exists), patch.object(
        frappe.db, "get_value", side_effect=get_value
    ):
        access = _staff_access()
        access.validate()
        assert access.pin is None


def test_inactive_staff_access_preserves_historical_identity_without_granting_access() -> None:
    with patch.object(frappe.db, "exists", return_value=False), patch.object(
        frappe.db, "get_value", return_value=None
    ):
        access = _staff_access(active=0)
        access.pin_hash = None
        access.validate()


def test_staff_identity_issue_is_clear_for_missing_and_mismatched_links() -> None:
    assert "requires both" in (staff_identity_issue(user="", employee="EMP-001") or "")


def test_revoked_central_identity_is_not_serialized_or_replaced_by_legacy_rows() -> None:
    device = SimpleNamespace(
        central_staff_access_locked=1,
        pos_profile="Outlet POS",
        device_users=[
            SimpleNamespace(
                user="staff@example.com",
                active=1,
                pin_hash="scrypt$256$" + ("a" * 32) + "$" + ("b" * 64),
            )
        ],
    )
    profile = SimpleNamespace(name="Outlet POS", company="JiJi", warehouse="Outlet - JiJi")

    def get_all(doctype: str, **_kwargs):
        if doctype == "KoPOS Staff Outlet":
            return [{"parent": "ACCESS-001"}]
        if doctype == "KoPOS Staff Access":
            return [{
                "name": "ACCESS-001",
                "user": "staff@example.com",
                "employee": "EMP-001",
                "access_level": "Staff",
                "revision": 2,
                "pin_hash": "",
            }]
        return []

    def get_value(doctype: str, _name: str, fieldname: str):
        if doctype == "User" and fieldname == "enabled":
            return 0
        return None

    with patch.object(frappe.db, "exists", side_effect=lambda doctype, _name: doctype in {"DocType", "User", "Employee"}), patch.object(
        frappe.db, "get_value", side_effect=get_value
    ), patch.object(frappe, "get_all", side_effect=get_all):
        assert _central_rows(company="JiJi", warehouse="Outlet - JiJi", outlet="Outlet POS") == []
        assert resolve_staff_access_for_device(device, profile_doc=profile) == []


def test_outlet_matrix_reports_legacy_roles_and_sensitive_permissions_without_exposing_users() -> None:
    from kopos_connector.acceptance import restored_outlet_matrix as matrix

    class Meta:
        def __init__(self, fields: set[str]) -> None:
            self.fields = fields

        def has_field(self, fieldname: str) -> bool:
            return fieldname in self.fields

    class FakeFrappe:
        rows = {
            "Employee": [
                {"name": "EMP-STAFF", "user_id": "staff@example.com", "status": "Active"},
                {"name": "EMP-DIRECTOR", "user_id": "director@example.com", "status": "Active"},
                {"name": "EMP-ADMIN", "user_id": "admin@example.com", "status": "Active"},
            ],
            "User": [
                {"name": "staff@example.com", "enabled": 1},
                {"name": "director@example.com", "enabled": 1},
                {"name": "admin@example.com", "enabled": 1},
            ],
            "KoPOS Staff Access": [
                {"name": "ACCESS-STAFF", "user": "staff@example.com", "employee": "EMP-STAFF", "active": 1, "pin_hash": "x"},
                {"name": "ACCESS-DIRECTOR", "user": "director@example.com", "employee": "EMP-DIRECTOR", "active": 1, "pin_hash": "x"},
                {"name": "ACCESS-ADMIN", "user": "admin@example.com", "employee": "EMP-ADMIN", "active": 1, "pin_hash": "x"},
            ],
            "KoPOS Staff Outlet": [
                {"name": "ASSIGN-STAFF", "parent": "ACCESS-STAFF", "company": "Cafe Co", "outlet": "Cafe POS", "warehouse": "Cafe - WH"},
                {"name": "ASSIGN-DIRECTOR", "parent": "ACCESS-DIRECTOR", "company": "Cafe Co", "outlet": "Cafe POS", "warehouse": "Cafe - WH"},
                {"name": "ASSIGN-ADMIN", "parent": "ACCESS-ADMIN", "company": "Cafe Co", "outlet": "Cafe POS", "warehouse": "Cafe - WH"},
            ],
            "Has Role": [
                {"name": "ROLE-STAFF-1", "parent": "staff@example.com", "parenttype": "User", "role": "KoPOS Manager"},
                {"name": "ROLE-STAFF-2", "parent": "staff@example.com", "parenttype": "User", "role": "Stock User"},
                {"name": "ROLE-DIRECTOR", "parent": "director@example.com", "parenttype": "User", "role": "Company Director"},
                {"name": "ROLE-ADMIN", "parent": "admin@example.com", "parenttype": "User", "role": "System Manager"},
            ],
            "DocPerm": [
                {"name": "PERM-STOCK", "parent": "Stock Entry", "role": "Stock User", "read": 1},
                {"name": "PERM-DIRECTOR", "parent": "Stock Entry", "role": "Company Director", "read": 1},
            ],
        }

        def get_meta(self, doctype: str) -> Meta:
            fields = {"name"}
            for row in self.rows.get(doctype, []):
                fields.update(row)
            return Meta(fields)

        def get_all(self, doctype: str, **_kwargs):
            return list(self.rows.get(doctype, []))

    ctx = {"missing": [], "sourceCounts": {}, "fixtureCounts": {}, "fixtureHashes": {}}
    with patch.object(matrix, "frappe", FakeFrappe()):
        people = _people(ctx)
    assert people["legacyManagerRoleUserCount"] == 1
    assert people["sensitivePermissionUserCount"] == 1
    assert people["technicalAdminUserCount"] == 1
    assert people["companyDirectorUserCount"] == 1
    assert people["permissionViolations"]
    assert "staff@example.com" not in json.dumps(people)
    assert "Cafe Co" not in json.dumps(people)
