from __future__ import annotations

import unittest

from kopos_connector.kopos.services.inventory_autopilot.promotion_economics import (
    PromotionEconomicsError,
    calculate_promotion_economics,
)


class PromotionEconomicsTest(unittest.TestCase):
    def test_reports_cogs_margin_and_scenarios_in_integer_sen(self) -> None:
        result = calculate_promotion_economics(
            items=[{
                "units": 10,
                "baseline_price_sen": 1000,
                "promoted_price_sen": 800,
                "cogs_sen": 300,
                "components": [{"item": "Milk", "qty": 2}],
            }],
            scenarios={"low": 5, "base": 10, "high": 20},
        )
        self.assertEqual(result["revenue_sen"], 8000)
        self.assertEqual(result["cogs_sen"], 3000)
        self.assertEqual(result["gross_profit_sen"], 5000)
        self.assertEqual(result["ingredient_demand"], {"Milk": "20"})
        self.assertEqual(result["planning_mode"], "Review First")

    def test_missing_cost_blocks_publication(self) -> None:
        with self.assertRaisesRegex(PromotionEconomicsError, "cogs_sen is required"):
            calculate_promotion_economics(items=[{
                "units": 1,
                "baseline_price_sen": 100,
                "promoted_price_sen": 50,
            }])


if __name__ == "__main__":
    unittest.main()
