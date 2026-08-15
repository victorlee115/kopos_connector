from decimal import Decimal
from datetime import date, timedelta
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

    def test_reliability_requires_configured_safety_stock_to_cover_measured_underforecast(self):
        actuals = [1, 8] * 14
        missing = evaluate_forecast(actuals)
        too_low = evaluate_forecast(actuals, safety_stock=0)
        covered = evaluate_forecast(actuals, safety_stock=20)

        self.assertIn("safety_stock_not_configured", missing.reasons)
        self.assertIn("safety_stock_below_measured_p90", too_low.reasons)
        self.assertEqual(covered.state, "Reliable")

    def test_same_weekday_candidate_uses_calendar_dates_not_open_day_position(self):
        first = date(2026, 1, 1)
        dates = [first + timedelta(days=index) for index in range(35)]
        actuals = [Decimal(value.weekday() + 1) for value in dates]

        result = evaluate_forecast(
            actuals,
            operating_dates=dates,
            forecast_date=dates[-1] + timedelta(days=1),
            safety_stock=10,
        )

        self.assertEqual(result.selected_model, "same_weekday_seasonal_naive")
        self.assertEqual(result.forecast, Decimal((dates[-1] + timedelta(days=1)).weekday() + 1))
        self.assertEqual(result.mae, Decimal("0"))

    def test_zero_actual_demand_omits_wape_instead_of_inventing_zero_accuracy(self):
        result = evaluate_forecast([0] * 28, safety_stock=0)

        self.assertEqual(result.state, "Reliable")
        self.assertIsNone(result.wape)
