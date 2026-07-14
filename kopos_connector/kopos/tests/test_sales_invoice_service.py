from __future__ import annotations

import unittest
from contextlib import nullcontext
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

import frappe

from kopos_connector.kopos.services.accounting.sales_invoice_service import (
    _apply_rounding,
    _append_payment_rows,
    _resolve_line_rate,
    _resolve_customer,
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
        order.sale_datetime = datetime(2026, 7, 12, 0, 30, 45)
        order.sales_invoice = None
        order.tax_total = Decimal("0.96")
        order.rounding_adjustment = Decimal("-0.01")
        order.net_total = Decimal("12.00")
        order.grand_total = Decimal("12.95")
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
        order.items = [
            frappe._dict(
                {
                    "item": "E2E-MATCHA-LATTE",
                    "line_id": "LINE-1",
                    "item_name_snapshot": "E2E Matcha Latte",
                    "qty": 1,
                    "uom": "Nos",
                    "unit_price": Decimal("12.00"),
                    "modifier_total": Decimal("0.00"),
                    "discount_amount": Decimal("0.00"),
                    "line_total": Decimal("12.00"),
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
                "kopos_connector.kopos.services.accounting.sales_invoice_service.frappe.get_meta",
                return_value=frappe._dict({"has_field": lambda fieldname: True}),
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.privileged_device_api_operation",
                return_value=nullcontext(),
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.bind_qr_payment_settlement"
            ) as bind_qr,
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
                    "net_total": 12.0,
                    "grand_total": 12.96,
                    "total_taxes_and_charges": Decimal("0.96"),
                    "rounded_total": 0,
                    "disable_rounded_total": 0,
                    "write_off_amount": 0,
                    "base_write_off_amount": 0,
                    "paid_amount": 0,
                    "change_amount": 0,
                    "outstanding_amount": 0,
                    "append": append_invoice_row,
                    "set": lambda field, value: invoice.__setitem__(field, value),
                    "insert": lambda **kwargs: None,
                    "submit": lambda: invoice.__setitem__("docstatus", 1),
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
        self.assertEqual(invoice.posting_date, "2026-07-12")
        self.assertEqual(invoice.posting_time, "00:30:45")
        self.assertEqual(len(invoice.taxes), 1)
        self.assertEqual(invoice.taxes[0].charge_type, "Actual")
        self.assertEqual(invoice.taxes[0].account_head, "Duties and Taxes - WP")
        self.assertEqual(invoice.taxes[0].tax_amount, Decimal("0.96"))
        self.assertEqual(invoice.taxes[0].base_tax_amount, Decimal("0.96"))
        self.assertEqual(invoice.disable_rounded_total, 1)
        self.assertEqual(invoice.write_off_amount, Decimal("0.01"))
        self.assertEqual(invoice.base_write_off_amount, Decimal("0.01"))
        self.assertEqual(invoice.write_off_account, "Write Off - WP")
        self.assertEqual(invoice.write_off_cost_center, "Main - WP")
        self.assertEqual(invoice.payments[0].amount, Decimal("12.95"))
        bind_qr.assert_called_once_with(order, "SINV-CASH-001")

    def test_resolve_customer_falls_back_to_pos_profile_customer(self):
        order = self.make_fb_order_stub()
        order.customer = None

        with (
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.get_device_pos_profile_doc",
                return_value=frappe._dict({"customer": "POS Walk-In Customer"}),
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.frappe.db.exists",
                return_value=None,
            ),
        ):
            customer = _resolve_customer(order)

        self.assertEqual(customer, "POS Walk-In Customer")

    def test_existing_invoice_rebinds_the_maybank_claim_idempotently(self):
        order = self.make_fb_order_stub()
        order.external_idempotency_key = "idem-existing-1"
        order.sales_invoice = "SINV-EXISTING-1"
        invoice = frappe._dict(
            {
                "doctype": "Sales Invoice",
                "name": order.sales_invoice,
                "docstatus": 1,
                "is_return": 0,
                "is_pos": 1,
                "update_stock": 0,
                "pos_profile": "KoPOS Test Profile",
                "custom_fb_order": order.name,
                "custom_fb_shift": order.shift,
                "custom_fb_device_id": order.device_id,
                "custom_fb_idempotency_key": order.external_idempotency_key,
                "company": order.company,
                "currency": order.currency,
                "net_total": order.net_total,
                "total_taxes_and_charges": order.tax_total,
                "grand_total": Decimal("12.96"),
                "disable_rounded_total": 1,
                "rounded_total": Decimal("0.00"),
                "write_off_amount": Decimal("0.01"),
                "paid_amount": order.grand_total,
                "change_amount": Decimal("0.00"),
                "outstanding_amount": Decimal("0.00"),
                "items": [
                    frappe._dict(
                        {
                            "custom_fb_order_line_ref": "LINE-1",
                            "item_code": "E2E-MATCHA-LATTE",
                            "qty": 1,
                            "amount": order.net_total,
                            "net_amount": order.net_total,
                            "custom_kopos_modifier_total": Decimal("0.00"),
                            "warehouse": None,
                        }
                    )
                ],
                "payments": [
                    frappe._dict(
                        {
                            "mode_of_payment": "Cash",
                            "amount": order.grand_total,
                            "account": "Cash - TC",
                            "custom_fb_source_payment_id": "PAY-CASH-001",
                        }
                    )
                ],
                "taxes": [
                    frappe._dict(
                        {
                            "charge_type": "Actual",
                            "account_head": "Duties and Taxes - TC",
                            "tax_amount": order.tax_total,
                            "tax_amount_after_discount_amount": order.tax_total,
                            "included_in_print_rate": 0,
                        }
                    )
                ],
            }
        )

        with (
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service._coerce_doc",
                side_effect=lambda doctype, value: invoice
                if doctype == "Sales Invoice"
                else value,
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.bind_qr_payment_settlement"
            ) as bind_qr,
        ):
            result = create_sales_invoice(order)

        self.assertEqual(result, "SINV-EXISTING-1")
        bind_qr.assert_called_once_with(order, "SINV-EXISTING-1")

    def test_existing_invoice_recovery_rejects_draft_or_unbound_document(self):
        order = self.make_fb_order_stub()
        order.external_idempotency_key = "idem-draft-1"
        order.sales_invoice = "SINV-DRAFT-1"
        invoice = frappe._dict(
            {
                "doctype": "Sales Invoice",
                "name": order.sales_invoice,
                "docstatus": 0,
                "is_return": 0,
                "is_pos": 1,
                "update_stock": 0,
                "pos_profile": "KoPOS Test Profile",
                "custom_fb_order": None,
                "grand_total": order.grand_total,
                "paid_amount": order.grand_total,
                "outstanding_amount": Decimal("0.00"),
            }
        )

        with patch(
            "kopos_connector.kopos.services.accounting.sales_invoice_service._coerce_doc",
            side_effect=lambda doctype, value: invoice
            if doctype == "Sales Invoice"
            else value,
        ):
            with self.assertRaisesRegex(frappe.ValidationError, "not submitted"):
                create_sales_invoice(order)

        invoice.docstatus = 1
        with patch(
            "kopos_connector.kopos.services.accounting.sales_invoice_service._coerce_doc",
            side_effect=lambda doctype, value: invoice
            if doctype == "Sales Invoice"
            else value,
        ):
            with self.assertRaisesRegex(
                frappe.ValidationError, "belongs to another FB Order"
            ):
                create_sales_invoice(order)

    def test_existing_invoice_recovery_rejects_header_only_document(self):
        order = self.make_fb_order_stub()
        order.external_idempotency_key = "idem-header-only-1"
        order.sales_invoice = "SINV-HEADER-ONLY-1"
        invoice = frappe._dict(
            {
                "doctype": "Sales Invoice",
                "name": order.sales_invoice,
                "docstatus": 1,
                "is_return": 0,
                "is_pos": 1,
                "update_stock": 0,
                "pos_profile": "KoPOS Test Profile",
                "custom_fb_order": order.name,
                "custom_fb_shift": order.shift,
                "custom_fb_device_id": order.device_id,
                "custom_fb_idempotency_key": order.external_idempotency_key,
                "company": order.company,
                "currency": order.currency,
                "net_total": order.net_total,
                "total_taxes_and_charges": order.tax_total,
                "grand_total": Decimal("12.96"),
                "disable_rounded_total": 1,
                "rounded_total": Decimal("0.00"),
                "write_off_amount": Decimal("0.01"),
                "paid_amount": order.grand_total,
                "change_amount": Decimal("0.00"),
                "outstanding_amount": Decimal("0.00"),
                "items": [],
                "payments": [],
                "taxes": [],
            }
        )

        with patch(
            "kopos_connector.kopos.services.accounting.sales_invoice_service._coerce_doc",
            side_effect=lambda doctype, value: invoice
            if doctype == "Sales Invoice"
            else value,
        ):
            with self.assertRaisesRegex(
                frappe.ValidationError,
                "item count does not match",
            ):
                create_sales_invoice(order)

    def test_static_qr_payment_projects_to_suspense_account(self):
        order = self.make_fb_order_stub()
        order.payments = [
            frappe._dict(
                {
                    "payment_method": "DuitNow QR",
                    "amount": Decimal("12.95"),
                    "change_amount": Decimal("0.00"),
                    "reference_no": "STATIC-REF-1",
                    "settlement_status": "pending_reconciliation",
                    "suspense_account": "Manual QR Suspense - TC",
                }
            )
        ]
        invoice = frappe._dict(
            {
                "company": self.company,
                "payments": [],
                "append": lambda field, value: invoice[field].append(
                    frappe._dict(value)
                ),
            }
        )

        with patch(
            "kopos_connector.kopos.services.accounting.sales_invoice_service._resolve_mode_of_payment_context",
            return_value={"account": "QR Clearing - TC", "type": "Bank"},
        ):
            _append_payment_rows(invoice, order)

        self.assertEqual(len(invoice.payments), 1)
        self.assertEqual(invoice.payments[0].account, "Manual QR Suspense - TC")
        self.assertEqual(invoice.payments[0].amount, Decimal("12.95"))

    def test_payment_projection_rejects_fractional_sen(self):
        order = self.make_fb_order_stub()
        order.payments[0].amount = Decimal("12.951")
        invoice = frappe._dict(
            {
                "company": self.company,
                "payments": [],
                "append": lambda field, value: invoice[field].append(
                    frappe._dict(value)
                ),
            }
        )

        with self.assertRaisesRegex(ValueError, "fractional sen"):
            _append_payment_rows(invoice, order)

        self.assertEqual(invoice.payments, [])

    def test_cash_over_tender_projects_erp_paid_amount_and_change(self):
        order = self.make_fb_order_stub()
        order.payments[0].tendered_amount = Decimal("20.00")
        order.payments[0].change_amount = Decimal("7.05")
        invoice = frappe._dict(
            {
                "company": self.company,
                "payments": [],
                "append": lambda field, value: invoice[field].append(
                    frappe._dict(value)
                ),
            }
        )

        with patch(
            "kopos_connector.kopos.services.accounting.sales_invoice_service._resolve_mode_of_payment_context",
            return_value={"account": "Cash - TC", "type": "Cash"},
        ):
            _append_payment_rows(invoice, order)

        self.assertEqual(invoice.payments[0].amount, Decimal("20.00"))
        self.assertEqual(invoice.paid_amount, Decimal("20.00"))
        self.assertEqual(invoice.change_amount, Decimal("7.05"))

    def test_positive_rounding_projects_as_signed_exact_write_off(self):
        order = self.make_fb_order_stub()
        order.tax_total = Decimal("0.94")
        order.rounding_adjustment = Decimal("0.01")
        order.grand_total = Decimal("12.95")
        invoice = frappe._dict(
            {
                "grand_total": Decimal("12.94"),
                "rounded_total": Decimal("0.00"),
                "disable_rounded_total": 0,
            }
        )

        with patch(
            "kopos_connector.kopos.services.accounting.sales_invoice_service._resolve_write_off_defaults",
            return_value={"account": "Write Off - WP", "cost_center": "Main - WP"},
        ):
            _apply_rounding(invoice, order)

        self.assertEqual(invoice.disable_rounded_total, 1)
        self.assertEqual(invoice.write_off_amount, Decimal("-0.01"))
        self.assertEqual(invoice.base_write_off_amount, Decimal("-0.01"))

    def test_zero_rounding_always_disables_and_clears_implicit_rounded_total(self):
        order = self.make_fb_order_stub()
        order.tax_total = Decimal("0.03")
        order.rounding_adjustment = Decimal("0.00")
        order.grand_total = Decimal("12.03")
        invoice = frappe._dict(
            {
                "grand_total": Decimal("12.03"),
                "rounded_total": Decimal("12.05"),
                "base_rounded_total": Decimal("12.05"),
                "rounding_adjustment": Decimal("0.02"),
                "base_rounding_adjustment": Decimal("0.02"),
                "disable_rounded_total": 0,
            }
        )

        _apply_rounding(invoice, order)

        self.assertEqual(invoice.disable_rounded_total, 1)
        self.assertEqual(invoice.rounded_total, Decimal("0.00"))
        self.assertEqual(invoice.base_rounded_total, Decimal("0.00"))
        self.assertEqual(invoice.write_off_amount, Decimal("0.00"))

    def test_line_rate_uses_decimal_without_float_rounding(self):
        line = frappe._dict(
            {
                "qty": 3,
                "line_total": Decimal("10.01"),
            }
        )

        rate = _resolve_line_rate(line)

        self.assertIsInstance(rate, Decimal)
        self.assertEqual(rate * Decimal("3"), Decimal("10.01"))

    def test_resolve_customer_falls_back_to_walk_in_customer(self):
        order = self.make_fb_order_stub()
        order.customer = None

        with (
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.get_device_pos_profile_doc",
                return_value=frappe._dict({"customer": None}),
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.frappe.db.exists",
                return_value="Walk-in Customer",
            ),
        ):
            customer = _resolve_customer(order)

        self.assertEqual(customer, "Walk-in Customer")

    def test_resolve_customer_raises_when_no_customer_source_exists(self):
        order = self.make_fb_order_stub()
        order.customer = None

        with (
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.get_device_pos_profile_doc",
                return_value=frappe._dict({"customer": None}),
            ),
            patch(
                "kopos_connector.kopos.services.accounting.sales_invoice_service.frappe.db.exists",
                return_value=None,
            ),
        ):
            with self.assertRaises(ValueError) as error:
                _resolve_customer(order)

        self.assertEqual(
            str(error.exception), "customer is required to create Sales Invoice"
        )
