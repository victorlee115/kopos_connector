from __future__ import annotations

import unittest
from pathlib import Path


ERP_ROOT = Path(
    "/Users/victor/dev/jiji/JiJiPOS-Everything/worktree-fnb-erpnext/kopos_connector"
)


class TestFBServiceContracts(unittest.TestCase):
    def test_sales_invoice_service_forces_update_stock_zero(self):
        content = (
            ERP_ROOT / "kopos" / "services" / "accounting" / "sales_invoice_service.py"
        ).read_text()
        self.assertIn("invoice.update_stock = 0", content)

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

    def test_return_invoice_service_builds_credit_note_without_payment_rows(self):
        content = (
            ERP_ROOT / "kopos" / "services" / "accounting" / "return_invoice_service.py"
        ).read_text()
        self.assertIn("return_invoice.is_return = 1", content)
        self.assertIn("return_against = original_invoice_name", content)
        self.assertNotIn("_append_return_payments", content)

    def test_return_service_updates_resolved_sale_status(self):
        content = (
            ERP_ROOT / "kopos" / "services" / "operations" / "return_service.py"
        ).read_text()
        self.assertIn("Partially Returned", content)
        self.assertIn("Returned", content)
        self.assertIn('resolved_sale.db_set("status"', content)

    def test_transfer_service_uses_resolved_basic_rate(self):
        content = (
            ERP_ROOT / "kopos" / "services" / "inventory" / "transfer_service.py"
        ).read_text()
        self.assertIn("_resolve_basic_rate", content)
        self.assertIn("valuation_rate", content)
        self.assertIn("standard_rate", content)

    def test_fb_order_updates_shift_expected_cash(self):
        content = (
            ERP_ROOT / "kopos" / "doctype" / "fb_order" / "fb_order.py"
        ).read_text()
        self.assertIn("update_shift_expected_cash", content)
        self.assertIn("expected_cash", content)
        self.assertIn("mode_of_payment", content)
