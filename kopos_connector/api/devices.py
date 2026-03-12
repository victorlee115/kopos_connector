from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime


KOPOS_DEVICE_API_ROLE = "KoPOS Device API"


def get_session_roles(user: str | None = None) -> set[str]:
    resolved_user = (
        cstr(user).strip() or cstr(getattr(frappe.session, "user", None)).strip()
    )
    if not resolved_user or resolved_user == "Guest":
        return set()

    get_roles = getattr(frappe, "get_roles", None)
    if not callable(get_roles):
        return set()

    roles = get_roles(resolved_user) or []
    if not isinstance(roles, (list, tuple, set)):
        roles = []

    return {cstr(role).strip() for role in roles if cstr(role).strip()}


def require_system_manager(user: str | None = None) -> None:
    if "System Manager" not in get_session_roles(user=user):
        frappe.throw(
            _("Only a System Manager can perform this action"),
            frappe.ValidationError,
        )


def require_device_api_access(device_doc) -> None:
    resolved_user = cstr(getattr(frappe.session, "user", None)).strip()
    if not resolved_user or resolved_user == "Guest":
        frappe.throw(_("Authentication required"), frappe.ValidationError)

    roles = get_session_roles(resolved_user)
    if "System Manager" in roles:
        return

    if KOPOS_DEVICE_API_ROLE not in roles:
        frappe.throw(
            _("User {0} is not allowed to access KoPOS device APIs").format(
                resolved_user
            ),
            frappe.ValidationError,
        )

    api_user = cstr(getattr(device_doc, "api_user", None)).strip()
    if not api_user or api_user != resolved_user:
        frappe.throw(
            _("User {0} is not authorized for KoPOS Device {1}").format(
                resolved_user, cstr(getattr(device_doc, "device_id", None)).strip()
            ),
            frappe.ValidationError,
        )


def require_kopos_api_access() -> None:
    roles = get_session_roles()
    if "System Manager" in roles or KOPOS_DEVICE_API_ROLE in roles:
        return

    frappe.throw(
        _("User {0} is not allowed to access KoPOS APIs").format(
            cstr(getattr(frappe.session, "user", None)).strip() or _("Guest")
        ),
        frappe.ValidationError,
    )


def require_device_context(device_id: str | None = None, name: str | None = None):
    roles = get_session_roles()
    if "System Manager" in roles:
        if cstr(device_id).strip() or cstr(name).strip():
            return get_device_doc(device_id=device_id, name=name)
        return None

    if KOPOS_DEVICE_API_ROLE not in roles:
        frappe.throw(
            _("User {0} is not allowed to access KoPOS device APIs").format(
                cstr(getattr(frappe.session, "user", None)).strip() or _("Guest")
            ),
            frappe.ValidationError,
        )

    if not cstr(device_id).strip() and not cstr(name).strip():
        frappe.throw(
            _("device_id is required for device API requests"), frappe.ValidationError
        )

    device_doc = get_device_doc(device_id=device_id, name=name)
    require_device_api_access(device_doc)
    return device_doc


def get_device_doc(device_id: str | None = None, name: str | None = None):
    device_id_value = cstr(device_id).strip()
    name_value = cstr(name).strip()

    if device_id_value:
        docname = frappe.db.get_value(
            "KoPOS Device", {"device_id": device_id_value}, "name"
        )
        if not docname:
            frappe.throw(
                _("KoPOS Device {0} was not found").format(device_id_value),
                frappe.ValidationError,
            )
        return frappe.get_doc("KoPOS Device", docname)

    if name_value:
        if not frappe.db.exists("KoPOS Device", name_value):
            frappe.throw(
                _("KoPOS Device {0} was not found").format(name_value),
                frappe.ValidationError,
            )
        return frappe.get_doc("KoPOS Device", name_value)

    frappe.throw(_("KoPOS Device is required"), frappe.ValidationError)


def get_device_pos_profile_doc(device_id: str | None = None, name: str | None = None):
    device = get_device_doc(device_id=device_id, name=name)
    return frappe.get_cached_doc("POS Profile", device.pos_profile)


def serialize_device_config(
    device_doc,
    *,
    include_secrets: bool = False,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> dict[str, Any]:
    profile_doc = frappe.get_cached_doc("POS Profile", device_doc.pos_profile)
    company = cstr(getattr(profile_doc, "company", None)).strip() or None
    warehouse = cstr(getattr(profile_doc, "warehouse", None)).strip() or None
    currency = cstr(getattr(profile_doc, "currency", None)).strip() or None
    if not currency and company:
        currency = (
            cstr(frappe.db.get_value("Company", company, "default_currency")).strip()
            or None
        )

    payload = {
        "version": 2,
        "device_id": cstr(device_doc.device_id).strip(),
        "device_name": cstr(device_doc.device_name).strip() or None,
        "device_prefix": cstr(device_doc.device_prefix).strip().upper() or None,
        "enabled": bool(cint(device_doc.enabled)),
        "managed_by_erp": True,
        "config_version": cint(device_doc.config_version or 1),
        "pos_profile": cstr(device_doc.pos_profile).strip(),
        "company": company,
        "warehouse": warehouse,
        "currency": currency,
        "allow_training_mode": bool(cint(device_doc.allow_training_mode)),
        "allow_manual_settings_override": bool(
            cint(device_doc.allow_manual_settings_override)
        ),
        "app_min_version": cstr(device_doc.app_min_version).strip() or None,
        "printers": [
            {
                "role": cstr(row.role).strip(),
                "enabled": bool(cint(row.enabled)),
                "protocol": cstr(row.protocol).strip(),
                "host": cstr(row.host).strip(),
                "port": cint(row.port or 9100),
                "label_width_mm": cint(row.label_width_mm or 0) or None,
                "label_height_mm": cint(row.label_height_mm or 0) or None,
                "copies": max(1, cint(row.copies or 1)),
            }
            for row in (device_doc.printers or [])
        ],
        "users": [
            {
                "id": cstr(row.user).strip(),
                "display_name": cstr(row.display_name).strip()
                or cstr(row.user).strip(),
                "pin_hash": cstr(getattr(row, "pin_hash", None)).strip(),
                "active": bool(cint(row.active)),
                "can_manager_override": bool(cint(row.can_manager_override)),
                "can_refund": bool(cint(row.can_refund)),
                "can_void": bool(cint(row.can_void)),
                "can_open_shift": bool(cint(row.can_open_shift)),
                "can_close_shift": bool(cint(row.can_close_shift)),
                "default_cashier": bool(cint(row.default_cashier)),
            }
            for row in (device_doc.device_users or [])
            if cstr(row.user).strip()
        ],
        "demo_mode": False,
        "erpnext_url": frappe.utils.get_url().rstrip("/"),
    }

    if include_secrets:
        payload["api_key"] = cstr(api_key).strip()
        payload["api_secret"] = cstr(api_secret).strip()

    return payload


def mark_device_seen(device_id: str | None = None, name: str | None = None) -> None:
    device = get_device_doc(device_id=device_id, name=name)
    now_iso = now_datetime().isoformat()
    frappe.db.set_value(
        "KoPOS Device",
        device.name,
        {"last_seen_at": now_iso, "last_sync_at": now_iso},
        update_modified=False,
    )
