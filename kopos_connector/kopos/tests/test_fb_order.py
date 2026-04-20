from __future__ import annotations

from importlib import import_module
from unittest.mock import patch

import pytest

pytest.importorskip("frappe")

frappe = import_module("frappe")
FrappeTestCase = import_module("frappe.tests.utils").FrappeTestCase

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

    def create_submittable_test_order(self):
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
        order.append("payments", {"payment_method": "Cash", "amount": 10.0})
        order.net_total = 10.0
        order.grand_total = 10.0
        order.insert()
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

    def test_submit_logs_advisory_stock_shortfall(self):
        order = self.create_submittable_test_order()
        line_resolutions = [
            {
                "resolved_components": [
                    {
                        "item": "TEST-INGREDIENT",
                        "warehouse": self.warehouse,
                        "stock_qty": 1.25,
                        "affects_stock": 1,
                    },
                    {
                        "item": "TEST-INGREDIENT",
                        "warehouse": self.warehouse,
                        "stock_qty": 0.75,
                        "affects_stock": 1,
                    },
                ]
            }
        ]

        with (
            patch.object(
                FBOrder, "build_line_resolutions", return_value=line_resolutions
            ),
            patch.object(FBOrder, "create_resolved_sales", return_value=None),
            patch.object(FBOrder, "get_resolved_sales", return_value=[]),
            patch.object(
                FBOrder, "create_projection_entry", side_effect=["INV-LOG", "STOCK-LOG"]
            ),
            patch.object(FBOrder, "update_shift_expected_cash", return_value=None),
            patch(
                "kopos_connector.kopos.doctype.fb_order.fb_order.create_sales_invoice",
                return_value="SINV-TEST-0001",
            ),
            patch(
                "kopos_connector.kopos.doctype.fb_order.fb_order.create_ingredient_stock_entry",
                return_value=None,
            ),
            patch(
                "kopos_connector.kopos.doctype.fb_order.fb_order.update_projection_state",
                return_value=None,
            ),
            patch(
                "kopos_connector.kopos.services.inventory.warning_service.get_available_stock",
                return_value=1.0,
            ),
        ):
            order.submit()

        order.reload()
        self.assertEqual(order.docstatus, 1)
        self.assertEqual(order.status, "Submitted")

        logs = frappe.get_all(
            "FB Stock Override Log",
            filters={"fb_order": order.name},
            fields=[
                "item",
                "warehouse",
                "requested_qty",
                "available_qty_before",
                "shortfall_qty",
                "order_reference",
                "logged_at",
            ],
        )

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].item, "TEST-INGREDIENT")
        self.assertEqual(logs[0].warehouse, self.warehouse)
        self.assertEqual(logs[0].requested_qty, 2.0)
        self.assertEqual(logs[0].available_qty_before, 1.0)
        self.assertEqual(logs[0].shortfall_qty, 1.0)
        self.assertEqual(logs[0].order_reference, order.order_id)
        self.assertIsNotNone(logs[0].logged_at)

    def test_submit_still_raises_non_stock_failures(self):
        order = self.create_submittable_test_order()

        with (
            patch.object(FBOrder, "build_line_resolutions", return_value=[]),
            patch.object(
                FBOrder,
                "create_resolved_sales",
                side_effect=RuntimeError("resolved sale projection failed"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "resolved sale projection failed"
            ):
                order.submit()
