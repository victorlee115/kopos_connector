from types import SimpleNamespace
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.kopos.doctype.kopos_device.kopos_device import KoPOSDevice
from kopos_connector.kopos.services.inventory_autopilot.staff_access import (
    _serialize_central_row,
    _serialize_legacy_row,
    discover_legacy_staff_access,
    invalidate_devices_for_staff_access,
    outlet_erp_role_failures,
    staff_access_acknowledgement_readiness,
)


PIN_HASHES = {
    "1234": "scrypt$256$" + ("a" * 32) + "$" + ("b" * 64),
    "2345": "scrypt$256$" + ("c" * 32) + "$" + ("d" * 64),
}


def _pin_hash(pin: str) -> str:
    return PIN_HASHES[pin]


def test_central_manager_access_derives_inventory_capabilities_and_keeps_default_hint():
    legacy = SimpleNamespace(default_cashier=1, pin_hash=_pin_hash("2345"))
    row = {
        "user": "manager@example.com",
        "employee": "EMP-001",
        "access_level": "Manager",
        "revision": 3,
        "pin_hash": "",
    }
    with patch("kopos_connector.kopos.services.inventory_autopilot.staff_access.frappe.db.get_value", return_value="Manager Siti"):
        result = _serialize_central_row(row, legacy)

    assert result["source"] == "central"
    assert result["access_level"] == "Manager"
    assert result["can_manager_override"] is True
    assert result["can_refund"] is True
    assert result["can_void"] is True
    assert result["default_cashier"] is True
    assert result["pin_hash"] == legacy.pin_hash


def test_legacy_access_is_explicit_compatibility_and_preserves_existing_flags():
    result = _serialize_legacy_row(SimpleNamespace(
        user="staff@example.com",
        display_name="Staff Ahmad",
        active=1,
        pin_hash=_pin_hash("1234"),
        can_manager_override=0,
        can_refund=1,
        can_void=0,
        can_open_shift=1,
        can_close_shift=1,
        default_cashier=1,
    ))

    assert result["source"] == "legacy_device_user"
    assert result["access_level"] == "Staff"
    assert result["can_refund"] is True
    assert result["default_cashier"] is True


def _legacy_row(user: str, pin: str, *, manager: bool = False, default: bool = False):
    return SimpleNamespace(
        user=user,
        display_name=user.split("@", 1)[0].title(),
        active=1,
        default_cashier=1 if default else 0,
        pin_hash=_pin_hash(pin),
        can_manager_override=1 if manager else 0,
        can_refund=1 if manager else 0,
        can_void=1 if manager else 0,
        can_open_shift=1,
        can_close_shift=1,
    )


def test_discovery_merges_one_user_across_devices_without_selecting_a_pin():
    row_a = _legacy_row("staff@example.com", "1234", default=True)
    row_b = _legacy_row("staff@example.com", "1234", manager=True)
    devices = [
        SimpleNamespace(
            name="DEVICE-A",
            device_id="TAB-A",
            pos_profile="POS-A",
            device_users=[row_a],
        ),
        SimpleNamespace(
            name="DEVICE-B",
            device_id="TAB-B",
            pos_profile="POS-B",
            device_users=[row_b],
        ),
    ]

    def get_doc(_doctype, name):
        return next(device for device in devices if device.name == name)

    def get_cached_doc(_doctype, name):
        return SimpleNamespace(
            company="JiJi Sdn Bhd",
            warehouse=f"{name} Warehouse",
            name=name,
        )

    with (
        patch.object(frappe.db, "exists", return_value=True),
        patch.object(
            frappe,
            "get_all",
            return_value=[
                {"name": "DEVICE-A", "device_id": "TAB-A", "pos_profile": "POS-A"},
                {"name": "DEVICE-B", "device_id": "TAB-B", "pos_profile": "POS-B"},
            ],
        ),
        patch.object(frappe, "get_doc", side_effect=get_doc),
        patch.object(frappe, "get_cached_doc", side_effect=get_cached_doc),
        patch.object(frappe.db, "get_value", return_value="EMP-001"),
    ):
        report = discover_legacy_staff_access()

    assert len(report) == 1
    assert report[0]["user"] == "staff@example.com"
    assert report[0]["devices"] == ["TAB-A", "TAB-B"]
    assert report[0]["pin_conflict"] is False
    assert report[0]["pin_hash_present"] is True
    assert report[0]["access_level"] == "Manager"


