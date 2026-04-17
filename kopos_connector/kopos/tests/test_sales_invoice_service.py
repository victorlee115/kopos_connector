from __future__ import annotations

import unittest
from contextlib import nullcontext
from unittest.mock import patch

import frappe

from kopos_connector.kopos.services.accounting.sales_invoice_service import (
    create_sales_invoice,
)


class TestSalesInvoiceService(unittest.TestCase):
    def setUp(self):
        self.company = "Test Company"

    def make_fb_order_stub(self):
        order = frappe._dict()
        order.doctype = "FB Order"
        order.name = f"FB-ORDER-{frappe.generate_hash(length=8)}"
        order.customer = "Walk-in Customer"
        order.company = self.company
        order.currency = "MYR"
        order.event_project = None
        order.device_id = "TEST-DEVICE-CASH"
        order.shift = "SHIFT-CASH"
        order.notes = None
        order.sales_invoice = None
        order.tax_total = 0.96
        order.rounding_adjustment = -0.01
        order.grand_total = 12.95
        order.payments = [
            frappe._dict(
                {
                    "payment_method": "Cash",
                    "amount": 12.95,
                    "tendered_amount": 12.95,
                    "change_amount": 0,
                    "reference_no": None,
                    "external_transaction_id": None,
                }
            )
        ]
        order.items = [
            frappe._dict(
                {
                    "item": "E2E-MATCHA-LATTE",
                    "item_name_snapshot": "E2E Matcha Latte",
                    "qty": 1,
                    "uom": "Nos",
                    "unit_price": 12.0,
                    "modifier_total": 0,
                    "discount_amount": 0,
                    "line_total": 12.0,
                    "remarks": None,
                }
            )
        ]
        order.db_set = lambda *args, **kwargs: None
        order.save = lambda *args, **kwargs: None
        return order

    def test_create_sales_invoice_carries_tax_and_rounding(self):
        order = self.make_fb_order_stub()

        with (
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.frappe.new_doc"
            ) as new_doc_mock,
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.frappe.get_all"
            ) as get_all_mock,
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service._coerce_doc"
            ) as coerce_doc_mock,
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.frappe.get_meta",
                return_value=frappe._dict({"has_field": lambda fieldname: True}),
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.elevate_device_api_user",
                return_value=nullcontext(),
            ),
        ):
            invoice = frappe._dict(
                {
                    "doctype": "Sales Invoice",
                    "items": [],
                    "payments": [],
                    "taxes": [],
                    "net_total": 12.0,
                    "grand_total": 12.96,
                    "rounded_total": 0,
                    "disable_rounded_total": 0,
                    "write_off_amount": 0,
                    "base_write_off_amount": 0,
                    "append": lambda field, value: invoice[field].append(
                        frappe._dict(value)
                    ),
                    "set": lambda field, value: invoice.__setitem__(field, value),
                    "insert": lambda **kwargs: None,
                    "submit": lambda: None,
                    "set_missing_values": lambda: None,
                    "calculate_taxes_and_totals": lambda: None,
                    "name": "SINV-CASH-001",
                }
            )
            new_doc_mock.return_value = invoice
            get_all_mock.side_effect = [
                ["Duties and Taxes - WP"],
                [{"write_off_account": "Write Off - WP", "cost_center": "Main - WP"}],
            ]
            coerce_doc_mock.side_effect = (
                lambda doctype, value: value
                if getattr(value, "doctype", None) == doctype
                else frappe._dict(
                    {
                        "name": value,
                        "item_name": "E2E Matcha Latte",
                        "stock_uom": "Nos",
                        "description": None,
                    }
                )
            )

            result = create_sales_invoice(order)

        self.assertEqual(result, "SINV-CASH-001")
        self.assertEqual(len(invoice.taxes), 1)
        self.assertEqual(invoice.taxes[0].charge_type, "Actual")
        self.assertEqual(invoice.taxes[0].account_head, "Duties and Taxes - WP")
        self.assertEqual(invoice.taxes[0].tax_amount, 0.96)
        self.assertEqual(invoice.taxes[0].base_tax_amount, 0.96)
        self.assertEqual(invoice.disable_rounded_total, 1)
        self.assertEqual(invoice.write_off_amount, 0.01)
        self.assertEqual(invoice.base_write_off_amount, 0.01)
        self.assertEqual(invoice.write_off_account, "Write Off - WP")
        self.assertEqual(invoice.write_off_cost_center, "Main - WP")
        self.assertEqual(invoice.payments[0].amount, 12.95)
