from __future__ import annotations

from typing import Any

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.kopos.api.fb_orders import submit_order_payload
from kopos_connector.kopos.tests.frappe_test_fixtures import (
    build_sen_v1_sale_payload,
    create_open_test_shift,
)


class TestEndToEndSaleFlow(FrappeTestCase):
    def setUp(self) -> None:
        frappe.set_user("Administrator")
        self.shift = create_open_test_shift(
            prefix="KOPOS-E2E-SALE", replenish_stock=True
        )

    def tearDown(self) -> None:
        frappe.db.rollback()

    def submit_sale(
        self, *, quantity: int = 1
    ) -> tuple[dict[str, Any], dict[str, Any], Any]:
        payload = build_sen_v1_sale_payload(
            self.shift,
            prefix="KOPOS-E2E-SALE",
            quantity=quantity,
        )
        result = submit_order_payload(payload)
        return payload, result, frappe.get_doc("FB Order", result["fb_order"])

    def test_complete_sale_creates_one_submitted_sales_invoice_not_pos_invoice(
        self,
    ) -> None:
        payload, result, fb_order = self.submit_sale()

        self.assertEqual(result["status"], "ok")
        self.assertTrue(fb_order.sales_invoice)
        self.assertTrue(frappe.db.exists("Sales Invoice", fb_order.sales_invoice))
        self.assertFalse(frappe.db.exists("POS Invoice", fb_order.sales_invoice))

        sales_invoice = frappe.get_doc("Sales Invoice", fb_order.sales_invoice)
        self.assertEqual(sales_invoice.docstatus, 1)
        self.assertEqual(sales_invoice.is_pos, 1)
        self.assertEqual(sales_invoice.update_stock, 0)
        invoices = frappe.get_all(
            "Sales Invoice",
            filters={
                "custom_fb_idempotency_key": payload["idempotency_key"],
                "docstatus": 1,
            },
            pluck="name",
        )
        self.assertEqual(invoices, [sales_invoice.name])

    def test_sales_invoice_has_exact_order_shift_device_evidence(self) -> None:
        payload, _, fb_order = self.submit_sale(quantity=2)
        sales_invoice = frappe.get_doc("Sales Invoice", fb_order.sales_invoice)

        self.assertEqual(sales_invoice.custom_fb_order, fb_order.name)
        self.assertEqual(sales_invoice.custom_fb_shift, self.shift.name)
        self.assertEqual(sales_invoice.custom_fb_device_id, self.shift.device_id)
        self.assertEqual(
            sales_invoice.custom_fb_idempotency_key,
            payload["idempotency_key"],
        )
        self.assertEqual(sales_invoice.grand_total, 24)

    def test_sale_creates_submitted_ingredient_stock_issue(self) -> None:
        _, _, fb_order = self.submit_sale()

        self.assertTrue(fb_order.ingredient_stock_entry)
        stock_entry = frappe.get_doc("Stock Entry", fb_order.ingredient_stock_entry)
        self.assertEqual(stock_entry.docstatus, 1)
        self.assertEqual(stock_entry.stock_entry_type, "Material Issue")
        self.assertGreater(len(stock_entry.items), 0)
        self.assertTrue(all(row.s_warehouse == self.shift.warehouse for row in stock_entry.items))

    def test_sale_projection_bundle_is_terminal_success(self) -> None:
        _, _, fb_order = self.submit_sale()
        logs = frappe.get_all(
            "FB Projection Log",
            filters={"source_doctype": "FB Order", "source_name": fb_order.name},
            fields=["projection_type", "state", "target_name"],
            order_by="projection_type asc",
        )

        self.assertEqual(
            {row.projection_type for row in logs},
            {"FB Shift", "Sales Invoice", "Stock Issue"},
        )
        self.assertTrue(all(row.state == "Succeeded" for row in logs))
        self.assertTrue(all(row.target_name for row in logs))

    def test_sale_persists_resolved_recipe_components(self) -> None:
        _, _, fb_order = self.submit_sale()
        resolved_sales = frappe.get_all(
            "FB Resolved Sale",
            filters={"fb_order": fb_order.name},
            pluck="name",
        )
        self.assertEqual(len(resolved_sales), 1)

        resolved_sale = frappe.get_doc("FB Resolved Sale", resolved_sales[0])
        self.assertEqual(resolved_sale.fb_order, fb_order.name)
        self.assertEqual(resolved_sale.booth_warehouse, self.shift.warehouse)
        self.assertGreater(len(resolved_sale.resolved_components), 0)
        self.assertTrue(
            all(
                row.item and row.stock_uom and row.stock_qty > 0
                for row in resolved_sale.resolved_components
                if row.affects_stock
            )
        )
