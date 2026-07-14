from __future__ import annotations

import json
import os
import unittest
from pathlib import Path


ERP_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ERP_ROOT.parents[1]
POS_ROOT = WORKSPACE_ROOT / "JiJiPOS" / "kopos"
TS_ROOT = Path(os.environ.get("KOPOS_TYPESCRIPT_ROOT", POS_ROOT / "src"))


class TestFBPublicContracts(unittest.TestCase):
    def test_public_api_modules_exist(self):
        for relative in [
            "api/fb_orders.py",
            "api/fb_returns.py",
            "api/fb_remakes.py",
            "api/fb_waste.py",
            "api/fb_refill.py",
            "api/fb_shifts.py",
        ]:
            self.assertTrue((ERP_ROOT / relative).exists(), relative)

    def test_public_api_methods_are_whitelisted(self):
        expected = {
            "api/fb_orders.py": [
                ("submit_order", "POST"),
                ("get_order_status", "GET"),
                ("retry_failed_projections", "POST"),
            ],
            "api/fb_returns.py": [("process_return", "POST")],
            "api/fb_remakes.py": [("process_remake", "POST")],
            "api/fb_waste.py": [("process_waste", "POST")],
            "api/fb_refill.py": [("process_refill", "POST")],
        }
        for relative, methods in expected.items():
            content = (ERP_ROOT / relative).read_text()
            for method, expected_method in methods:
                if expected_method == "POST":
                    self.assertIn(
                        "@frappe.whitelist(methods=[\"POST\"])",
                        content,
                        f"{relative}:{method}",
                    )
                else:
                    self.assertIn("@frappe.whitelist()", content, f"{relative}:{method}")
                self.assertIn(f"def {method}", content, f"{relative}:{method}")

    def test_operational_events_use_doctype_controllers_only(self):
        content = (ERP_ROOT / "hooks.py").read_text()
        for doctype in [
            "FB Return Event",
            "FB Remake Event",
            "FB Waste Event",
            "FB Booth Refill Request",
        ]:
            self.assertNotIn(doctype, content)
        for relative in [
            "kopos/doctype/fb_return_event/fb_return_event.py",
            "kopos/doctype/fb_remake_event/fb_remake_event.py",
            "kopos/doctype/fb_waste_event/fb_waste_event.py",
            "kopos/doctype/fb_booth_refill_request/fb_booth_refill_request.py",
        ]:
            self.assertIn("def on_submit", (ERP_ROOT / relative).read_text())

    def test_hooks_do_not_activate_legacy_pos_invoice_behavior(self):
        content = (ERP_ROOT / "hooks.py").read_text()
        self.assertIn("doctype_js = {}", content)
        self.assertNotIn('"POS Invoice": "public/js/pos_invoice.js"', content)

    def test_custom_field_installer_covers_standard_docs(self):
        content = (ERP_ROOT / "kopos" / "install" / "fb_custom_fields.py").read_text()
        for doctype in ["Item", "Sales Invoice", "Sales Invoice Item", "Stock Entry"]:
            self.assertIn(f'"{doctype}"', content)
        for field in [
            "custom_fb_item_role",
            "custom_fb_recipe_required",
            "custom_fb_order",
            "custom_fb_shift",
            "custom_fb_resolved_sale",
            "custom_fb_reason_code",
        ]:
            self.assertIn(field, content)

    def test_typescript_contracts_include_required_fields(self):
        if not TS_ROOT.exists():
            self.skipTest(
                "TypeScript contract checkout is not present; the cross-repository CI gate supplies KOPOS_TYPESCRIPT_ROOT"
            )
        contracts = (TS_ROOT / "services" / "api" / "fb-contracts.ts").read_text()
        types = (TS_ROOT / "types" / "fb-types.ts").read_text()
        for token in [
            "idempotency_key",
            "device_id",
            "shift_id",
            "staff_id",
            "event_project",
            "line_id",
            "item_code",
            "payment_method",
            "ingredient_stock_entry",
            "order_status",
        ]:
            self.assertIn(token, contracts)
        for token in [
            "FbOrderStatus",
            "FbInvoiceStatus",
            "FbStockStatus",
            "FbShiftStatus",
            "Exception",
        ]:
            self.assertIn(token, types)

    def test_maybank_qr_schema_uses_new_links(self):
        schema = json.loads(
            (
                ERP_ROOT
                / "kopos"
                / "doctype"
                / "maybank_qr_transaction"
                / "maybank_qr_transaction.json"
            ).read_text()
        )
        names = {field.get("fieldname") for field in schema.get("fields", [])}
        self.assertIn("fb_order", names)
        self.assertIn("sales_invoice", names)
        self.assertNotIn("pos_invoice", names)
