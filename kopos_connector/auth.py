from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr

from kopos_connector.api.devices import KOPOS_DEVICE_API_ROLE, get_session_roles


def enforce_device_api_restrictions() -> None:
    user = cstr(getattr(frappe.session, "user", None)).strip()
    if not user or user == "Guest":
        return

    roles = get_session_roles(user=user)
    if KOPOS_DEVICE_API_ROLE not in roles:
        return

    request = getattr(frappe.local, "request", None)
    path = cstr(getattr(request, "path", None)).strip()
    if path.startswith("/api/"):
        return

    frappe.throw(
        _("KoPOS device API users may only access API endpoints"),
        frappe.ValidationError,
    )
