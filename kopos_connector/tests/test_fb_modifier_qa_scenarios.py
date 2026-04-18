from __future__ import annotations
import unittest
from unittest.mock import MagicMock, patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()


class TestDeterminism(unittest.TestCase):
    def test_stable_code_is_deterministic_across_10_calls(self):
        from kopos_connector.api.modifier_migration import (
            _stable_backfill_code,
            _stable_fb_group_code,
            _stable_fb_modifier_code,
        )

        group_codes = set()
        modifier_codes = set()
        for _ in range(10):
            group_codes.add(_stable_backfill_code("kopos-fb-group", "grp-ice-LEVEL"))
            group_codes.add(_stable_fb_group_code("grp-ice-LEVEL"))
            modifier_codes.add(_stable_fb_modifier_code("opt-NoIce "))
        self.assertEqual(
            len(group_codes),
            1,
            f"Expected 1 unique group code, got {len(group_codes)}: {group_codes}",
        )
        self.assertEqual(
            len(modifier_codes),
            1,
            f"Expected 1 unique modifier code, got {len(modifier_codes)}: {modifier_codes}",
        )

    def test_stable_code_normalizes_whitespace_and_case(self):
        from kopos_connector.api.modifier_migration import _stable_fb_group_code

        code1 = _stable_fb_group_code("grp-ICE")
        code2 = _stable_fb_group_code("  grp-ice  ")
        code3 = _stable_fb_group_code("GRP-ICE")
        self.assertEqual(code1, code2)
        self.assertEqual(code2, code3)


class TestMaxSelectionDefaults(unittest.TestCase):
    def test_single_selection_defaults_to_1_when_max_not_set(self):
        from kopos_connector.api.modifier_migration import (
            build_fb_modifier_backfill_plan,
        )

        groups = [
            {
                "legacy_id": "g1",
                "group_name": "Single",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": None,
                "max_selections": None,
                "display_order": 1,
                "is_active": 1,
                "parent_option_id": None,
                "default_resolution_policy": "Require Explicit Selection",
                "options": [],
            }
        ]
        plan = build_fb_modifier_backfill_plan(groups)
        self.assertEqual(plan["groups"][0]["max_selection"], 1)

    def test_multiple_selection_defaults_to_0_when_max_not_set(self):
        from kopos_connector.api.modifier_migration import (
            build_fb_modifier_backfill_plan,
        )

        groups = [
            {
                "legacy_id": "g1",
                "group_name": "Multi",
                "selection_type": "multiple",
                "is_required": 0,
                "min_selections": None,
                "max_selections": None,
                "display_order": 1,
                "is_active": 1,
                "parent_option_id": None,
                "default_resolution_policy": "Require Explicit Selection",
                "options": [],
            }
        ]
        plan = build_fb_modifier_backfill_plan(groups)
        self.assertEqual(plan["groups"][0]["max_selection"], 0)


class TestPricePrecision(unittest.TestCase):
    def test_price_adjustment_rounded_to_2_decimal_places(self):
        from kopos_connector.api.modifier_migration import (
            build_fb_modifier_backfill_plan,
        )

        groups = [
            {
                "legacy_id": "g1",
                "group_name": "Price",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 1,
                "is_active": 1,
                "parent_option_id": None,
                "default_resolution_policy": "Require Explicit Selection",
                "options": [
                    {
                        "legacy_id": "o1",
                        "option_name": "Small",
                        "price_adjustment": 0.001,
                        "is_default": 0,
                        "is_active": 1,
                        "display_order": 1,
                    },
                    {
                        "legacy_id": "o2",
                        "option_name": "Medium",
                        "price_adjustment": 1.005,
                        "is_default": 0,
                        "is_active": 1,
                        "display_order": 2,
                    },
                    {
                        "legacy_id": "o3",
                        "option_name": "Large",
                        "price_adjustment": -0.50,
                        "is_default": 1,
                        "is_active": 1,
                        "display_order": 3,
                    },
                ],
            }
        ]
        plan = build_fb_modifier_backfill_plan(groups)
        prices = {m["modifier_name"]: m["price_adjustment"] for m in plan["modifiers"]}
        self.assertEqual(prices["Small"], 0.0)
        self.assertEqual(prices["Medium"], 1.01)
        self.assertEqual(prices["Large"], -0.5)


