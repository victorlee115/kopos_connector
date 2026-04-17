from __future__ import annotations

import json
import unittest
from pathlib import Path


ERP_ROOT = Path(
    "/Users/victor/dev/jiji/JiJiPOS-Everything/worktree-fnb-erpnext/kopos_connector"
)
DOCTYPE_ROOT = ERP_ROOT / "kopos" / "doctype"


def load_doctype(name: str) -> dict:
    path = DOCTYPE_ROOT / name / f"{name}.json"
    return json.loads(path.read_text())


def fieldnames(doctype: dict) -> set[str]:
    return {
        field.get("fieldname")
        for field in doctype.get("fields", [])
        if field.get("fieldname")
    }


class TestFBSchemaContract(unittest.TestCase):
    def test_fb_shift_schema(self):
        doc = load_doctype("fb_shift")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "shift_code",
                "device_id",
                "staff_id",
                "warehouse",
                "company",
                "status",
                "expected_cash",
                "counted_cash",
                "cash_variance",
                "close_blocked_reason",
            }.issubset(names)
        )
        status_field = next(
            field for field in doc["fields"] if field.get("fieldname") == "status"
        )
        self.assertIn("Exception", status_field["options"])

    def test_fb_order_schema(self):
        doc = load_doctype("fb_order")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "order_id",
                "external_idempotency_key",
                "shift",
                "staff_id",
                "booth_warehouse",
                "sales_invoice",
                "ingredient_stock_entry",
                "invoice_status",
                "stock_status",
                "items",
                "payments",
            }.issubset(names)
        )
        self.assertEqual(doc.get("is_submittable"), 1)

    def test_fb_order_line_schema(self):
        doc = load_doctype("fb_order_line")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "line_id",
                "backend_line_uuid",
                "item",
                "qty",
                "uom",
                "unit_price",
                "line_total",
                "recipe",
                "recipe_version",
                "resolved_sale",
                "selected_modifiers",
                "resolved_components_snapshot",
            }.issubset(names)
        )
        self.assertEqual(doc.get("istable"), 1)

    def test_fb_recipe_schema(self):
        doc = load_doctype("fb_recipe")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "recipe_code",
                "recipe_name",
                "sellable_item",
                "recipe_type",
                "status",
                "version_no",
                "yield_qty",
                "yield_uom",
                "default_serving_qty",
                "default_serving_uom",
                "components",
                "allowed_modifier_groups",
            }.issubset(names)
        )

    def test_fb_modifier_schema(self):
        doc = load_doctype("fb_modifier")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "modifier_code",
                "modifier_name",
                "modifier_group",
                "kind",
                "price_adjustment",
                "target_substitution_key",
                "target_item",
                "new_item",
                "qty_delta",
                "qty_uom",
                "scale_percent",
                "instruction_text",
                "affects_stock",
                "affects_recipe",
                "is_default",
            }.issubset(names)
        )
        kind_field = next(
            field for field in doc["fields"] if field.get("fieldname") == "kind"
        )
        for option in ["Instruction Only", "Add", "Replace", "Remove", "Scale"]:
            self.assertIn(option, kind_field["options"])

    def test_fb_resolved_sale_schema(self):
        doc = load_doctype("fb_resolved_sale")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "resolved_sale_id",
                "fb_order",
                "fb_order_line",
                "backend_line_uuid",
                "sales_invoice",
                "sellable_item",
                "qty",
                "recipe",
                "recipe_version",
                "resolution_hash",
                "stock_entry_issue",
                "stock_entry_reversal",
                "selected_modifiers",
                "resolved_components",
            }.issubset(names)
        )

    def test_fb_projection_log_schema(self):
        doc = load_doctype("fb_projection_log")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "projection_id",
                "source_doctype",
                "source_name",
                "projection_type",
                "idempotency_key",
                "payload_hash",
                "target_doctype",
                "target_name",
                "state",
                "retry_count",
                "last_error",
                "created_at",
                "last_attempt_at",
            }.issubset(names)
        )
        state_field = next(
            field for field in doc["fields"] if field.get("fieldname") == "state"
        )
        for option in ["Pending", "Succeeded", "Failed", "Reversed"]:
            self.assertIn(option, state_field["options"])

    def test_fb_return_event_schema(self):
        doc = load_doctype("fb_return_event")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "return_id",
                "fb_order",
                "original_sales_invoice",
                "return_sales_invoice",
                "reason_code",
                "return_to_stock",
                "status",
                "lines",
            }.issubset(names)
        )
        self.assertEqual(doc.get("is_submittable"), 1)

    def test_fb_remake_event_schema(self):
        doc = load_doctype("fb_remake_event")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "remake_id",
                "original_order",
                "original_order_line",
                "original_resolved_sale",
                "reason_code",
                "replacement_stock_entry",
                "status",
            }.issubset(names)
        )
        self.assertEqual(doc.get("is_submittable"), 1)

    def test_fb_waste_event_schema(self):
        doc = load_doctype("fb_waste_event")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "waste_id",
                "company",
                "warehouse",
                "reason_code",
                "stock_entry",
                "status",
                "lines",
            }.issubset(names)
        )
        self.assertEqual(doc.get("is_submittable"), 1)

    def test_fb_refill_schema(self):
        doc = load_doctype("fb_booth_refill_request")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "request_id",
                "company",
                "from_warehouse",
                "to_warehouse",
                "status",
                "requested_by",
                "approved_by",
                "fulfilled_stock_entry",
                "lines",
            }.issubset(names)
        )
        self.assertEqual(doc.get("is_submittable"), 1)
        status_field = next(
            field for field in doc["fields"] if field.get("fieldname") == "status"
        )
        self.assertIn("Fulfilled", status_field["options"])

    def test_child_tables_exist_and_are_tables(self):
        for child in [
            "fb_recipe_component",
            "fb_allowed_modifier_group",
            "fb_selected_modifier",
            "fb_resolved_component",
            "fb_order_payment",
            "fb_return_event_line",
            "fb_waste_event_line",
            "fb_booth_refill_line",
        ]:
            doc = load_doctype(child)
            self.assertEqual(doc.get("istable"), 1, child)
