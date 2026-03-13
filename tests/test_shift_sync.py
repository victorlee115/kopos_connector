import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


install_fake_frappe_modules()
install_module = importlib.import_module("kopos_connector.install.install")
shifts = importlib.import_module("kopos_connector.api.shifts")


def make_doc(**kwargs):
    doc = SimpleNamespace(**kwargs)
    setattr(doc, "get", lambda key, default=None: getattr(doc, key, default))
    return doc


def make_device_user(
    user: str,
    *,
    active: bool = True,
    can_open_shift: bool = True,
    can_close_shift: bool = True,
    display_name: str = "",
    pin_hash: str = "hashed-pin",
):
    """Helper to create a device user row for testing."""
    return make_doc(
        user=user,
        active=1 if active else 0,
        can_open_shift=1 if can_open_shift else 0,
        can_close_shift=1 if can_close_shift else 0,
        display_name=display_name or user,
        pin_hash=pin_hash,
    )


class MutableDoc(SimpleNamespace):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.insert_calls = []
        self.submit_calls = 0

    def get(self, key, default=None):
        return getattr(self, key, default)

    def insert(self, ignore_permissions=False):
        self.insert_calls.append(ignore_permissions)
        return self

    def submit(self):
        self.submit_calls += 1
        return self


class ShiftSyncTests(unittest.TestCase):
    def test_create_kopos_custom_fields_includes_shift_tracking_fields(self):
        captured = {}

        def fake_create_custom_fields(custom_fields, update=False):
            captured["custom_fields"] = custom_fields
            captured["update"] = update

        with (
            patch.object(
                install_module,
                "create_custom_fields",
                side_effect=fake_create_custom_fields,
            ),
            patch.object(install_module.frappe.db, "commit", return_value=None),
            patch.object(
                install_module.frappe, "flags", SimpleNamespace(), create=True
            ),
        ):
            install_module.create_kopos_custom_fields()

        opening_fields = {
            field["fieldname"]
            for field in captured["custom_fields"]["POS Opening Entry"]
        }
        closing_fields = {
            field["fieldname"]
            for field in captured["custom_fields"]["POS Closing Entry"]
        }

        self.assertTrue(captured["update"])
        self.assertTrue(
            {
                "custom_kopos_idempotency_key",
                "custom_kopos_shift_id",
                "custom_kopos_device_id",
            }.issubset(opening_fields)
        )
        self.assertTrue(
            {
                "custom_kopos_idempotency_key",
                "custom_kopos_shift_id",
                "custom_kopos_device_id",
            }.issubset(closing_fields)
        )

    def test_open_shift_stores_custom_identity_fields(self):
        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            payments=[make_doc(mode_of_payment="Cash", default=1)],
        )
        device_doc = make_doc(
            name="DEVICE-1",
            device_id="DEVICE-1",
            pos_profile="Counter 1",
            device_users=[
                make_device_user(
                    user="john@example.com",
                    active=True,
                    can_open_shift=True,
                    can_close_shift=True,
                )
            ],
        )
        opening_doc = MutableDoc(
            name="OPEN-1",
            custom_kopos_idempotency_key=None,
            custom_kopos_shift_id=None,
            custom_kopos_device_id=None,
        )

        def fake_get_doc(*args, **kwargs):
            if args and isinstance(args[0], dict):
                for key, value in args[0].items():
                    setattr(opening_doc, key, value)
                return opening_doc
            raise AssertionError(f"unexpected get_doc call: {args}")

        def fake_get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
            if doctype == "KoPOS Device":
                return 1
            if doctype == "User":
                return 1  # enabled
            return None

        with (
            patch.object(shifts, "get_device_doc", return_value=device_doc),
            patch.object(shifts.frappe.db, "get_value", side_effect=fake_get_value),
            patch.object(
                shifts.frappe.db,
                "exists",
                side_effect=lambda doctype, *_args, **_kwargs: doctype == "User",
            ),
            patch.object(shifts.frappe, "get_cached_doc", return_value=pos_profile),
            patch.object(shifts.frappe, "get_doc", side_effect=fake_get_doc),
        ):
            result = shifts.open_shift_payload(
                {
                    "idempotency_key": "shift-open-SHIFT-1",
                    "device_id": "DEVICE-1",
                    "staff_id": "john@example.com",
                    "shift_id": "SHIFT-1",
                    "opening_float_sen": 5000,
                    "opened_at": "2026-03-13T10:00:00Z",
                }
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pos_opening_entry"], "OPEN-1")
        self.assertEqual(opening_doc.custom_kopos_idempotency_key, "shift-open-SHIFT-1")
        self.assertEqual(opening_doc.custom_kopos_shift_id, "SHIFT-1")
        self.assertEqual(opening_doc.custom_kopos_device_id, "DEVICE-1")
        self.assertEqual(opening_doc.balance_details[0]["mode_of_payment"], "Cash")
        self.assertIn("KoPOS shift_id: SHIFT-1", opening_doc.remarks)

    def test_close_shift_resolves_opening_entry_by_shift_id_and_device(self):
        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            payments=[make_doc(mode_of_payment="Cash", default=1)],
        )
        device_doc = make_doc(
            name="DEVICE-1",
            device_id="DEVICE-1",
            pos_profile="Counter 1",
            device_users=[
                make_device_user(
                    user="john@example.com",
                    active=True,
                    can_open_shift=True,
                    can_close_shift=True,
                )
            ],
        )
        opening_entry = make_doc(
            name="OPEN-1",
            docstatus=1,
            status="Open",
            pos_profile="Counter 1",
            company="JiJi",
            user="john@example.com",
            balance_details=[make_doc(mode_of_payment="Cash", opening_amount=50.0)],
            custom_kopos_device_id="DEVICE-1",
            custom_kopos_shift_id="SHIFT-1",
        )
        closing_doc = MutableDoc(
            name="CLOSE-1",
            custom_kopos_idempotency_key=None,
            custom_kopos_shift_id=None,
            custom_kopos_device_id=None,
        )

        def fake_get_doc(*args, **kwargs):
            if args[:2] == ("POS Opening Entry", "OPEN-1"):
                return opening_entry
            if args and isinstance(args[0], dict):
                for key, value in args[0].items():
                    setattr(closing_doc, key, value)
                return closing_doc
            raise AssertionError(f"unexpected get_doc call: {args}")

        def fake_get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
            if doctype == "KoPOS Device":
                return 1
            if doctype == "User":
                return 1  # enabled
            return None

        with (
            patch.object(shifts, "get_device_doc", return_value=device_doc),
            patch.object(shifts.frappe.db, "get_value", side_effect=fake_get_value),
            patch.object(
                shifts.frappe.db,
                "exists",
                side_effect=lambda doctype, *_args, **_kwargs: doctype == "User",
            ),
            patch.object(shifts.frappe, "get_cached_doc", return_value=pos_profile),
            patch.object(shifts.frappe, "get_doc", side_effect=fake_get_doc),
            patch.object(
                shifts, "_find_opening_entry_name", return_value="OPEN-1"
            ) as find_open_mock,
            patch.object(shifts, "_find_closing_entry_name", return_value=None),
        ):
            result = shifts.close_shift_payload(
                {
                    "idempotency_key": "shift-close-SHIFT-1",
                    "device_id": "DEVICE-1",
                    "staff_id": "john@example.com",
                    "shift_id": "SHIFT-1",
                    "counted_cash_sen": 6500,
                    "closed_at": "2026-03-13T10:10:00Z",
                }
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pos_opening_entry"], "OPEN-1")
        self.assertEqual(result["pos_closing_entry"], "CLOSE-1")
        self.assertEqual(closing_doc.custom_kopos_shift_id, "SHIFT-1")
        self.assertEqual(closing_doc.custom_kopos_device_id, "DEVICE-1")
        self.assertEqual(closing_doc.balance_details[0]["closing_amount"], 65.0)
        self.assertEqual(find_open_mock.call_count, 1)

    def test_close_shift_rejects_opening_entry_from_another_device(self):
        device_doc = make_doc(
            name="DEVICE-1",
            device_id="DEVICE-1",
            pos_profile="Counter 1",
            device_users=[
                make_device_user(
                    user="john@example.com",
                    active=True,
                    can_open_shift=True,
                    can_close_shift=True,
                )
            ],
        )
        opening_entry = make_doc(
            name="OPEN-1",
            docstatus=1,
            status="Open",
            pos_profile="Counter 1",
            company="JiJi",
            user="john@example.com",
            balance_details=[make_doc(mode_of_payment="Cash", opening_amount=50.0)],
            custom_kopos_device_id="DEVICE-2",
            custom_kopos_shift_id="SHIFT-1",
        )

        def fake_get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
            if doctype == "KoPOS Device":
                return 1
            if doctype == "User":
                return 1  # enabled
            return None

        with (
            patch.object(shifts, "get_device_doc", return_value=device_doc),
            patch.object(shifts.frappe.db, "get_value", side_effect=fake_get_value),
            patch.object(
                shifts.frappe.db,
                "exists",
                side_effect=lambda doctype, *_args, **_kwargs: doctype == "User",
            ),
            patch.object(shifts, "_find_closing_entry_name", return_value=None),
            patch.object(shifts.frappe, "get_doc", return_value=opening_entry),
        ):
            with self.assertRaises(shifts.frappe.ValidationError):
                shifts.close_shift_payload(
                    {
                        "idempotency_key": "shift-close-SHIFT-1",
                        "device_id": "DEVICE-1",
                        "staff_id": "john@example.com",
                        "shift_id": "SHIFT-1",
                        "pos_opening_entry": "OPEN-1",
                        "counted_cash_sen": 6500,
                        "closed_at": "2026-03-13T10:10:00Z",
                    }
                )

    # -------------------------------------------------------------------------
    # Phase 1 & 2 Security Tests - Identity and Permission Enforcement
    # -------------------------------------------------------------------------

    def test_open_shift_succeeds_for_assigned_active_user_with_permission(self):
        """Open shift should succeed for an assigned, active user with can_open_shift=True."""
        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            payments=[make_doc(mode_of_payment="Cash", default=1)],
        )
        device_doc = make_doc(
            name="DEVICE-1",
            device_id="DEVICE-1",
            pos_profile="Counter 1",
            device_users=[
                make_device_user(
                    user="john@example.com",
                    active=True,
                    can_open_shift=True,
                    can_close_shift=True,
                )
            ],
        )
        opening_doc = MutableDoc(
            name="OPEN-1",
            custom_kopos_idempotency_key=None,
            custom_kopos_shift_id=None,
            custom_kopos_device_id=None,
        )

        def fake_get_doc(*args, **kwargs):
            if args and isinstance(args[0], dict):
                for key, value in args[0].items():
                    setattr(opening_doc, key, value)
                return opening_doc
            raise AssertionError(f"unexpected get_doc call: {args}")

        def fake_get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
            if doctype == "KoPOS Device":
                return 1  # enabled
            if doctype == "User":
                return 1  # enabled
            return None

        with (
            patch.object(shifts, "get_device_doc", return_value=device_doc),
            patch.object(shifts.frappe.db, "get_value", side_effect=fake_get_value),
            patch.object(
                shifts.frappe.db,
                "exists",
                side_effect=lambda doctype, *_args, **_kwargs: doctype == "User",
            ),
            patch.object(shifts.frappe, "get_cached_doc", return_value=pos_profile),
            patch.object(shifts.frappe, "get_doc", side_effect=fake_get_doc),
        ):
            result = shifts.open_shift_payload(
                {
                    "idempotency_key": "shift-open-SHIFT-1",
                    "device_id": "DEVICE-1",
                    "staff_id": "john@example.com",
                    "shift_id": "SHIFT-1",
                    "opening_float_sen": 5000,
                    "opened_at": "2026-03-13T10:00:00Z",
                }
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pos_opening_entry"], "OPEN-1")

    def test_open_shift_fails_for_unassigned_user(self):
        """Open shift should fail if the user is not assigned to the device."""
        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            payments=[make_doc(mode_of_payment="Cash", default=1)],
        )
        device_doc = make_doc(
            name="DEVICE-1",
            device_id="DEVICE-1",
            pos_profile="Counter 1",
            device_users=[
                make_device_user(
                    user="john@example.com",
                    active=True,
                    can_open_shift=True,
                )
            ],
        )

        with (
            patch.object(shifts, "get_device_doc", return_value=device_doc),
            patch.object(
                shifts.frappe.db, "get_value", return_value=1
            ),  # device enabled
            patch.object(shifts.frappe, "get_cached_doc", return_value=pos_profile),
        ):
            with self.assertRaises(shifts.frappe.ValidationError) as ctx:
                shifts.open_shift_payload(
                    {
                        "idempotency_key": "shift-open-SHIFT-1",
                        "device_id": "DEVICE-1",
                        "staff_id": "unassigned@example.com",  # Not in device_users
                        "shift_id": "SHIFT-1",
                        "opening_float_sen": 5000,
                    }
                )
            self.assertIn("not assigned", str(ctx.exception))

    def test_open_shift_fails_for_disabled_erp_user(self):
        """Open shift should fail if the ERP user is disabled."""
        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            payments=[make_doc(mode_of_payment="Cash", default=1)],
        )
        device_doc = make_doc(
            name="DEVICE-1",
            device_id="DEVICE-1",
            pos_profile="Counter 1",
            device_users=[
                make_device_user(
                    user="john@example.com",
                    active=True,
                    can_open_shift=True,
                )
            ],
        )

        def fake_get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
            if doctype == "KoPOS Device":
                return 1  # enabled
            if doctype == "User":
                return 0  # disabled!
            return None

        with (
            patch.object(shifts, "get_device_doc", return_value=device_doc),
            patch.object(shifts.frappe.db, "get_value", side_effect=fake_get_value),
            patch.object(
                shifts.frappe.db,
                "exists",
                side_effect=lambda doctype, *_args, **_kwargs: doctype == "User",
            ),
            patch.object(shifts.frappe, "get_cached_doc", return_value=pos_profile),
        ):
            with self.assertRaises(shifts.frappe.ValidationError) as ctx:
                shifts.open_shift_payload(
                    {
                        "idempotency_key": "shift-open-SHIFT-1",
                        "device_id": "DEVICE-1",
                        "staff_id": "john@example.com",
                        "shift_id": "SHIFT-1",
                        "opening_float_sen": 5000,
                    }
                )
            self.assertIn("disabled", str(ctx.exception))

    def test_open_shift_fails_when_can_open_shift_is_false(self):
        """Open shift should fail if can_open_shift is False for the device user."""
        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            payments=[make_doc(mode_of_payment="Cash", default=1)],
        )
        device_doc = make_doc(
            name="DEVICE-1",
            device_id="DEVICE-1",
            pos_profile="Counter 1",
            device_users=[
                make_device_user(
                    user="john@example.com",
                    active=True,
                    can_open_shift=False,  # Not allowed to open shifts!
                    can_close_shift=True,
                )
            ],
        )

        def fake_get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
            if doctype == "KoPOS Device":
                return 1  # enabled
            if doctype == "User":
                return 1  # enabled
            return None

        with (
            patch.object(shifts, "get_device_doc", return_value=device_doc),
            patch.object(shifts.frappe.db, "get_value", side_effect=fake_get_value),
            patch.object(
                shifts.frappe.db,
                "exists",
                side_effect=lambda doctype, *_args, **_kwargs: doctype == "User",
            ),
            patch.object(shifts.frappe, "get_cached_doc", return_value=pos_profile),
        ):
            with self.assertRaises(shifts.frappe.ValidationError) as ctx:
                shifts.open_shift_payload(
                    {
                        "idempotency_key": "shift-open-SHIFT-1",
                        "device_id": "DEVICE-1",
                        "staff_id": "john@example.com",
                        "shift_id": "SHIFT-1",
                        "opening_float_sen": 5000,
                    }
                )
            self.assertIn("not authorized to open shifts", str(ctx.exception))

    def test_close_shift_fails_when_can_close_shift_is_false(self):
        """Close shift should fail if can_close_shift is False for the device user."""
        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            payments=[make_doc(mode_of_payment="Cash", default=1)],
        )
        device_doc = make_doc(
            name="DEVICE-1",
            device_id="DEVICE-1",
            pos_profile="Counter 1",
            device_users=[
                make_device_user(
                    user="john@example.com",
                    active=True,
                    can_open_shift=True,
                    can_close_shift=False,  # Not allowed to close shifts!
                )
            ],
        )

        def fake_get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
            if doctype == "KoPOS Device":
                return 1  # enabled
            if doctype == "User":
                return 1  # enabled
            return None

        with (
            patch.object(shifts, "get_device_doc", return_value=device_doc),
            patch.object(shifts.frappe.db, "get_value", side_effect=fake_get_value),
            patch.object(
                shifts.frappe.db,
                "exists",
                side_effect=lambda doctype, *_args, **_kwargs: doctype == "User",
            ),
            patch.object(shifts.frappe, "get_cached_doc", return_value=pos_profile),
        ):
            with self.assertRaises(shifts.frappe.ValidationError) as ctx:
                shifts.close_shift_payload(
                    {
                        "idempotency_key": "shift-close-SHIFT-1",
                        "device_id": "DEVICE-1",
                        "staff_id": "john@example.com",
                        "shift_id": "SHIFT-1",
                        "counted_cash_sen": 6500,
                    }
                )
            self.assertIn("not authorized to close shifts", str(ctx.exception))

    def test_open_shift_fails_for_inactive_device_user(self):
        """Open shift should fail if the device user row is inactive."""
        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            payments=[make_doc(mode_of_payment="Cash", default=1)],
        )
        device_doc = make_doc(
            name="DEVICE-1",
            device_id="DEVICE-1",
            pos_profile="Counter 1",
            device_users=[
                make_device_user(
                    user="john@example.com",
                    active=False,  # Inactive on this device!
                    can_open_shift=True,
                )
            ],
        )

        def fake_get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
            if doctype == "KoPOS Device":
                return 1  # enabled
            if doctype == "User":
                return 1  # enabled
            return None

        with (
            patch.object(shifts, "get_device_doc", return_value=device_doc),
            patch.object(shifts.frappe.db, "get_value", side_effect=fake_get_value),
            patch.object(
                shifts.frappe.db,
                "exists",
                side_effect=lambda doctype, *_args, **_kwargs: doctype == "User",
            ),
            patch.object(shifts.frappe, "get_cached_doc", return_value=pos_profile),
        ):
            with self.assertRaises(shifts.frappe.ValidationError) as ctx:
                shifts.open_shift_payload(
                    {
                        "idempotency_key": "shift-open-SHIFT-1",
                        "device_id": "DEVICE-1",
                        "staff_id": "john@example.com",
                        "shift_id": "SHIFT-1",
                        "opening_float_sen": 5000,
                    }
                )
            self.assertIn("not active on this device", str(ctx.exception))

    def test_close_shift_fails_for_wrong_shift_id(self):
        """Close shift should fail if shift_id doesn't match the opening entry."""
        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            payments=[make_doc(mode_of_payment="Cash", default=1)],
        )
        device_doc = make_doc(
            name="DEVICE-1",
            device_id="DEVICE-1",
            pos_profile="Counter 1",
            device_users=[
                make_device_user(
                    user="john@example.com",
                    active=True,
                    can_open_shift=True,
                    can_close_shift=True,
                )
            ],
        )
        opening_doc = MutableDoc(
            name="OPEN-1",
            custom_kopos_shift_id="CORRECT-SHIFT",
            custom_kopos_device_id="DEVICE-1",
            pos_profile="Counter 1",
            user="john@example.com",
            company="JiJi",
            status="Open",
            docstatus=1,
            balance_details=[make_doc(mode_of_payment="Cash", opening_amount=50)],
        )

        def fake_get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
            if doctype == "KoPOS Device":
                return 1  # enabled
            if doctype == "User":
                return 1  # enabled
            return None

        with (
            patch.object(shifts, "get_device_doc", return_value=device_doc),
            patch.object(shifts.frappe.db, "get_value", side_effect=fake_get_value),
            patch.object(
                shifts.frappe.db,
                "exists",
                side_effect=lambda doctype, *_args, **_kwargs: doctype == "User",
            ),
            patch.object(shifts.frappe, "get_cached_doc", return_value=pos_profile),
            patch.object(shifts, "_find_opening_entry_name", return_value="OPEN-1"),
            patch.object(shifts, "_find_closing_entry_name", return_value=None),
            patch.object(shifts.frappe, "get_doc", return_value=opening_doc),
        ):
            with self.assertRaises(shifts.frappe.ValidationError) as ctx:
                shifts.close_shift_payload(
                    {
                        "idempotency_key": "shift-close-WRONG-SHIFT",
                        "device_id": "DEVICE-1",
                        "staff_id": "john@example.com",
                        "shift_id": "WRONG-SHIFT",  # Wrong!
                        "counted_cash_sen": 6500,
                    }
                )
            self.assertIn("does not belong to shift", str(ctx.exception))

    def test_expired_manager_approval_token_is_rejected(self):
        """Expired manager approval token should be rejected."""
        manager_approval = importlib.import_module(
            "kopos_connector.utils.manager_approval"
        )

        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            payments=[make_doc(mode_of_payment="Cash", default=1)],
        )
        device_doc = make_doc(
            name="DEVICE-1",
            device_id="DEVICE-1",
            pos_profile="Counter 1",
            device_users=[
                make_device_user(
                    user="john@example.com",
                    active=True,
                    can_open_shift=True,
                )
            ],
        )
        opening_doc = MutableDoc(
            name="OPEN-1",
            custom_kopos_idempotency_key=None,
            custom_kopos_shift_id=None,
            custom_kopos_device_id=None,
        )

        def fake_get_doc(*args, **kwargs):
            if args and isinstance(args[0], dict):
                for key, value in args[0].items():
                    setattr(opening_doc, key, value)
                return opening_doc
            raise AssertionError(f"unexpected get_doc call: {args}")

        def fake_get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
            if doctype == "KoPOS Device":
                return 1
            if doctype == "User":
                return 1
            return None

        expired_token = "v1.7b22hWR1bW15Ig.v1.expired_signature"

        with (
            patch.object(shifts, "get_device_doc", return_value=device_doc),
            patch.object(shifts.frappe.db, "get_value", side_effect=fake_get_value),
            patch.object(
                shifts.frappe.db,
                "exists",
                side_effect=lambda doctype, *_args, **_kwargs: doctype == "User",
            ),
            patch.object(shifts.frappe, "get_cached_doc", return_value=pos_profile),
            patch.object(shifts.frappe, "get_doc", side_effect=fake_get_doc),
            patch.object(
                manager_approval, "_get_signing_secret", return_value="test-secret"
            ),
            patch.object(manager_approval.time, "time", return_value=9999999999),
        ):
            with self.assertRaises(shifts.frappe.ValidationError) as ctx:
                shifts.open_shift_payload(
                    {
                        "idempotency_key": "shift-open-SHIFT-1",
                        "device_id": "DEVICE-1",
                        "staff_id": "john@example.com",
                        "shift_id": "SHIFT-1",
                        "opening_float_sen": 5000,
                        "manager_approval_token": expired_token,
                    }
                )
            self.assertIn("token", str(ctx.exception).lower())

    def test_reused_manager_approval_token_is_rejected(self):
        """Reused manager approval token should be rejected."""
        manager_approval = importlib.import_module(
            "kopos_connector.utils.manager_approval"
        )

        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            payments=[make_doc(mode_of_payment="Cash", default=1)],
        )
        device_doc = make_doc(
            name="DEVICE-1",
            device_id="DEVICE-1",
            pos_profile="Counter 1",
            device_users=[
                make_device_user(
                    user="john@example.com",
                    active=True,
                    can_open_shift=True,
                )
            ],
        )
        opening_doc = MutableDoc(
            name="OPEN-1",
            custom_kopos_idempotency_key=None,
            custom_kopos_shift_id=None,
            custom_kopos_device_id=None,
        )

        def fake_get_doc(*args, **kwargs):
            if args and isinstance(args[0], dict):
                for key, value in args[0].items():
                    setattr(opening_doc, key, value)
                return opening_doc
            raise AssertionError(f"unexpected get_doc call: {args}")

        def fake_get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
            if doctype == "KoPOS Device":
                return 1
            if doctype == "User":
                return 1
            return None

        current_time = int(1700000000)
        payload = {
            "device_id": "DEVICE-1",
            "staff_id": "john@example.com",
            "action": "open_shift",
            "manager_id": "manager@example.com",
            "shift_id": "SHIFT-1",
            "issued_at": current_time - 60,
            "expires_at": current_time + 300,
            "token_id": "reused-token-id-123",
        }

        with (
            patch.object(shifts, "get_device_doc", return_value=device_doc),
            patch.object(shifts.frappe.db, "get_value", side_effect=fake_get_value),
            patch.object(
                shifts.frappe.db,
                "exists",
                side_effect=lambda doctype, *_args, **_kwargs: doctype == "User",
            ),
            patch.object(shifts.frappe, "get_cached_doc", return_value=pos_profile),
            patch.object(shifts.frappe, "get_doc", side_effect=fake_get_doc),
            patch.object(
                manager_approval, "_get_signing_secret", return_value="test-secret"
            ),
            patch.object(manager_approval.time, "time", return_value=current_time),
            patch.object(manager_approval, "_is_token_reused", return_value=True),
        ):
            signature = manager_approval._create_token_signature(payload)
            token = manager_approval._encode_token(payload, signature)

            with self.assertRaises(shifts.frappe.ValidationError) as ctx:
                shifts.open_shift_payload(
                    {
                        "idempotency_key": "shift-open-SHIFT-1",
                        "device_id": "DEVICE-1",
                        "staff_id": "john@example.com",
                        "shift_id": "SHIFT-1",
                        "opening_float_sen": 5000,
                        "manager_approval_token": token,
                    }
                )
            self.assertIn("already been used", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
