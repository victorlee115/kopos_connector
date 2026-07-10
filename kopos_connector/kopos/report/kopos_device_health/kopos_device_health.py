# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt
# pyright: reportMissingImports=false

from __future__ import annotations

from collections.abc import Mapping

import frappe
from frappe import _
from frappe.utils import cint, cstr

from kopos_connector.api.devices import get_session_roles


SUPPORT_REPORT_ROLES = {"System Manager", "KoPOS Manager", "POS Manager"}


def execute(filters=None):
    """Return a redacted device health list for support-safe Desk review."""
    require_support_report_access()
    return get_columns(), get_data()


def require_support_report_access(user: str | None = None) -> None:
    roles = get_session_roles(user=user)
    if roles.isdisjoint(SUPPORT_REPORT_ROLES):
        frappe.throw(
            _("Only System Manager, KoPOS Manager, or POS Manager can view KoPOS device health"),
            frappe.PermissionError,
        )


def get_columns() -> list[dict[str, object]]:
    return [
        {"label": _("Device ID"), "fieldname": "device_id", "fieldtype": "Data", "width": 150},
        {"label": _("Device Name"), "fieldname": "device_name", "fieldtype": "Data", "width": 180},
        {"label": _("POS Profile"), "fieldname": "pos_profile", "fieldtype": "Link", "options": "POS Profile", "width": 180},
        {"label": _("Config Version"), "fieldname": "config_version", "fieldtype": "Int", "width": 120},
        {"label": _("Enabled"), "fieldname": "enabled", "fieldtype": "Check", "width": 90},
        {"label": _("Last Seen"), "fieldname": "last_seen_at", "fieldtype": "Datetime", "width": 170},
        {"label": _("Last Sync"), "fieldname": "last_sync_at", "fieldtype": "Datetime", "width": 170},
        {"label": _("Printer Summary"), "fieldname": "printer_summary", "fieldtype": "Data", "width": 260},
        {"label": _("User Summary"), "fieldname": "user_summary", "fieldtype": "Data", "width": 220},
        {"label": _("QR Provisioning Action"), "fieldname": "provisioning_action", "fieldtype": "Data", "width": 260},
    ]


def get_data() -> list[dict[str, object]]:
    rows = frappe.get_all(
        "KoPOS Device",
        fields=[
            "name",
            "device_id",
            "device_name",
            "pos_profile",
            "config_version",
            "enabled",
            "last_seen_at",
            "last_sync_at",
        ],
        order_by="last_seen_at desc, modified desc",
        limit_page_length=500,
    )

    data: list[dict[str, object]] = []
    for row in rows:
        device_name = cstr(_read(row, "name")).strip()
        device_doc = frappe.get_doc("KoPOS Device", device_name) if device_name else None
        data.append(
            {
                "device_id": cstr(_read(row, "device_id")).strip(),
                "device_name": cstr(_read(row, "device_name")).strip(),
                "pos_profile": cstr(_read(row, "pos_profile")).strip(),
                "config_version": cint(_read(row, "config_version") or 0),
                "enabled": cint(_read(row, "enabled") or 0),
                "last_seen_at": _read(row, "last_seen_at"),
                "last_sync_at": _read(row, "last_sync_at"),
                "printer_summary": summarize_printers(device_doc),
                "user_summary": summarize_users(device_doc),
                "provisioning_action": _(
                    "System Manager: open KoPOS Provisioning for a one-time QR"
                ),
            }
        )
    return data


def summarize_printers(device_doc: object | None) -> str:
    if device_doc is None:
        return _("No printer rows")

    enabled: list[str] = []
    disabled_count = 0
    for printer in getattr(device_doc, "printers", None) or []:
        role = cstr(_read(printer, "role")).strip() or _("printer")
        protocol = cstr(_read(printer, "protocol")).strip() or _("unknown")
        host = cstr(_read(printer, "host")).strip() or _("no host")
        port = cint(_read(printer, "port") or 0)
        if cint(_read(printer, "enabled") or 0):
            enabled.append(f"{role}: {protocol} {host}:{port or '-'}")
        else:
            disabled_count += 1

    if not enabled and not disabled_count:
        return _("No printer rows")
    summary = "; ".join(enabled) if enabled else _("No enabled printers")
    if disabled_count:
        summary = _("{0}; {1} disabled").format(summary, disabled_count)
    return summary


def summarize_users(device_doc: object | None) -> str:
    if device_doc is None:
        return _("No user rows")

    rows = list(getattr(device_doc, "device_users", None) or [])
    if not rows:
        return _("No user rows")

    active_count = sum(1 for row in rows if cint(_read(row, "active") or 0))
    default_names = [
        cstr(_read(row, "display_name")).strip() or cstr(_read(row, "user")).strip()
        for row in rows
        if cint(_read(row, "default_cashier") or 0)
    ]
    default_copy = ", ".join(name for name in default_names if name) or _("not set")
    return _("{0} active / {1} users; default: {2}").format(
        active_count,
        len(rows),
        default_copy,
    )


def _read(row: object, fieldname: str) -> object:
    if isinstance(row, Mapping):
        return row.get(fieldname)
    return getattr(row, fieldname, None)
