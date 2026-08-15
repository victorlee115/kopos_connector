from datetime import datetime
from decimal import Decimal

from kopos_connector.kopos.services.inventory_autopilot.forecast import ForecastResult
from kopos_connector.kopos.services.inventory_autopilot.planning import (
    aggregate_consumption,
    overall_forecast_state,
)


def test_aggregate_consumption_uses_observed_days_and_zero_fills_items():
    days, series = aggregate_consumption([
        {
            "observed_at": datetime(2026, 8, 1, 2, 0),
            "item": "MILK",
            "stock_qty": "2.5",
            "affects_stock": 1,
        },
        {
            "observed_at": datetime(2026, 8, 1, 4, 0),
            "item": "SYRUP",
            "stock_qty": "1",
            "affects_stock": 1,
        },
        {
            "observed_at": datetime(2026, 8, 3, 4, 0),
            "item": "MILK",
            "stock_qty": "3",
            "affects_stock": 1,
        },
        {
            "observed_at": datetime(2026, 8, 2, 4, 0),
            "item": "IGNORED",
            "stock_qty": "9",
            "affects_stock": 0,
        },
    ])

    assert days == (datetime(2026, 8, 1).date(), datetime(2026, 8, 3).date())
    assert series["MILK"] == (Decimal("2.5"), Decimal("3"))
    assert series["SYRUP"] == (Decimal("1"), Decimal("0"))
    assert "IGNORED" not in series


def test_overall_forecast_state_is_fail_closed():
    base = ForecastResult("Reliable", "model", Decimal("1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), 28, ())
    assert overall_forecast_state([base]) == "Reliable"
    assert overall_forecast_state([base, ForecastResult("Please check", None, None, None, None, None, None, 28, ())]) == "Please check"
    assert overall_forecast_state([base, ForecastResult("Not ready", None, None, None, None, None, None, 0, ())]) == "Not ready"
    assert overall_forecast_state([]) == "Not ready"
