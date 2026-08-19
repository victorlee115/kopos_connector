from decimal import Decimal
import unittest

from kopos_connector.kopos.services.inventory_autopilot.recipe_compiler import (
    RecipeCompilerError,
    compile_recipe_components,
    compile_recipe_vector,
)


class RecipeCompilerTests(unittest.TestCase):
    def test_compiles_yield_and_modifiers_as_a_stable_decimal_vector(self):
        result = compile_recipe_components(
            {
                "yield_qty": "2",
                "default_serving_qty": "1",
                "components": [
                    {"item": "MILK", "qty": "0.25", "stock_uom": "L"},
                    {"item": "SYRUP", "stock_qty": "0.010", "stock_uom": "L"},
                    {"item": "NON_STOCK", "qty": "99", "stock_uom": "Each", "affects_stock": False},
                ],
            },
            servings="2",
            modifiers=[{"item": "SYRUP", "stock_qty": "0.005"}],
        )
        self.assertEqual(result, {"MILK": Decimal("0.25"), "SYRUP": Decimal("0.02")})

    def test_rejects_missing_or_invalid_recipe_data(self):
        with self.assertRaisesRegex(RecipeCompilerError, "recipe yield"):
            compile_recipe_components({"components": []})
        with self.assertRaisesRegex(RecipeCompilerError, "component 1 is missing an Item"):
            compile_recipe_components(
                {"yield_qty": 1, "components": [{"qty": 1, "stock_uom": "Each"}]}
            )

    def test_same_inputs_have_same_sorted_keys(self):
        recipe = {
            "yield_qty": 1,
            "components": [
                {"item": "Z", "qty": 1, "stock_uom": "Each"},
                {"item": "A", "qty": 1, "stock_uom": "Each"},
            ],
        }
        self.assertEqual(list(compile_recipe_components(recipe)), ["A", "Z"])

    def test_frozen_effects_use_published_stock_uom_values_without_float_math(self):
        result = compile_recipe_vector(
            {
                "yield_qty": "4",
                "default_serving_qty": "1",
                "components": [
                    {
                        "item": "MILK",
                        "stock_qty": "0.80",
                        "stock_uom": "L",
                        "substitution_key": "milk",
                    },
                    {"item": "SYRUP", "stock_qty": "0.040", "stock_uom": "L"},
                ],
            },
            servings="2",
            modifiers=[
                {
                    "modifier": "OAT",
                    "kind": "Replace",
                    "target_substitution_key": "milk",
                    "new_item": "OAT-MILK",
                    "stock_uom": "L",
                    "affects_stock": True,
                    "affects_recipe": True,
                },
                {
                    "modifier": "EXTRA-SYRUP",
                    "kind": "Add",
                    "new_item": "SYRUP",
                    "stock_qty_delta": "0.010",
                    "stock_uom": "L",
                    "affects_stock": True,
                    "affects_recipe": True,
                },
            ],
        )
        self.assertEqual(
            result,
            [
                {
                    "item": "OAT-MILK",
                    "stock_qty": "0.4",
                    "stock_uom": "L",
                    "source_type": "Frozen Recipe",
                    "source_reference": "recipe-vector",
                },
                {
                    "item": "SYRUP",
                    "stock_qty": "0.04",
                    "stock_uom": "L",
                    "source_type": "Frozen Recipe",
                    "source_reference": "recipe-vector",
                },
            ],
        )

    def test_canonical_decimal_text_wins_over_rounded_legacy_float_values(self):
        result = compile_recipe_components(
            {
                "yield_qty": 1.0,
                "yield_qty_decimal": "3.0000000000000001",
                "default_serving_qty": 1.0,
                "default_serving_qty_decimal": "1.0000000000000001",
                "components": [
                    {
                        "item": "MILK",
                        "qty": 0.0,
                        "qty_decimal": "0.1234567890123456789",
                        "stock_qty": 0.0,
                        "stock_qty_decimal": "0.3703703670370370367",
                        "stock_uom": "L",
                    }
                ],
            },
            servings="1.0000000000000001",
        )
        expected = Decimal("0.3703703670370370367") / Decimal("3.0000000000000001")
        self.assertEqual(result["MILK"], expected)

    def test_canonical_conversion_and_loss_are_used_when_stock_quantity_is_absent(self):
        result = compile_recipe_components(
            {
                "yield_qty": "1",
                "default_serving_qty": "1",
                "components": [
                    {
                        "item": "SYRUP",
                        "qty": 0.0,
                        "qty_decimal": "0.1234567890123456789",
                        "stock_qty_decimal": "",
                        "stock_conversion_factor": 0.0,
                        "stock_conversion_factor_decimal": "2.5",
                        "loss_factor_pct": 0.0,
                        "loss_factor_pct_decimal": "1.25",
                        "stock_uom": "L",
                    }
                ],
            }
        )
        self.assertEqual(
            result["SYRUP"],
            Decimal("0.1234567890123456789") * Decimal("2.5") * Decimal("1.0125"),
        )