class TestOrderingPreservation(unittest.TestCase):
    def test_groups_sorted_by_display_order_then_name_then_id(self):
        from kopos_connector.api.modifier_migration import (
            build_fb_modifier_backfill_plan,
        )

        groups = [
            {
                "legacy_id": "g3",
                "group_name": "Group C",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 3,
                "is_active": 1,
                "parent_option_id": None,
                "default_resolution_policy": "Require Explicit Selection",
                "options": [
                    {
                        "legacy_id": "o3",
                        "option_name": "Opt C",
                        "price_adjustment": 0,
                        "is_default": 0,
                        "is_active": 1,
                        "display_order": 3,
                    }
                ],
            },
            {
                "legacy_id": "g1",
                "group_name": "Group A",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 1,
                "is_active": 1,
                "parent_option_id": None,
                "default_resolution_policy": "Require Explicit Selection",
                "options": [
                    {
                        "legacy_id": "o1",
                        "option_name": "Opt A",
                        "price_adjustment": 0,
                        "is_default": 0,
                        "is_active": 1,
                        "display_order": 1,
                    },
                    {
                        "legacy_id": "o2",
                        "option_name": "Opt B",
                        "price_adjustment": 0,
                        "is_default": 0,
                        "is_active": 1,
                        "display_order": 2,
                    },
                ],
            },
        ]
        plan = build_fb_modifier_backfill_plan(groups)
        names = [g["group_name"] for g in plan["groups"]]
        self.assertEqual(names, ["Group A", "Group C"])
        grp_a = [g for g in plan["groups"] if g["group_name"] == "Group A"][0]
        mods_a = [
            m for m in plan["modifiers"] if m["modifier_group"] == grp_a["group_code"]
        ]
        self.assertEqual([m["modifier_name"] for m in mods_a], ["Opt A", "Opt B"])

    def test_display_order_field_preserved_on_groups_and_modifiers(self):
        from kopos_connector.api.modifier_migration import (
            build_fb_modifier_backfill_plan,
        )

        groups = [
            {
                "legacy_id": "g1",
                "group_name": "First",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 5,
                "is_active": 1,
                "parent_option_id": None,
                "default_resolution_policy": "Require Explicit Selection",
                "options": [
                    {
                        "legacy_id": "o1",
                        "option_name": "Opt",
                        "price_adjustment": 0,
                        "is_default": 0,
                        "is_active": 1,
                        "display_order": 99,
                    }
                ],
            },
        ]
        plan = build_fb_modifier_backfill_plan(groups)
        self.assertEqual(plan["groups"][0]["display_order"], 5)
        self.assertEqual(plan["modifiers"][0]["display_order"], 99)


class TestActiveStatePreservation(unittest.TestCase):
    def test_is_active_preserved_for_groups_and_options(self):
        from kopos_connector.api.modifier_migration import (
            build_fb_modifier_backfill_plan,
        )

        groups = [
            {
                "legacy_id": "g1",
                "group_name": "Active Group",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 1,
                "is_active": 1,
                "parent_option_id": None,
                "default_resolution_policy": "Require Explicit Selection",
                "options": [
                    {
                        "legacy_id": "o1",
                        "option_name": "Active Opt",
                        "price_adjustment": 0,
                        "is_default": 0,
                        "is_active": 1,
                        "display_order": 1,
                    },
                    {
                        "legacy_id": "o2",
                        "option_name": "Inactive Opt",
                        "price_adjustment": 0,
                        "is_default": 0,
                        "is_active": 0,
                        "display_order": 2,
                    },
                ],
            },
            {
                "legacy_id": "g2",
                "group_name": "Inactive Group",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 2,
                "is_active": 0,
                "parent_option_id": None,
                "default_resolution_policy": "Require Explicit Selection",
                "options": [],
            },
        ]
        plan = build_fb_modifier_backfill_plan(groups)
        gmap = {g["group_name"]: g for g in plan["groups"]}
        mmap = {m["modifier_name"]: m for m in plan["modifiers"]}
        self.assertEqual(gmap["Active Group"]["active"], 1)
        self.assertEqual(gmap["Inactive Group"]["active"], 0)
        self.assertEqual(mmap["Active Opt"]["active"], 1)
        self.assertEqual(mmap["Inactive Opt"]["active"], 0)


