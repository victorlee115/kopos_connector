"""Pure classification for the external Inventory Autopilot watchdog."""

from __future__ import annotations

from typing import Any


def classify_health(payload: dict[str, Any]) -> dict[str, Any]:
    """Map the manager health read model to a small monitor status."""

    exceptions = payload.get("exceptions") if isinstance(payload.get("exceptions"), dict) else {}
    critical = [str(value) for value in exceptions.get("critical_reasons", []) if value]
    warnings = [str(value) for value in exceptions.get("warning_reasons", []) if value]
    if payload.get("draft_purchase_order_safety") == "unsafe":
        critical.append("draft_purchase_order_outbound_configuration")
    if payload.get("automation_state") == "Paused":
        warnings.append("inventory_automation_paused")
    status = "critical" if critical else "warning" if warnings else "ok"
    return {
        "status": status,
        "critical_reasons": sorted(set(critical)),
        "warning_reasons": sorted(set(warnings)),
    }
