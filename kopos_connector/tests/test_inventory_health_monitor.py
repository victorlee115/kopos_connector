from kopos_connector.kopos.services.inventory_autopilot.health_monitor import (
    classify_health,
    health_blocks_rollout,
)


def test_health_monitor_classifies_critical_and_warning_reasons():
    result = classify_health({
        "automation_state": "Active",
        "draft_purchase_order_safety": "unsafe",
        "exceptions": {
            "critical_reasons": ["inventory_projection_dead_letter"],
            "warning_reasons": ["inventory_device_stale"],
        },
    })
    assert result == {
        "status": "critical",
        "critical_reasons": ["draft_purchase_order_outbound_configuration", "inventory_projection_dead_letter"],
        "warning_reasons": ["inventory_device_stale"],
    }


def test_health_monitor_reports_paused_policy_as_warning():
    assert classify_health({"automation_state": "Paused", "exceptions": {}})["status"] == "warning"


def test_core_cutover_does_not_block_on_draft_purchase_order_safety():
    payload = {
        "draft_purchase_order_safety": "unsafe",
        "exceptions": {
            "critical_reasons": ["draft_purchase_order_outbound_configuration"],
        },
    }
    assert health_blocks_rollout(payload)
    assert not health_blocks_rollout(payload, include_draft_purchase_order=False)
