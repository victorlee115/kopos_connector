from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.kopos.doctype.fb_shift.fb_shift import FBShift


class TestFBShift(FrappeTestCase):
    def setUp(self):
        self.cleanup_test_shifts()

    def tearDown(self):
        self.cleanup_test_shifts()

    def cleanup_test_shifts(self):
        frappe.db.delete("FB Shift", {"shift_code": ("like", "TEST-%")})
        frappe.db.commit()

    def create_test_shift(self, **kwargs):
        shift = frappe.new_doc("FB Shift")
        shift.shift_code = kwargs.get(
            "shift_code", f"TEST-SHIFT-{frappe.generate_hash(length=8)}"
        )
        shift.device_id = kwargs.get("device_id", "TEST-DEVICE-001")
        shift.staff_id = kwargs.get("staff_id", frappe.session.user)
        shift.warehouse = kwargs.get("warehouse", "WH - Test Booth")
        shift.company = kwargs.get(
            "company", frappe.defaults.get_defaults().get("company", "Test Company")
        )
        shift.opening_float = kwargs.get("opening_float", 300.0)
        shift.status = kwargs.get("status", "Open")
        return shift

    def test_shift_creation(self):
        shift = self.create_test_shift()
        shift.insert()

        self.assertIsNotNone(shift.name)
        self.assertEqual(shift.status, "Open")

    def test_cash_variance_calculation(self):
        shift = self.create_test_shift(
            opening_float=300.0, expected_cash=500.0, counted_cash=480.0
        )
        shift.validate()

        self.assertEqual(shift.cash_variance, -20.0)

    def test_status_transition_open_to_closing(self):
        shift = self.create_test_shift(status="Open")
        shift.insert()

        shift.status = "Closing"
        shift.validate()

        self.assertEqual(shift.status, "Closing")

    def test_status_transition_closing_to_closed(self):
        shift = self.create_test_shift(status="Open")
        shift.insert()
        shift.status = "Closing"
        shift.save()

        shift.status = "Closed"
        shift.validate()

        self.assertEqual(shift.status, "Closed")

    def test_invalid_status_transition_open_to_closed(self):
        shift = self.create_test_shift(status="Open")
        shift.insert()

        shift.status = "Closed"

        with self.assertRaises(Exception):
            shift.validate()

    def test_status_transition_closed_cannot_change(self):
        shift = self.create_test_shift(status="Open")
        shift.insert()
        shift.status = "Closing"
        shift.save()
        shift.status = "Closed"
        shift.save()

        shift.status = "Open"

        with self.assertRaises(Exception):
            shift.validate()

    def test_exception_status_from_closing(self):
        shift = self.create_test_shift(status="Open")
        shift.insert()
        shift.status = "Closing"
        shift.save()

        shift.status = "Exception"
        shift.validate()

        self.assertEqual(shift.status, "Exception")

    def test_close_blocked_reason_set(self):
        shift = self.create_test_shift(status="Open")
        shift.insert()
        shift.status = "Closing"
        shift.close_blocked_reason = "Pending order projections"
        shift.save()

        self.assertEqual(shift.close_blocked_reason, "Pending order projections")

    def test_get_shift_expected_cash(self):
        from kopos_connector.kopos.doctype.fb_shift.fb_shift import (
            get_shift_expected_cash,
        )

        shift = self.create_test_shift(
            shift_code="TEST-EXPECTED-CASH", opening_float=200.0
        )
        shift.insert()

        result = get_shift_expected_cash(shift.name)

        self.assertEqual(result["opening_float"], 200.0)
        self.assertIn("cash_sales", result)
        self.assertIn("expected_cash", result)
