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
            return None

        with (
            patch.object(
                shifts,
                "get_device_doc",
                return_value=make_doc(name="DEVICE-1", pos_profile="Counter 1"),
            ),
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
            return None

        with (
            patch.object(
                shifts,
                "get_device_doc",
                return_value=make_doc(name="DEVICE-1", pos_profile="Counter 1"),
            ),
            patch.object(shifts.frappe.db, "get_value", side_effect=fake_get_value),
            patch.object(shifts.frappe.db, "exists", return_value=False),
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
                    "closed_at": "2026-03-13T18:00:00Z",
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
            return None

        with (
            patch.object(
                shifts,
                "get_device_doc",
                return_value=make_doc(name="DEVICE-1", pos_profile="Counter 1"),
            ),
            patch.object(shifts.frappe.db, "get_value", side_effect=fake_get_value),
            patch.object(shifts.frappe.db, "exists", return_value=False),
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
                        "closed_at": "2026-03-13T18:00:00Z",
                    }
                )


if __name__ == "__main__":
    unittest.main()
