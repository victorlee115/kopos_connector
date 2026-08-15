from types import SimpleNamespace
from unittest.mock import patch

from kopos_connector.kopos.services.inventory_autopilot.staff_access import (
    _serialize_central_row,
    _serialize_legacy_row,
)
from kopos_connector.utils.pin import hash_pin


def test_central_manager_access_derives_inventory_capabilities_and_keeps_default_hint():
    legacy = SimpleNamespace(default_cashier=1, pin_hash=hash_pin("2345"))
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
        pin_hash=hash_pin("1234"),
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
