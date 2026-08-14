from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from typing import Any

import frappe
from frappe.utils import cstr, now_datetime

from kopos_connector.kopos.services.inventory_autopilot.holds import (
    active_holds,
    choose_availability,
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
        hold_fingerprints.extend(cstr(hold.get("name")) for hold in holds)
        rule = _availability_rule(target_id, company, warehouse)
        # Auto Pause & Restore is deliberately conservative until the measured
        # forecast worker can prove a Reliable result. Surface the shortage as
        # a warning rather than silently creating a hold on weak evidence.
        warning = rule in {"Warn", "Ask Manager", "Auto Pause & Restore"} and _actual_qty(target_id, warehouse) <= Decimal("0")
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
                    "label": "Stock is at or below zero",
                    "source": "stock",
                }] if warning else []) + ([{
                    "code": "automation_waiting_for_reliable_evidence",
                    "label": "Automation is waiting for a reliable stock forecast",
                    "source": "policy",
                }] if warning and rule == "Auto Pause & Restore" else []) + [
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
        stock_item = cstr(option.get("new_item") or option.get("target_item")).strip()
        holds = active_holds(target_type="Modifier", target_id=target_id, warehouse=warehouse)
        hold_fingerprints.extend(cstr(hold.get("name")) for hold in holds)
        warning = bool(stock_item) and _actual_qty(stock_item, warehouse) <= Decimal("0")
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
                "label": "Stock is at or below zero",
                "source": "stock",
            }] if warning else []) + [{
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
        "generated_at": generated_at_iso,
        "valid_until": _iso_with_offset(generated_at + timedelta(minutes=int(policy.get("max_source_age_minutes") or 30))),
        "policy_hash": policy_hash,
        "cutover_token": cstr(policy.get("cutover_token")),
        "hold_set_hash": hold_set_hash,
        "items": targets,
        "modifier_options": modifier_targets,
    }


def _policy(company: str, warehouse: str) -> dict[str, Any] | None:
    rows = frappe.get_all(
        "FB Inventory Policy",
        filters={"company": company, "warehouse": warehouse},
        fields=["name", "automation_state", "cutover_token", "max_source_age_minutes"],
        limit_page_length=1,
    )
    return rows[0] if rows else None


def _availability_rule(item: str, company: str, warehouse: str) -> str:
    return cstr(frappe.db.get_value(
        "FB Inventory Availability Rule",
        {"target_type": "Item", "target_id": item, "company": company, "warehouse": warehouse},
        "mode",
    )).strip() or "Off"


def _actual_qty(item: str, warehouse: str) -> Decimal:
    raw = frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty")
    try:
        value = Decimal(str(raw or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return value if value.is_finite() else Decimal("0")


def _iso_with_offset(value: Any) -> str:
    current = value
    if getattr(current, "tzinfo", None) is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
    return current.isoformat()


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
