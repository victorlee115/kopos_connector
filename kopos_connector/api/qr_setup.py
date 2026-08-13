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
from kopos_connector.services.static_qr_commissioning import commissioned_profile_static_qr_config


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


def _require_setup_role() -> None:
    roles = set(frappe.get_roles())
    if not roles.intersection({"System Manager", "Accounts Manager"}):
        frappe.throw("System Manager or Accounts Manager permission is required", frappe.PermissionError)


def get_qr_setup_preview(profile_name: str, config: dict[str, Any] | str | None = None) -> dict[str, Any]:
    _require_setup_role()
    profile = frappe.get_doc("POS Profile", profile_name)
    if isinstance(config, str):
        config = frappe.parse_json(config)
    if config is not None:
        if not isinstance(config, dict):
            frappe.throw("QR configuration must be an object", frappe.ValidationError)
        _apply_profile_config(profile, config)
    mode_mapping = _mode_of_payment_mapping(profile)
    readiness = get_payment_readiness_payload(_empty_device(profile), profile)
    return {
        "profile": profile.name,
        "company": cstr(getattr(profile, "company", None)).strip(),
        "currency": cstr(getattr(profile, "currency", None)).strip().upper(),
        "static_qr": readiness["static_qr"],
        "automatic_qr": readiness["automatic_qr"],
        "cash": readiness["cash"],
        "mode_of_payment": mode_mapping,
        "changes_are_company_wide": True,
        "checked_at": now_datetime().isoformat(),
    }


def apply_qr_configuration(profile_name: str, config: dict[str, Any] | str) -> dict[str, Any]:
    """Apply only explicitly supplied profile fields inside one savepoint."""
    _require_setup_role()
    if isinstance(config, str):
        config = frappe.parse_json(config)
    if not isinstance(config, dict):
        frappe.throw("QR configuration must be an object", frappe.ValidationError)
    profile = frappe.get_doc("POS Profile", profile_name)
    frappe.db.savepoint("kopos_qr_setup")
    try:
        _apply_profile_config(profile, config)
        _validate_profile(profile)
        readiness = get_payment_readiness_payload(_empty_device(profile), profile)
        if cint(getattr(profile, PROFILE_STATIC_ENABLED_FIELD, 0)) and not readiness["static_qr"]["ready"]:
            frappe.throw(
                "Static QR is not ready: " + cstr(readiness["static_qr"].get("reason_code")),
                frappe.ValidationError,
            )
        if cint(getattr(profile, PROFILE_AUTOMATIC_ENABLED_FIELD, 0)) and not readiness["automatic_qr"]["ready"]:
            frappe.throw(
                "Automatic QR is not ready: " + cstr(readiness["automatic_qr"].get("reason_code")),
                frappe.ValidationError,
            )
        profile.custom_kopos_qr_configuration_status = "Ready"
        profile.save(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback(save_point="kopos_qr_setup")
        raise
    return get_qr_setup_preview(profile.name)


def validate_qr_configuration(profile_name: str) -> dict[str, Any]:
    return get_qr_setup_preview(profile_name)


def _validate_profile(profile: Any) -> None:
    company = cstr(getattr(profile, "company", None)).strip()
    currency = cstr(getattr(profile, "currency", None)).strip().upper()
    if not company or currency != "MYR":
        frappe.throw("QR payments require a company with MYR currency", frappe.ValidationError)
    if cint(getattr(profile, PROFILE_STATIC_ENABLED_FIELD, 0)):
        commissioned_profile_static_qr_config(profile, expected_company=company)
        if not cstr(getattr(profile, PROFILE_SUSPENSE_FIELD, None)).strip():
            frappe.throw("Manual QR Suspense Account is required for Static QR", frappe.ValidationError)
    if cint(getattr(profile, PROFILE_AUTOMATIC_ENABLED_FIELD, 0)):
        if not cstr(getattr(profile, PROFILE_ACCOUNT_FIELD, None)).strip() or not cstr(getattr(profile, PROFILE_OUTLET_FIELD, None)).strip():
            frappe.throw("Automatic QR requires a Maybank account and outlet ID", frappe.ValidationError)


def _apply_profile_config(profile: Any, config: dict[str, Any]) -> None:
    for fieldname in PROFILE_CONFIG_FIELDS:
        if fieldname in config:
            setattr(profile, fieldname, config[fieldname])


def _mode_of_payment_mapping(profile: Any) -> dict[str, Any]:
    try:
        rows = frappe.get_all(
            "Mode of Payment Account",
            filters={"parent": "DuitNow QR", "company": getattr(profile, "company", None)},
            fields=["account", "company"],
            limit_page_length=1,
        )
    except Exception:
        rows = []
    return {
        "name": "DuitNow QR",
        "account": cstr(rows[0].get("account")) if rows else "",
        "type": cstr(frappe.db.get_value("Mode of Payment", "DuitNow QR", "type")),
        "company": cstr(getattr(profile, "company", None)).strip(),
    }


def _empty_device(profile: Any) -> Any:
    return type("ProfileDevice", (), {"device_id": "profile-setup"})()
