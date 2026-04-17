from __future__ import annotations

import unittest
from pathlib import Path


ERP_ROOT = Path(
    "/Users/victor/dev/jiji/JiJiPOS-Everything/worktree-fnb-erpnext/kopos_connector"
)


class TestFBAPIValidationContract(unittest.TestCase):
    def test_return_api_validates_resolved_sale_identity(self):
        content = (ERP_ROOT / "api" / "fb_returns.py").read_text()
        self.assertIn("original_resolved_sale", content)
        self.assertIn("resolved_sale_id", content)
        self.assertIn("qty_returned", content)
        self.assertIn("original_sales_invoice", content)

    def test_remake_api_validates_original_resolved_sale(self):
        content = (ERP_ROOT / "api" / "fb_remakes.py").read_text()
        self.assertIn("original_resolved_sale", content)
        self.assertIn("original_order", content)
        self.assertIn("reason_code", content)

    def test_waste_api_requires_company_warehouse_and_lines(self):
        content = (ERP_ROOT / "api" / "fb_waste.py").read_text()
        for token in ["waste_id", "company", "warehouse", "lines", "reason_code"]:
            self.assertIn(token, content)

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
