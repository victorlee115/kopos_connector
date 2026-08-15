from decimal import Decimal
import unittest

from kopos_connector.kopos.services.inventory_autopilot.recipe_compiler import (
    RecipeCompilerError,
    compile_recipe_components,
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
        self.assertEqual(result, {"MILK": Decimal("0.25"), "SYRUP": Decimal("0.015")})

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
