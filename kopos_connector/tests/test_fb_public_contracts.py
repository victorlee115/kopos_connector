from __future__ import annotations

import json
import unittest
from pathlib import Path


ERP_ROOT = Path(
    "/Users/victor/dev/jiji/JiJiPOS-Everything/worktree-fnb-erpnext/kopos_connector"
)
POS_ROOT = Path(
    "/Users/victor/dev/jiji/JiJiPOS-Everything/.worktrees/jijipos-mobile-sync-health/kopos"
)
TS_ROOT = Path(
    "/Users/victor/dev/jiji/JiJiPOS-Everything/.worktrees/jijipos-mobile-sync-health/kopos/src"
)


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

    def test_hooks_register_new_operational_events(self):
        content = (ERP_ROOT / "hooks.py").read_text()
        for doctype in [
            "FB Return Event",
            "FB Remake Event",
            "FB Waste Event",
            "FB Booth Refill Request",
        ]:
            self.assertIn(doctype, content)

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

    def test_shared_response_fixture_uses_canonical_vocabulary(self):
        fixture = json.loads(
            (POS_ROOT / "tests" / "fixtures" / "erpnext-responses.json").read_text()
        )
        fixture_text = json.dumps(fixture, sort_keys=True)

        for token in [
            "fb_shift",
            "fb_order",
            "sales_invoice",
            "idempotency_key",
            "projection_status",
        ]:
            self.assertIn(token, fixture_text)
        for forbidden in [
            "POS Invoice",
            "POS Opening Entry",
            "POS Closing Entry",
            "pos_invoice",
            "pos_opening_entry",
            "pos_closing_entry",
        ]:
            self.assertNotIn(forbidden, fixture_text)