def test_conflicting_legacy_pins_never_become_a_central_fallback():
    user = "staff@example.com"
    devices = [
        SimpleNamespace(name="DEVICE-A", device_users=[_legacy_row(user, "1234")]),
        SimpleNamespace(name="DEVICE-B", device_users=[_legacy_row(user, "2345")]),
    ]

    with (
        patch.object(frappe.db, "exists", return_value=True),
        patch.object(
            frappe,
            "get_all",
            return_value=[{"name": "DEVICE-A"}, {"name": "DEVICE-B"}],
        ),
        patch.object(frappe, "get_doc", side_effect=lambda _doctype, name: next(
            device for device in devices if device.name == name
        )),
        patch.object(frappe.db, "get_value", return_value="Staff"),
    ):
        try:
            _serialize_central_row(
                {
                    "user": user,
                    "employee": "EMP-001",
                    "access_level": "Staff",
                    "revision": 1,
                    "pin_hash": "",
                },
                devices[0].device_users[0],
            )
        except frappe.ValidationError as error:
            assert "ambiguous legacy PIN" in str(error)
        else:
            raise AssertionError("conflicting legacy PINs must fail closed")


def test_staff_access_config_bump_clears_the_previous_device_report():
    device = KoPOSDevice()
    device.config_version = 7
    device.inventory_report_revision = 4
    device.inventory_config_version = 7
    device.inventory_report_received_at = "2026-08-15T10:00:00+08:00"
    device.inventory_overlay_version = "overlay-7"
    device.inventory_overlay_hash = "hash-7"
    device.inventory_sales_pending = 0
    device.inventory_commands_pending = 0

    assert device.invalidate_for_staff_access() == 8
    assert device.config_version == 8
    assert device.central_staff_access_locked == 1
    assert device.inventory_report_revision == 0
    assert device.inventory_config_version is None
    assert device.inventory_report_received_at is None
    assert device.inventory_overlay_version is None
    assert device.inventory_overlay_hash is None


def test_central_access_invalidation_is_idempotent_per_device_lock():
    class Device:
        name = "DEVICE-A"
        device_id = "TAB-A"
        config_version = 4
        central_staff_access_locked = 0
        saves = 0

        def invalidate_for_staff_access(self):
            self.config_version += 1
            self.central_staff_access_locked = 1
            return self.config_version

        def save(self, *, ignore_permissions):
            assert ignore_permissions is True
            self.saves += 1

    device = Device()
    access = SimpleNamespace(
        outlet_assignments=[
            SimpleNamespace(company="JiJi Sdn Bhd", warehouse="Outlet A")
        ]
    )

    def get_all(doctype, **_kwargs):
        if doctype == "POS Profile":
            return [{"name": "POS-A"}]
        if doctype == "KoPOS Device":
            return [{"name": "DEVICE-A"}]
        raise AssertionError(doctype)

    with (
        patch.object(frappe, "get_all", side_effect=get_all),
        patch.object(frappe, "get_doc", return_value=device),
    ):
        first = invalidate_devices_for_staff_access(access)
        second = invalidate_devices_for_staff_access(access)

    assert first["count"] == 1
    assert first["devices"] == [{"device": "TAB-A", "config_version": 5}]
    assert second["count"] == 0
    assert second["skipped_already_locked"] == ["TAB-A"]
    assert device.saves == 1


