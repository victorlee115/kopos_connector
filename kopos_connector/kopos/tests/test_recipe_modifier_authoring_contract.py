from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


class TestRecipeModifierAuthoringContract(FrappeTestCase):
    """Prove invalid recipe-specific rules fail during a real Frappe save."""

    def tearDown(self) -> None:
        frappe.db.rollback()

    def test_recipe_save_rejects_selection_rule_drift(self) -> None:
        modifier_group = self._new_modifier_group()
        recipe = self._new_draft_recipe(
            modifier_group=modifier_group.name,
            required=1,
        )

        with self.assertRaisesRegex(
            frappe.ValidationError,
            "changes the selection rules.*Use a separate modifier group",
        ):
            recipe.insert(ignore_permissions=True, ignore_links=True)

    def test_recipe_save_accepts_persisted_zero_overrides_as_unset(self) -> None:
        modifier_group = self._new_modifier_group()
        recipe = self._new_draft_recipe(
            modifier_group=modifier_group.name,
            required=0,
        )

        recipe.insert(ignore_permissions=True, ignore_links=True)
        recipe.reload()

        row = recipe.allowed_modifier_groups[0]
        self.assertEqual(row.override_min_selection, 0)
        self.assertEqual(row.override_max_selection, 0)

    def test_invalid_legacy_recipe_can_be_retired_without_definition_change(self) -> None:
        modifier_group = self._new_modifier_group()
        recipe = self._new_draft_recipe(
            modifier_group=modifier_group.name,
            required=0,
        )
        recipe.insert(ignore_permissions=True, ignore_links=True)
        child_name = recipe.allowed_modifier_groups[0].name
        frappe.db.set_value(
            "FB Recipe",
            recipe.name,
            "status",
            "Active",
            update_modified=False,
        )
        frappe.db.set_value(
            "FB Allowed Modifier Group",
            child_name,
            "required",
            1,
            update_modified=False,
        )
        frappe.clear_document_cache("FB Recipe", recipe.name)

        legacy = frappe.get_doc("FB Recipe", recipe.name)
        legacy.status = "Retired"
        legacy.save(ignore_permissions=True)
        legacy.reload()

        self.assertEqual(legacy.status, "Retired")
        self.assertEqual(legacy.allowed_modifier_groups[0].required, 1)

    def _new_modifier_group(self):
        suffix = frappe.generate_hash(length=10)
        modifier_group = frappe.get_doc(
            {
                "doctype": "FB Modifier Group",
                "group_code": f"KOPOS-AUTHORING-{suffix}",
                "group_name": "Authoring Contract Group",
                "selection_type": "Multiple",
                "is_required": 0,
                "min_selection": 0,
                "max_selection": 3,
                "active": 1,
            }
        )
        modifier_group.insert(ignore_permissions=True)
        return modifier_group

    def _new_draft_recipe(self, *, modifier_group: str, required: int):
        suffix = frappe.generate_hash(length=10)
        return frappe.get_doc(
            {
                "doctype": "FB Recipe",
                "recipe_code": f"KOPOS-AUTHORING-RECIPE-{suffix}",
                "recipe_name": "Authoring Contract Recipe",
                "sellable_item": f"KOPOS-NONSTOCK-TEST-{suffix}",
                "recipe_type": "Finished Drink",
                "status": "Draft",
                "version_no": 1,
                "yield_qty": 1,
                "yield_uom": "Nos",
                "default_serving_qty": 1,
                "default_serving_uom": "Nos",
                "company": f"KOPOS-TEST-COMPANY-{suffix}",
                "allowed_modifier_groups": [
                    {
                        "modifier_group": modifier_group,
                        "required": required,
                    }
                ],
            }
        )
