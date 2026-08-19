from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

import frappe

from kopos_connector.kopos.services.inventory_autopilot.promotion_economics import (
    PromotionEconomicsError,
    calculate_actual_cogs_from_stock_entries,
    calculate_promotion_economics,
    normalize_scenarios,
    summarize_actual_promotion_results,
)


class PromotionEconomicsTest(unittest.TestCase):
    def test_reports_cogs_margin_and_scenarios_in_integer_sen(self) -> None:
        result = calculate_promotion_economics(
            items=[{
                "units": 10,
                "baseline_price_sen": 1000,
                "promoted_price_sen": 800,
                "cogs_sen": 300,
                "components": [{"item": "Milk", "qty": 2}],
            }],
            scenarios={"low": 5, "base": 10, "high": 20},
        )
        self.assertEqual(result["revenue_sen"], 8000)
        self.assertEqual(result["cogs_sen"], 3000)
        self.assertEqual(result["gross_profit_sen"], 5000)
        self.assertEqual(result["ingredient_demand"], {"Milk": "20"})
        self.assertEqual(result["planning_mode"], "Review First")

    def test_missing_cost_blocks_publication(self) -> None:
        with self.assertRaisesRegex(PromotionEconomicsError, "cogs_sen is required"):
            calculate_promotion_economics(items=[{
                "units": 1,
                "baseline_price_sen": 100,
                "promoted_price_sen": 50,
            }])

    def test_reports_net_revenue_batch_impact_and_runout_without_float_authority(self) -> None:
        result = calculate_promotion_economics(
            items=[{
                "units": 10,
                "baseline_price_sen": 1000,
                "promoted_price_sen": 800,
                "baseline_net_revenue_sen": 1000,
                "net_revenue_sen": 800,
                "tax_sen": 64,
                "cogs_sen": 300,
                "item_group": "Cold drinks",
                "components": [{
                    "item": "Cold Foam",
                    "qty": "2",
                    "prepared": {"bom": "BOM-COLD-FOAM", "batch_qty": "10", "lead_minutes": "30"},
                    "inventory": {"usable_stock": "5"},
                }],
            }],
            scenarios={"low": 5, "base": 10, "high": 20},
        )
        self.assertEqual(result["net_revenue_sen"], 8000)
        self.assertEqual(result["tax_sen"], 640)
        self.assertEqual(result["worst_affected_item_group"]["item_group"], "Cold drinks")
        self.assertEqual(result["batch_preparation_impact"]["components"][0]["batches_required"], 2)
        self.assertEqual(result["batch_preparation_impact"]["components"][0]["scenario_batches"]["high"], 4)
        self.assertEqual(result["runout_waste_risk"]["items"][0]["runout"], "risk")

    def test_actual_results_use_persisted_promotion_provenance_and_disclose_missing_cogs(self) -> None:
        result = summarize_actual_promotion_results(
            records=[{
                "promotion_payload": {
                    "applied_promotions": [{"promotion_id": "PROMO-1", "amount_sen": 100}],
                    "items": [{"line_id": "LINE-1", "qty": 2, "unit_price_sen": 500, "line_total_sen": 1000, "promotion_allocations": [{"promotion_id": "PROMO-1", "amount_sen": 100, "quantity": 2}]}],
                },
                "tax_rate": "0.08",
            }],
            promotion_id="PROMO-1",
        )
        self.assertEqual(result["status"], "not_available")
        self.assertEqual(result["attribution_status"], "available")
        self.assertEqual(result["net_revenue_sen"], 1000)
        self.assertEqual(result["promoted_units"], 2)
        self.assertIsNone(result["cogs_sen"])
        self.assertIn("unavailable", result["note"])

    def test_actual_results_calculate_cogs_gross_profit_and_margin_from_submitted_issue(self) -> None:
        result = summarize_actual_promotion_results(
            records=[
                {
                    "promotion_payload": {
                        "applied_promotions": [{"promotion_id": "PROMO-1", "amount_sen": 100}],
                        "items": [{"line_id": "LINE-1", "qty": 2, "unit_price_sen": 500, "line_total_sen": 1000, "promotion_allocations": [{"promotion_id": "PROMO-1", "quantity": 2}]}],
                    },
                    "tax_rate": "0.08",
                    "actual_cogs_status": "available",
                    "actual_cogs_sen": 300,
                },
                {
                    "promotion_payload": {
                        "applied_promotions": [{"promotion_id": "PROMO-1", "amount_sen": 50}],
                        "items": [{"line_id": "LINE-2", "qty": 1, "unit_price_sen": 1200, "line_total_sen": 1200, "promotion_allocations": [{"promotion_id": "PROMO-1", "quantity": 1}]}],
                    },
                    "tax_rate": "0.08",
                    "actual_cogs_status": "available",
                    "actual_cogs_sen": 500,
                },
            ],
            promotion_id="PROMO-1",
        )
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["net_revenue_sen"], 2200)
        self.assertEqual(result["tax_sen"], 176)
        self.assertEqual(result["cogs_sen"], 800)
        self.assertEqual(result["gross_profit_sen"], 1400)
        self.assertEqual(result["margin_percent"], "63.64")
        self.assertEqual(result["cogs_source"], "submitted Material Issue Stock Entry valuation")

    def test_actual_results_use_only_promoted_share_of_mixed_order_line_totals(self) -> None:
        result = summarize_actual_promotion_results(
            records=[{
                "promotion_payload": {
                    "applied_promotions": [{"promotion_id": "PROMO-1", "amount_sen": 100}],
                    "items": [
                        {
                            "line_id": "LINE-PROMO",
                            "qty": 2,
                            "unit_price_sen": 1000,
                            "line_total_sen": 2000,
                            "promotion_allocations": [{"promotion_id": "PROMO-1", "quantity": 1}],
                        },
                        {
                            "line_id": "LINE-NORMAL",
                            "qty": 1,
                            "unit_price_sen": 5000,
                            "line_total_sen": 5000,
                            "promotion_allocations": [],
                        },
                    ],
                },
                "tax_rate": "0.08",
                "actual_cogs_status": "available",
                "actual_cogs_sen": 400,
            }],
            promotion_id="PROMO-1",
        )
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["promoted_units"], 1)
        self.assertEqual(result["net_revenue_sen"], 1000)
        self.assertEqual(result["tax_sen"], 80)
        self.assertEqual(result["gross_profit_sen"], 600)
        self.assertEqual(result["margin_percent"], "60.00")

    def test_actual_results_fail_closed_when_promoted_line_revenue_evidence_is_missing(self) -> None:
        result = summarize_actual_promotion_results(
            records=[{
                "promotion_payload": {
                    "applied_promotions": [{"promotion_id": "PROMO-1", "amount_sen": 100}],
                    "items": [{
                        "line_id": "LINE-1",
                        "promotion_allocations": [{"promotion_id": "PROMO-1", "quantity": 1}],
                    }],
                },
                "tax_rate": "0.08",
                "actual_cogs_status": "available",
                "actual_cogs_sen": 100,
            }],
            promotion_id="PROMO-1",
        )
        self.assertEqual(result["status"], "not_available")
        self.assertIsNone(result["net_revenue_sen"])
        self.assertIsNone(result["gross_profit_sen"])
        self.assertIn("revenue evidence is missing", result["reason"])

    def test_actual_cogs_allocation_uses_submitted_valuation_and_promoted_quantity(self) -> None:
        result = calculate_actual_cogs_from_stock_entries(
            resolved_sales=[
                {
                    "line_id": "LINE-PROMO",
                    "qty": "2",
                    "stock_entry": "STE-1",
                    "components": [{
                        "item": "Cold Foam",
                        "stock_qty": "4",
                        "stock_uom": "Unit",
                        "warehouse": "Outlet - JJ",
                        "affects_stock": 1,
                        "affects_cogs": 1,
                    }],
                },
                {
                    "line_id": "LINE-FULL",
                    "qty": "1",
                    "stock_entry": "STE-1",
                    "components": [{
                        "item": "Cold Foam",
                        "stock_qty": "2",
                        "stock_uom": "Unit",
                        "warehouse": "Outlet - JJ",
                        "affects_stock": 1,
                        "affects_cogs": 1,
                    }],
                },
            ],
            promoted_line_quantities={"LINE-PROMO": "1"},
            stock_entries={"STE-1": {
                "docstatus": 1,
                "stock_entry_type": "Material Issue",
                "items": [{
                    "item_code": "Cold Foam",
                    "s_warehouse": "Outlet - JJ",
                    "stock_uom": "Unit",
                    "qty": "6",
                    "basic_amount_sen": 900,
                }],
            }},
        )
        self.assertEqual(result, {
            "status": "available",
            "cogs_sen": 300,
            "source": "submitted Material Issue Stock Entry valuation",
        })

    def test_actual_cogs_never_treats_missing_valuation_as_zero(self) -> None:
        result = calculate_actual_cogs_from_stock_entries(
            resolved_sales=[{
                "line_id": "LINE-1",
                "qty": "1",
                "stock_entry": "STE-1",
                "components": [{
                    "item": "Milk",
                    "stock_qty": "1",
                    "stock_uom": "L",
                    "warehouse": "Outlet - JJ",
                    "affects_stock": 1,
                    "affects_cogs": 1,
                }],
            }],
            promoted_line_quantities={"LINE-1": "1"},
            stock_entries={"STE-1": {
                "docstatus": 1,
                "stock_entry_type": "Material Issue",
                "items": [{
                    "item_code": "Milk",
                    "s_warehouse": "Outlet - JJ",
                    "stock_uom": "L",
                    "qty": "1",
                }],
            }},
        )
        self.assertEqual(result["status"], "not_available")
        self.assertIn("no authoritative valuation", result["reason"])

    def test_actual_results_are_explicitly_unavailable_without_attribution(self) -> None:
        result = summarize_actual_promotion_results(records=[], promotion_id="PROMO-1")
        self.assertEqual(result["status"], "not_available")

    def test_commercial_promotion_snapshot_contains_no_economics_values(self) -> None:
        from kopos_connector.api.promotions import serialize_promotion

        promotion = SimpleNamespace(
            name="PROMO-1",
            promotion_name="Launch offer",
            display_label="Launch offer",
            customer_message="10% off",
            promotion_type="item_discount",
            activation_mode="automatic",
            offline_allowed=0,
            priority=10,
            stacking_policy="exclusive",
            discount_target="cheaper_eligible",
            discount_type="percentage",
            discount_value=10,
            buy_qty=0,
            discount_qty=0,
            repeat_mode="once",
            eligible_scope_mode="eligible_pool",
            comparison_basis="base_item_only",
            discount_basis="base_item_only",
            modifier_policy="excluded_by_default",
            valid_from=None,
            valid_upto=None,
            eligible_items=[],
            min_qty=0,
            min_amount=0,
            eligible_item_groups=[],
            eligible_pos_profiles=[],
            economics_status="Ready",
            economics_source_hash="secret-hash",
            cogs_sen=100,
        )
        payload = serialize_promotion(promotion, "POS-1")
        self.assertNotIn("cogs_sen", payload)
        self.assertNotIn("economics_source_hash", payload)
        self.assertNotIn("economics_status", payload)

    def test_missing_tax_configuration_and_stale_valuation_fail_closed(self) -> None:
        from kopos_connector.api import inventory

        with self.assertRaisesRegex(PromotionEconomicsError, "tax evidence is missing"):
            inventory._promotion_tax_rate({"custom_kopos_enable_sst": 1})
        with patch.object(inventory, "now_datetime", return_value=inventory.get_datetime("2026-03-13T18:05:00")):
            with self.assertRaisesRegex(PromotionEconomicsError, "valuation evidence is stale"):
                inventory._require_current_valuation_evidence(
                    item_code="Milk",
                    source="Bin",
                    modified="2026-03-13T16:00:00",
                    max_source_age_minutes=30,
                )

    def test_scenarios_are_limited_to_bounded_whole_number_inputs(self) -> None:
        self.assertEqual(
            normalize_scenarios({"low": 5, "base": "10", "high": 25.0}),
            {"low": 5, "base": 10, "high": 25},
        )
        with self.assertRaisesRegex(PromotionEconomicsError, "only contain low, base and high"):
            normalize_scenarios({"items": 10})
        with self.assertRaisesRegex(PromotionEconomicsError, "between 1 and"):
            normalize_scenarios({"low": 1_000_001})

    def test_economics_source_hash_is_stable_for_mapping_order(self) -> None:
        from kopos_connector.kopos.services.inventory_autopilot.promotion_economics import (
            economics_source_hash,
        )

        self.assertEqual(
            economics_source_hash({"b": 2, "a": 1}),
            economics_source_hash({"a": 1, "b": 2}),
        )

    def test_api_rejects_browser_supplied_items_and_cogs(self) -> None:
        from kopos_connector.api import inventory

        with self.assertRaisesRegex(
            frappe.ValidationError,
            "Item and COGS data are server-owned",
        ):
            inventory.get_promotion_economics(
                payload=json.dumps(
                    {
                        "promotion": "PROMO-1",
                        "items": [{"cogs_sen": 1, "promoted_price_sen": 100}],
                    }
                )
            )

    def test_override_requires_a_different_director_than_the_checker(self) -> None:
        from kopos_connector.api import inventory

        promotion = SimpleNamespace(
            economics_checked_by="Administrator",
            economics_source_hash="a" * 64,
        )
        with patch.object(frappe, "get_doc", return_value=promotion):
            with self.assertRaisesRegex(
                frappe.ValidationError,
                "second Company Director",
            ):
                inventory.approve_promotion_economics_override(
                    payload=json.dumps(
                        {
                            "promotion": "PROMO-1",
                            "economics_hash": "a" * 64,
                            "reason": "Director-approved exception for an explicitly documented launch offer.",
                        }
                    )
                )


if __name__ == "__main__":
    unittest.main()
