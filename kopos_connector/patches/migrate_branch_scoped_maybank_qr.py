from __future__ import annotations

import hashlib
from typing import Any

import frappe
from frappe.utils import cstr, now_datetime


LEGACY_DOCTYPE = "Maybank Settings"
ACCOUNT_DOCTYPE = "Maybank QRPayBiz Account"


def execute() -> None:
    """Migrate legacy QR settings without inventing branch configuration."""

    reload_doc = getattr(frappe, "reload_doc", None)
    if callable(reload_doc):
        reload_doc("kopos", "doctype", "maybank_qrpaybiz_account")
        reload_doc("kopos", "doctype", "maybank_qr_transaction")
    if not frappe.db.table_exists(LEGACY_DOCTYPE) or not frappe.db.table_exists(ACCOUNT_DOCTYPE):
        return

    legacy = frappe.get_single(LEGACY_DOCTYPE)
    account_name = _ensure_account(legacy)
    if not account_name:
        return

    profiles = _profiles_for_devices()
    _migrate_profile_assignments(profiles, account_name, cstr(getattr(legacy, "outlet_id", None)).strip())
    _migrate_static_qr(profiles)
    _backfill_transaction_snapshots(account_name, profiles)


def _ensure_account(legacy: Any) -> str | None:
    username = cstr(getattr(legacy, "username", None)).strip()
    if not username:
        return None
    base_url = cstr(getattr(legacy, "base_url", None)).strip()
    user_type = cstr(getattr(legacy, "user_type", None)).strip().lower() or "merchant"
    existing = frappe.db.get_value(
        ACCOUNT_DOCTYPE,
        {"username": username, "base_url": base_url, "user_type": user_type},
        "name",
    )
    if existing:
        return cstr(existing).strip()
    account = frappe.get_doc(
        {
            "doctype": ACCOUNT_DOCTYPE,
            "display_name": f"Migrated Maybank {username}",
            "enabled": int(getattr(legacy, "enabled", 0) or 0),
            "environment": "production",
            "base_url": base_url,
            "username": username,
            "user_type": user_type,
            "provider_device_id": cstr(getattr(legacy, "provider_device_id", None)).strip()
            or hashlib.sha256(f"maybank|{username}|{base_url}|{user_type}".encode()).hexdigest()[:32],
            "provider_device_name": cstr(getattr(legacy, "provider_device_name", None)).strip(),
            "provider_device_os": cstr(getattr(legacy, "provider_device_os", None)).strip(),
        }
    )
    # Password APIs keep plaintext out of logs and the database document dict.
    encrypted_pin = legacy.get_password("encrypted_pin") or ""
    account.insert(ignore_permissions=True)
    if encrypted_pin:
        account.set_password("encrypted_pin", encrypted_pin)
        account.save(ignore_permissions=True)
    return cstr(account.name).strip()


def _profiles_for_devices() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for row in frappe.get_all(
        "KoPOS Device",
        fields=["pos_profile", "device_id"],
        filters={"pos_profile": ["is", "set"]},
        limit_page_length=0,
    ):
        profile = cstr(row.get("pos_profile")).strip()
        device_id = cstr(row.get("device_id")).strip()
        if profile and device_id:
            result.setdefault(profile, []).append(device_id)
    return result


def _migrate_profile_assignments(profiles: dict[str, list[str]], account_name: str, outlet: str) -> None:
    if not outlet:
        return
    for profile_name in profiles:
        profile = frappe.get_doc("POS Profile", profile_name)
        if cstr(getattr(profile, "custom_kopos_maybank_qrpaybiz_account", None)).strip():
            continue
        # One legacy outlet can be assigned only when the site has one
        # unambiguous profile. Multiple profiles require guided assignment.
        if len(profiles) != 1:
            profile.custom_kopos_qr_configuration_status = "Needs Review"
            profile.save(ignore_permissions=True)
            continue
        profile.custom_kopos_maybank_qrpaybiz_account = account_name
        profile.custom_kopos_maybank_outlet_id = outlet
        profile.custom_kopos_automatic_qr_enabled = int(getattr(profile, "custom_kopos_automatic_qr_enabled", 0) or 0)
        profile.save(ignore_permissions=True)


def _migrate_static_qr(profiles: dict[str, list[str]]) -> None:
    for profile_name, device_ids in profiles.items():
        profile = frappe.get_doc("POS Profile", profile_name)
        if cstr(getattr(profile, "custom_kopos_static_qr_payload", None)).strip():
            continue
        devices = [frappe.get_doc("KoPOS Device", device_id) for device_id in device_ids]
        payloads = {
            cstr(getattr(device, "static_qr_payload", None)).strip()
            for device in devices
            if cstr(getattr(device, "static_qr_payload", None)).strip()
        }
        if not payloads:
            continue
        if len(payloads) != 1:
            profile.custom_kopos_qr_configuration_status = "Needs Review"
            profile.save(ignore_permissions=True)
            continue
        source = next(device for device in devices if cstr(getattr(device, "static_qr_payload", None)).strip())
        profile.custom_kopos_static_qr_enabled = 1
        profile.custom_kopos_static_qr_payload = next(iter(payloads))
        for old, new in (
            ("static_qr_payload_sha256", "custom_kopos_static_qr_payload_sha256"),
            ("static_qr_merchant_id", "custom_kopos_static_qr_merchant_id"),
            ("static_qr_acquirer_id", "custom_kopos_static_qr_acquirer_id"),
            ("static_qr_merchant_name", "custom_kopos_static_qr_merchant_name"),
            ("static_qr_version", "custom_kopos_static_qr_version"),
            ("static_qr_commissioned_at", "custom_kopos_static_qr_commissioned_at"),
        ):
            setattr(profile, new, getattr(source, old, None))
        profile.save(ignore_permissions=True)


def _backfill_transaction_snapshots(account_name: str, profiles: dict[str, list[str]]) -> None:
    if not frappe.db.table_exists("Maybank QR Transaction"):
        return
    profile_rows = {profile: frappe.get_doc("POS Profile", profile) for profile in profiles}
    for transaction in frappe.get_all(
        "Maybank QR Transaction",
        fields=["name", "device_id", "outlet_id", "maybank_qrpaybiz_account"],
        filters={"maybank_qrpaybiz_account": ["is", "not set"]},
        limit_page_length=0,
    ):
        profile_name = next(
            (name for name, devices in profiles.items() if cstr(transaction.get("device_id")).strip() in devices),
            None,
        )
        profile = profile_rows.get(profile_name) if profile_name else None
        if profile is None or cstr(getattr(profile, "custom_kopos_maybank_outlet_id", None)).strip() != cstr(transaction.get("outlet_id")).strip():
            continue
        frappe.db.set_value(
            "Maybank QR Transaction",
            transaction.get("name"),
            {
                "pos_profile": profile.name,
                "maybank_qrpaybiz_account": account_name,
                "suspense_account": getattr(profile, "custom_kopos_manual_qr_suspense_account", None),
                "clearing_account": getattr(profile, "custom_kopos_qr_clearing_account", None),
                "settlement_bank_account": getattr(profile, "custom_kopos_qr_settlement_bank_account", None),
            },
            update_modified=False,
        )
