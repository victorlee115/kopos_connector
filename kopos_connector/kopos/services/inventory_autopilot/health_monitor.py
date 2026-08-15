"""Pure classification for the external Inventory Autopilot watchdog."""

from __future__ import annotations

from typing import Any


def critical_health_reasons(payload: dict[str, Any]) -> tuple[str, ...]:
    """Return the fail-closed reasons that must stop outlet expansion."""

    exceptions = payload.get("exceptions")
    if not isinstance(exceptions, dict):
        return ()
    return tuple(sorted({
        str(value).strip()
        for value in exceptions.get("critical_reasons", [])
        if str(value).strip()
    }))


def health_blocks_rollout(payload: dict[str, Any]) -> bool:
    """Keep cutover/next-outlet rollout closed while health is critical."""

    return bool(critical_health_reasons(payload)) or payload.get("draft_purchase_order_safety") == "unsafe"


def classify_health(payload: dict[str, Any]) -> dict[str, Any]:
    """Map the manager health read model to a small monitor status."""

    exceptions = payload.get("exceptions") if isinstance(payload.get("exceptions"), dict) else {}
    critical = list(critical_health_reasons(payload))
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
