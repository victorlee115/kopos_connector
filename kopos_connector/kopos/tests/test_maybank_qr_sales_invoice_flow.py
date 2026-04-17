from __future__ import annotations

import unittest
from contextlib import nullcontext
from unittest.mock import patch

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
        order.tax_total = 0
        order.rounding_adjustment = 0
        order.grand_total = 12.5
        order.payments = [
            frappe._dict(
                {
                    "payment_method": "DuitNow QR",
                    "amount": 12.5,
                    "tendered_amount": 12.5,
                    "change_amount": 0,
                    "reference_no": "MBQR-REF-001",
                    "external_transaction_id": "TXN-001",
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
                    "unit_price": 12.5,
                    "modifier_total": 0,
                    "discount_amount": 0,
                    "line_total": 12.5,
                    "remarks": None,
                }
            )
        ]
        order.db_set = lambda *args, **kwargs: None
        order.save = lambda *args, **kwargs: None
        return order

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
                    "append": lambda field, value: invoice[field].append(
                        frappe._dict(value)
                    ),
                    "set": lambda field, value: invoice.__setitem__(field, value),
                    "insert": lambda **kwargs: None,
                    "submit": lambda: None,
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
        self.assertEqual(invoice.payments[0].amount, 12.5)
        self.assertEqual(invoice.taxes, [])

    def test_sales_invoice_service_carries_tax_and_rounding(self):
        order = self.make_fb_order_stub()
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
        self.assertEqual(invoice.taxes[0].tax_amount, 0.96)
        self.assertEqual(invoice.disable_rounded_total, 1)
        self.assertEqual(invoice.write_off_amount, 0.01)
        self.assertEqual(invoice.base_write_off_amount, 0.01)
        self.assertEqual(invoice.write_off_account, "Write Off - WP")
        self.assertEqual(invoice.write_off_cost_center, "Main - WP")
        self.assertEqual(invoice.payments[0].amount, 12.95)

    def test_generate_maybank_qr_payload_persists_fb_order_and_sales_invoice(self):
        from kopos_connector.api.maybank_qr import generate_maybank_qr_payload

        fb_order_name = "FB-ORDER-QR-001"
        sales_invoice_name = "SINV-QR-001"

        with patch("kopos_connector.api.maybank_qr.frappe.db.exists") as exists_mock:

            def exists_side_effect(doctype, name=None):
                if doctype == "FB Order" and name == fb_order_name:
                    return True
                if doctype == "Sales Invoice" and name == sales_invoice_name:
                    return True
                return False

            exists_mock.side_effect = exists_side_effect

            with patch("kopos_connector.api.maybank_qr._check_rate_limit"):
                with patch(
                    "kopos_connector.api.maybank_qr.MaybankClient.from_settings"
                ) as client_mock:
                    client = client_mock.return_value
                    client.outlet_id = "OUTLET-001"
                    client.generate_qr.return_value = {
                        "status": "QR000",
                        "data": [
                            {
                                "transaction_refno": "MBQR-001",
                                "qr_data": "000201010211TESTQR",
                                "expires_in_seconds": 60,
                            }
                        ],
                    }

                    inserted = {}

                    def fake_get_doc(data):
                        inserted.update(data)
                        doc = frappe._dict(data)
                        doc.insert = lambda ignore_permissions=True: None
                        return doc

                    with patch(
                        "kopos_connector.api.maybank_qr.frappe.get_doc",
                        side_effect=fake_get_doc,
                    ):
                        response = generate_maybank_qr_payload(
                            {
                                "amount_sen": 1250,
                                "device_id": "TEST-DEVICE-QR",
                                "idempotency_key": "QR-IDEMP-001",
                                "fb_order": fb_order_name,
                                "sales_invoice": sales_invoice_name,
                            }
                        )

        self.assertEqual(response["status"], "ok")
        self.assertEqual(inserted.get("fb_order"), fb_order_name)
        self.assertEqual(inserted.get("sales_invoice"), sales_invoice_name)
        self.assertEqual(inserted.get("idempotency_key"), "QR-IDEMP-001")
