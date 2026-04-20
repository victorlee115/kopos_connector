from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import frappe
from frappe import _
from frappe.twofactor import get_qr_svg_code
from frappe.utils.password import get_decrypted_password, set_encrypted_password
from frappe.utils import cint, cstr, now_datetime

from kopos_connector.api.devices import (
    KOPOS_DEVICE_API_ROLE,
    ensure_unique_device_api_user,
    get_device_doc,
    require_device_api_access,
    require_system_manager,
    serialize_device_config,
)


PROVISIONING_CACHE_PREFIX = "kopos:provisioning:"
DEFAULT_TTL_SECONDS = 15 * 60
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 24 * 60 * 60
DEVICE_USER_EMAIL_DOMAIN = "kopos.local"


def ensure_device_api_credentials(
    device_doc, rotate: bool | int | str = False
) -> dict[str, str]:
    should_rotate = bool(cint(rotate))
    resolved_user = _ensure_device_api_user(device_doc)
    frappe.db.commit()
    api_key_value = cstr(frappe.db.get_value("User", resolved_user, "api_key")).strip()
    api_secret_value = _read_device_api_secret(resolved_user)

    if should_rotate or not api_key_value:
        api_key_value = cstr(frappe.generate_hash(length=15)).strip()
        frappe.db.set_value("User", resolved_user, "api_key", api_key_value)
        frappe.db.commit()

    if should_rotate or not api_secret_value:
        next_secret = cstr(frappe.generate_hash(length=32)).strip()
        set_encrypted_password("User", resolved_user, next_secret, "api_secret")
        frappe.db.commit()
        api_secret_value = _read_device_api_secret(resolved_user)
        if api_secret_value != next_secret:
            frappe.throw(
                _("Failed to persist a usable API secret for device user {0}").format(
                    resolved_user
                ),
                frappe.ValidationError,
            )

    if not api_key_value or not api_secret_value:
        frappe.throw(_("Failed to initialize KoPOS device API credentials"))

    return {
        "user": resolved_user,
        "api_key": api_key_value,
        "api_secret": api_secret_value,
    }


def create_device_provisioning_qr(
    device: str | None = None,
    erpnext_url: str | None = None,
    expires_in_seconds: int | str | None = None,
    rotate_credentials: bool | int | str = False,
) -> dict[str, Any]:
    require_system_manager()
    device_doc = get_device_doc(name=device)
    credentials = ensure_device_api_credentials(device_doc, rotate=rotate_credentials)
    payload = create_pos_provisioning(
        device=device_doc.name,
        erpnext_url=erpnext_url,
        api_key=credentials["api_key"],
        api_secret=credentials["api_secret"],
        expires_in_seconds=expires_in_seconds,
    )
    payload.setdefault("setup_preview", {})["provisioning_user"] = credentials["user"]
    return payload


def create_pos_provisioning(
    device: str | None = None,
    pos_profile: str | None = None,
    erpnext_url: str | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
    warehouse: str | None = None,
    company: str | None = None,
    currency: str | None = None,
    device_name: str | None = None,
    device_prefix: str | None = None,
    expires_in_seconds: int | str | None = None,
) -> dict[str, Any]:
    require_system_manager()

    api_key_value = cstr(api_key).strip()
    api_secret_value = cstr(api_secret).strip()
    if not api_key_value or not api_secret_value:
        frappe.throw(_("API key and API secret are required"))

    device_doc = None
    if cstr(device).strip():
        device_doc = get_device_doc(name=device)
    elif cstr(pos_profile).strip():
        profile_name = cstr(pos_profile).strip()
        device_name = frappe.db.get_value(
            "KoPOS Device", {"pos_profile": profile_name, "enabled": 1}, "name"
        )
        if device_name:
            device_doc = get_device_doc(name=device_name)
        else:
            frappe.throw(_("KoPOS Device is required"))
    else:
        frappe.throw(_("KoPOS Device is required"))

    profile_doc = frappe.get_cached_doc("POS Profile", device_doc.pos_profile)
    resolved_company = (
        cstr(company).strip() or cstr(getattr(profile_doc, "company", None)).strip()
    )
    resolved_warehouse = (
        cstr(warehouse).strip() or cstr(getattr(profile_doc, "warehouse", None)).strip()
    )
    resolved_currency = (
        cstr(currency).strip() or cstr(getattr(profile_doc, "currency", None)).strip()
    )
    if not resolved_currency and resolved_company:
        resolved_currency = cstr(
            frappe.db.get_value("Company", resolved_company, "default_currency")
        ).strip()

    ttl_seconds = max(
        MIN_TTL_SECONDS,
        min(MAX_TTL_SECONDS, cint(expires_in_seconds or DEFAULT_TTL_SECONDS)),
    )
    issued_at = now_datetime()
    expires_at = (issued_at + timedelta(seconds=ttl_seconds)).isoformat()
    token = frappe.generate_hash(length=32)
    base_url = cstr(erpnext_url).strip().rstrip("/") or frappe.utils.get_url().rstrip(
        "/"
    )

    setup_payload = serialize_device_config(
        device_doc,
        include_secrets=True,
        api_key=api_key_value,
        api_secret=api_secret_value,
    )
    setup_payload["erpnext_url"] = base_url
    if cstr(device_name).strip():
        setup_payload["device_name"] = cstr(device_name).strip()
    if cstr(device_prefix).strip():
        setup_payload["device_prefix"] = cstr(device_prefix).strip().upper()
    if resolved_company:
        setup_payload["company"] = resolved_company
    if resolved_warehouse:
        setup_payload["warehouse"] = resolved_warehouse
    if resolved_currency:
        setup_payload["currency"] = resolved_currency

    cache_payload = {
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at,
        "setup": setup_payload,
    }

    frappe.cache().set_value(
        _cache_key(token),
        json.dumps(cache_payload, sort_keys=True),
        expires_in_sec=ttl_seconds,
    )

    return {
        "status": "ok",
        "token": token,
        "issued_at": cache_payload["issued_at"],
        "expires_at": expires_at,
        "provisioning_url": f"kopos://provision?base_url={quote(base_url, safe='')}&token={quote(token, safe='')}",
        "provisioning_link": f"kopos://provision?base_url={quote(base_url, safe='')}&token={quote(token, safe='')}",
        "provisioning_qr_svg": get_qr_svg_code(
            f"kopos://provision?base_url={quote(base_url, safe='')}&token={quote(token, safe='')}"
        ).decode(),
        "setup_preview": {
            "device": device_doc.name,
            "device_id": cstr(device_doc.device_id).strip(),
            "erpnext_url": base_url,
            "pos_profile": cstr(device_doc.pos_profile).strip(),
            "warehouse": resolved_warehouse or None,
            "company": resolved_company or None,
            "currency": resolved_currency or None,
            "device_name": setup_payload.get("device_name"),
            "device_prefix": setup_payload.get("device_prefix"),
        },
    }