class TestDefaultResolutionPolicy(unittest.TestCase):
    def test_active_default_option_maps_to_auto_apply(self):
        from kopos_connector.api.modifier_migration import (
            build_fb_modifier_backfill_plan,
        )

        groups = [
            {
                "legacy_id": "g1",
                "group_name": "A",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 1,
                "is_active": 1,
                "parent_option_id": None,
                "default_resolution_policy": "Auto Apply Default",
                "options": [
                    {
                        "legacy_id": "o1",
                        "option_name": "Default",
                        "price_adjustment": 0,
                        "is_default": 1,
                        "is_active": 1,
                        "display_order": 1,
                    }
                ],
            }
        ]
        plan = build_fb_modifier_backfill_plan(groups)
        self.assertEqual(
            plan["groups"][0]["default_resolution_policy"], "Auto Apply Default"
        )

    def test_inactive_default_option_maps_to_require_explicit(self):
        from kopos_connector.api.modifier_migration import (
            build_fb_modifier_backfill_plan,
        )

        groups = [
            {
                "legacy_id": "g1",
                "group_name": "A",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 1,
                "is_active": 1,
                "parent_option_id": None,
                "default_resolution_policy": "Require Explicit Selection",
                "options": [
                    {
                        "legacy_id": "o1",
                        "option_name": "Default",
                        "price_adjustment": 0,
                        "is_default": 1,
                        "is_active": 0,
                        "display_order": 1,
                    }
                ],
            }
        ]
        plan = build_fb_modifier_backfill_plan(groups)
        self.assertEqual(
            plan["groups"][0]["default_resolution_policy"], "Require Explicit Selection"
        )

    def test_no_default_maps_to_require_explicit(self):
        from kopos_connector.api.modifier_migration import (
            build_fb_modifier_backfill_plan,
        )

        groups = [
            {
                "legacy_id": "g1",
                "group_name": "A",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 1,
                "is_active": 1,
                "parent_option_id": None,
                "default_resolution_policy": "Require Explicit Selection",
                "options": [
                    {
                        "legacy_id": "o1",
                        "option_name": "Opt",
                        "price_adjustment": 0,
                        "is_default": 0,
                        "is_active": 1,
                        "display_order": 1,
                    }
                ],
            }
        ]
        plan = build_fb_modifier_backfill_plan(groups)
        self.assertEqual(
            plan["groups"][0]["default_resolution_policy"], "Require Explicit Selection"
        )


class TestUnresolvedParentFailsSafely(unittest.TestCase):
    def test_missing_parent_option_id_raises_with_clear_message(self):
        from kopos_connector.api.modifier_migration import (
            build_fb_modifier_backfill_plan,
        )

        groups = [
            {
                "legacy_id": "g1",
                "group_name": "Sauce",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 1,
                "is_active": 1,
                "parent_option_id": "opt-nonexistent-parent",
                "default_resolution_policy": "Require Explicit Selection",
                "options": [
                    {
                        "legacy_id": "o1",
                        "option_name": "Ketchup",
                        "price_adjustment": 0,
                        "is_default": 0,
                        "is_active": 1,
                        "display_order": 1,
                    }
                ],
            }
        ]
        with self.assertRaises(Exception) as ctx:
            build_fb_modifier_backfill_plan(groups)
        msg = str(ctx.exception)
        self.assertIn(
            "parent_option_id",
            msg.lower(),
            f"Error message should mention parent_option_id: {msg}",
        )
        self.assertIn(
            "opt-nonexistent-parent",
            msg,
            f"Error message should mention the missing ID: {msg}",
        )

    def test_multiple_missing_parent_ids_raises_with_all_names(self):
        from kopos_connector.api.modifier_migration import (
            build_fb_modifier_backfill_plan,
        )

        groups = [
            {
                "legacy_id": "g1",
                "group_name": "Group One",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 1,
                "is_active": 1,
                "parent_option_id": "missing-a",
                "default_resolution_policy": "Require Explicit Selection",
                "options": [
                    {
                        "legacy_id": "o1",
                        "option_name": "O1",
                        "price_adjustment": 0,
                        "is_default": 0,
                        "is_active": 1,
                        "display_order": 1,
                    }
                ],
            },
            {
                "legacy_id": "g2",
                "group_name": "Group Two",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 2,
                "is_active": 1,
                "parent_option_id": "missing-b",
                "default_resolution_policy": "Require Explicit Selection",
                "options": [
                    {
                        "legacy_id": "o2",
                        "option_name": "O2",
                        "price_adjustment": 0,
                        "is_default": 0,
                        "is_active": 1,
                        "display_order": 1,
                    }
                ],
            },
        ]
        with self.assertRaises(Exception) as ctx:
            build_fb_modifier_backfill_plan(groups)
        msg = str(ctx.exception)
        self.assertIn("missing-a", msg, f"Error should list first missing ID: {msg}")
        self.assertIn("missing-b", msg, f"Error should list second missing ID: {msg}")


