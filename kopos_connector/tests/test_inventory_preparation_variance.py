from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector.kopos.services.inventory_autopilot import preparation


class InventoryPreparationVarianceTests(TestCase):
    def test_bom_ceiling_takes_precedence_over_policy_ceiling(self) -> None:
        self.assertEqual(
            preparation._select_preparation_variance_ceiling(
                bom_value="4",
                policy_value="8",
                has_bom_field=True,
                has_policy_field=True,
            ),
            4,
        )

    def test_blank_ceiling_is_not_replaced_with_a_guessed_default(self) -> None:
        with self.assertRaises(ValueError):
            preparation._select_preparation_variance_ceiling(
                bom_value="",
                policy_value="",
                has_bom_field=True,
                has_policy_field=True,
            )

    def test_preflight_reports_enabled_bom_without_threshold(self) -> None:
        with patch.object(preparation.frappe.db, "exists", return_value=True), patch.object(
            preparation.frappe, "get_all", return_value=[{"name": "BOM-COLD-FOAM", "item": "COLD-FOAM"}],
        ), patch.object(preparation.frappe.db, "get_value", return_value=""):
            failures = preparation.preparation_variance_preflight(
                company="JiJi",
                warehouse="Outlet - KL",
            )
        self.assertEqual(
            failures,
            ("batch_preparation_variance_threshold_invalid:BOM-COLD-FOAM:a BOM or policy variance ceiling is required",),
        )

    def test_posted_batch_above_ceiling_opens_one_warning_exception(self) -> None:
        order = SimpleNamespace(
            bom_no="BOM-COLD-FOAM",
            company="JiJi",
            fg_warehouse="Outlet - KL",
            production_item="COLD-FOAM",
            qty="10",
        )
        stock_entry = SimpleNamespace(name="MAT-0001")
        with patch.object(preparation.frappe.db, "exists", return_value=True), patch.object(
            preparation.frappe.db,
            "get_value",
            side_effect=["5", ""],
        ), patch.object(
            preparation,
            "upsert_inventory_exception",
            return_value="EX-1",
        ) as upsert:
            result = preparation.record_preparation_variance(
                order=order,
                stock_entry=stock_entry,
                actual_yield="7",
                waste_qty="1",
            )

        self.assertEqual(result, "EX-1")
        self.assertEqual(upsert.call_args.kwargs["reason_code"], "batch_preparation_variance")
        self.assertEqual(upsert.call_args.kwargs["source_name"], "MAT-0001")


if __name__ == "__main__":
    import unittest

    unittest.main()
