import importlib
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


install_fake_frappe_modules()
install_module = importlib.import_module("kopos_connector.install.install")
shifts = importlib.import_module("kopos_connector.api.shifts")
cash_service = importlib.import_module(
    "kopos_connector.kopos.services.accounting.return_invoice_service"
)


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
        self.db_set_calls = []

    def get(self, key, default=None):
        return getattr(self, key, default)

    def insert(self, ignore_permissions=False):
        self.insert_calls.append(ignore_permissions)
        return self

    def submit(self):
        self.submit_calls += 1
        return self

    def db_set(self, fieldname, value, update_modified=True):
        self.db_set_calls.append((fieldname, value, update_modified))
        setattr(self, fieldname, value)
        return self


class ShiftSyncTests(unittest.TestCase):
    def test_offline_event_time_converts_equivalent_utc_to_site_local(self):
        server_now = datetime(2026, 3, 13, 18, 0, 0)

        with patch.object(shifts, "now_datetime", return_value=server_now):
            parsed = shifts._normalize_offline_event_datetime(
                datetime(2026, 3, 13, 10, 0, 0, tzinfo=timezone.utc),
                "opened_at",
            )

        self.assertEqual(parsed, datetime(2026, 3, 13, 18, 0, 0))
        self.assertIsNone(parsed.tzinfo)

    def test_offline_event_time_preserves_hours_old_timestamp(self):
        server_now = datetime(2026, 3, 13, 18, 0, 0)

        with patch.object(shifts, "now_datetime", return_value=server_now):
            parsed = shifts._normalize_offline_event_datetime(
                "2026-03-13T02:00:00Z",
                "opened_at",
            )

        self.assertEqual(parsed, datetime(2026, 3, 13, 10, 0, 0))

    def test_offline_event_time_rejects_excessive_future_skew(self):
        server_now = datetime(2026, 3, 13, 18, 0, 0)

        for field_name in ("opened_at", "closed_at"):
            with (
                self.subTest(field_name=field_name),
                patch.object(shifts, "now_datetime", return_value=server_now),
                self.assertRaises(shifts.frappe.ValidationError) as error,
            ):
                shifts._normalize_offline_event_datetime(
                    "2026-03-13T10:05:01Z",
                    field_name,
                )

            self.assertIn("more than 5 minutes in the future", str(error.exception))

    def test_offline_open_and_close_times_can_sync_in_order(self):
        server_now = datetime(2026, 3, 14, 9, 0, 0)

        with patch.object(shifts, "now_datetime", return_value=server_now):
            opened_at = shifts._normalize_offline_event_datetime(
                "2026-03-13T02:00:00Z",
                "opened_at",
            )
            closed_at = shifts._normalize_offline_event_datetime(
                "2026-03-13T09:00:00Z",
                "closed_at",
            )

        shift_doc = make_doc(name="FB-SHIFT-1", opened_at=opened_at)
        shifts._validate_closed_at_not_before_opened_at(shift_doc, closed_at)

        self.assertEqual(opened_at, datetime(2026, 3, 13, 10, 0, 0))
        self.assertEqual(closed_at, datetime(2026, 3, 13, 17, 0, 0))

    def test_close_time_rejects_timestamp_before_persisted_open_time(self):
        shift_doc = make_doc(
            name="FB-SHIFT-1",
            opened_at=datetime(2026, 3, 13, 10, 0, 0),
        )

        with self.assertRaises(shifts.frappe.ValidationError) as error:
            shifts._validate_closed_at_not_before_opened_at(
                shift_doc,
                datetime(2026, 3, 13, 9, 59, 59),
            )

        self.assertIn("closed_at cannot be before", str(error.exception))

    def test_coerce_to_site_local_naive_converts_utc_for_storage(self):
        converted = shifts._coerce_to_site_local_naive(
            datetime(2026, 3, 13, 12, 15, 15, 88000, tzinfo=timezone.utc)
        )

        self.assertEqual(converted.tzinfo, None)
        self.assertEqual(converted, datetime(2026, 3, 13, 20, 15, 15, 88000))

    def test_create_kopos_custom_fields_excludes_legacy_shift_documents(self):
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

        self.assertTrue(captured["update"])
        self.assertEqual(
            set(captured["custom_fields"]),
            {"Item", "POS Profile", "Sales Invoice", "Sales Invoice Item"},
        )
        invoice_item_fields = {
            field["fieldname"]
            for field in captured["custom_fields"]["Sales Invoice Item"]
        }
        self.assertEqual(
            invoice_item_fields,
            {
                "custom_kopos_modifiers",
                "custom_kopos_modifier_total",
                "custom_kopos_has_modifiers",
                "custom_kopos_promotion_allocation",
            },
        )

    def test_open_shift_stores_custom_identity_fields(self):
        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            warehouse="Main Warehouse",
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
        fb_shift_doc = MutableDoc(name="FB-SHIFT-1")

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
            patch.object(shifts.frappe, "new_doc", return_value=fb_shift_doc),
        ):
            result = shifts.open_shift_payload(
                {
                    "idempotency_key": "shift-open-SHIFT-1",
                    "device_id": "DEVICE-1",
                    "staff_id": "john@example.com",
                    "shift_id": "SHIFT-1",
                    "opening_float_sen": 5000,
                    "opened_at": "2026-03-13T02:00:00Z",
                }
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["fb_shift"], "FB-SHIFT-1")
        self.assertEqual(fb_shift_doc.shift_code, "SHIFT-1")
        self.assertEqual(fb_shift_doc.device_id, "DEVICE-1")
        self.assertEqual(fb_shift_doc.staff_id, "john@example.com")
        self.assertEqual(fb_shift_doc.opening_float, Decimal("50"))
        self.assertEqual(fb_shift_doc.expected_cash, Decimal("50"))
        self.assertEqual(fb_shift_doc.opened_at, datetime(2026, 3, 13, 10, 0, 0))
        self.assertIn("KoPOS shift_id: SHIFT-1", fb_shift_doc.remarks)

    def test_open_shift_rejects_fractional_opening_float_sen(self):
        with self.assertRaises(shifts.frappe.ValidationError) as error:
            shifts.open_shift_payload(
                {
                    "idempotency_key": "shift-open-SHIFT-1",
                    "device_id": "DEVICE-1",
                    "staff_id": "john@example.com",
                    "shift_id": "SHIFT-1",
                    "opening_float_sen": "5000.5",
                }
            )

        self.assertIn("must be an integer number of sen", str(error.exception))

    def test_close_shift_resolves_opening_entry_by_shift_id_and_device(self):
        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            warehouse="Main Warehouse",
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
        opening_entry = MutableDoc(
            name="FB-SHIFT-1",
            docstatus=1,
            status="Open",
            company="JiJi",
            device_id="DEVICE-1",
            staff_id="john@example.com",
            shift_code="SHIFT-1",
            expected_cash=50.0,
            opened_at=datetime(2026, 3, 13, 10, 0, 0),
            remarks="",
        )
        closing_doc = MutableDoc(
            name="CLOSE-1",
            custom_kopos_idempotency_key=None,
            custom_kopos_shift_id=None,
            custom_kopos_device_id=None,
        )

        def fake_get_doc(*args, **kwargs):
            if args[:2] == ("FB Shift", "FB-SHIFT-1"):
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
            if doctype == "FB Shift" and fieldname == "expected_cash":
                return opening_entry.expected_cash
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
            patch.object(cash_service, "refresh_fb_shift_cash", return_value=None),
            patch.object(
                shifts, "_find_fb_shift_name", return_value="FB-SHIFT-1"
            ) as find_open_mock,
        ):
            result = shifts.close_shift_payload(
                {
                    "idempotency_key": "shift-close-SHIFT-1",
                    "device_id": "DEVICE-1",
                    "staff_id": "john@example.com",
                    "shift_id": "SHIFT-1",
                    "counted_cash_sen": 6500,
                    "closed_at": "2026-03-13T09:00:00Z",
                }
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["fb_shift"], "FB-SHIFT-1")
        self.assertEqual(opening_entry.status, "Closed")
        self.assertEqual(opening_entry.counted_cash, 65.0)
        self.assertEqual(opening_entry.cash_variance, 15.0)
        self.assertEqual(opening_entry.closed_at, datetime(2026, 3, 13, 17, 0, 0))
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
            name="FB-SHIFT-1",
            docstatus=1,
            status="Open",
            company="JiJi",
            device_id="DEVICE-2",
            staff_id="john@example.com",
            shift_code="SHIFT-1",
            expected_cash=50.0,
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
            patch.object(shifts, "_find_fb_shift_name", return_value="FB-SHIFT-1"),
            patch.object(shifts.frappe, "get_doc", return_value=opening_entry),
        ):
            with self.assertRaises(shifts.frappe.ValidationError):
                shifts.close_shift_payload(
                    {
                        "idempotency_key": "shift-close-SHIFT-1",
                        "device_id": "DEVICE-1",
                        "staff_id": "john@example.com",
                        "shift_id": "SHIFT-1",
                        "fb_shift": "FB-SHIFT-1",
                        "counted_cash_sen": 6500,
                        "closed_at": "2026-03-13T10:10:00Z",
                    }
                )

    def test_close_shift_does_not_fallback_to_later_open_shift_for_same_device(self):
        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            warehouse="Main Warehouse",
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

        def fake_get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
            if doctype == "KoPOS Device":
                return 1
            if doctype == "User":
                return 1
            return None

        with (
            patch.object(shifts, "get_device_doc", return_value=device_doc),
            patch.object(shifts.frappe.db, "get_value", side_effect=fake_get_value),
            patch.object(
                shifts.frappe.db,
                "exists",
                side_effect=lambda doctype, *_args, **_kwargs: doctype == "User",
            ),
            patch.object(
                shifts, "_find_fb_shift_name", return_value=None
            ) as find_open_mock,
        ):
            with self.assertRaises(shifts.frappe.ValidationError) as ctx:
                shifts.close_shift_payload(
                    {
                        "idempotency_key": "shift-close-SHIFT-OLD",
                        "device_id": "DEVICE-1",
                        "staff_id": "john@example.com",
                        "shift_id": "SHIFT-OLD",
                        "counted_cash_sen": 6500,
                        "closed_at": "2026-03-13T10:10:00Z",
                    }
                )

        self.assertIn(
            "No open FB Shift found for device DEVICE-1", str(ctx.exception)
        )
        find_open_mock.assert_called_once_with("SHIFT-OLD")

    def test_get_device_open_shift_returns_none_after_close_only(self):
        device_doc = make_doc(
            name="DEVICE-1",
            device_id="DEVICE-1",
            pos_profile="Counter 1",
        )

        with (
            patch.object(shifts, "get_device_doc", return_value=device_doc),
            patch.object(shifts.frappe, "get_all", return_value=[]),
        ):
            result = shifts.get_device_open_shift_payload("DEVICE-1")

        self.assertIsNone(result)

    def test_get_device_open_shift_returns_explicit_new_shift_after_close_only(self):
        device_doc = make_doc(
            name="DEVICE-1",
            device_id="DEVICE-1",
            pos_profile="Counter 1",
        )

        with (
            patch.object(shifts, "get_device_doc", return_value=device_doc),
            patch.object(
                shifts.frappe,
                "get_all",
                return_value=[
                    {
                        "name": "FB-SHIFT-2",
                        "staff_id": "john@example.com",
                        "opened_at": "2026-03-13 10:20:00",
                        "shift_code": "SHIFT-2",
                        "device_id": "DEVICE-1",
                        "opening_float": 50.0,
                    }
                ],
            ),
        ):
            result = shifts.get_device_open_shift_payload("DEVICE-1")

        self.assertEqual(
            result,
            {
                "fb_shift": "FB-SHIFT-2",
                "shift_id": "SHIFT-2",
                "device_id": "DEVICE-1",
                "staff_id": "john@example.com",
                "opening_float_sen": 5000,
                "opened_at": "2026-03-13 10:20:00",
            },
        )

    # -------------------------------------------------------------------------
    # Phase 1 & 2 Security Tests - Identity and Permission Enforcement
    # -------------------------------------------------------------------------

    def test_open_shift_succeeds_for_assigned_active_user_with_permission(self):
        """Open shift should succeed for an assigned, active user with can_open_shift=True."""
        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            warehouse="Main Warehouse",
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
        fb_shift_doc = MutableDoc(name="FB-SHIFT-1")

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
            patch.object(shifts.frappe, "new_doc", return_value=fb_shift_doc),
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
        self.assertEqual(result["fb_shift"], "FB-SHIFT-1")

    def test_open_shift_fails_for_unassigned_user(self):
        """Open shift should fail if the user is not assigned to the device."""
        pos_profile = make_doc(
            name="Counter 1",
            company="JiJi",
            warehouse="Main Warehouse",
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
            warehouse="Main Warehouse",
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
            warehouse="Main Warehouse",
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
            warehouse="Main Warehouse",
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
            warehouse="Main Warehouse",
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
            warehouse="Main Warehouse",
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
            name="FB-SHIFT-1",
            shift_code="CORRECT-SHIFT",
            device_id="DEVICE-1",
            staff_id="john@example.com",
            company="JiJi",
            status="Open",
            docstatus=1,
            expected_cash=50.0,
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
            patch.object(shifts, "_find_fb_shift_name", return_value="FB-SHIFT-1"),
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
            warehouse="Main Warehouse",
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
            warehouse="Main Warehouse",
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
            "resource_id": "SHIFT-1",
            "amount_sen": 5000,
            "context_hash": manager_approval.canonical_context_hash({"reason": ""}),
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
            patch.object(
                manager_approval,
                "_load_approval_for_update",
                return_value={"name": "reused-token-id-123", "status": "consumed"},
            ),
            patch.object(manager_approval, "_validate_persisted_approval"),
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
