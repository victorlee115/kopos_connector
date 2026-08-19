from __future__ import annotations

import json
import unittest
from pathlib import Path

import pytest


ERP_ROOT = Path(__file__).resolve().parents[1]
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
                "open_idempotency_key",
                "open_request_fingerprint",
                "close_idempotency_key",
                "close_request_fingerprint",
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
                "request_fingerprint",
                "accepted_sale_fingerprint",
                "automatic_qr_state",
                "automatic_qr_payment",
                "automatic_qr_winner_channel",
                "automatic_qr_static_reconciliation",
                "automatic_qr_accepted_at",
                "sale_datetime",
                "shift",
                "staff_id",
                "sales_invoice",
                "invoice_status",
                "items",
                "payments",
            }.issubset(names)
        )
        self.assertEqual(doc.get("is_submittable"), 1)
        sale_datetime = next(
            field
            for field in doc["fields"]
            if field.get("fieldname") == "sale_datetime"
        )
        self.assertEqual(sale_datetime["fieldtype"], "Datetime")
        self.assertNotEqual(sale_datetime.get("reqd"), 1)

        automatic_qr_state = next(
            field
            for field in doc["fields"]
            if field.get("fieldname") == "automatic_qr_state"
        )
        for state in (
            "prepared",
            "provider_pending",
            "provider_ambiguous",
            "provider_rejected",
            "provider_paid",
            "manual_pending_reconciliation",
            "finalized",
        ):
            self.assertIn(state, automatic_qr_state["options"])

        winner_channel = next(
            field
            for field in doc["fields"]
            if field.get("fieldname") == "automatic_qr_winner_channel"
        )
        self.assertIn("maybank_qr", winner_channel["options"])
        self.assertIn("static_qr", winner_channel["options"])
        static_reconciliation = next(
            field
            for field in doc["fields"]
            if field.get("fieldname")
            == "automatic_qr_static_reconciliation"
        )
        self.assertEqual(
            static_reconciliation["options"],
            "Manual QR Reconciliation",
        )

    @pytest.mark.inventory_regression
    def test_fb_order_inventory_schema(self):
        names = fieldnames(load_doctype("fb_order"))
        self.assertTrue(
            {
                "booth_warehouse",
                "ingredient_stock_entry",
                "stock_status",
            }.issubset(names)
        )

    def test_maybank_duplicate_winner_schema_supports_static_sales(self):
        doc = load_doctype("maybank_qr_transaction")
        fields = {
            field["fieldname"]: field
            for field in doc["fields"]
            if field.get("fieldname")
        }
        self.assertIn("maybank_qr", fields["duplicate_winning_channel"]["options"])
        self.assertIn("static_qr", fields["duplicate_winning_channel"]["options"])
        self.assertEqual(
            fields["duplicate_winning_static_reconciliation"]["options"],
            "Manual QR Reconciliation",
        )

    def test_manual_qr_reconciliation_distinguishes_secondary_static_claims(self):
        doc = load_doctype("manual_qr_reconciliation")
        fields = {
            field["fieldname"]: field
            for field in doc["fields"]
            if field.get("fieldname")
        }
        self.assertIn("winning_settlement", fields["claim_role"]["options"])
        self.assertIn(
            "secondary_possible_duplicate",
            fields["claim_role"]["options"],
        )
        self.assertEqual(
            fields["winning_maybank_qr_transaction"]["options"],
            "Maybank QR Transaction",
        )
        self.assertNotEqual(fields["suspense_account"].get("reqd"), 1)
        self.assertIn(
            "pending_review",
            fields["finance_resolution_status"]["options"],
        )
        self.assertIn(
            "no_second_credit",
            fields["finance_resolution_status"]["options"],
        )
        self.assertIn(
            "refund_required",
            fields["finance_resolution_status"]["options"],
        )
        self.assertIn(
            "refunded",
            fields["finance_resolution_status"]["options"],
        )
        for required_field in (
            "finance_resolution_key",
            "finance_resolution_idempotency_key",
            "finance_credit_evidence_file",
            "finance_credit_evidence_sha256",
            "finance_credit_evidence_byte_length",
            "finance_liability_journal_entry",
            "finance_refund_key",
            "finance_refund_idempotency_key",
            "finance_refund_evidence_file",
            "finance_refund_evidence_sha256",
            "finance_refund_evidence_byte_length",
            "finance_refund_journal_entry",
            "finance_resolved_by",
            "finance_resolved_at",
        ):
            self.assertIn(required_field, fields)
        for optional_unique_field in (
            "finance_resolution_key",
            "finance_resolution_idempotency_key",
            "finance_credit_reference",
            "finance_refund_key",
            "finance_refund_idempotency_key",
            "finance_refund_reference",
        ):
            field = fields[optional_unique_field]
            self.assertEqual(field.get("unique"), 1)
            self.assertNotEqual(field.get("reqd"), 1)
            self.assertNotIn("default", field)

    @pytest.mark.inventory_regression
    def test_prepared_resolved_sale_schema(self):
        doc = load_doctype("fb_resolved_sale")
        status = next(
            field for field in doc["fields"] if field.get("fieldname") == "status"
        )
        self.assertIn("Prepared", status["options"])

    @pytest.mark.inventory_regression
    def test_fb_stock_override_log_schema(self):
        doc = load_doctype("fb_stock_override_log")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "override_id",
                "fb_order",
                "order_reference",
                "warehouse",
                "item",
                "requested_qty",
                "available_qty_before",
                "shortfall_qty",
                "reason_code",
                "reason_text",
                "approved_at",
                "logged_at",
            }.issubset(names)
        )

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
                "commercial_modifier_snapshot_json",
            }.issubset(names)
        )
        self.assertEqual(doc.get("istable"), 1)

    @pytest.mark.inventory_regression
    def test_fb_order_line_inventory_schema(self):
        names = fieldnames(load_doctype("fb_order_line"))
        self.assertTrue(
            {
                "recipe",
                "recipe_version",
                "resolved_sale",
                "selected_modifiers",
                "resolved_components_snapshot",
            }.issubset(names)
        )

    def test_fb_order_payment_distinguishes_provider_wait_from_reconciliation(self):
        doc = load_doctype("fb_order_payment")
        settlement_status = next(
            field
            for field in doc["fields"]
            if field.get("fieldname") == "settlement_status"
        )
        self.assertIn("awaiting_provider", settlement_status["options"])
        self.assertIn("pending_reconciliation", settlement_status["options"])

    @pytest.mark.inventory_regression
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

    @pytest.mark.inventory_regression
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

    @pytest.mark.inventory_regression
    def test_fb_modifier_group_schema(self):
        doc = load_doctype("fb_modifier_group")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "group_code",
                "group_name",
                "selection_type",
                "is_required",
                "min_selection",
                "max_selection",
                "display_order",
                "active",
                "parent_modifier",
                "default_resolution_policy",
            }.issubset(names)
        )
        parent_modifier_field = next(
            field
            for field in doc["fields"]
            if field.get("fieldname") == "parent_modifier"
        )
        self.assertEqual(parent_modifier_field["fieldtype"], "Link")
        self.assertEqual(parent_modifier_field["options"], "FB Modifier")
        self.assertEqual(parent_modifier_field["label"], "Parent Modifier Option")
        self.assertIn("Ice Level", parent_modifier_field["description"])

    @pytest.mark.inventory_regression
    def test_fb_modifier_group_authoring_script_exists(self):
        script_path = DOCTYPE_ROOT / "fb_modifier_group" / "fb_modifier_group.js"

        self.assertTrue(script_path.exists())
        script = script_path.read_text()
        self.assertIn('frappe.ui.form.on("FB Modifier Group"', script)
        self.assertIn("parent_modifier", script)
        self.assertIn("Temperature contains Hot and Iced", script)

    @pytest.mark.inventory_regression
    def test_fb_modifier_dependency_authoring_doc_exists(self):
        doc_path = ERP_ROOT / "docs" / "FB_MODIFIER_AUTHORING.md"

        self.assertTrue(doc_path.exists())
        content = doc_path.read_text()
        self.assertIn("Temperature", content)
        self.assertIn("Ice Level", content)
        self.assertIn("Iced", content)

    @pytest.mark.inventory_regression
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

    @pytest.mark.inventory_regression
    def test_fb_resolved_component_preserves_exact_decimal_text_for_inventory(self):
        doc = load_doctype("fb_resolved_component")
        fields = {field["fieldname"]: field for field in doc["fields"]}
        for fieldname in ("qty_decimal", "stock_qty_decimal"):
            self.assertIn(fieldname, fields)
            self.assertEqual(fields[fieldname]["fieldtype"], "Data")
            self.assertEqual(fields[fieldname].get("hidden"), 1)
            self.assertEqual(fields[fieldname].get("read_only"), 1)

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
        projection_type_field = next(
            field
            for field in doc["fields"]
            if field.get("fieldname") == "projection_type"
        )
        for option in ["Sales Invoice", "FB Shift"]:
            self.assertIn(option, projection_type_field["options"])

    @pytest.mark.inventory_regression
    def test_fb_projection_log_inventory_schema(self):
        doc = load_doctype("fb_projection_log")
        projection_type_field = next(
            field
            for field in doc["fields"]
            if field.get("fieldname") == "projection_type"
        )
        self.assertIn("Stock Issue", projection_type_field["options"])

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
                "status",
                "request_fingerprint",
                "approval_token_id",
                "approved_by_manager",
                "lines",
            }.issubset(names)
        )
        self.assertEqual(doc.get("is_submittable"), 1)

    @pytest.mark.inventory_regression
    def test_fb_return_event_inventory_schema(self):
        names = fieldnames(load_doctype("fb_return_event"))
        self.assertIn("return_to_stock", names)

    def test_fb_return_event_line_supports_commercial_and_legacy_identity(self):
        doc = load_doctype("fb_return_event_line")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "original_sales_invoice_item",
                "original_fb_order_line_ref",
                "qty_returned",
                "commercial_modifier_snapshot_json",
            }.issubset(names)
        )
        invoice_item_field = next(
            field
            for field in doc["fields"]
            if field.get("fieldname") == "original_sales_invoice_item"
        )
        self.assertEqual(invoice_item_field.get("search_index"), 1)

    @pytest.mark.inventory_regression
    def test_fb_return_event_line_inventory_schema(self):
        doc = load_doctype("fb_return_event_line")
        names = fieldnames(doc)
        self.assertTrue(
            {"original_resolved_sale", "reversal_stock_entry"}.issubset(names)
        )
        resolved_sale_field = next(
            field
            for field in doc["fields"]
            if field.get("fieldname") == "original_resolved_sale"
        )
        self.assertFalse(resolved_sale_field.get("reqd"))

    def test_manager_approval_schema_is_durable_and_non_deletable(self):
        doc = load_doctype("kopos_manager_approval")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "token_id",
                "status",
                "token_digest",
                "device_id",
                "staff_id",
                "manager_id",
                "authorization_mode",
                "action",
                "shift_id",
                "resource_id",
                "amount_sen",
                "context_hash",
                "expires_at",
                "consumed_at",
                "consumed_idempotency_key",
            }.issubset(names)
        )
        self.assertTrue(
            all(not permission.get("delete") for permission in doc["permissions"])
        )

    @pytest.mark.inventory_regression
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

    @pytest.mark.inventory_regression
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

    @pytest.mark.inventory_regression
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
            "fb_order_payment",
            "fb_return_event_line",
        ]:
            doc = load_doctype(child)
            self.assertEqual(doc.get("istable"), 1, child)

    @pytest.mark.inventory_regression
    def test_inventory_child_tables_exist_and_are_tables(self):
        for child in [
            "fb_recipe_component",
            "fb_allowed_modifier_group",
            "fb_selected_modifier",
            "fb_resolved_component",
            "fb_waste_event_line",
            "fb_booth_refill_line",
        ]:
            doc = load_doctype(child)
            self.assertEqual(doc.get("istable"), 1, child)

    def test_maybank_qr_replacement_identity_is_durable(self):
        doc = load_doctype("maybank_qr_transaction")
        names = fieldnames(doc)
        self.assertTrue(
            {
                "replacement_reason",
                "replaces_transaction_refno",
                "round_number",
            }.issubset(names)
        )
        reason_field = next(
            field
            for field in doc["fields"]
            if field.get("fieldname") == "replacement_reason"
        )
        self.assertIn("expired_display", reason_field["options"])
        self.assertIn("unrenderable_display", reason_field["options"])
