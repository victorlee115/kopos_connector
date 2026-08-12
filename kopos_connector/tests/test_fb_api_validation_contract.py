from __future__ import annotations

import unittest
from pathlib import Path

import pytest


ERP_ROOT = Path(__file__).resolve().parents[1]


class TestFBAPIValidationContract(unittest.TestCase):
    def test_return_api_prefers_commercial_identity_and_keeps_legacy_identity(self):
        content = (ERP_ROOT / "api" / "fb_returns.py").read_text()
        self.assertIn("original_sales_invoice_item", content)
        self.assertIn("original_fb_order_line_ref", content)
        self.assertIn("commercial_modifier_snapshot_json", content)
        self.assertIn("original_resolved_sale", content)
        self.assertIn("resolved_sale_id", content)
        self.assertIn("qty_returned", content)
        self.assertIn("original_sales_invoice", content)

    @pytest.mark.inventory_regression
    def test_remake_api_validates_original_resolved_sale(self):
        content = (ERP_ROOT / "api" / "fb_remakes.py").read_text()
        self.assertIn("original_resolved_sale", content)
        self.assertIn("original_order", content)
        self.assertIn("reason_code", content)

    @pytest.mark.inventory_regression
    def test_waste_api_requires_company_warehouse_and_lines(self):
        content = (ERP_ROOT / "api" / "fb_waste.py").read_text()
        for token in ["waste_id", "company", "warehouse", "lines", "reason_code"]:
            self.assertIn(token, content)

    @pytest.mark.inventory_regression
    def test_refill_api_requires_company_and_warehouse_pair(self):
        content = (ERP_ROOT / "api" / "fb_refill.py").read_text()
        for token in [
            "request_id",
            "company",
            "from_warehouse",
            "to_warehouse",
            "lines",
        ]:
            self.assertIn(token, content)

    def test_order_api_response_uses_ingredient_stock_entry(self):
        content = (ERP_ROOT / "kopos" / "api" / "fb_orders.py").read_text()
        self.assertIn('"ingredient_stock_entry"', content)
        self.assertIn('"order_status"', content)
        self.assertNotIn(
            '"stock_entry": cstr(order_doc.ingredient_stock_entry)', content
        )

    def test_order_api_validates_and_persists_canonical_sale_datetime(self):
        content = (ERP_ROOT / "kopos" / "api" / "fb_orders.py").read_text()

        self.assertIn('order_payload.get("created_at")', content)
        self.assertIn("validate_submit_sale_datetime", content)
        self.assertIn('order_doc.sale_datetime = validated["sale_datetime"]', content)

    def test_shift_api_uses_offline_first_timestamp_contract(self):
        content = (ERP_ROOT / "api" / "shifts.py").read_text()

        self.assertIn("MAX_FUTURE_TIMESTAMP_SKEW_SECONDS", content)
        self.assertIn("_normalize_offline_event_datetime", content)
        self.assertIn("_validate_closed_at_not_before_opened_at", content)
        self.assertNotIn("MAX_TIMESTAMP_SKEW_SECONDS", content)
