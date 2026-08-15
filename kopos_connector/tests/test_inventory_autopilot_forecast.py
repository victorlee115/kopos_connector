from decimal import Decimal
import unittest

from kopos_connector.kopos.services.inventory_autopilot.forecast import evaluate_forecast


class ForecastTests(unittest.TestCase):
    def test_insufficient_post_cutover_history_is_not_ready(self):
        result = evaluate_forecast([1] * 27)
        self.assertEqual(result.state, "Not ready")

    def test_model_selection_is_rolling_origin_and_measured(self):
        result = evaluate_forecast([Decimal(str(10 + (index % 7))) for index in range(35)])
        self.assertIn(result.state, {"Reliable", "Please check"})
        self.assertIsNotNone(result.selected_model)
        self.assertIsNotNone(result.mae)
        self.assertIsNotNone(result.positive_underforecast_p90)

    def test_shelf_life_cap_degrades_when_it_cannot_cover_uncertainty(self):
        result = evaluate_forecast([1, 20] * 20, shelf_life_days=1, shelf_life_cap=1)
        self.assertEqual(result.state, "Please check")
        self.assertIn("shelf_life_cap_below_measured_uncertainty", result.reasons)
