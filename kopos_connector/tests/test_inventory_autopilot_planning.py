from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.kopos.services.inventory_autopilot.forecast import ForecastResult
from kopos_connector.kopos.services.inventory_autopilot import planning as planning_module
from kopos_connector.kopos.services.inventory_autopilot.planning import (
    aggregate_consumption,
    automation_ceiling_gates,
    build_forecast_evidence,
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


def test_aggregate_consumption_keeps_proven_zero_demand_operating_days():
    proven_days = (
        datetime(2026, 8, 1).date(),
        datetime(2026, 8, 2).date(),
        datetime(2026, 8, 3).date(),
    )
    days, series = aggregate_consumption(
        [{
            "observed_at": datetime(2026, 8, 1, 2, 0),
            "item": "MILK",
            "stock_qty": "2.5",
            "affects_stock": 1,
        }],
        operating_days=proven_days,
        tracked_items=("MILK", "SYRUP"),
    )

    assert days == proven_days
    assert series["MILK"] == (Decimal("2.5"), Decimal("0"), Decimal("0"))
    assert series["SYRUP"] == (Decimal("0"), Decimal("0"), Decimal("0"))


def test_overall_forecast_state_is_fail_closed():
    base = ForecastResult("Reliable", "model", Decimal("1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), 28, ())
    assert overall_forecast_state([base]) == "Reliable"
    assert overall_forecast_state([base, ForecastResult("Please check", None, None, None, None, None, None, 28, ())]) == "Please check"
    assert overall_forecast_state([base, ForecastResult("Not ready", None, None, None, None, None, None, 0, ())]) == "Not ready"
    assert overall_forecast_state([]) == "Not ready"


def test_forecast_evidence_keeps_readable_rolling_origin_arithmetic():
    evidence = build_forecast_evidence(
        operating_days=(datetime(2026, 8, 1).date(), datetime(2026, 8, 2).date()),
        item_data=({
            "item": "MILK",
            "algorithm_version": "inventory-autopilot-forecast-v1",
            "model": "trailing_open_day_median",
            "forecast_state": "Reliable",
            "forecast": "4.5",
            "training_days": 14,
            "test_days": 14,
            "valid_operating_days": 28,
            "mae": "0.25",
            "wape": "0.05",
            "signed_bias": "-0.1",
            "positive_underforecast_p90": "0.5",
            "reasons": (),
            "explanation": "Selected the lower-MAE candidate using prior observations only",
        },),
    )

    assert evidence["algorithm_version"] == "inventory-autopilot-forecast-v1"
    assert evidence["data_window"] == {"first": "2026-08-01", "last": "2026-08-02"}
    assert evidence["items"][0]["mae"] == "0.25"
    assert evidence["items"][0]["positive_underforecast_p90"] == "0.5"


def test_automation_ceilings_are_explicit_and_fail_closed():
    assert automation_ceiling_gates(
        quantity_ceiling=None,
        value_ceiling=None,
        proposed_quantity=Decimal("1"),
        proposed_value=Decimal("10"),
    ) == {"quantity_ceiling": False, "value_ceiling": False}

    assert automation_ceiling_gates(
        quantity_ceiling=Decimal("5"),
        value_ceiling=Decimal("20"),
        proposed_quantity=Decimal("5"),
        proposed_value=Decimal("20"),
    ) == {"quantity_ceiling": True, "value_ceiling": True}

    assert automation_ceiling_gates(
        quantity_ceiling=Decimal("4.99"),
        value_ceiling=Decimal("19.99"),
        proposed_quantity=Decimal("5"),
        proposed_value=Decimal("20"),
    ) == {"quantity_ceiling": False, "value_ceiling": False}


def test_device_gate_requires_clean_inventory_command_queue():
    now = datetime(2026, 8, 15, 12, 0)
    row = {
        "name": "TABLET-1",
        "inventory_report_received_at": now,
        "inventory_observed_at": now,
        "config_version": 2,
        "inventory_config_version": 2,
        "inventory_catalog_version": "catalog-2",
        "inventory_overlay_version": "overlay-2",
        "inventory_overlay_hash": "hash-2",
        "inventory_sales_pending": 0,
        "inventory_sales_syncing": 0,
        "inventory_sales_failed": 0,
        "inventory_sales_dead_letter": 0,
        "inventory_commands_pending": 1,
        "inventory_commands_syncing": 0,
        "inventory_commands_failed": 0,
        "inventory_commands_dead_letter": 0,
    }
    with patch.object(planning_module.frappe.db, "exists", return_value=True), patch.object(
        planning_module.frappe.db, "sql", return_value=[row]
    ), patch.object(planning_module, "now_datetime", return_value=now), patch.object(
        planning_module, "device_overlay_is_current", return_value=True
    ):
        assert planning_module._devices_current("Outlet - KL", 30) is False
        row["inventory_commands_pending"] = 0
        assert planning_module._devices_current("Outlet - KL", 30) is True


def test_planning_health_marker_is_warehouse_scoped():
    first = planning_module._planning_marker_key("Outlet - KL")
    second = planning_module._planning_marker_key("Outlet - PJ")

    assert first.startswith("kopos:inventory-autopilot:health:last_plan:")
    assert first != second


def test_supplier_configuration_uses_standard_item_purchase_authority():
    values = {
        "stock_uom": "Gram",
        "purchase_uom": "Bag",
        "min_order_qty": "1500",
        "lead_time_days": "3",
    }
    with patch.object(planning_module.frappe.db, "exists", return_value=True), patch.object(
        planning_module.frappe,
        "get_all",
        side_effect=[
            [{"supplier": "SUPPLIER-A", "custom_kopos_supplier_pack_size": "999"}],
            [{"conversion_factor": "500"}],
        ],
    ):
        result = planning_module._supplier_configuration("COFFEE", item_values=values)

    assert result == {
        "source_current": True,
        "lead_time_days": Decimal("3"),
        "supplier_pack": Decimal("500"),
        "supplier_minimum": Decimal("1500"),
        "purchase_conversion_factor": Decimal("500"),
    }


def test_purchase_uom_conversion_is_exact_and_fail_closed():
    assert planning_module._purchase_uom_conversion(
        item="MILK", stock_uom="Litre", purchase_uom="Litre"
    ) == Decimal("1")
    with patch.object(planning_module.frappe.db, "exists", return_value=True), patch.object(
        planning_module.frappe,
        "get_all",
        return_value=[{"conversion_factor": "12"}, {"conversion_factor": "12"}],
    ):
        assert planning_module._purchase_uom_conversion(
            item="MILK", stock_uom="Each", purchase_uom="Carton"
        ) is None


def test_purchase_plan_keeps_stock_truth_and_standard_purchase_uom():
    line = planning_module.ReplenishmentLine(
        "COFFEE", "Outlet", Decimal("1500"), "shortfall"
    )
    planned = planning_module._planned_document_line(
        line=line,
        item_data={
            "action": "Purchase",
            "config": {
                "stock_uom": "Gram",
                "purchase_uom": "Bag",
                "purchase_conversion_factor": Decimal("500"),
            },
        },
    )
    assert planned["quantity_decimal"] == "3"
    assert planned["uom"] == "Bag"
    assert planned["stock_quantity_decimal"] == "1500"
    assert planned["stock_uom"] == "Gram"
    assert planned["conversion_factor_decimal"] == "500"


def test_transfer_source_cannot_be_the_destination():
    result = planning_module._transfer_source_configuration(
        item="MILK",
        source_warehouse="Outlet",
        destination_warehouse="Outlet",
        company="Cafe Co",
        max_source_age=30,
    )
    assert result["source_current"] is False
    assert result["source_available"] is None


def test_transfer_route_requires_one_explicit_standard_reorder_row():
    meta = type("Meta", (), {"has_field": lambda self, field: field == "custom_kopos_source_warehouse"})()
    with patch.object(planning_module.frappe.db, "exists", return_value=True), patch.object(
        planning_module.frappe, "get_meta", return_value=meta
    ), patch.object(
        planning_module.frappe,
        "get_all",
        return_value=[{
            "name": "REORDER-1",
            "material_request_type": "Transfer",
            "custom_kopos_source_warehouse": "Central Kitchen",
        }],
    ):
        assert planning_module._replenishment_route(item="MILK", warehouse="Outlet") == {
            "action": "Transfer",
            "source_warehouse": "Central Kitchen",
            "route_current": True,
        }
