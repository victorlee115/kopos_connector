from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cint, cstr, now_datetime

from kopos_connector.api.maybank_qr_readiness import get_payment_readiness_payload
from kopos_connector.services.maybank.branch_config import (
    PROFILE_ACCOUNT_FIELD,
    PROFILE_AUTOMATIC_ENABLED_FIELD,
    PROFILE_CLEARING_FIELD,
    PROFILE_OUTLET_FIELD,
    PROFILE_SETTLEMENT_BANK_FIELD,
    PROFILE_STATIC_ENABLED_FIELD,
    PROFILE_STATIC_PAYLOAD_FIELD,
    PROFILE_SUSPENSE_FIELD,
)
from kopos_connector.services.static_qr_commissioning import (
    commissioned_profile_static_qr_config,
    inspect_paynet_static_qr,
)
from kopos_connector.utils.diagnostics import make_savepoint, rollback_to_savepoint


PROFILE_CONFIG_FIELDS = (
    PROFILE_STATIC_ENABLED_FIELD,
    PROFILE_STATIC_PAYLOAD_FIELD,
    PROFILE_SUSPENSE_FIELD,
    PROFILE_AUTOMATIC_ENABLED_FIELD,
    PROFILE_ACCOUNT_FIELD,
    PROFILE_OUTLET_FIELD,
    PROFILE_CLEARING_FIELD,
    PROFILE_SETTLEMENT_BANK_FIELD,
)
STATIC_METADATA_FIELDS = ("payload_sha256", "merchant_id", "acquirer_id", "merchant_name", "version")


def _require_setup_role() -> None:
    roles = set(frappe.get_roles())
    if not roles.intersection({"System Manager", "Accounts Manager"}):
        frappe.throw("System Manager or Accounts Manager permission is required", frappe.PermissionError)


def get_qr_setup_preview(profile_name: str, config: dict[str, Any] | str | None = None) -> dict[str, Any]:
    _require_setup_role()
    profile = frappe.get_doc("POS Profile", profile_name)
    profile.check_permission("read")
    merchant = None
    requires_confirmation = False
    if config is not None:
        profile.check_permission("write")
        config = _parse_config(config)
        previous_payload = cstr(getattr(profile, PROFILE_STATIC_PAYLOAD_FIELD, None))
        _apply_profile_config(profile, config)
        merchant, requires_confirmation = _prepare_static_qr(profile, previous_payload, config, preview=True)
    return _build_preview(profile, merchant, requires_confirmation=requires_confirmation)


def _build_preview(profile: Any, merchant: dict[str, str] | None = None, *, requires_confirmation: bool = False) -> dict[str, Any]:
    mode_mapping = _mode_of_payment_mapping(profile)
    readiness = get_payment_readiness_payload(_empty_device(profile), profile)
    if merchant is None and cint(getattr(profile, PROFILE_STATIC_ENABLED_FIELD, 0)) and readiness["static_qr"]["ready"]:
        merchant = {field: cstr(getattr(profile, "custom_kopos_static_qr_" + field, None)) for field in STATIC_METADATA_FIELDS}
    return {
        "profile": profile.name,
        "profile_modified": cstr(profile.modified),
        "company": cstr(getattr(profile, "company", None)).strip(),
        "currency": cstr(getattr(profile, "currency", None)).strip().upper(),
        "static_qr": readiness["static_qr"],
        "automatic_qr": readiness["automatic_qr"],
        "cash": readiness["cash"],
        "mode_of_payment": mode_mapping,
        "changes_are_company_wide": False,
        "static_qr_merchant": ({key: merchant[key] for key in ("merchant_name", "merchant_id", "acquirer_id", "payload_sha256")} if merchant else None),
        "requires_merchant_confirmation": requires_confirmation,
        "checked_at": now_datetime().isoformat(),
    }


def apply_qr_configuration(profile_name: str, config: dict[str, Any] | str) -> dict[str, Any]:
    """Save a reviewed profile through native permissions and optimistic locking.

    Frappe owns the request commit. Response construction stays inside the same
    savepoint so an unsuccessful response cannot leave a silently saved setup.
    """
    _require_setup_role()
    config = _parse_config(config)
    profile = frappe.get_doc("POS Profile", profile_name)
    profile.check_permission("write")
    expected_modified = config.get("expected_modified")
    if not isinstance(expected_modified, str) or not expected_modified.strip():
        frappe.throw("Refresh the QR setup preview before saving", frappe.ValidationError)
    previous_payload = cstr(getattr(profile, PROFILE_STATIC_PAYLOAD_FIELD, None))
    savepoint = make_savepoint("kopos_qr_setup")
    try:
        _apply_profile_config(profile, config)
        merchant, _ = _prepare_static_qr(profile, previous_payload, config, preview=False)
        _validate_profile(profile)
        # Native Document.save compares this preview revision with the current
        # database row. It cannot silently overwrite a concurrent profile edit.
        profile.modified = expected_modified
        enabled = cint(getattr(profile, PROFILE_STATIC_ENABLED_FIELD, 0)) or cint(getattr(profile, PROFILE_AUTOMATIC_ENABLED_FIELD, 0))
        profile.custom_kopos_qr_configuration_status = "Ready" if enabled else "Disabled"
        profile.save()
        result = _build_preview(profile, merchant)
        if cint(getattr(profile, PROFILE_STATIC_ENABLED_FIELD, 0)) and not result["static_qr"]["ready"]:
            frappe.throw(
                "Static QR is not ready: " + cstr(result["static_qr"].get("reason_code")),
                frappe.ValidationError,
            )
        if cint(getattr(profile, PROFILE_AUTOMATIC_ENABLED_FIELD, 0)) and not result["automatic_qr"]["ready"]:
            frappe.throw(
                "Automatic QR is not ready: " + cstr(result["automatic_qr"].get("reason_code")),
                frappe.ValidationError,
            )
        return result
    except Exception:
        rollback_to_savepoint(savepoint, title="QR setup rollback failed")
        raise


