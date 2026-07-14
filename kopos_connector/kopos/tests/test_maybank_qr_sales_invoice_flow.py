from __future__ import annotations

import unittest
from contextlib import nullcontext
from decimal import Decimal
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

import frappe

from kopos_connector.kopos.services.accounting.sales_invoice_service import (
    create_sales_invoice,
)


class TestMaybankQRSalesInvoiceFlow(unittest.TestCase):
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
        order.device_id = "TEST-DEVICE-QR"
        order.shift = "SHIFT-QR"
        order.notes = None
        order.sales_invoice = None
        order.tax_total = Decimal("0.00")
        order.rounding_adjustment = Decimal("0.00")
        order.net_total = Decimal("12.50")
        order.grand_total = Decimal("12.50")
        order.payments = [
            frappe._dict(
                {
                    "payment_method": "DuitNow QR",
                    "source_payment_id": "PAY-QR-001",
                    "amount": Decimal("12.50"),
                    "tendered_amount": Decimal("12.50"),
                    "change_amount": Decimal("0.00"),
                    "reference_no": "MBQR-REF-001",
                    "external_transaction_id": "TXN-001",
                }
            )
        ]
        order.items = [
            frappe._dict(
                {
                    "item": "E2E-MATCHA-LATTE",
                    "line_id": "LINE-QR-001",
                    "item_name_snapshot": "E2E Matcha Latte",
                    "qty": 1,
                    "uom": "Nos",
                    "unit_price": Decimal("12.50"),
                    "modifier_total": Decimal("0.00"),
                    "discount_amount": Decimal("0.00"),
                    "line_total": Decimal("12.50"),
                    "remarks": None,
                }
            )
        ]
        order.db_set = lambda *args, **kwargs: None
        order.save = lambda *args, **kwargs: None
        return order

    def coerce_doc_stub(self, doctype, value):
        if getattr(value, "doctype", None) == doctype:
            return value
        return frappe._dict(
            {
                "name": value,
                "item_name": "E2E Matcha Latte",
                "stock_uom": "Nos",
                "description": None,
            }
        )

    def test_sales_invoice_service_uses_qr_mode_of_payment(self):
        order = self.make_fb_order_stub()

        with (
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.frappe.new_doc"
            ) as new_doc_mock,
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.frappe.get_meta",
                return_value=frappe._dict({"has_field": lambda fieldname: True}),
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service._coerce_doc",
                side_effect=self.coerce_doc_stub,
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service._resolve_pos_profile_context",
                return_value={
                    "pos_profile": "KoPOS Test Profile",
                    "company": self.company,
                },
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service._set_if_present",
                side_effect=lambda doc, fieldnames, value: setattr(
                    doc, fieldnames[0], value
                ),
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.privileged_device_api_operation",
                return_value=nullcontext(),
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.bind_qr_payment_settlement"
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service._resolve_mode_of_payment_context",
                return_value={"account": "Bank - TC", "type": "Bank"},
            ),
        ):
            def append_invoice_row(field, value):
                row = frappe._dict(value)
                if field == "items":
                    row.net_amount = row.amount
                invoice[field].append(row)
                return row

            invoice = frappe._dict(
                {
                    "doctype": "Sales Invoice",
                    "items": [],
                    "payments": [],
                    "taxes": [],
                    "docstatus": 0,
                    "is_return": 0,
                    "update_stock": 0,
                    "net_total": Decimal("12.50"),
                    "total_taxes_and_charges": Decimal("0.00"),
                    "grand_total": Decimal("12.50"),
                    "rounded_total": Decimal("0.00"),
                    "disable_rounded_total": 0,
                    "write_off_amount": Decimal("0.00"),
                    "paid_amount": Decimal("0.00"),
                    "change_amount": Decimal("0.00"),
                    "outstanding_amount": Decimal("0.00"),
                    "append": append_invoice_row,
                    "set": lambda field, value: invoice.__setitem__(field, value),
                    "insert": lambda **kwargs: None,
                    "submit": lambda: invoice.__setitem__("docstatus", 1),
                    "set_missing_values": lambda: None,
                    "calculate_taxes_and_totals": lambda: None,
                    "name": "SINV-QR-001",
                }
            )
            new_doc_mock.return_value = invoice

            result = create_sales_invoice(order)

        self.assertEqual(result, "SINV-QR-001")
        self.assertEqual(invoice.is_pos, 1)
        self.assertEqual(invoice.update_stock, 0)
        self.assertEqual(len(invoice.payments), 1)
        self.assertEqual(invoice.payments[0].mode_of_payment, "DuitNow QR")
        self.assertEqual(invoice.payments[0].amount, Decimal("12.50"))
        self.assertEqual(invoice.taxes, [])

    def test_sales_invoice_service_carries_tax_and_rounding(self):
        order = self.make_fb_order_stub()
        order.tax_total = Decimal("0.96")
        order.rounding_adjustment = Decimal("-0.01")
        order.net_total = Decimal("12.00")
        order.grand_total = Decimal("12.95")
        order["items"][0].unit_price = Decimal("12.00")
        order["items"][0].line_total = Decimal("12.00")
        order.payments = [
            frappe._dict(
                {
                    "payment_method": "Cash",
                    "source_payment_id": "PAY-CASH-001",
                    "amount": Decimal("12.95"),
                    "tendered_amount": Decimal("12.95"),
                    "change_amount": Decimal("0.00"),
                    "reference_no": None,
                    "external_transaction_id": None,
                }
            )
        ]

        with (
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.frappe.new_doc"
            ) as new_doc_mock,
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.frappe.get_all"
            ) as get_all_mock,
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.frappe.get_meta",
                return_value=frappe._dict({"has_field": lambda fieldname: True}),
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service._coerce_doc",
                side_effect=self.coerce_doc_stub,
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service._resolve_pos_profile_context",
                return_value={
                    "pos_profile": "KoPOS Test Profile",
                    "company": self.company,
                },
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service._set_if_present",
                side_effect=lambda doc, fieldnames, value: setattr(
                    doc, fieldnames[0], value
                ),
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.privileged_device_api_operation",
                return_value=nullcontext(),
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.bind_qr_payment_settlement"
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service._resolve_mode_of_payment_context",
                return_value={"account": "Cash - TC", "type": "Cash"},
            ),
        ):
            def append_invoice_row(field, value):
                row = frappe._dict(value)
                if field == "items":
                    row.net_amount = row.amount
                invoice[field].append(row)
                return row

            invoice = frappe._dict(
                {
                    "doctype": "Sales Invoice",
                    "items": [],
                    "payments": [],
                    "taxes": [],
                    "docstatus": 0,
                    "is_return": 0,
                    "update_stock": 0,
                    "net_total": Decimal("12.00"),
                    "total_taxes_and_charges": Decimal("0.96"),
                    "grand_total": Decimal("12.96"),
                    "rounded_total": Decimal("0.00"),
                    "disable_rounded_total": 0,
                    "write_off_amount": 0,
                    "base_write_off_amount": 0,
                    "paid_amount": Decimal("0.00"),
                    "change_amount": Decimal("0.00"),
                    "outstanding_amount": Decimal("0.00"),
                    "append": append_invoice_row,
                    "set": lambda field, value: invoice.__setitem__(field, value),
                    "insert": lambda **kwargs: None,
                    "submit": lambda: invoice.__setitem__("docstatus", 1),
                    "set_missing_values": lambda: None,
                    "calculate_taxes_and_totals": lambda: None,
                    "name": "SINV-QR-002",
                }
            )
            new_doc_mock.return_value = invoice
            get_all_mock.side_effect = [
                ["Duties and Taxes - WP"],
                [{"write_off_account": "Write Off - WP", "cost_center": "Main - WP"}],
            ]

            result = create_sales_invoice(order)

        self.assertEqual(result, "SINV-QR-002")
        self.assertEqual(len(invoice.taxes), 1)
        self.assertEqual(invoice.taxes[0].charge_type, "Actual")
        self.assertEqual(invoice.taxes[0].account_head, "Duties and Taxes - WP")
        self.assertEqual(invoice.taxes[0].tax_amount, Decimal("0.96"))
        self.assertEqual(invoice.disable_rounded_total, 1)
        self.assertEqual(invoice.write_off_amount, Decimal("0.01"))
        self.assertEqual(invoice.base_write_off_amount, Decimal("0.01"))
        self.assertEqual(invoice.write_off_account, "Write Off - WP")
        self.assertEqual(invoice.write_off_cost_center, "Main - WP")
        self.assertEqual(invoice.payments[0].amount, Decimal("12.95"))

    def test_generate_maybank_qr_payload_rejects_unverified_prebinding(self):
        from kopos_connector.api.maybank_qr import generate_maybank_qr_payload

        with self.assertRaises(frappe.ValidationError) as error:
            generate_maybank_qr_payload(
                {
                    "amount_sen": 1250,
                    "device_id": "TEST-DEVICE-QR",
                    "idempotency_key": "QR-IDEMP-001",
                    "fb_order": "FB-ORDER-QR-001",
                    "sales_invoice": "SINV-QR-001",
                }
            )

        self.assertIn("verified sale submission", str(error.exception))
