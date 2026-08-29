"""The commissioning fixture must stay hypothetical and exactly convertible."""

from __future__ import annotations

from decimal import Decimal
from unittest import TestCase

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.acceptance import restored_commissioning as commissioning
from kopos_connector.acceptance.restored_inventory_acceptance import AUTHORITY_PREFIX


class RestoredCommissioningContractTests(TestCase):
    def test_every_commissioned_document_carries_the_fixture_prefix(self) -> None:
        """Nothing this producer creates may look like production data."""

        names = [spec["code"] for spec in commissioning.INGREDIENTS]
        names += [spec["code"] for spec in commissioning.PREPARED_COMPONENTS]
        names += [spec["code"] for spec in commissioning.SELLABLES]
        names += [spec["code"] for spec in commissioning.MODIFIER_GROUPS]
        names += [option["code"] for spec in commissioning.MODIFIER_GROUPS for option in spec["options"]]
        names += [spec["name"] for spec in commissioning.PROMOTIONS]
        names += [commissioning.SUPPLIER, commissioning.RECIPE_CODE]
        for name in names:
            self.assertTrue(
                name.startswith(AUTHORITY_PREFIX),
                f"{name} is not clearly marked as a fixture",
            )

    def test_every_purchase_conversion_is_exact_and_whole(self) -> None:
        """A rounded or inferred factor would corrupt every downstream quantity."""

        for spec in commissioning.INGREDIENTS:
            factor = spec["conversion"]
            self.assertIsInstance(factor, Decimal)
            self.assertGreater(factor, 0)
            self.assertEqual(factor, factor.to_integral_value())

    def test_supplier_pack_rates_resolve_to_exact_stock_valuation(self) -> None:
        for spec in commissioning.INGREDIENTS:
            self.assertEqual(
                spec["valuation_rate"] * spec["conversion"],
                spec["purchase_rate"],
                f"{spec['code']} does not have exact pack economics",
            )

    def test_ingredients_use_the_standard_fnb_item_roles(self) -> None:
        allowed = {"Ingredient", "Prep Item", "Packaging", "Tool", "Asset Managed Gear", "Sellable Drink"}
        for spec in commissioning.INGREDIENTS:
            self.assertIn(spec["role"], allowed)

    def test_short_life_stock_declares_batch_tracking(self) -> None:
        """Expiry cannot be enforced on stock that is not batch tracked."""

        for spec in commissioning.INGREDIENTS:
            if spec["shelf_life_days"] and spec["shelf_life_days"] <= 30:
                self.assertTrue(
                    spec["has_batch_no"],
                    f"{spec['code']} has a short shelf life but no batch tracking",
                )

    def test_the_recipe_consumes_prepared_stock_not_its_bom(self) -> None:
        """A sale consumes the prepared Item; manufacturing consumes the BOM.

        The BOM builds 1,000 ml of cold foam. If the recipe expanded the BOM it
        would consume that batch quantity again on every single sale.
        """

        components = dict(commissioning.RECIPE_COMPONENTS)
        self.assertIn(commissioning.PREPARED_ITEM, components)
        self.assertEqual(Decimal(components[commissioning.PREPARED_ITEM]), Decimal("50"))
        self.assertLess(
            Decimal(components[commissioning.PREPARED_ITEM]),
            commissioning.PREPARED_BOM_QTY,
        )

    def test_preparation_thresholds_are_orderable(self) -> None:
        self.assertGreater(commissioning.PREPARED_BOM_QTY, commissioning.PREPARED_MIN_READY_QTY)
        self.assertGreater(commissioning.PREPARED_LEAD_MINUTES, 0)

    def test_fictional_cafe_is_a_complete_stakeholder_dataset(self) -> None:
        self.assertEqual(len(commissioning.INGREDIENTS), 12)
        self.assertEqual(len(commissioning.PREPARED_COMPONENTS), 2)
        self.assertEqual(len(commissioning.SELLABLES), 6)
        self.assertEqual(len(commissioning.MODIFIER_GROUPS), 3)
        self.assertEqual(len(commissioning.PROMOTIONS), 2)
        self.assertEqual(
            set(commissioning.OPENING_PACKS),
            {spec["code"] for spec in commissioning.INGREDIENTS},
        )
        self.assertTrue(all(packs > 0 for packs in commissioning.OPENING_PACKS.values()))

    def test_every_sellable_has_packaging_and_unique_stock_components(self) -> None:
        packaging = {row["item"] for row in commissioning.PACKAGING_COMPONENTS}
        for sellable in commissioning.SELLABLES:
            components = sellable["components"]
            item_codes = [row["item"] for row in components]
            self.assertEqual(len(item_codes), len(set(item_codes)))
            self.assertTrue(packaging.issubset(item_codes))
            self.assertGreater(sellable["price"], 0)

    def test_every_prepared_batch_has_exact_yield_and_real_inputs(self) -> None:
        stock_items = {spec["code"] for spec in commissioning.INGREDIENTS}
        for prepared in commissioning.PREPARED_COMPONENTS:
            self.assertEqual(
                sum(Decimal(qty) for _item, qty in prepared["inputs"]),
                prepared["batch_qty"],
            )
            self.assertTrue(all(item in stock_items for item, _qty in prepared["inputs"]))
            self.assertGreater(prepared["batch_qty"], prepared["min_ready_qty"])
            self.assertGreater(len(prepared["instructions"]), 40)

    def test_every_stock_modifier_has_an_exact_operational_effect(self) -> None:
        for group in commissioning.MODIFIER_GROUPS:
            defaults = [option for option in group["options"] if option["default"]]
            if group["required"]:
                self.assertEqual(len(defaults), 1)
            for option in group["options"]:
                if option["kind"] == "Add":
                    self.assertIsInstance(option["qty_delta"], Decimal)
                    self.assertGreater(option["qty_delta"], 0)
                    self.assertTrue(option["qty_uom"])
                    self.assertTrue(option["new_item"])
                if option["kind"] == "Replace":
                    self.assertTrue(option["target_substitution_key"])
                    self.assertTrue(option["target_item"])
                    self.assertTrue(option["new_item"])


if __name__ == "__main__":
    import unittest

    unittest.main()


class PlanningFailsClosedTests(TestCase):
    """Planning must create nothing while its evidence gates are unmet."""

    def test_forecast_state_is_not_ready_without_history(self) -> None:
        from kopos_connector.kopos.services.inventory_autopilot import planning

        self.assertEqual(planning.overall_forecast_state([]), "Not ready")

    def test_only_the_two_approved_forecast_models_exist(self) -> None:
        from kopos_connector.kopos.services.inventory_autopilot import forecast

        self.assertEqual(
            tuple(forecast.MODEL_ORDER),
            ("same_weekday_seasonal_naive", "trailing_open_day_median"),
        )
