from __future__ import annotations

import frappe
import pytest
from frappe.tests.utils import FrappeTestCase

pytestmark = pytest.mark.inventory_regression


def _resolver():
    from kopos_connector.kopos.services.recipe import resolver

    return resolver


def _fixtures():
    from kopos_connector.kopos.tests import frappe_test_fixtures

    return frappe_test_fixtures


def _smoke():
    from kopos_connector import smoke

    return smoke


def apply_defaults(*args, **kwargs):
    return _resolver().apply_defaults(*args, **kwargs)


def calculate_stock_qty(*args, **kwargs):
    return _resolver().calculate_stock_qty(*args, **kwargs)


def resolve_components(*args, **kwargs):
    return _resolver().resolve_components(*args, **kwargs)


def resolve_sale_line(*args, **kwargs):
    return _resolver().resolve_sale_line(*args, **kwargs)


def ensure_canonical_test_base(*args, **kwargs):
    return _fixtures().ensure_canonical_test_base(*args, **kwargs)


def ensure_persisted_test_modifier(*args, **kwargs):
    return _fixtures().ensure_persisted_test_modifier(*args, **kwargs)


def modifier_doc(*args, **kwargs):
    return _fixtures().modifier_doc(*args, **kwargs)


class TestRecipeResolver(FrappeTestCase):
    def setUp(self):
        self.base = ensure_canonical_test_base(include_inventory_regression=True)
        self.company = self.base["company"]
        self.warehouse = self.base["warehouse"]

    def tearDown(self):
        frappe.db.rollback()

    def test_resolve_simple_recipe(self):
        resolved = resolve_sale_line(
            item_code=self.base["item_code"],
            qty=1.0,
            modifiers=[],
            warehouse=self.warehouse,
        )

        self.assertIn("recipe", resolved)
        self.assertIn("resolved_components", resolved)
        self.assertEqual(resolved["qty"], 1.0)
        self.assertEqual(resolved["sellable_item"], self.base["item_code"])

    def test_resolve_with_add_modifier(self):
        modifier = ensure_persisted_test_modifier(
            "KOPOS-TEST-EXTRA-MATCHA",
            kind="Add",
            new_item=_smoke().DEMO_MATCHA_ITEM,
            qty_delta=9,
            qty_uom="Gram",
        )
        resolved = resolve_sale_line(
            item_code=self.base["item_code"],
            qty=1.0,
            modifiers=[{"modifier": modifier}],
            warehouse=self.warehouse,
        )

        component_items = [c["item"] for c in resolved["resolved_components"]]
        self.assertIn(_smoke().DEMO_MATCHA_ITEM, component_items)

    def test_resolve_with_replace_modifier(self):
        modifier = ensure_persisted_test_modifier(
            "KOPOS-TEST-REPLACE-MILK",
            kind="Replace",
            target_item=_smoke().DEMO_MILK_ITEM,
            new_item=_smoke().DEMO_STRAWBERRY_ITEM,
        )
        resolved = resolve_sale_line(
            item_code=self.base["item_code"],
            qty=1.0,
            modifiers=[{"modifier": modifier}],
            warehouse=self.warehouse,
        )

        component_items = [c["item"] for c in resolved["resolved_components"]]
        self.assertNotIn(_smoke().DEMO_MILK_ITEM, component_items)
        self.assertIn(_smoke().DEMO_STRAWBERRY_ITEM, component_items)

    def test_resolve_with_scale_modifier(self):
        modifier = ensure_persisted_test_modifier(
            "KOPOS-TEST-DOUBLE-MATCHA",
            kind="Scale",
            target_item=_smoke().DEMO_MATCHA_ITEM,
            scale_percent=200,
        )
        resolved = resolve_sale_line(
            item_code=self.base["item_code"],
            qty=1.0,
            modifiers=[{"modifier": modifier}],
            warehouse=self.warehouse,
        )

        matcha_component = next(
            (
                c
                for c in resolved["resolved_components"]
                if c["item"] == _smoke().DEMO_MATCHA_ITEM
            ),
            None,
        )
        self.assertIsNotNone(matcha_component)
        self.assertGreater(matcha_component["qty"], 18.0)

    def test_resolve_qty_scaling(self):
        resolved_single = resolve_sale_line(
            item_code=self.base["item_code"],
            qty=1.0,
            modifiers=[],
            warehouse=self.warehouse,
        )

        resolved_double = resolve_sale_line(
            item_code=self.base["item_code"],
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
        modifier = ensure_persisted_test_modifier(
            "KOPOS-TEST-INSTRUCTION",
            kind="Instruction Only",
            affects_recipe=0,
            affects_stock=0,
        )
        resolved = resolve_sale_line(
            item_code=self.base["item_code"],
            qty=1.0,
            modifiers=[{"modifier": modifier}],
            warehouse=self.warehouse,
        )

        base_resolved = resolve_sale_line(
            item_code=self.base["item_code"],
            qty=1.0,
            modifiers=[],
            warehouse=self.warehouse,
        )

        self.assertEqual(
            len(resolved["resolved_components"]),
            len(base_resolved["resolved_components"]),
        )

    def test_calculate_stock_qty_with_conversion(self):
        qty = calculate_stock_qty(
            qty=100.0,
            uom="Millilitre",
            item=_smoke().DEMO_MILK_ITEM,
        )
        self.assertEqual(qty, 100.0)

    def test_calculate_stock_qty_no_conversion_needed(self):
        qty = calculate_stock_qty(
            qty=50.0,
            uom="Gram",
            item=_smoke().DEMO_MATCHA_ITEM,
        )
        self.assertEqual(qty, 50.0)

    def test_apply_defaults_required_group(self):
        recipe = frappe.get_doc("FB Recipe", self.base["recipe"])
        modifier_groups = list(recipe.allowed_modifier_groups)

        defaults = apply_defaults(modifier_groups)
        self.assertEqual(len(defaults), 1)
        self.assertTrue(defaults[0].active)

    def test_apply_defaults_optional_group(self):
        defaults = apply_defaults([])
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
            modifier_doc(
                {
                    "doctype": "FB Modifier",
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
            modifier_doc(
                {
                    "doctype": "FB Modifier",
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
