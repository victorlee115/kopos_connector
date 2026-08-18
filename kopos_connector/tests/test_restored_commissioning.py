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
        names += [
            commissioning.PREPARED_ITEM,
            commissioning.SUPPLIER,
            commissioning.RECIPE_CODE,
            commissioning.SELLABLE_ITEM,
        ]
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

        The BOM builds 2 litres of cold foam from 2 litres of milk.  If the
        recipe expanded the BOM it would consume that batch quantity again on
        every single sale.
        """

        components = dict(commissioning.RECIPE_COMPONENTS)
        self.assertIn(commissioning.PREPARED_ITEM, components)
        self.assertEqual(Decimal(components[commissioning.PREPARED_ITEM]), Decimal("0.05"))
        self.assertLess(
            Decimal(components[commissioning.PREPARED_ITEM]),
            commissioning.PREPARED_BOM_QTY,
        )

    def test_preparation_thresholds_are_orderable(self) -> None:
        self.assertGreater(commissioning.PREPARED_BOM_QTY, commissioning.PREPARED_MIN_READY_QTY)
        self.assertGreater(commissioning.PREPARED_LEAD_MINUTES, 0)


if __name__ == "__main__":
    import unittest

    unittest.main()
