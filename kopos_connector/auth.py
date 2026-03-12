from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr

from kopos_connector.api.devices import KOPOS_DEVICE_API_ROLE, get_session_roles


ALLOWED_DEVICE_API_PREFIXES = ("/api/method/kopos_connector.api.",)


def enforce_device_api_restrictions() -> None:
    user = cstr(getattr(frappe.session, "user", None)).strip()
    if not user or user == "Guest":
        return

    roles = get_session_roles(user=user)
    if KOPOS_DEVICE_API_ROLE not in roles:
        return

    request = getattr(frappe.local, "request", None)
    path = cstr(getattr(request, "path", None)).strip()
    if any(path.startswith(prefix) for prefix in ALLOWED_DEVICE_API_PREFIXES):
        return

    frappe.throw(
        _("KoPOS device API users may only access KoPOS API endpoints"),
        frappe.ValidationError,
    )
