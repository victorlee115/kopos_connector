from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from typing import Any

import frappe
from frappe.utils import cstr, get_datetime, now_datetime

from kopos_connector.kopos.services.inventory_autopilot.holds import (
    active_holds,
    choose_availability,
)
from kopos_connector.kopos.services.inventory_autopilot.availability_capacity import (
    target_capacity,
)


def build_inventory_overlay(
    *,
    warehouse: str | None,
    company: str | None,
    items: list[dict[str, Any]],
    modifier_options: list[dict[str, Any]] | None = None,
    known_version: str | None = None,
) -> dict[str, Any]:
    generated_at = now_datetime()
    generated_at_iso = _iso_with_offset(generated_at)
    if not warehouse or not company:
        return {
            "schema_version": "inventory-overlay-v1",
            "status": "unavailable",
            "generated_at": generated_at_iso,
            "items": [],
            "modifier_options": [],
        }
    policy = _policy(company, warehouse)
    if not policy:
        return {
            "schema_version": "inventory-overlay-v1",
            "status": "unavailable",
            "generated_at": generated_at_iso,
            "items": [],
            "modifier_options": [],
            "reasons": [{"code": "policy_missing", "label": "Inventory policy is not configured", "source": "policy"}],
        }
    targets: list[dict[str, Any]] = []
    modifier_targets: list[dict[str, Any]] = []
    hold_fingerprints: list[str] = []
    for item in items:
        target_id = cstr(item.get("id")).strip()
        if not target_id:
            continue
        holds = active_holds(target_type="Item", target_id=target_id, warehouse=warehouse)
        hold_fingerprints.extend(
            f"{cstr(hold.get('name'))}:{int(bool(hold.get('stale')))}:{cstr(hold.get('last_evaluated_at'))}"
            for hold in holds
        )
        rule = _availability_rule(target_id, company, warehouse)
        capacity = target_capacity(
            target_type="Item",
            target_id=target_id,
            company=company,
            warehouse=warehouse,
        )
        warning, shortfall, capacity_reason = _stock_warning(rule, capacity)
        availability = choose_availability(
            commercially_enabled=bool(item.get("is_active", 1)),
            holds=holds,
            warning=warning,
        )
        targets.append(
            {
                "target_id": target_id,
                "availability": availability,
                "freshness": "current",
                "reasons": ([{
                    "code": "inventory_short",
                    "label": "Stock cannot cover one more serving",
                    "source": "stock",
                }] if shortfall else []) + ([{
                    "code": "automation_waiting_for_reliable_evidence",
                    "label": capacity_reason,
                    "source": "stock",
                }] if capacity_reason and not capacity.reliable and rule != "Off" else []) + ([{
                    "code": "stock_check_overdue",
                    "label": "Stock check overdue; selling remains paused until evidence is refreshed",
                    "source": "automation",
                }] if any(bool(hold.get("stale")) for hold in holds) else []) + [
                    {
                        "code": cstr(hold.get("reason_code")),
                        "label": cstr(hold.get("reason_label")),
                        "source": cstr(hold.get("source")),
                        "hold_id": cstr(hold.get("name")),
                    }
                    for hold in holds
                ],
            }
        )
    for option in modifier_options or []:
        target_id = cstr(option.get("id")).strip()
        if not target_id:
            continue
        holds = active_holds(target_type="Modifier", target_id=target_id, warehouse=warehouse)
        hold_fingerprints.extend(
            f"{cstr(hold.get('name'))}:{int(bool(hold.get('stale')))}:{cstr(hold.get('last_evaluated_at'))}"
            for hold in holds
        )
        rule = _availability_rule(target_id, company, warehouse, target_type="Modifier")
        capacity = target_capacity(
            target_type="Modifier",
            target_id=target_id,
            company=company,
            warehouse=warehouse,
        )
        warning, shortfall, capacity_reason = _stock_warning(rule, capacity)
        availability = choose_availability(
            commercially_enabled=bool(option.get("is_active", 1)),
            holds=holds,
            warning=warning,
        )
        modifier_targets.append({
            "target_id": target_id,
            "availability": availability,
            "freshness": "current",
            "reasons": ([{
                "code": "inventory_short",
                "label": "Stock cannot cover one more serving",
                "source": "stock",
            }] if shortfall else []) + ([{
                "code": "inventory_evidence_not_ready",
                "label": capacity_reason,
                "source": "stock",
            }] if capacity_reason and not capacity.reliable and rule != "Off" else []) + ([{
                "code": "stock_check_overdue",
                "label": "Stock check overdue; selling remains paused until evidence is refreshed",
                "source": "automation",
            }] if any(bool(hold.get("stale")) for hold in holds) else []) + [{
                "code": cstr(hold.get("reason_code")),
                "label": cstr(hold.get("reason_label")),
                "source": cstr(hold.get("source")),
                "hold_id": cstr(hold.get("name")),
            } for hold in holds],
        })
    hold_set_hash = _hash(sorted(hold_fingerprints))
    policy_hash = _hash({"company": company, "warehouse": warehouse, "policy": policy})
    version = _hash({"policy_hash": policy_hash, "hold_set_hash": hold_set_hash, "items": targets, "modifier_options": modifier_targets})
    if known_version and cstr(known_version).strip() == version:
        return {
            "schema_version": "inventory-overlay-v1",
            "status": "unchanged",
            "version": version,
            "overlay_hash": version,
            "generated_at": generated_at_iso,
            "valid_until": _iso_with_offset(generated_at + timedelta(minutes=int(policy.get("max_source_age_minutes") or 30))),
            "policy_hash": policy_hash,
            "cutover_token": cstr(policy.get("cutover_token")),
            "hold_set_hash": hold_set_hash,
        }
    return {
        "schema_version": "inventory-overlay-v1",
        "status": "ok",
        "version": version,
        "overlay_hash": version,
        "generated_at": generated_at_iso,
        "valid_until": _iso_with_offset(generated_at + timedelta(minutes=int(policy.get("max_source_age_minutes") or 30))),
        "policy_hash": policy_hash,
        "cutover_token": cstr(policy.get("cutover_token")),
        "hold_set_hash": hold_set_hash,
        "items": targets,
        "modifier_options": modifier_targets,
    }


