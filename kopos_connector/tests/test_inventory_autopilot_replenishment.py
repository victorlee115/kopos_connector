from decimal import Decimal
import unittest

from kopos_connector.kopos.services.inventory_autopilot.replenishment import (
    ReplenishmentInput,
    build_replenishment_plan,
    evaluate_automation_gates,
    shelf_life_allows_replenishment,
)


class ReplenishmentTests(unittest.TestCase):
    def test_rounds_to_pack_and_supplier_minimum_without_buying_when_covered(self):
        lines = build_replenishment_plan([
            ReplenishmentInput("MILK", "Kitchen", Decimal("2"), Decimal("1"), Decimal("0"), Decimal("0"), Decimal("10"), Decimal("2"), Decimal("6"), Decimal("12"), Decimal("30")),
            ReplenishmentInput("SYRUP", "Kitchen", Decimal("20"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("10"), Decimal("1"), Decimal("1"), Decimal("1"), None),
        ])
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].quantity, Decimal("12"))

    def test_any_failed_gate_blocks_automation(self):
        allowed, failed = evaluate_automation_gates({"policy_active": True, "forecast_reliable": False, "device_clean": True})
        self.assertFalse(allowed)
        self.assertEqual(failed, ("forecast_reliable",))

    def test_shelf_life_caps_the_resulting_position_not_only_the_new_order(self):
        value = ReplenishmentInput(
            "CREAM",
            "Kitchen",
            Decimal("8"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("20"),
            Decimal("0"),
            Decimal("6"),
            Decimal("0"),
            Decimal("10"),
        )

        self.assertFalse(shelf_life_allows_replenishment(value))
        self.assertEqual(build_replenishment_plan([value]), ())

    def test_shelf_life_keeps_an_exact_supplier_pack_that_fits_remaining_capacity(self):
        value = ReplenishmentInput(
            "JUICE",
            "Kitchen",
            Decimal("4"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("9"),
            Decimal("0"),
            Decimal("6"),
            Decimal("0"),
            Decimal("10"),
        )

        self.assertTrue(shelf_life_allows_replenishment(value))
        self.assertEqual(build_replenishment_plan([value])[0].quantity, Decimal("6"))
