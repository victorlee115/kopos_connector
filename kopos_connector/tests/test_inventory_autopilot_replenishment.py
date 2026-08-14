from decimal import Decimal
import unittest

from kopos_connector.kopos.services.inventory_autopilot.replenishment import (
    ReplenishmentInput,
    build_replenishment_plan,
    evaluate_automation_gates,
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
