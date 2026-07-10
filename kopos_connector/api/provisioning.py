# pyright: reportMissingImports=false

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
from kopos_connector.utils.diagnostics import log_sanitized_error


PROVISIONING_CACHE_PREFIX = "kopos:provisioning:"
DEFAULT_TTL_SECONDS = 15 * 60
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 24 * 60 * 60
DEVICE_USER_EMAIL_DOMAIN = "kopos.local"
ATOMIC_CACHE_CONSUME_SCRIPT = """
local value = redis.call('GET', KEYS[1])
if value then
    redis.call('DEL', KEYS[1])
end
return value
"""


def ensure_device_api_credentials(
    device_doc, rotate: bool | int | str = False
) -> dict[str, str]:
    should_rotate = bool(cint(rotate))
    resolved_user = _ensure_device_api_user(device_doc)
    frappe.db.commit()
    api_key_value = cstr(frappe.db.get_value("User", resolved_user, "api_key")).strip()
    api_secret_value = _read_device_api_secret(resolved_user)

    if should_rotate or not api_key_value or not api_secret_value:
        if should_rotate:
            _delete_stale_device_api_secret(resolved_user)
        api_key_value = cstr(frappe.generate_hash(length=15)).strip()
        api_secret_value = cstr(frappe.generate_hash(length=32)).strip()
        frappe.db.set_value("User", resolved_user, "api_key", api_key_value)
        set_encrypted_password("User", resolved_user, api_secret_value, "api_secret")
        frappe.db.commit()

        persisted_secret = _read_device_api_secret(resolved_user)
        if persisted_secret != api_secret_value:
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


def _delete_stale_device_api_secret(user_email: str) -> None:
    try:
        frappe.db.delete(
            "__Auth",
            {
                "doctype": "User",
                "name": user_email,
                "fieldname": "api_secret",
            },
        )
    except Exception as error:
        log_sanitized_error("KoPOS stale device API secret cleanup failed", error)
        return


def create_device_provisioning_qr(
    device: str | None = None,
    erpnext_url: str | None = None,
    expires_in_seconds: int | str | None = None,
    rotate_credentials: bool | int | str = False,
) -> dict[str, Any]:
    require_system_manager()
    device_doc = get_device_doc(name=device)
    if device_doc is None:
        frappe.throw(_("KoPOS Device is required"), frappe.ValidationError)
        raise ValueError("KoPOS Device is required")
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
    if device_doc is None:
        frappe.throw(_("KoPOS Device is required"), frappe.ValidationError)
        raise ValueError("KoPOS Device is required")

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

    _persist_cached_value(
        _cache_key(token),
        json.dumps(cache_payload, sort_keys=True, separators=(",", ":")),
        ttl_seconds,
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

    cached = _consume_cached_value(_cache_key(token_value))
    if not cached:
        frappe.throw(_("Provisioning token is invalid or expired"))

    try:
        payload = json.loads(cached)
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError("Provisioning token cache payload is invalid") from error
    if not isinstance(payload, dict):
        raise RuntimeError("Provisioning token cache payload is invalid")
    return {
        "status": "ok",
        "issued_at": payload.get("issued_at"),
        "expires_at": payload.get("expires_at"),
        "setup": payload.get("setup") or {},
    }


def get_device_config(device_id: str | None = None) -> dict[str, Any]:
    device_doc = get_device_doc(device_id=device_id)
    if device_doc is None:
        frappe.throw(_("KoPOS Device is required"), frappe.ValidationError)
        raise ValueError("KoPOS Device is required")
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


def _persist_cached_value(key: str, value: str, ttl_seconds: int) -> None:
    """Persist raw JSON directly in Redis and verify its value and expiry."""
    cache = frappe.cache()
    make_key = getattr(cache, "make_key", None)
    set_direct = getattr(cache, "set", None)
    get_direct = getattr(cache, "get", None)
    get_ttl = getattr(cache, "ttl", None)
    if (
        not callable(make_key)
        or not callable(set_direct)
        or not callable(get_direct)
        or not callable(get_ttl)
    ):
        raise RuntimeError(
            "Frappe Redis cache does not support confirmed token persistence"
        )

    storage_key = make_key(key)
    encoded_value = value.encode("utf-8")
    try:
        stored = set_direct(storage_key, encoded_value, ex=ttl_seconds)
        persisted_value = get_direct(storage_key)
        persisted_ttl = get_ttl(storage_key)
    except Exception as error:
        _delete_unconfirmed_cached_value(cache, storage_key)
        raise RuntimeError(
            "Provisioning token could not be persisted to Redis"
        ) from error

    persisted_matches = persisted_value in (value, encoded_value)
    if not stored or not persisted_matches or not _has_positive_ttl(persisted_ttl):
        _delete_unconfirmed_cached_value(cache, storage_key)
        raise RuntimeError("Provisioning token persistence could not be confirmed")


def _has_positive_ttl(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _delete_unconfirmed_cached_value(cache: Any, storage_key: Any) -> None:
    delete_direct = getattr(cache, "delete", None)
    if not callable(delete_direct):
        return
    try:
        delete_direct(storage_key)
    except Exception as error:
        log_sanitized_error("KoPOS unconfirmed provisioning token cleanup failed", error)


def _consume_cached_value(key: str) -> str | None:
    """Atomically fetch and delete a raw JSON token payload from Redis."""
    cache = frappe.cache()
    make_key = getattr(cache, "make_key", None)
    eval_script = getattr(cache, "eval", None)
    if not callable(make_key) or not callable(eval_script):
        raise RuntimeError("Frappe Redis cache does not support atomic token consumption")

    storage_key = make_key(key)
    raw_value = eval_script(ATOMIC_CACHE_CONSUME_SCRIPT, 1, storage_key)
    local_cache = getattr(getattr(frappe, "local", None), "cache", None)
    if isinstance(local_cache, dict):
        local_cache.pop(storage_key, None)
    if raw_value is None:
        return None
    if isinstance(raw_value, str):
        return raw_value
    if isinstance(raw_value, (bytes, bytearray, memoryview)):
        try:
            return bytes(raw_value).decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError("Provisioning token cache payload is invalid") from error
    raise RuntimeError("Provisioning token cache payload is invalid")


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
    except Exception as error:
        log_sanitized_error("KoPOS device API secret read failed", error)
        return ""
