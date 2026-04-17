from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.kopos.doctype.fb_order.fb_order import FBOrder


class TestFBOrder(FrappeTestCase):
    def setUp(self):
        self.company = frappe.defaults.get_defaults().get("company", "Test Company")
        self.warehouse = "WH - Test Booth"
        self.shift = self.create_test_shift()

    def create_test_shift(self):
        shift = frappe.new_doc("FB Shift")
        shift.shift_code = f"TEST-SHIFT-{frappe.generate_hash(length=8)}"
        shift.device_id = "TEST-DEVICE-001"
        shift.staff_id = frappe.session.user
        shift.warehouse = self.warehouse
        shift.company = self.company
        shift.opening_float = 300.0
        shift.status = "Open"
        shift.insert()
        return shift.name

    def create_test_order(self):
        order = frappe.new_doc("FB Order")
        order.order_id = f"TEST-ORDER-{frappe.generate_hash(length=8)}"
        order.external_idempotency_key = f"IDEMP-{frappe.generate_hash(length=16)}"
        order.source = "API"
        order.device_id = "TEST-DEVICE-001"
        order.shift = self.shift
        order.staff_id = frappe.session.user
        order.booth_warehouse = self.warehouse
        order.company = self.company
        order.currency = "MYR"
        return order

    def test_order_validation_required_fields(self):
        order = frappe.new_doc("FB Order")

        with self.assertRaises(Exception) as context:
            order.validate()

        self.assertIn("order_id", str(context.exception))

    def test_order_validation_idempotency(self):
        idempotency_key = f"IDEMP-{frappe.generate_hash(length=16)}"

        order1 = self.create_test_order()
        order1.external_idempotency_key = idempotency_key
        order1.append(
            "items",
            {
                "line_id": "LINE-1",
                "item": "TEST-ITEM",
                "qty": 1.0,
                "uom": "Nos",
                "unit_price": 10.0,
                "line_total": 10.0,
            },
        )
        order1.append("payments", {"payment_method": "Cash", "amount": 10.0})
        order1.net_total = 10.0
        order1.grand_total = 10.0
        order1.insert()

        order2 = self.create_test_order()
        order2.external_idempotency_key = idempotency_key
        order2.append(
            "items",
            {
                "line_id": "LINE-1",
                "item": "TEST-ITEM",
                "qty": 1.0,
                "uom": "Nos",
                "unit_price": 10.0,
                "line_total": 10.0,
            },
        )
        order2.append("payments", {"payment_method": "Cash", "amount": 10.0})
        order2.net_total = 10.0
        order2.grand_total = 10.0

        with self.assertRaises(Exception) as context:
            order2.validate()

        self.assertIn("Idempotency", str(context.exception))

    def test_order_calculation_totals(self):
        order = self.create_test_order()
        order.append(
            "items",
            {
                "line_id": "LINE-1",
                "item": "TEST-ITEM-1",
                "qty": 2.0,
                "uom": "Nos",
                "unit_price": 10.0,
                "modifier_total": 2.0,
                "discount_amount": 1.0,
                "line_total": 21.0,
            },
        )
        order.append(
            "items",
            {
                "line_id": "LINE-2",
                "item": "TEST-ITEM-2",
                "qty": 1.0,
                "uom": "Nos",
                "unit_price": 15.0,
                "line_total": 15.0,
            },
        )
        order.net_total = 36.0
        order.tax_total = 0.0
        order.grand_total = 36.0
        order.append("payments", {"payment_method": "Cash", "amount": 36.0})

        order.validate()

        self.assertEqual(order.net_total, 36.0)
        self.assertEqual(order.grand_total, 36.0)

    def test_order_validation_payment_mismatch(self):
        order = self.create_test_order()
        order.append(
            "items",
            {
                "line_id": "LINE-1",
                "item": "TEST-ITEM",
                "qty": 1.0,
                "uom": "Nos",
                "unit_price": 10.0,
                "line_total": 10.0,
            },
        )
        order.net_total = 10.0
        order.grand_total = 10.0
        order.append("payments", {"payment_method": "Cash", "amount": 5.0})

        with self.assertRaises(Exception) as context:
            order.validate()

        self.assertIn("payment", str(context.exception).lower())

    def test_order_validation_accepts_rounding_adjustment(self):
        order = self.create_test_order()
        order.append(
            "items",
            {
                "line_id": "LINE-1",
                "item": "TEST-ITEM",
                "qty": 1.0,
                "uom": "Nos",
                "unit_price": 12.0,
                "line_total": 12.0,
            },
        )
        order.tax_total = 0.96
        order.rounding_adjustment = -0.01
        order.grand_total = 12.95
        order.append("payments", {"payment_method": "Cash", "amount": 12.95})

        order.validate()

        self.assertEqual(order.net_total, 12.0)
        self.assertEqual(order.tax_total, 0.96)
        self.assertEqual(order.rounding_adjustment, -0.01)
        self.assertEqual(order.grand_total, 12.95)

    def test_order_line_required_fields(self):
        order = self.create_test_order()
        order.append(
            "items",
            {
                "line_id": "",
                "item": "TEST-ITEM",
                "qty": 1.0,
                "uom": "Nos",
                "unit_price": 10.0,
                "line_total": 10.0,
            },
        )
        order.net_total = 10.0
        order.grand_total = 10.0
        order.append("payments", {"payment_method": "Cash", "amount": 10.0})

        with self.assertRaises(Exception) as context:
            order.validate()

    def test_order_line_zero_qty(self):
        order = self.create_test_order()
        order.append(
            "items",
            {
                "line_id": "LINE-1",
                "item": "TEST-ITEM",
                "qty": 0.0,
                "uom": "Nos",
                "unit_price": 10.0,
                "line_total": 0.0,
            },
        )
        order.net_total = 0.0
        order.grand_total = 0.0
        order.append("payments", {"payment_method": "Cash", "amount": 0.0})

        with self.assertRaises(Exception) as context:
            order.validate()

        self.assertIn("qty", str(context.exception).lower())

    def test_payment_required_fields(self):
        order = self.create_test_order()
        order.append(
            "items",
            {
                "line_id": "LINE-1",
                "item": "TEST-ITEM",
                "qty": 1.0,
                "uom": "Nos",
                "unit_price": 10.0,
                "line_total": 10.0,
            },
        )
        order.net_total = 10.0
        order.grand_total = 10.0
        order.append("payments", {"payment_method": "", "amount": 10.0})

        with self.assertRaises(Exception) as context:
            order.validate()

    def test_payment_zero_amount(self):
        order = self.create_test_order()
        order.append(
            "items",
            {
                "line_id": "LINE-1",
                "item": "TEST-ITEM",
                "qty": 1.0,
                "uom": "Nos",
                "unit_price": 10.0,
                "line_total": 10.0,
            },
        )
        order.net_total = 10.0
        order.grand_total = 10.0
        order.append("payments", {"payment_method": "Cash", "amount": 0.0})

        with self.assertRaises(Exception) as context:
            order.validate()

        self.assertIn("amount", str(context.exception).lower())
