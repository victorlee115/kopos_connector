from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from kopos_connector.kopos.services.recipe.resolver import (
    apply_defaults,
    calculate_stock_qty,
    resolve_components,
    resolve_sale_line,
)


class TestRecipeResolver(FrappeTestCase):
    def setUp(self):
        self.company = frappe.defaults.get_defaults().get("company", "Test Company")
        self.warehouse = "WH - Test Booth"

    def test_resolve_simple_recipe(self):
        resolved = resolve_sale_line(
            item_code="TEST-MATCHA-LATTE",
            qty=1.0,
            modifiers=[],
            warehouse=self.warehouse,
        )

        self.assertIn("recipe", resolved)
        self.assertIn("resolved_components", resolved)
        self.assertEqual(resolved["qty"], 1.0)
        self.assertEqual(resolved["sellable_item"], "TEST-MATCHA-LATTE")

    def test_resolve_with_add_modifier(self):
        resolved = resolve_sale_line(
            item_code="TEST-MATCHA-LATTE",
            qty=1.0,
            modifiers=[{"modifier": "EXTRA-MATCHA"}],
            warehouse=self.warehouse,
        )

        component_items = [c["item"] for c in resolved["resolved_components"]]
        self.assertIn("MATCHA-POWDER", component_items)

    def test_resolve_with_replace_modifier(self):
        resolved = resolve_sale_line(
            item_code="TEST-MATCHA-LATTE",
            qty=1.0,
            modifiers=[{"modifier": "OAT-MILK"}],
            warehouse=self.warehouse,
        )

        milk_components = [
            c for c in resolved["resolved_components"] if "MILK" in c["item"]
        ]
        self.assertTrue(any("OAT" in c["item"] for c in milk_components))

    def test_resolve_with_scale_modifier(self):
        resolved = resolve_sale_line(
            item_code="TEST-MATCHA-LATTE",
            qty=1.0,
            modifiers=[{"modifier": "DOUBLE-MATCHA"}],
            warehouse=self.warehouse,
        )

        matcha_component = next(
            (c for c in resolved["resolved_components"] if "MATCHA" in c["item"]), None
        )
        self.assertIsNotNone(matcha_component)
        self.assertGreater(matcha_component["qty"], 18.0)

    def test_resolve_qty_scaling(self):
        resolved_single = resolve_sale_line(
            item_code="TEST-MATCHA-LATTE",
            qty=1.0,
            modifiers=[],
            warehouse=self.warehouse,
        )

        resolved_double = resolve_sale_line(
            item_code="TEST-MATCHA-LATTE",
            qty=2.0,
            modifiers=[],
            warehouse=self.warehouse,
        )

        single_matcha = next(
            c["qty"]
            for c in resolved_single["resolved_components"]
            if "MATCHA" in c["item"]
        )
        double_matcha = next(
            c["qty"]
            for c in resolved_double["resolved_components"]
            if "MATCHA" in c["item"]
        )

        self.assertAlmostEqual(double_matcha, single_matcha * 2.0, places=1)

    def test_instruction_only_modifier_no_effect(self):
        resolved = resolve_sale_line(
            item_code="TEST-MATCHA-LATTE",
            qty=1.0,
            modifiers=[{"modifier": "LESS-SWEET"}],
            warehouse=self.warehouse,
        )

        base_resolved = resolve_sale_line(
            item_code="TEST-MATCHA-LATTE",
            qty=1.0,
            modifiers=[],
            warehouse=self.warehouse,
        )

        self.assertEqual(
            len(resolved["resolved_components"]),
            len(base_resolved["resolved_components"]),
        )

    def test_calculate_stock_qty_with_conversion(self):
        qty = calculate_stock_qty(qty=100.0, uom="ml", item="MILK-1L")
        self.assertEqual(qty, 100.0)

    def test_calculate_stock_qty_no_conversion_needed(self):
        qty = calculate_stock_qty(qty=50.0, uom="g", item="MATCHA-POWDER")
        self.assertEqual(qty, 50.0)

    def test_apply_defaults_required_group(self):
        modifier_groups = [
            frappe._dict(
                {
                    "modifier_group": "MILK-CHOICE",
                    "required": 1,
                    "default_modifier": "DAIRY-MILK",
                }
            )
        ]

        defaults = apply_defaults(modifier_groups)
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0].name, "DAIRY-MILK")

    def test_apply_defaults_optional_group(self):
        modifier_groups = [
            frappe._dict(
                {"modifier_group": "ADD-ONS", "required": 0, "default_modifier": None}
            )
        ]

        defaults = apply_defaults(modifier_groups)
        self.assertEqual(len(defaults), 0)

    def test_resolve_components_base_only(self):
        recipe = frappe._dict(
            {
                "name": "TEST-RECIPE",
                "components": [
                    frappe._dict(
                        {
                            "item": "MATCHA-POWDER",
                            "qty": 18.0,
                            "uom": "g",
                            "affects_stock": 1,
                            "affects_cogs": 1,
                        }
                    ),
                    frappe._dict(
                        {
                            "item": "MILK",
                            "qty": 160.0,
                            "uom": "ml",
                            "affects_stock": 1,
                            "affects_cogs": 1,
                        }
                    ),
                ],
            }
        )

        components = resolve_components(recipe, [])
        self.assertEqual(len(components), 2)
        self.assertEqual(components[0]["source_type"], "Base Recipe")

    def test_resolve_components_with_add(self):
        recipe = frappe._dict(
            {
                "name": "TEST-RECIPE",
                "components": [
                    frappe._dict(
                        {
                            "item": "MATCHA-POWDER",
                            "qty": 18.0,
                            "uom": "g",
                            "affects_stock": 1,
                            "affects_cogs": 1,
                        }
                    )
                ],
            }
        )

        modifiers = [
            frappe._dict(
                {
                    "name": "EXTRA-MATCHA",
                    "kind": "Add",
                    "new_item": "MATCHA-POWDER",
                    "qty_delta": 9.0,
                    "qty_uom": "g",
                    "affects_recipe": 1,
                    "affects_stock": 1,
                }
            )
        ]

        components = resolve_components(recipe, modifiers)
        matcha_total = sum(c["qty"] for c in components if c["item"] == "MATCHA-POWDER")
        self.assertEqual(matcha_total, 27.0)

    def test_resolve_components_with_remove(self):
        recipe = frappe._dict(
            {
                "name": "TEST-RECIPE",
                "components": [
                    frappe._dict(
                        {
                            "item": "SYRUP",
                            "qty": 15.0,
                            "uom": "ml",
                            "affects_stock": 1,
                            "substitution_key": "sweetener",
                        }
                    ),
                    frappe._dict(
                        {
                            "item": "MATCHA-POWDER",
                            "qty": 18.0,
                            "uom": "g",
                            "affects_stock": 1,
                        }
                    ),
                ],
            }
        )

        modifiers = [
            frappe._dict(
                {
                    "name": "NO-SYRUP",
                    "kind": "Remove",
                    "target_substitution_key": "sweetener",
                    "affects_recipe": 1,
                }
            )
        ]

        components = resolve_components(recipe, modifiers)
        item_codes = [c["item"] for c in components]
        self.assertNotIn("SYRUP", item_codes)
        self.assertIn("MATCHA-POWDER", item_codes)

    def test_invalid_item_raises_error(self):
        with self.assertRaises(Exception):
            resolve_sale_line(
                item_code="NON-EXISTENT-ITEM",
                qty=1.0,
                modifiers=[],
                warehouse=self.warehouse,
            )

    def test_zero_qty_raises_error(self):
        with self.assertRaises(Exception):
            resolve_sale_line(
                item_code="TEST-MATCHA-LATTE",
                qty=0.0,
                modifiers=[],
                warehouse=self.warehouse,
            )

    def test_negative_qty_raises_error(self):
        with self.assertRaises(Exception):
            resolve_sale_line(
                item_code="TEST-MATCHA-LATTE",
                qty=-1.0,
                modifiers=[],
                warehouse=self.warehouse,
            )
