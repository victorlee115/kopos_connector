# pyright: reportMissingImports=false

from __future__ import annotations

import hmac
from contextlib import contextmanager
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime

from kopos_connector.utils.pin import is_supported_pin_hash


KOPOS_DEVICE_API_ROLE = "KoPOS Device API"

PRIVILEGED_OPERATION_REASONS = {
    "device_safe_reset",
    "device_config_read",
    "manual_qr_receipt_file",
    "projection_retry",
    "sales_invoice_projection",
    "shift_lifecycle",
    "stock_projection",
}


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

    normalized_roles = {
        cstr(role).strip() for role in roles if cstr(role).strip()
    }
    if resolved_user == "Administrator":
        # Frappe's built-in superuser reports every Role as effective even when
        # it has no Has Role assignment. KoPOS Device API is an identity marker,
        # not a general permission, so treating that synthetic membership as a
        # device session would lock Administrator out of Desk and login routes.
        normalized_roles.discard(KOPOS_DEVICE_API_ROLE)
    return normalized_roles


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

    ensure_unique_device_api_user(
        resolved_user,
        current_device_name=cstr(getattr(device_doc, "name", None)).strip() or None,
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
    if not cint(getattr(device_doc, "enabled", 1)):
        frappe.throw(
            _("KoPOS Device {0} is disabled").format(
                cstr(getattr(device_doc, "device_id", None)).strip()
            ),
            frappe.ValidationError,
        )
    return device_doc


def lock_device_for_operational_mutation(
    device_id: str | None = None,
    *,
    name: str | None = None,
) -> Any:
    """Serialize a device mutation against credential rotation and safe reset.

    The Device row is the first business lock for every tablet-originated mutation.
    Re-reading its authority after that lock closes the race where an API request is
    authenticated with credentials that a concurrent safe reset subsequently rotates.
    The lock is intentionally held until the caller's transaction commits or rolls back.
    """
    requested_device_id = cstr(device_id).strip()
    requested_name = cstr(name).strip()
    if not requested_device_id and not requested_name:
        authenticated_device = get_authenticated_device_doc()
        requested_device_id = cstr(
            getattr(authenticated_device, "device_id", None)
        ).strip()
        requested_name = cstr(getattr(authenticated_device, "name", None)).strip()

    if requested_device_id:
        lookup_value = requested_device_id
        lookup_query = """
            SELECT name, device_id, api_user, enabled, config_version
            FROM `tabKoPOS Device`
            WHERE device_id = %s
            LIMIT 1
            FOR UPDATE
        """
    elif requested_name:
        lookup_value = requested_name
        lookup_query = """
            SELECT name, device_id, api_user, enabled, config_version
            FROM `tabKoPOS Device`
            WHERE name = %s
            LIMIT 1
            FOR UPDATE
        """
    else:
        frappe.throw(_("KoPOS Device is required"), frappe.ValidationError)
        raise AssertionError("frappe.throw must raise")

    locked_rows = frappe.db.sql(
        lookup_query,
        (lookup_value,),
        as_dict=True,
    )
    if not locked_rows:
        frappe.throw(
            _("KoPOS Device {0} was not found").format(lookup_value),
            frappe.ValidationError,
        )

    locked_row = locked_rows[0]
    locked_name = cstr(_row_value(locked_row, "name")).strip()
    locked_device_id = cstr(_row_value(locked_row, "device_id")).strip()
    if requested_name and not hmac.compare_digest(locked_name, requested_name):
        frappe.throw(_("KoPOS Device binding changed"), frappe.ValidationError)
    if requested_device_id and not hmac.compare_digest(
        locked_device_id, requested_device_id
    ):
        frappe.throw(_("KoPOS Device binding changed"), frappe.ValidationError)

    device_doc = frappe.get_doc("KoPOS Device", locked_name)
    for fieldname in ("name", "device_id", "api_user", "enabled", "config_version"):
        setattr(device_doc, fieldname, _row_value(locked_row, fieldname))

    session_user = cstr(getattr(frappe.session, "user", None)).strip()
    roles = get_session_roles(session_user)
    if "System Manager" not in roles:
        require_device_api_access(device_doc)

    if not cint(_row_value(locked_row, "enabled")):
        frappe.throw(
            _("KoPOS Device {0} is disabled").format(locked_device_id),
            frappe.ValidationError,
        )

    active_reset_rows = frappe.db.sql(
        """
        SELECT name, status
        FROM `tabKoPOS Device Safe Reset`
        WHERE device_id = %s
          AND (
            status IS NULL
            OR status NOT IN ('completed', 'cancelled', 'expired')
          )
        ORDER BY creation DESC, name DESC
        LIMIT 1
        FOR UPDATE
        """,
        (locked_device_id,),
        as_dict=True,
    )
    if active_reset_rows:
        frappe.throw(
            _(
                "KoPOS Device {0} has an active or unresolved safe reset; complete "
                "or cancel it before processing operational mutations"
            ).format(locked_device_id),
            frappe.ValidationError,
        )

    if "System Manager" not in roles:
        presented_api_key = _presented_device_api_key()
        if not presented_api_key:
            frappe.throw(
                _("KoPOS operational mutations require token authentication"),
                frappe.ValidationError,
            )

        api_user = cstr(_row_value(locked_row, "api_user")).strip()
        user_rows = frappe.db.sql(
            """
            SELECT name, api_key, enabled
            FROM `tabUser`
            WHERE name = %s
            LIMIT 1
            FOR UPDATE
            """,
            (api_user,),
            as_dict=True,
        )
        if not user_rows or not cint(_row_value(user_rows[0], "enabled")):
            frappe.throw(
                _("KoPOS Device API user is disabled or missing"),
                frappe.ValidationError,
            )
        current_api_key = cstr(_row_value(user_rows[0], "api_key")).strip()
        if not current_api_key or not hmac.compare_digest(
            current_api_key, presented_api_key
        ):
            frappe.throw(
                _(
                    "KoPOS Device credentials changed while this request was in "
                    "flight; authenticate again with the current credentials"
                ),
                frappe.ValidationError,
            )

    flags = getattr(frappe, "flags", None)
    if flags is not None:
        setattr(flags, "kopos_device", device_doc)
    return device_doc


def _presented_device_api_key() -> str:
    request = getattr(frappe, "request", None) or getattr(
        getattr(frappe, "local", None), "request", None
    )
    headers = getattr(request, "headers", None)
    get_header = getattr(headers, "get", None)
    authorization = cstr(
        get_header("Authorization") if callable(get_header) else None
    ).strip()
    scheme, separator, credentials = authorization.partition(" ")
    if not separator or scheme.lower() != "token":
        return ""
    api_key, credential_separator, api_secret = credentials.strip().partition(":")
    if not credential_separator or not api_key.strip() or not api_secret.strip():
        return ""
    return api_key.strip()


def _row_value(row: Any, fieldname: str) -> Any:
    if isinstance(row, dict):
        return row.get(fieldname)
    return getattr(row, fieldname, None)


def require_device_operational_scope(
    device_id: str | None,
    *,
    company: str | None = None,
    warehouse: str | None = None,
    currency: str | None = None,
) -> tuple[Any, Any]:
    """Authorize a device and bind operational values to its POS Profile."""
    device_doc = require_device_context(device_id=device_id)
    if device_doc is None:
        frappe.throw(_("KoPOS Device is required"), frappe.ValidationError)

    resolved_device_id = cstr(getattr(device_doc, "device_id", None)).strip()
    if not cint(getattr(device_doc, "enabled", 0)):
        frappe.throw(
            _("KoPOS Device {0} is disabled").format(resolved_device_id),
            frappe.ValidationError,
        )

    profile_name = cstr(getattr(device_doc, "pos_profile", None)).strip()
    if not profile_name:
        frappe.throw(
            _("KoPOS Device {0} has no POS Profile configured").format(
                resolved_device_id
            ),
            frappe.ValidationError,
        )
    profile_doc = frappe.get_cached_doc("POS Profile", profile_name)
    profile_company = cstr(getattr(profile_doc, "company", None)).strip()
    profile_warehouse = cstr(getattr(profile_doc, "warehouse", None)).strip()
    profile_currency = cstr(getattr(profile_doc, "currency", None)).strip()
    if not profile_currency and profile_company:
        profile_currency = cstr(
            frappe.db.get_value("Company", profile_company, "default_currency")
        ).strip()

    submitted_company = cstr(company).strip()
    if submitted_company and submitted_company != profile_company:
        frappe.throw(
            _("Company {0} is outside KoPOS Device {1} scope").format(
                submitted_company, resolved_device_id
            ),
            frappe.ValidationError,
        )
    submitted_warehouse = cstr(warehouse).strip()
    if submitted_warehouse and submitted_warehouse != profile_warehouse:
        frappe.throw(
            _("Warehouse {0} is outside KoPOS Device {1} scope").format(
                submitted_warehouse, resolved_device_id
            ),
            frappe.ValidationError,
        )
    submitted_currency = cstr(currency).strip()
    if submitted_currency and profile_currency and submitted_currency != profile_currency:
        frappe.throw(
            _("Currency {0} is outside KoPOS Device {1} scope").format(
                submitted_currency, resolved_device_id
            ),
            frappe.ValidationError,
        )

    return device_doc, profile_doc


@contextmanager
def elevate_device_api_user():
    """Temporarily elevate device API requests for server-side ERP document work."""
    session_user = cstr(getattr(frappe.session, "user", None)).strip()
    if not session_user or session_user == "Guest":
        yield
        return

    roles = get_session_roles(session_user)
    if "System Manager" in roles or KOPOS_DEVICE_API_ROLE not in roles:
        yield
        return

    set_user = getattr(frappe, "set_user", None)
    if not callable(set_user):
        yield
        return

    try:
        set_user("Administrator")
        yield
    finally:
        set_user(session_user)


@contextmanager
def privileged_device_api_operation(reason: str):
    """Named boundary for server-side writes done on behalf of a device API user."""
    if reason not in PRIVILEGED_OPERATION_REASONS:
        frappe.throw(
            _("Unknown KoPOS privileged operation reason: {0}").format(reason),
            frappe.ValidationError,
        )
    with elevate_device_api_user():
        yield


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


def get_authenticated_device_doc():
    cached_device = getattr(getattr(frappe, "flags", None), "kopos_device", None)
    cached_user = cstr(getattr(cached_device, "api_user", None)).strip()
    session_user = cstr(getattr(frappe.session, "user", None)).strip()
    if cached_device and cached_user and cached_user == session_user:
        return cached_device

    if not session_user or session_user == "Guest":
        frappe.throw(_("Authentication required"), frappe.ValidationError)

    roles = get_session_roles(session_user)
    if "System Manager" in roles:
        frappe.throw(_("System Manager must provide device_id"), frappe.ValidationError)

    if KOPOS_DEVICE_API_ROLE not in roles:
        frappe.throw(
            _("User {0} is not allowed to access KoPOS device APIs").format(
                session_user
            ),
            frappe.ValidationError,
        )

    device_rows = frappe.get_all(
        "KoPOS Device",
        filters={"api_user": session_user},
        fields=["name"],
        limit_page_length=2,
    )
    if not device_rows:
        frappe.throw(
            _("No KoPOS Device found for user {0}").format(session_user),
            frappe.ValidationError,
        )

    if len(device_rows) > 1:
        frappe.throw(
            _("User {0} is assigned to multiple KoPOS Devices").format(session_user),
            frappe.ValidationError,
        )

    device_doc = get_device_doc(name=cstr(device_rows[0].get("name")).strip())
    setattr(frappe.flags, "kopos_device", device_doc)
    return device_doc


def ensure_unique_device_api_user(
    api_user: str | None, *, current_device_name: str | None = None
) -> None:
    resolved_user = cstr(api_user).strip()
    if not resolved_user:
        return

    filters: dict[str, Any] = {"api_user": resolved_user}
    current_name = cstr(current_device_name).strip()
    if current_name:
        filters["name"] = ["!=", current_name]

    conflicting_devices = frappe.get_all(
        "KoPOS Device",
        filters=filters,
        fields=["name", "device_id"],
        limit_page_length=1,
    )
    if not conflicting_devices:
        return

    conflict_name = (
        cstr(conflicting_devices[0].get("device_id")).strip()
        or cstr(conflicting_devices[0].get("name")).strip()
    )
    frappe.throw(
        _("API user {0} is already assigned to KoPOS Device {1}").format(
            resolved_user, conflict_name
        ),
        frappe.ValidationError,
    )


def get_device_pos_profile_doc(device_id: str | None = None, name: str | None = None):
    device = get_device_doc(device_id=device_id, name=name)
    pos_profile = cstr(getattr(device, "pos_profile", None)).strip()
    if not pos_profile:
        frappe.throw(_("KoPOS Device has no POS Profile configured"), frappe.ValidationError)
    return frappe.get_cached_doc("POS Profile", pos_profile)


def serialize_device_config(
    device_doc,
    *,
    include_secrets: bool = False,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> dict[str, Any]:
    from kopos_connector.api.catalog import get_tax_rate_value

    if not cint(getattr(device_doc, "enabled", 0)):
        frappe.throw(
            _("KoPOS Device {0} is disabled").format(
                cstr(getattr(device_doc, "device_id", None)).strip()
            ),
            frappe.ValidationError,
        )

    active_device_users = [
        row
        for row in (device_doc.device_users or [])
        if cstr(getattr(row, "user", None)).strip()
        and cint(getattr(row, "active", 0))
    ]
    if not active_device_users:
        frappe.throw(
            _("KoPOS Device {0} must have at least one active device user").format(
                cstr(getattr(device_doc, "device_id", None)).strip()
            ),
            frappe.ValidationError,
        )

    active_users_with_pin_hashes = []
    for row in active_device_users:
        user_id = cstr(getattr(row, "user", None)).strip()
        pin_hash = cstr(getattr(row, "pin_hash", None)).strip()
        if not is_supported_pin_hash(pin_hash):
            frappe.throw(
                _(
                    "Device user {0} has an unsupported PIN verifier; enter a new "
                    "4-digit PIN and save the device before provisioning or refresh"
                ).format(user_id),
                frappe.ValidationError,
            )
        active_users_with_pin_hashes.append((row, pin_hash))

    profile_doc = frappe.get_doc("POS Profile", device_doc.pos_profile)
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
        "static_qr_payload": cstr(
            getattr(device_doc, "static_qr_payload", None)
        ).strip()
        or None,
        "enabled": bool(cint(device_doc.enabled)),
        "managed_by_erp": True,
        "config_version": cint(device_doc.config_version or 1),
        "pos_profile": cstr(device_doc.pos_profile).strip(),
        "company": company,
        "warehouse": warehouse,
        "currency": currency,
        "tax_rate": get_tax_rate_value(device_id=cstr(device_doc.device_id).strip()),
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
                "pin_hash": pin_hash,
                "active": bool(cint(row.active)),
                "can_manager_override": bool(cint(row.can_manager_override)),
                "can_refund": bool(cint(row.can_refund)),
                "can_void": bool(cint(row.can_void)),
                "can_open_shift": bool(cint(row.can_open_shift)),
                "can_close_shift": bool(cint(row.can_close_shift)),
                "default_cashier": bool(cint(row.default_cashier)),
            }
            for row, pin_hash in active_users_with_pin_hashes
        ],
        "demo_mode": False,
        "erpnext_url": frappe.utils.get_url().rstrip("/"),
    }

    if include_secrets:
        payload["api_key"] = cstr(api_key).strip()
        payload["api_secret"] = cstr(api_secret).strip()

    return payload


def mark_device_seen(device_id: str | None = None, name: str | None = None) -> None:
    device = lock_device_for_operational_mutation(device_id=device_id, name=name)
    device_name = cstr(getattr(device, "name", None)).strip()
    if not device_name:
        frappe.throw(_("KoPOS Device is required"), frappe.ValidationError)
    now_iso = now_datetime().isoformat()
    frappe.db.set_value(
        "KoPOS Device",
        device_name,
        {"last_seen_at": now_iso, "last_sync_at": now_iso},
        update_modified=False,
    )
