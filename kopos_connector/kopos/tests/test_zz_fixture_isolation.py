from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


class TestBroadFixtureIsolation(FrappeTestCase):
    """Keep broad tests from poisoning the subsequent business-state smoke."""

    def test_no_transactional_test_business_records_were_committed(self) -> None:
        leaked_shifts = frappe.get_all(
            "FB Shift",
            filters={"shift_code": ("like", "KOPOS-%")},
            pluck="name",
        )
        leaked_orders = frappe.get_all(
            "FB Order",
            filters={"external_idempotency_key": ("like", "KOPOS-%")},
            pluck="name",
        )

        self.assertEqual(leaked_shifts, [])
        self.assertEqual(leaked_orders, [])
