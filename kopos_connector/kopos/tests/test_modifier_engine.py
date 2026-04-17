from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


class TestModifierEngine(FrappeTestCase):
    def setUp(self):
        self.company = frappe.defaults.get_defaults().get("company", "Test Company")

    def test_add_modifier_increases_component_count(self):
        from kopos_connector.kopos.services.recipe.resolver import resolve_components

        recipe = frappe._dict(
            {
                "name": "TEST-RECIPE",
                "components": [
                    frappe._dict(
                        {
                            "item": "BASE-ITEM",
                            "qty": 10.0,
                            "uom": "g",
                            "affects_stock": 1,
                        }
                    )
                ],
            }
        )

        modifiers = [
            frappe._dict(
                {
                    "name": "ADD-ITEM",
                    "kind": "Add",
                    "new_item": "EXTRA-ITEM",
                    "qty_delta": 5.0,
                    "qty_uom": "g",
                    "affects_recipe": 1,
                    "affects_stock": 1,
                }
            )
        ]

        components = resolve_components(recipe, modifiers)

        self.assertEqual(len(components), 2)
        item_codes = [c["item"] for c in components]
        self.assertIn("BASE-ITEM", item_codes)
        self.assertIn("EXTRA-ITEM", item_codes)

    def test_replace_modifier_changes_item_code(self):
        from kopos_connector.kopos.services.recipe.resolver import resolve_components

        recipe = frappe._dict(
            {
                "name": "TEST-RECIPE",
                "components": [
                    frappe._dict(
                        {
                            "item": "DAIRY-MILK",
                            "qty": 100.0,
                            "uom": "ml",
                            "affects_stock": 1,
                            "substitution_key": "milk",
                        }
                    )
                ],
            }
        )

        modifiers = [
            frappe._dict(
                {
                    "name": "OAT-MILK",
                    "kind": "Replace",
                    "target_substitution_key": "milk",
                    "new_item": "OAT-MILK",
                    "affects_recipe": 1,
                }
            )
        ]

        components = resolve_components(recipe, modifiers)

        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["item"], "OAT-MILK")
        self.assertEqual(components[0]["source_type"], "Modifier Replace")

    def test_remove_modifier_eliminates_component(self):
        from kopos_connector.kopos.services.recipe.resolver import resolve_components

        recipe = frappe._dict(
            {
                "name": "TEST-RECIPE",
                "components": [
                    frappe._dict(
                        {"item": "SUGAR", "qty": 10.0, "uom": "g", "affects_stock": 1}
                    ),
                    frappe._dict(
                        {"item": "MATCHA", "qty": 5.0, "uom": "g", "affects_stock": 1}
                    ),
                ],
            }
        )

        modifiers = [
            frappe._dict(
                {
                    "name": "NO-SUGAR",
                    "kind": "Remove",
                    "target_item": "SUGAR",
                    "affects_recipe": 1,
                }
            )
        ]

        components = resolve_components(recipe, modifiers)

        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["item"], "MATCHA")

    def test_scale_modifier_multiplies_qty(self):
        from kopos_connector.kopos.services.recipe.resolver import resolve_components

        recipe = frappe._dict(
            {
                "name": "TEST-RECIPE",
                "components": [
                    frappe._dict(
                        {"item": "MATCHA", "qty": 10.0, "uom": "g", "affects_stock": 1}
                    )
                ],
            }
        )

        modifiers = [
            frappe._dict(
                {
                    "name": "DOUBLE-MATCHA",
                    "kind": "Scale",
                    "scale_percent": 200.0,
                    "affects_recipe": 1,
                }
            )
        ]

        components = resolve_components(recipe, modifiers)

        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["qty"], 20.0)
        self.assertEqual(components[0]["source_type"], "Modifier Scale")

    def test_instruction_only_modifier_no_change(self):
        from kopos_connector.kopos.services.recipe.resolver import resolve_components

        recipe = frappe._dict(
            {
                "name": "TEST-RECIPE",
                "components": [
                    frappe._dict(
                        {"item": "MATCHA", "qty": 10.0, "uom": "g", "affects_stock": 1}
                    )
                ],
            }
        )

        modifiers = [
            frappe._dict(
                {
                    "name": "LESS-SWEET",
                    "kind": "Instruction Only",
                    "instruction_text": "Use half the sugar",
                    "affects_recipe": 0,
                }
            )
        ]

        components = resolve_components(recipe, modifiers)

        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["qty"], 10.0)
        self.assertEqual(components[0]["source_type"], "Base Recipe")

    def test_multiple_modifiers_applied_in_order(self):
        from kopos_connector.kopos.services.recipe.resolver import resolve_components

        recipe = frappe._dict(
            {
                "name": "TEST-RECIPE",
                "components": [
                    frappe._dict(
                        {"item": "MATCHA", "qty": 10.0, "uom": "g", "affects_stock": 1}
                    )
                ],
            }
        )

        modifiers = [
            frappe._dict(
                {
                    "name": "ADD-EXTRA",
                    "kind": "Add",
                    "new_item": "VANILLA",
                    "qty_delta": 5.0,
                    "qty_uom": "ml",
                    "affects_recipe": 1,
                }
            ),
            frappe._dict(
                {
                    "name": "DOUBLE-ALL",
                    "kind": "Scale",
                    "scale_percent": 200.0,
                    "affects_recipe": 1,
                }
            ),
        ]

        components = resolve_components(recipe, modifiers)

        self.assertEqual(len(components), 2)

        matcha = next(c for c in components if c["item"] == "MATCHA")
        vanilla = next(c for c in components if c["item"] == "VANILLA")

        self.assertEqual(matcha["qty"], 20.0)
        self.assertEqual(vanilla["qty"], 10.0)
