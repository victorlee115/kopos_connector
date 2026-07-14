from __future__ import annotations

import unittest
from pathlib import Path


ERP_ROOT = Path(__file__).resolve().parents[1]


class TestFBServiceContracts(unittest.TestCase):
    def test_sales_invoice_service_forces_update_stock_zero(self):
        content = (
            ERP_ROOT / "kopos" / "services" / "accounting" / "sales_invoice_service.py"
        ).read_text()
        self.assertIn("invoice.update_stock = 0", content)

    def test_sale_and_ingredient_stock_services_use_fb_order_sale_datetime(self):
        for relative in [
            "kopos/services/accounting/sales_invoice_service.py",
            "kopos/services/inventory/stock_issue_service.py",
        ]:
            content = (ERP_ROOT / relative).read_text()
            self.assertIn("resolve_order_sale_datetime", content, relative)
            self.assertIn("posting_dt.date().isoformat()", content, relative)
            self.assertIn('posting_dt.time().strftime("%H:%M:%S")', content, relative)

    def test_sales_invoice_service_maps_custom_fb_fields(self):
        content = (
            ERP_ROOT / "kopos" / "services" / "accounting" / "sales_invoice_service.py"
        ).read_text()
        for token in [
            "custom_fb_order",
            "custom_fb_shift",
            "custom_fb_device_id",
            "custom_fb_event_project",
            "custom_fb_idempotency_key",
            "custom_fb_operational_status",
            "custom_fb_order_line_ref",
            "custom_fb_resolved_sale",
            "custom_fb_recipe_snapshot_json",
            "custom_fb_resolution_hash",
        ]:
            self.assertIn(token, content)

    def test_return_invoice_service_uses_standard_credit_note_without_pos_payments(self):
        content = (
            ERP_ROOT / "kopos" / "services" / "accounting" / "return_invoice_service.py"
        ).read_text()
        self.assertIn("make_return_doc(\"Sales Invoice\"", content)
        self.assertIn("return_invoice.is_pos = 0", content)
        self.assertIn('return_invoice.set("payments", [])', content)
        self.assertIn("_validate_full_standard_return_items", content)

    def test_return_service_updates_resolved_sale_status(self):
        content = (
            ERP_ROOT / "kopos" / "services" / "operations" / "return_service.py"
        ).read_text()
        self.assertIn("Partially Returned", content)
        self.assertIn("Returned", content)
        self.assertIn('resolved_sale.db_set("status"', content)

    def test_return_quantity_guard_locks_resolved_sales_before_validation(self):
        content = (
            ERP_ROOT
            / "kopos"
            / "services"
            / "operations"
            / "return_guard_service.py"
        ).read_text()
        controller = (
            ERP_ROOT
            / "kopos"
            / "doctype"
            / "fb_return_event"
            / "fb_return_event.py"
        ).read_text()
        self.assertIn("FOR UPDATE", content)
        self.assertIn("return_event.docstatus = 1", content)
        self.assertIn("Partial ERP returns are not supported", content)
        self.assertIn("ORDER BY name", content)
        self.assertIn("lock_and_validate_return_quantities", controller)

    def test_transfer_service_uses_resolved_basic_rate(self):
        content = (
            ERP_ROOT / "kopos" / "services" / "inventory" / "transfer_service.py"
        ).read_text()
        self.assertIn("_resolve_basic_rate", content)
        self.assertIn("valuation_rate", content)
        self.assertIn("standard_rate", content)

    def test_fb_order_updates_shift_expected_cash(self):
        order_content = (
            ERP_ROOT / "kopos" / "doctype" / "fb_order" / "fb_order.py"
        ).read_text()
        cash_service_content = (
            ERP_ROOT
            / "kopos"
            / "services"
            / "accounting"
            / "return_invoice_service.py"
        ).read_text()
        self.assertIn("update_shift_expected_cash", order_content)
        self.assertIn("refresh_fb_shift_cash", order_content)
        self.assertIn("expected_cash", cash_service_content)
        self.assertIn("mode_of_payment", cash_service_content)
        self.assertIn("FOR UPDATE", cash_service_content)
