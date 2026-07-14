from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import cstr


def execute(allow_clear: bool = False, backup_verified: bool = False) -> None:
    if not frappe.db.exists("DocType", "KoPOS Device"):
        return

    device_rows = frappe.get_all(
        "KoPOS Device",
        fields=["name", "device_id", "api_user", "last_seen_at", "modified"],
        filters={"api_user": ["is", "set"]},
        order_by="modified desc",
    )

    devices_by_api_user: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in device_rows:
        api_user = cstr(row.get("api_user")).strip()
        if not api_user:
            continue
        devices_by_api_user[api_user].append(row)

    duplicate_devices: list[tuple[str, str]] = []
    for api_user, grouped_rows in devices_by_api_user.items():
        if len(grouped_rows) < 2:
            continue

        sorted_rows = sorted(
            grouped_rows,
            key=lambda row: (
                cstr(row.get("modified")).strip(),
                cstr(row.get("last_seen_at")).strip(),
                cstr(row.get("name")).strip(),
            ),
            reverse=True,
        )
        for duplicate_row in sorted_rows[1:]:
            duplicate_id = (
                cstr(duplicate_row.get("device_id")).strip()
                or cstr(duplicate_row.get("name")).strip()
            )
            duplicate_devices.append((api_user, duplicate_id))

    if not duplicate_devices:
        return

    affected = ", ".join(
        f"{device_id} ({api_user})" for api_user, device_id in duplicate_devices
    )
    if not allow_clear or not backup_verified:
        frappe.throw(
            "Duplicate KoPOS Device api_user mappings require allow_clear=True and backup_verified=True after a verified backup before migration: "
            + affected,
            frappe.ValidationError,
        )

    cleared_devices: list[str] = []
    for api_user, grouped_rows in devices_by_api_user.items():
        if len(grouped_rows) < 2:
            continue
        sorted_rows = sorted(
            grouped_rows,
            key=lambda row: (
                cstr(row.get("modified")).strip(),
                cstr(row.get("last_seen_at")).strip(),
                cstr(row.get("name")).strip(),
            ),
            reverse=True,
        )
        for duplicate_row in sorted_rows[1:]:
            frappe.db.set_value(
                "KoPOS Device",
                duplicate_row.get("name"),
                "api_user",
                None,
                update_modified=False,
            )
            cleared_devices.append(
                cstr(duplicate_row.get("device_id")).strip()
                or cstr(duplicate_row.get("name")).strip()
            )

    frappe.log_error(
        title="KoPOS duplicate api_user mappings cleared",
        message=(
            "Cleared duplicate KoPOS Device api_user mappings for: "
            + ", ".join(cleared_devices)
            + ". Reprovision the affected devices to generate dedicated API credentials."
        ),
    )
