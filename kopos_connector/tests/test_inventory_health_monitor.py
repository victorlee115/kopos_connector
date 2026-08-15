from kopos_connector.kopos.services.inventory_autopilot.health_monitor import classify_health


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