def test_legacy_device_user_write_is_rejected_after_central_authority_exists():
    previous = SimpleNamespace(device_users=[_legacy_row("staff@example.com", "1234")])
    device = KoPOSDevice()
    device.name = "DEVICE-A"
    device.doctype = "KoPOS Device"
    device.pos_profile = "POS-A"
    device.device_users = [_legacy_row("staff@example.com", "2345")]
    device.get_doc_before_save = lambda: previous

    with patch(
        "kopos_connector.kopos.services.inventory_autopilot.staff_access.central_staff_access_exists_for_device",
        return_value=True,
    ):
        try:
            device._reject_legacy_user_edits_if_central_authority()
        except frappe.ValidationError as error:
            assert "read-only" in str(error)
        else:
            raise AssertionError("legacy child row edits must be rejected")


def test_new_enabled_device_may_use_only_central_staff_access():
    device = KoPOSDevice()
    device.enabled = 1
    device.pos_profile = "POS-A"
    device.device_users = []

    with patch(
        "kopos_connector.kopos.services.inventory_autopilot.staff_access.central_staff_access_exists_for_device",
        return_value=True,
    ):
        device._normalize_users()


def test_staff_access_acknowledgement_requires_every_device_to_report_exact_config():
    rows = [
        {
            "device_id": "TAB-A",
            "config_version": 8,
            "inventory_config_version": 8,
            "inventory_report_received_at": "2026-08-15T10:00:00+08:00",
            "inventory_report_revision": 2,
            "central_staff_access_locked": 1,
        },
        {
            "device_id": "TAB-B",
            "config_version": 9,
            "inventory_config_version": 8,
            "inventory_report_received_at": "2026-08-15T10:00:00+08:00",
            "inventory_report_revision": 2,
            "central_staff_access_locked": 1,
        },
    ]
    with patch.object(frappe, "get_all", return_value=rows):
        report = staff_access_acknowledgement_readiness(device_ids=["TAB-A", "TAB-B"])

    assert report["all_affected_devices_acknowledged"] is False
    assert report["cleanup_eligible"] is False
    assert report["legacy_rows_preserved"] is True
    assert report["deletion_performed"] is False
    assert report["affected_devices"][1]["reason"] == "exact_config_report_required"

    rows[1]["inventory_config_version"] = 9
    with patch.object(frappe, "get_all", return_value=rows):
        report = staff_access_acknowledgement_readiness(device_ids=["TAB-A", "TAB-B"])
    assert report["all_affected_devices_acknowledged"] is True
    assert report["cleanup_eligible"] is True
    assert report["deletion_performed"] is False


def test_cutover_role_boundary_rejects_outlet_business_erp_access() -> None:
    def get_all(doctype, **_kwargs):
        if doctype == "KoPOS Staff Outlet":
            return [{"parent": "ACCESS-1"}]
        if doctype == "KoPOS Staff Access":
            return [{"user": "staff@example.com"}]
        if doctype == "DocPerm":
            return [{"parent": "Stock Entry", "role": "Stock Manager", "read": 1}]
        if doctype == "Custom DocPerm":
            return []
        raise AssertionError(doctype)

    with (
        patch.object(frappe, "get_all", side_effect=get_all),
        patch.object(frappe, "get_roles", return_value=["Employee", "Stock Manager"]),
        patch.object(frappe.db, "exists", return_value=True),
    ):
        failures = outlet_erp_role_failures(
            company="JiJi Sdn Bhd", warehouse="Outlet A"
        )

    assert any("legacy_erp_role" in reason for reason in failures)
    assert any("business_erp_permission" in reason for reason in failures)


def test_company_director_may_also_use_the_pos_without_cutover_role_failure() -> None:
    def get_all(doctype, **_kwargs):
        if doctype == "KoPOS Staff Outlet":
            return [{"parent": "ACCESS-1"}]
        if doctype == "KoPOS Staff Access":
            return [{"user": "director@example.com"}]
        raise AssertionError(doctype)

    with (
        patch.object(frappe, "get_all", side_effect=get_all),
        patch.object(
            frappe, "get_roles", return_value=["Company Director", "Stock Manager"]
        ),
    ):
        failures = outlet_erp_role_failures(
            company="JiJi Sdn Bhd", warehouse="Outlet A"
        )

    assert failures == ()
