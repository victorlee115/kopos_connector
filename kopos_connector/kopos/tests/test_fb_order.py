from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.kopos.doctype.fb_order.fb_order import FBOrder
from kopos_connector.kopos.tests.frappe_test_fixtures import (
    create_open_test_shift,
    ensure_canonical_test_base,
)


class TestFBOrder(FrappeTestCase):
    def setUp(self):
        self.base = ensure_canonical_test_base()
        self.company = self.base["company"]
        self.warehouse = self.base["warehouse"]
        self.item = self.base["item_code"]
        self.shift = self.create_test_shift()

    def tearDown(self):
        frappe.db.rollback()

    def create_test_shift(self):
        return create_open_test_shift(prefix="KOPOS-ORDER-TEST").name

    def create_test_order(self):
        order = frappe.new_doc("FB Order")
        order.order_id = f"TEST-ORDER-{frappe.generate_hash(length=8)}"
        order.external_idempotency_key = f"IDEMP-{frappe.generate_hash(length=16)}"
        order.source = "API"
        order.device_id = self.base["device_id"]
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
                "item": self.item,
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
                "item": self.item,
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
                "item": self.item,
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
                "item": self.item,
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
                "item": self.item,
                "qty": 1.0,
                "uom": "Nos",
                "unit_price": 15.0,
                "line_total": 15.0,
            },
        )
        order.net_total = 38.0
        order.tax_total = 0.0
        order.grand_total = 38.0
        order.append("payments", {"payment_method": "Cash", "amount": 38.0})

        order.validate()

        self.assertEqual(order.net_total, Decimal("38.00"))
        self.assertEqual(order.grand_total, Decimal("38.00"))

    def test_order_validation_payment_mismatch(self):
        order = self.create_test_order()
        order.append(
            "items",
            {
                "line_id": "LINE-1",
                "item": self.item,
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
                "item": self.item,
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

        self.assertEqual(order.net_total, Decimal("12.00"))
        self.assertEqual(order.tax_total, Decimal("0.96"))
        self.assertEqual(order.rounding_adjustment, Decimal("-0.01"))
        self.assertEqual(order.grand_total, Decimal("12.95"))

    def test_order_line_required_fields(self):
        order = self.create_test_order()
        order.append(
            "items",
            {
                "line_id": "",
                "item": self.item,
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
                "item": self.item,
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
                "item": self.item,
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
                "item": self.item,
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
        get_single_value = frappe.db.get_single_value

        def get_test_single_value(doctype, fieldname, *args, **kwargs):
            if (doctype, fieldname) == ("Stock Settings", "allow_negative_stock"):
                return 1
            return get_single_value(doctype, fieldname, *args, **kwargs)

        line_resolutions = [
            {
                "resolved_components": [
                    {
                        "item": self.item,
                        "warehouse": self.warehouse,
                        "stock_qty": 1.25,
                        "affects_stock": 1,
                    },
                    {
                        "item": self.item,
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
                FBOrder,
                "create_projection_entry",
                side_effect=["INV-LOG", "STOCK-LOG", "SHIFT-LOG"],
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
            patch.object(
                frappe.db,
                "get_single_value",
                side_effect=get_test_single_value,
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
        self.assertEqual(logs[0].item, self.item)
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