def device_overlay_is_current(
    *,
    device_name: str,
    acknowledged_version: str,
    acknowledged_hash: str,
    acknowledged_catalog_version: str | None = None,
) -> bool:
    """Compare a device acknowledgement with its latest generated overlay.

    The catalog request publishes this compact identity to Redis.  Both the
    health screen and unattended purchasing must use this exact comparison;
    merely checking that a device reported *some* overlay would let an old menu
    approve a new stock action.
    """

    identifier = cstr(device_name).strip()
    version = cstr(acknowledged_version).strip()
    overlay_hash = cstr(acknowledged_hash).strip()
    if not identifier or not version or not overlay_hash:
        return False
    try:
        getter = getattr(frappe.cache(), "get_value", None)
        if not callable(getter):
            return False
        raw_identity = getter(f"kopos:inventory-autopilot:overlay:{identifier}")
        if isinstance(raw_identity, bytes):
            raw_identity = raw_identity.decode("utf-8")
        if isinstance(raw_identity, str):
            identity = json.loads(raw_identity)
        elif isinstance(raw_identity, dict):
            identity = raw_identity
        else:
            return False
        if not isinstance(identity, dict):
            return False
        valid_until = identity.get("valid_until")
        if valid_until and get_datetime(valid_until) < now_datetime():
            return False
        current_version = cstr(identity.get("version")).strip()
        current_hash = cstr(identity.get("overlay_hash") or current_version).strip()
        if acknowledged_catalog_version is not None:
            current_catalog = cstr(identity.get("catalog_version")).strip()
            if not current_catalog or current_catalog != cstr(acknowledged_catalog_version).strip():
                return False
        return bool(current_version and current_hash) and current_version == version and current_hash == overlay_hash
    except Exception:
        # Cache corruption or expiry is intentionally an unacknowledged
        # overlay, never a false-green gate or an exception on checkout.
        return False


def _policy(company: str, warehouse: str) -> dict[str, Any] | None:
    rows = frappe.get_all(
        "FB Inventory Policy",
        filters={"company": company, "warehouse": warehouse},
        fields=["name", "automation_state", "cutover_token", "max_source_age_minutes"],
        limit_page_length=1,
    )
    return rows[0] if rows else None


def _availability_rule(item: str, company: str, warehouse: str, *, target_type: str = "Item") -> str:
    return cstr(frappe.db.get_value(
        "FB Inventory Availability Rule",
        {"target_type": target_type, "target_id": item, "company": company, "warehouse": warehouse},
        "mode",
    )).strip() or "Off"


def _stock_warning(rule: str, capacity: Any) -> tuple[bool, bool, str | None]:
    """Apply the four explicit modes to one shared capacity result."""

    if rule == "Off":
        return False, False, None
    if getattr(capacity, "reliable", False) and getattr(capacity, "capacity", None) == Decimal("0"):
        return True, True, "Current recipe stock evidence shows zero sellable capacity"
    if not getattr(capacity, "reliable", False):
        return True, False, cstr(getattr(capacity, "reason", None)).strip() or "Current recipe stock evidence is not ready"
    return False, False, None


def _iso_with_offset(value: Any) -> str:
    current = value
    if getattr(current, "tzinfo", None) is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
    return current.isoformat()


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
