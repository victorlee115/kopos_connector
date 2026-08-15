from decimal import Decimal

import pytest

from kopos_connector.kopos.services.inventory_autopilot.preparation import (
    _preparation_alert,
    preparation_trigger_level,
    preparation_thresholds,
)


def test_preparation_thresholds_default_to_bom_quantity():
    assert preparation_thresholds(bom_quantity="12") == (Decimal("12"), Decimal("12"))


def test_preparation_thresholds_accept_explicit_batch_and_ready_levels():
    assert preparation_thresholds(
        bom_quantity="12",
        configured_batch_qty="24",
        configured_min_ready_qty="8",
    ) == (Decimal("24"), Decimal("8"))


def test_preparation_trigger_keeps_ready_level_and_only_adds_measured_lead_demand():
    assert preparation_trigger_level(
        minimum_ready_qty="8",
        daily_demand="24",
        lead_minutes="120",
    ) == Decimal("10")
    assert preparation_trigger_level(
        minimum_ready_qty="8",
        daily_demand=None,
        lead_minutes="120",
    ) == Decimal("8")


@pytest.mark.parametrize("lead_minutes", ["-1", "not-a-number", "NaN", "Infinity"])
def test_preparation_trigger_rejects_invalid_lead_time(lead_minutes):
    with pytest.raises(ValueError):
        preparation_trigger_level(
            minimum_ready_qty="8",
            daily_demand="24",
            lead_minutes=lead_minutes,
        )


@pytest.mark.parametrize("value", ["-1", "not-a-number", "NaN", "Infinity"])
def test_preparation_thresholds_reject_invalid_bom_quantity(value):
    with pytest.raises(ValueError):
        preparation_thresholds(bom_quantity=value)


def test_low_stock_derives_an_alert_without_a_work_order_identity():
    alert = _preparation_alert(
        policy={
            "company": "JiJi",
            "warehouse": "Outlet - KL",
            "automation_state": "Active",
        },
        bom={
            "name": "BOM-COLD-FOAM",
            "item_name": "Cold Foam",
            "modified": "2026-08-16 10:00:00",
            "custom_kopos_preparation_lead_minutes": 30,
        },
        item="COLD-FOAM",
        batch_qty=12,
        min_ready_qty=8,
        trigger_qty=8,
        actual_qty=2,
        stock_marker="2026-08-16 09:55:00",
    )

    assert alert["kind"] == "preparation"
    assert alert["preparation_alert"] is True
    assert alert["document"] == "BOM-COLD-FOAM"
    assert alert["qty"] == "12"
    assert alert["current_qty"] == "2"
    assert alert["fingerprint"]
    assert "work_order" not in alert


def test_review_first_alert_remains_actionable_for_routine_staff_preparation():
    alert = _preparation_alert(
        policy={
            "company": "JiJi",
            "warehouse": "Outlet - KL",
            "automation_state": "Review First",
        },
        bom={"name": "BOM-ORANGE-JUICE", "modified": "2026-08-16 10:00:00"},
        item="ORANGE-JUICE",
        batch_qty=10,
        min_ready_qty=5,
        trigger_qty=5,
        actual_qty=0,
        stock_marker="no-bin",
    )

    assert "blocked_reason" not in alert


def test_paused_alert_is_explicitly_blocked_for_staff():
    alert = _preparation_alert(
        policy={
            "company": "JiJi",
            "warehouse": "Outlet - KL",
            "automation_state": "Paused",
        },
        bom={"name": "BOM-ORANGE-JUICE", "modified": "2026-08-16 10:00:00"},
        item="ORANGE-JUICE",
        batch_qty=10,
        min_ready_qty=5,
        trigger_qty=5,
        actual_qty=0,
        stock_marker="no-bin",
        blocked_reason="Inventory automation is paused for this outlet; resume it before preparing this batch",
    )

    assert alert["blocked_reason"].startswith("Inventory automation is paused")
