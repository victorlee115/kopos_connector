from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from unittest import TestCase

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.kopos.doctype.fb_inventory_availability_rule.fb_inventory_availability_rule import (
    rule_identity,
)
from kopos_connector.kopos.services.inventory_autopilot.availability_capacity import (
    CapacityResult,
    _is_effective,
    calculate_capacity,
    target_capacity,
)
from kopos_connector.kopos.services.inventory_autopilot.overlay import _stock_warning
from kopos_connector.kopos.services.inventory_autopilot import holds


class InventoryAvailabilityCapacityTests(TestCase):
    def test_recipe_capacity_uses_the_lowest_component_whole_servings(self) -> None:
        result = calculate_capacity(
            {"MILK": Decimal("0.25"), "FOAM": Decimal("0.5")},
            {
                "MILK": {"current": True, "usable": Decimal("2.0")},
                "FOAM": {"current": True, "usable": Decimal("1.1")},
            },
            target_type="Item",
            target_id="MONT-BLANC",
        )
        self.assertTrue(result.reliable)
        self.assertEqual(result.capacity, Decimal("2"))

    def test_missing_evidence_is_not_treated_as_zero_capacity(self) -> None:
        result = calculate_capacity(
            {"MILK": Decimal("0.25")},
            {"MILK": {"current": False, "reason": "stock read unavailable"}},
            target_type="Item",
            target_id="MONT-BLANC",
        )
        self.assertFalse(result.reliable)
        self.assertIsNone(result.capacity)

    def test_modifier_uses_the_same_component_calculation(self) -> None:
        result = calculate_capacity(
            {"OAT-MILK": Decimal("0.3")},
            {"OAT-MILK": {"current": True, "usable": Decimal("0.6")}},
            target_type="Modifier",
            target_id="OAT-MILK-MOD",
        )
        self.assertEqual(result.capacity, Decimal("2"))

    def test_rule_identity_includes_company_warehouse_and_target(self) -> None:
        outlet_a = rule_identity(
            target_type="Item",
            target_id="MONT-BLANC",
            company="JiJi Sdn Bhd",
            warehouse="Outlet A",
        )
        outlet_b = rule_identity(
            target_type="Item",
            target_id="MONT-BLANC",
            company="JiJi Sdn Bhd",
            warehouse="Outlet B",
        )
        self.assertNotEqual(outlet_a, outlet_b)
        self.assertTrue(outlet_a.startswith("FB-RULE-"))
        self.assertLessEqual(len(outlet_a), 41)

    def test_explicit_modes_share_capacity_without_turning_off_into_warning(self) -> None:
        zero = CapacityResult("Item", "MONT-BLANC", Decimal("0"), True, "zero", {})
        unavailable = CapacityResult("Item", "MONT-BLANC", None, False, "not ready", {})
        self.assertEqual(_stock_warning("Off", zero), (False, False, None))
        self.assertEqual(_stock_warning("Warn", zero), (True, True, "Current recipe stock evidence shows zero sellable capacity"))
        self.assertEqual(_stock_warning("Ask Manager", unavailable), (True, False, "not ready"))
        self.assertEqual(_stock_warning("Auto Pause & Restore", unavailable), (True, False, "not ready"))

    def test_recipe_effective_time_boundaries_are_inclusive(self) -> None:
        row = {
            "effective_from": "2026-08-15 10:00:00",
            "effective_to": "2026-08-15 12:00:00",
        }
        self.assertFalse(_is_effective(row, "2026-08-15 09:59:59"))
        self.assertTrue(_is_effective(row, "2026-08-15 10:00:00"))
        self.assertTrue(_is_effective(row, "2026-08-15 12:00:00"))
        self.assertFalse(_is_effective(row, "2026-08-15 12:00:01"))

    def test_auto_pause_uses_reliable_capacity_before_forecast_matures(self) -> None:
        import frappe

        rule = {
            "name": "RULE-A",
            "target_type": "Item",
            "target_id": "MONT-BLANC",
            "company": "JiJi Sdn Bhd",
            "warehouse": "Outlet A",
            "mode": "Auto Pause & Restore",
        }
        zero = CapacityResult("Item", "MONT-BLANC", Decimal("0"), True, "zero", {})
        recovered = CapacityResult("Item", "MONT-BLANC", Decimal("2"), True, "recovered", {})
        with (
            patch.object(frappe.db, "exists", return_value=True),
            patch.object(frappe, "get_all", return_value=[rule]),
            patch.object(holds, "target_capacity", return_value=zero),
            patch.object(holds, "create_hold") as create_hold,
        ):
            self.assertEqual(holds.create_reliable_automation_holds(warehouse="Outlet A"), 1)
        create_hold.assert_called_once()

        active_hold = {**rule, "name": "HOLD-A", "source": "automation", "status": "Active"}
        with (
            patch.object(frappe.db, "exists", return_value=True),
            patch.object(frappe, "get_all", return_value=[active_hold]),
            patch.object(frappe.db, "get_value", return_value="Auto Pause & Restore"),
            patch.object(holds, "target_capacity", return_value=recovered),
            patch.object(holds, "release_hold") as release_hold,
        ):
            self.assertEqual(holds.restore_automation_holds(warehouse="Outlet A"), 1)
        release_hold.assert_called_once_with("HOLD-A")

    def test_non_stock_sellable_uses_frozen_recipe_components_not_finished_item_bin(self) -> None:
        import frappe

        policy = [{"name": "POLICY-1", "cutover_token": "CUT-1", "cutover_at": "2026-01-01 00:00:00"}]
        recipe = SimpleNamespace(
            name="RECIPE-1",
            yield_qty="1",
            default_serving_qty="1",
            effective_from=None,
            effective_to=None,
            components=[SimpleNamespace(item="MILK", qty="0.25", stock_qty="0.25", stock_uom="L", affects_stock=1, substitution_key=None, name="ROW-1")],
            allowed_modifier_groups=[],
            recipe_modifier_effects=[],
        )

        def db_get_value(doctype, name_or_filters, fields, **kwargs):
            if doctype == "Item":
                return {"name": "MONT-BLANC", "stock_uom": "Each", "is_stock_item": 0}
            return None

        def db_exists(doctype, *args, **kwargs):
            return doctype in {"DocType", "Bin", "FB Resolved Sale", "FB Resolved Component"}

        with (
            patch.object(frappe.db, "get_value", side_effect=db_get_value),
            patch.object(frappe.db, "exists", side_effect=db_exists),
            patch.object(frappe, "get_all", side_effect=[policy, [{"name": "RECIPE-1", "effective_from": None, "effective_to": None, "version_no": 1}], [{"item_code": "MILK", "actual_qty": "2", "reserved_qty": "0"}]]),
            patch.object(frappe, "get_cached_doc", return_value=recipe),
            patch.object(frappe, "get_meta", return_value=SimpleNamespace(has_field=lambda field: field in {"actual_qty", "reserved_qty"})),
            patch.object(frappe.db, "sql", return_value=[{"quantity": "0"}]),
        ):
            result = target_capacity(
                target_type="Item",
                target_id="MONT-BLANC",
                company="JiJi Sdn Bhd",
                warehouse="Outlet A",
            )

        self.assertTrue(result.reliable)
        self.assertEqual(result.capacity, Decimal("8"))
        self.assertEqual(result.requirements, {"MILK": Decimal("0.25")})

    def test_modifier_capacity_uses_the_frozen_effect_and_component_bins(self) -> None:
        import frappe

        recipe = SimpleNamespace(
            name="RECIPE-1",
            yield_qty="1",
            default_serving_qty="1",
            effective_from=None,
            effective_to=None,
            components=[SimpleNamespace(item="MILK", qty="0.25", stock_qty="0.25", stock_uom="L", affects_stock=1, substitution_key=None, name="ROW-1")],
            allowed_modifier_groups=[],
            recipe_modifier_effects=[SimpleNamespace(
                modifier="MOD-OAT",
                kind="Add",
                new_item="OAT",
                target_item=None,
                target_substitution_key=None,
                stock_qty_delta="0.1",
                stock_uom="L",
                affects_stock=1,
                affects_recipe=1,
            )],
        )

        def db_get_value(doctype, name_or_filters, fields, **kwargs):
            if doctype == "FB Modifier":
                return {"name": "MOD-OAT", "active": 1, "affects_stock": 1}
            return None

        def db_exists(doctype, *args, **kwargs):
            return doctype in {"DocType", "Bin", "FB Resolved Sale", "FB Resolved Component"}

        with (
            patch.object(frappe.db, "get_value", side_effect=db_get_value),
            patch.object(frappe.db, "exists", side_effect=db_exists),
            patch.object(frappe, "get_all", side_effect=[
                [{"name": "POLICY-1", "cutover_token": "CUT-1", "cutover_at": "2026-01-01 00:00:00"}],
                [{"name": "RECIPE-1", "effective_from": None, "effective_to": None, "version_no": 1}],
                [{"item_code": "MILK", "actual_qty": "1", "reserved_qty": "0"}, {"item_code": "OAT", "actual_qty": "0.2", "reserved_qty": "0"}],
            ]),
            patch.object(frappe, "get_cached_doc", return_value=recipe),
            patch.object(frappe, "get_meta", return_value=SimpleNamespace(has_field=lambda field: field in {"actual_qty", "reserved_qty"})),
            patch.object(frappe.db, "sql", return_value=[{"quantity": "0"}]),
        ):
            result = target_capacity(
                target_type="Modifier",
                target_id="MOD-OAT",
                company="JiJi Sdn Bhd",
                warehouse="Outlet A",
            )

        self.assertTrue(result.reliable)
        self.assertEqual(result.capacity, Decimal("2"))