class TestEdgeCases(unittest.TestCase):
    def test_empty_options_list_creates_group_with_no_modifiers(self):
        from kopos_connector.api.modifier_migration import (
            build_fb_modifier_backfill_plan,
        )

        groups = [
            {
                "legacy_id": "g1",
                "group_name": "Empty",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 1,
                "is_active": 1,
                "parent_option_id": None,
                "default_resolution_policy": "Require Explicit Selection",
                "options": [],
            }
        ]
        plan = build_fb_modifier_backfill_plan(groups)
        self.assertEqual(len(plan["groups"]), 1)
        self.assertEqual(len(plan["modifiers"]), 0)

    def test_whitespace_legacy_id_uses_legacy_fallback(self):
        from kopos_connector.api.modifier_migration import (
            build_fb_modifier_backfill_plan,
        )

        groups = [
            {
                "legacy_id": "  ",
                "group_name": "WS",
                "selection_type": "single",
                "is_required": 0,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 1,
                "is_active": 1,
                "parent_option_id": None,
                "default_resolution_policy": "Require Explicit Selection",
                "options": [
                    {
                        "legacy_id": "  ",
                        "option_name": "Opt",
                        "price_adjustment": 0,
                        "is_default": 0,
                        "is_active": 1,
                        "display_order": 1,
                    }
                ],
            }
        ]
        plan = build_fb_modifier_backfill_plan(groups)
        self.assertIn("legacy", plan["groups"][0]["group_code"])
        self.assertIn("legacy", plan["modifiers"][0]["modifier_code"])


class TestMinMaxRequiredPreservation(unittest.TestCase):
    def test_min_max_required_preserved_exactly(self):
        from kopos_connector.api.modifier_migration import (
            build_fb_modifier_backfill_plan,
        )

        groups = [
            {
                "legacy_id": "g1",
                "group_name": "MinMax",
                "selection_type": "single",
                "is_required": 1,
                "min_selections": 2,
                "max_selections": 5,
                "display_order": 1,
                "is_active": 1,
                "parent_option_id": None,
                "default_resolution_policy": "Require Explicit Selection",
                "options": [],
            }
        ]
        plan = build_fb_modifier_backfill_plan(groups)
        g = plan["groups"][0]
        self.assertEqual(g["is_required"], 1)
        self.assertEqual(g["min_selection"], 2)
        self.assertEqual(g["max_selection"], 5)
        self.assertEqual(g["selection_type"], "Single")


class TestPatchExecution(unittest.TestCase):
    def test_execute_calls_backfill_with_dry_run_false(self):
        from kopos_connector.patches.backfill_fb_modifiers_from_kopos import execute

        with patch(
            "kopos_connector.patches.backfill_fb_modifiers_from_kopos.backfill_kopos_modifiers_to_fb"
        ) as mock_fn:
            mock_fn.return_value = {
                "groups_created": 5,
                "modifiers_created": 20,
                "dry_run": False,
            }
            execute()
            mock_fn.assert_called_once_with(dry_run=False)


class TestDryRun(unittest.TestCase):
    def test_dry_run_returns_counts_without_creating_records(self):
        from kopos_connector.tests.test_fb_modifier_backfill import (
            _InMemoryFBStore,
            _legacy_group,
            _legacy_option,
        )
        from kopos_connector.api.modifier_migration import (
            backfill_kopos_modifiers_to_fb,
        )

        legacy_groups = [
            _legacy_group(
                "grp-temp",
                "Temperature",
                "single",
                1,
                0,
                1,
                1,
                1,
                None,
                [_legacy_option("o1", "Hot", 0.0, 0, 1, 1)],
            )
        ]
        store = _InMemoryFBStore(legacy_groups)
        with (
            patch(
                "kopos_connector.api.modifier_migration.frappe.get_all",
                side_effect=store.get_all,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.get_doc",
                side_effect=store.get_doc,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.new_doc",
                side_effect=store.new_doc,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.db.get_value",
                side_effect=store.get_value,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.db.exists",
                side_effect=store.exists,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.db.commit",
                side_effect=store.commit,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.db.rollback",
                side_effect=store.rollback,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.has_permission",
                return_value=True,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.log_error", MagicMock()
            ),
        ):
            result = backfill_kopos_modifiers_to_fb(dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(len(store.fb_groups), 0)
        self.assertEqual(len(store.fb_modifiers), 0)
        self.assertEqual(store.commits, 0)


if __name__ == "__main__":
    unittest.main()
