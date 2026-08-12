from __future__ import annotations

import frappe
import pytest
from frappe.tests.utils import FrappeTestCase

pytestmark = pytest.mark.inventory_regression


class TestCatalogPersistenceContract(FrappeTestCase):
    """Prove the real Frappe/MariaDB value shape used by catalog rules."""

    def tearDown(self) -> None:
        frappe.db.rollback()

    def test_blank_child_int_fields_round_trip_as_zero_and_mean_unset(self) -> None:
        from kopos_connector.kopos.services.recipe.modifier_bounds import (
            EffectiveModifierBounds,
            resolve_effective_modifier_bounds,
        )

        row = frappe.get_doc(
            {
                "doctype": "FB Allowed Modifier Group",
                "parent": f"KOPOS-PERSISTENCE-{frappe.generate_hash(length=10)}",
                "parenttype": "FB Recipe",
                "parentfield": "allowed_modifier_groups",
                "modifier_group": "KOPOS-PERSISTENCE-GROUP",
                "required": 0,
            }
        )
        # This is intentionally a direct child-row persistence check. It avoids
        # creating or changing any Item, recipe stock component, or inventory
        # document while exercising the real DocType and MariaDB column shape.
        row.db_insert()

        persisted = frappe.db.get_value(
            "FB Allowed Modifier Group",
            row.name,
            ["override_min_selection", "override_max_selection"],
            as_dict=True,
        )

        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.override_min_selection, 0)
        self.assertEqual(persisted.override_max_selection, 0)
        self.assertEqual(
            resolve_effective_modifier_bounds(
                selection_type="Single",
                group_is_required=1,
                group_min_selection=0,
                group_max_selection=1,
                recipe_required=0,
                override_min_selection=persisted.override_min_selection,
                override_max_selection=persisted.override_max_selection,
            ),
            EffectiveModifierBounds(
                selection_type="single",
                is_required=True,
                min_selection=1,
                max_selection=1,
            ),
        )