def redeem_pos_provisioning(token: str | None = None) -> dict[str, Any]:
    token_value = cstr(token).strip()
    if not token_value:
        frappe.throw(_("Provisioning token is required"))

    cached = frappe.cache().get_value(_cache_key(token_value))
    if not cached:
        frappe.throw(_("Provisioning token is invalid or expired"))

    frappe.cache().delete_value(_cache_key(token_value))
    payload = json.loads(cached)
    return {
        "status": "ok",
        "issued_at": payload.get("issued_at"),
        "expires_at": payload.get("expires_at"),
        "setup": payload.get("setup") or {},
    }


def get_device_config(device_id: str | None = None) -> dict[str, Any]:
    device_doc = get_device_doc(device_id=device_id)
    require_device_api_access(device_doc)
    if not cint(device_doc.enabled):
        frappe.throw(
            _("KoPOS Device {0} is disabled").format(device_doc.device_id),
            frappe.ValidationError,
        )

    setup = serialize_device_config(device_doc)
    return {
        "status": "ok",
        "device_id": setup["device_id"],
        "config_version": setup["config_version"],
        "setup": setup,
    }


def _cache_key(token: str) -> str:
    return f"{PROVISIONING_CACHE_PREFIX}{token}"


def _device_api_user_email(device_doc) -> str:
    slug = _slugify_device_id(cstr(getattr(device_doc, "device_id", None)).strip())
    return f"kopos.device.{slug}@{DEVICE_USER_EMAIL_DOMAIN}"


def _slugify_device_id(value: str) -> str:
    cleaned = [char.lower() if char.isalnum() else "." for char in value]
    slug = "".join(cleaned).strip(".")
    while ".." in slug:
        slug = slug.replace("..", ".")
    return slug or "unknown"


def _ensure_kopos_device_api_role() -> None:
    if frappe.db.exists("Role", KOPOS_DEVICE_API_ROLE):
        return

    frappe.get_doc({"doctype": "Role", "role_name": KOPOS_DEVICE_API_ROLE}).insert(
        ignore_permissions=True
    )


def _ensure_device_api_user(device_doc) -> str:
    _ensure_kopos_device_api_role()
    user_email = cstr(
        getattr(device_doc, "api_user", None)
    ).strip() or _device_api_user_email(device_doc)
    ensure_unique_device_api_user(
        user_email,
        current_device_name=cstr(getattr(device_doc, "name", None)).strip() or None,
    )
    display_name = (
        cstr(getattr(device_doc, "device_name", None)).strip()
        or cstr(getattr(device_doc, "device_id", None)).strip()
    )

    if not frappe.db.exists("User", user_email):
        user_doc = frappe.get_doc(
            {
                "doctype": "User",
                "email": user_email,
                "first_name": display_name,
                "enabled": 1,
                "user_type": "System User",
                "send_welcome_email": 0,
                "new_password": frappe.generate_hash(length=32),
            }
        )
        user_doc.append("roles", {"role": KOPOS_DEVICE_API_ROLE})
        user_doc.insert(ignore_permissions=True)
    else:
        user_doc = frappe.get_doc("User", user_email)
        user_doc.enabled = 1
        user_doc.first_name = display_name or user_doc.first_name
        user_doc.user_type = "System User"
        user_doc.send_welcome_email = 0
        user_doc.set(
            "roles",
            [{"doctype": "Has Role", "role": KOPOS_DEVICE_API_ROLE}],
        )
        user_doc.save(ignore_permissions=True)

    if cstr(getattr(device_doc, "api_user", None)).strip() != user_email:
        frappe.db.set_value(
            "KoPOS Device",
            device_doc.name,
            "api_user",
            user_email,
            update_modified=False,
        )
        setattr(device_doc, "api_user", user_email)

    return user_email


def _read_device_api_secret(user_email: str) -> str:
    try:
        return cstr(
            get_decrypted_password(
                "User",
                user_email,
                "api_secret",
                raise_exception=False,
            )
            or ""
        ).strip()
    except Exception:
        return ""