def validate_qr_configuration(profile_name: str) -> dict[str, Any]:
    return get_qr_setup_preview(profile_name)


def _validate_profile(profile: Any) -> None:
    company = cstr(getattr(profile, "company", None)).strip()
    currency = cstr(getattr(profile, "currency", None)).strip().upper()
    if not company or currency != "MYR":
        frappe.throw("QR payments require a company with MYR currency", frappe.ValidationError)
    if cint(getattr(profile, PROFILE_STATIC_ENABLED_FIELD, 0)):
        if not commissioned_profile_static_qr_config(profile, expected_company=company):
            frappe.throw("Add and verify your store QR before enabling Static QR", frappe.ValidationError)
        if not cstr(getattr(profile, PROFILE_SUSPENSE_FIELD, None)).strip():
            frappe.throw("Manual QR Suspense Account is required for Static QR", frappe.ValidationError)
    if cint(getattr(profile, PROFILE_AUTOMATIC_ENABLED_FIELD, 0)):
        if not cstr(getattr(profile, PROFILE_ACCOUNT_FIELD, None)).strip() or not cstr(getattr(profile, PROFILE_OUTLET_FIELD, None)).strip():
            frappe.throw("Automatic QR requires a Maybank account and outlet ID", frappe.ValidationError)


def _apply_profile_config(profile: Any, config: dict[str, Any]) -> None:
    for fieldname in PROFILE_CONFIG_FIELDS:
        if fieldname in config:
            value = config[fieldname]
            if fieldname in (PROFILE_STATIC_ENABLED_FIELD, PROFILE_AUTOMATIC_ENABLED_FIELD):
                if value not in (0, 1, "0", "1"):
                    frappe.throw("QR enable switches must be on or off", frappe.ValidationError)
                value = cint(value)
            elif value is not None and not isinstance(value, str):
                frappe.throw("QR code and account settings must be text", frappe.ValidationError)
            setattr(profile, fieldname, value)


def _parse_config(config: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(config, str):
        try:
            config = frappe.parse_json(config)
        except json.JSONDecodeError:
            frappe.throw("QR configuration could not be read; reopen setup", frappe.ValidationError)
    if not isinstance(config, dict):
        frappe.throw("QR configuration must be an object", frappe.ValidationError)
    if set(config) - set(PROFILE_CONFIG_FIELDS) - {"expected_modified", "confirmed_static_qr_sha256"}:
        frappe.throw("QR setup contains unsupported settings; reopen setup", frappe.ValidationError)
    return config


def _prepare_static_qr(profile: Any, previous_payload: str, config: dict[str, Any], *, preview: bool) -> tuple[dict[str, str] | None, bool]:
    payload = cstr(getattr(profile, PROFILE_STATIC_PAYLOAD_FIELD, None))
    if not payload:
        for field in (*STATIC_METADATA_FIELDS, "commissioned_at"):
            setattr(profile, "custom_kopos_static_qr_" + field, None)
        return None, False
    if payload == previous_payload and not cint(getattr(profile, PROFILE_STATIC_ENABLED_FIELD, 0)):
        # Disabling QR remains possible even if retained metadata needs repair.
        return None, False
    merchant = inspect_paynet_static_qr(payload)
    requires_confirmation = payload != previous_payload or any(
        not cstr(getattr(profile, "custom_kopos_static_qr_" + field, None))
        for field in (*STATIC_METADATA_FIELDS, "commissioned_at")
    )
    if requires_confirmation:
        if not preview and config.get("confirmed_static_qr_sha256") != merchant["payload_sha256"]:
            frappe.throw("Check the QR merchant details against your bank, then confirm this exact QR", frappe.ValidationError)
        for field in STATIC_METADATA_FIELDS:
            setattr(profile, "custom_kopos_static_qr_" + field, merchant[field])
        profile.custom_kopos_static_qr_commissioned_at = now_datetime()
    return merchant, requires_confirmation


def _mode_of_payment_mapping(profile: Any) -> dict[str, Any]:
    account = frappe.db.get_value(
        "Mode of Payment Account",
        {"parent": "DuitNow QR", "company": getattr(profile, "company", None)},
        "default_account",
    )
    return {
        "name": "DuitNow QR",
        "account": cstr(account),
        "type": cstr(frappe.db.get_value("Mode of Payment", "DuitNow QR", "type")),
        "company": cstr(getattr(profile, "company", None)).strip(),
    }


def _empty_device(profile: Any) -> Any:
    return type("ProfileDevice", (), {"device_id": "profile-setup"})()
