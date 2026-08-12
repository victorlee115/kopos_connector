from __future__ import annotations

import unittest
from typing import Any

import frappe
import pytest
from frappe.tests.utils import FrappeTestCase

pytestmark = pytest.mark.inventory_regression


@unittest.skip(
    "Inventory-backed return/remake behavior is excluded during the owner redesign; commercial refunds have a separate non-stock gate"
)
class SaleBackedOperationsTestCase(FrappeTestCase):
    def setUp(self) -> None:
        from kopos_connector.kopos.api.fb_orders import submit_order_payload
        from kopos_connector.kopos.tests.frappe_test_fixtures import (
            build_sen_v1_sale_payload,
            create_open_test_shift,
        )

        frappe.set_user("Administrator")
        self.shift = create_open_test_shift(
            prefix="KOPOS-E2E-OPS", replenish_stock=True
        )
        sale_payload = build_sen_v1_sale_payload(
            self.shift,
            prefix="KOPOS-E2E-OPS",
            include_optional_recipe=True,
        )
        sale_result = submit_order_payload(sale_payload)
        self.order = frappe.get_doc("FB Order", sale_result["fb_order"])
        resolved_sales = frappe.get_all(
            "FB Resolved Sale",
            filters={"fb_order": self.order.name},
            pluck="name",
        )
        self.assertEqual(len(resolved_sales), 1)
        self.resolved_sale = frappe.get_doc("FB Resolved Sale", resolved_sales[0])

    def tearDown(self) -> None:
        frappe.db.rollback()

    def return_payload(self, *, return_to_stock: bool) -> dict[str, Any]:
        return {
            "return_id": f"KOPOS-E2E-RETURN-{frappe.generate_hash(length=12)}",
            "device_id": self.order.device_id,
            "fb_order": self.order.name,
            "original_sales_invoice": self.order.sales_invoice,
            "reason_code": "Quality Issue",
            "reason_text": "Real-Frappe acceptance fixture",
            "refund_method": "cash",
            "return_to_stock": 1 if return_to_stock else 0,
            "lines": [
                {
                    "original_resolved_sale": self.resolved_sale.name,
                    "qty_returned": self.resolved_sale.qty,
                }
            ],
        }


class TestEndToEndReturnFlow(SaleBackedOperationsTestCase):
    def test_return_creates_submitted_sales_invoice_return_and_settlement(self) -> None:
        from kopos_connector.api.fb_returns import process_return_payload

        result = process_return_payload(
            self.return_payload(return_to_stock=False),
            require_manager_approval=False,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["settlement_status"], "Posted")
        self.assertIn(result["settlement_doctype"], {"Payment Entry", "Journal Entry"})
        self.assertTrue(frappe.db.exists(result["settlement_doctype"], result["settlement_document"]))

        return_invoice = frappe.get_doc("Sales Invoice", result["return_sales_invoice"])
        self.assertEqual(return_invoice.docstatus, 1)
        self.assertEqual(return_invoice.is_return, 1)
        self.assertEqual(return_invoice.return_against, self.order.sales_invoice)
        self.assertEqual(return_invoice.custom_fb_order, self.order.name)
        self.assertEqual(return_invoice.custom_fb_shift, self.shift.name)
        self.assertEqual(result["reversal_stock_entries"], [])

        return_event = frappe.get_doc("FB Return Event", result["return_event"])
        self.assertEqual(return_event.docstatus, 1)
        self.assertEqual(return_event.settlement_status, "Posted")
        self.resolved_sale.reload()
        self.assertEqual(self.resolved_sale.status, "Returned")

    def test_return_to_stock_creates_submitted_material_receipt(self) -> None:
        from kopos_connector.api.fb_returns import process_return_payload

        result = process_return_payload(
            self.return_payload(return_to_stock=True),
            require_manager_approval=False,
        )

        self.assertEqual(result["return_to_stock"], 1)
        self.assertEqual(len(result["reversal_stock_entries"]), 1)
        stock_entry = frappe.get_doc("Stock Entry", result["reversal_stock_entries"][0])
        self.assertEqual(stock_entry.docstatus, 1)
        self.assertEqual(stock_entry.stock_entry_type, "Material Receipt")
        self.assertGreater(len(stock_entry.items), 0)
        self.assertTrue(all(row.t_warehouse == self.shift.warehouse for row in stock_entry.items))


class TestEndToEndRemakeFlow(SaleBackedOperationsTestCase):
    def test_remake_creates_submitted_stock_issue_without_revenue(self) -> None:
        from kopos_connector.api.fb_remakes import process_remake

        invoices_before = set(
            frappe.get_all(
                "Sales Invoice",
                filters={"custom_fb_order": self.order.name},
                pluck="name",
            )
        )
        frappe.request = None
        frappe.form_dict = {
            "remake_id": f"KOPOS-E2E-REMAKE-{frappe.generate_hash(length=12)}",
            "device_id": self.order.device_id,
            "original_order": self.order.name,
            "original_order_line": self.order.items[0].name,
            "original_resolved_sale": self.resolved_sale.name,
            "reason_code": "Spill",
            "reason_text": "Real-Frappe acceptance fixture",
        }

        result = process_remake()

        self.assertEqual(result["status"], "ok")
        remake_event = frappe.get_doc("FB Remake Event", result["remake_event"])
        self.assertEqual(remake_event.docstatus, 1)
        stock_entry = frappe.get_doc("Stock Entry", result["replacement_stock_entry"])
        self.assertEqual(stock_entry.docstatus, 1)
        self.assertEqual(stock_entry.stock_entry_type, "Material Issue")
        invoices_after = set(
            frappe.get_all(
                "Sales Invoice",
                filters={"custom_fb_order": self.order.name},
                pluck="name",
            )
        )
        self.assertEqual(invoices_after, invoices_before)
