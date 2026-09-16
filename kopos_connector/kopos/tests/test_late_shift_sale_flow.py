"""Sales that reach ERP after their shift closed: recorded on that shift, marked, counted there."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.api.operational_errors import ShiftRequiredError
from kopos_connector.kopos.api.fb_orders import submit_order_payload
from kopos_connector.kopos.api.money_contract import persisted_money_to_sen
from kopos_connector.kopos.tests.frappe_test_fixtures import (
    build_sen_v1_sale_payload,
    create_open_test_shift,
)

PREFIX = "KOPOS-LATE-SALE"


def _sen(value: Any) -> int:
    return persisted_money_to_sen(value or 0, "late sale test")


class TestLateShiftSaleFlow(FrappeTestCase):
    """The tablet keeps selling while ERP has already closed its shift."""

    def setUp(self) -> None:
        frappe.set_user("Administrator")
        self.shift = create_open_test_shift(prefix=PREFIX, replenish_stock=False)

    def tearDown(self) -> None:
        frappe.db.rollback()

    def _sell(self) -> dict[str, Any]:
        return submit_order_payload(build_sen_v1_sale_payload(self.shift, prefix=PREFIX))

    def _set_status(self, status: str, **fields: Any) -> None:
        frappe.db.set_value("FB Shift", self.shift.name, {"status": status, **fields}, update_modified=False)

    def test_a_sale_that_arrives_after_the_close_is_recorded_on_its_own_shift(self) -> None:
        during = self._sell()
        counted_sen = _sen(frappe.db.get_value("FB Shift", self.shift.name, "expected_cash"))
        self._set_status("Closed", counted_cash=counted_sen / 100, cash_variance=0)

        late = self._sell()

        self.assertEqual(late["status"], "ok")
        self.assertEqual(
            [frappe.db.get_value("FB Order", name, "recorded_after_shift_close") for name in (during["fb_order"], late["fb_order"])],
            [0, 1],
        )
        late_order = frappe.get_doc("FB Order", late["fb_order"])
        self.assertEqual(late_order.shift, self.shift.name)
        self.assertEqual(frappe.db.get_value("Sales Invoice", late_order.sales_invoice, "custom_fb_shift"), self.shift.name)
        # The drawer took this cash after the count, so the shift says so.
        shift = frappe.db.get_value("FB Shift", self.shift.name, ["status", "expected_cash", "counted_cash", "cash_variance"], as_dict=True)
        self.assertEqual(shift.status, "Closed")
        self.assertEqual(_sen(shift.expected_cash), counted_sen + _sen(late_order.grand_total))
        self.assertEqual(_sen(shift.cash_variance), counted_sen - _sen(shift.expected_cash))

    def test_a_shift_that_took_no_sales_still_refuses_them(self) -> None:
        self._set_status("Cancelled")

        with self.assertRaisesRegex(ShiftRequiredError, "FB Shift that took sales"):
            self._sell()
        self.assertFalse(frappe.get_all("FB Order", filters={"shift": self.shift.name}, limit=1))
