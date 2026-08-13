# pyright: reportMissingImports=false

"""Read-only Maybank Automatic QR capability prewarm."""

from __future__ import annotations

import hashlib
from typing import Any

import frappe
from frappe.utils import cstr, now_datetime

from kopos_connector.kopos.services.accounting.maybank_payment_service import (
    resolve_manual_qr_suspense_account,
    resolve_verified_qr_settlement_account,
    validate_manual_qr_suspense_account,
)
from kopos_connector.services.maybank.client import MaybankClient
from kopos_connector.services.maybank.branch_config import (
    PROFILE_AUTOMATIC_ENABLED_FIELD,
    PROFILE_STATIC_ENABLED_FIELD,
    PROFILE_STATIC_PAYLOAD_FIELD,
    QrAccountingNotConfigured,
    account_name,
    outlet_id,
    suspense_account as profile_suspense_account,
    require_provider_binding,
    validate_settlement_bank_account,
)
from kopos_connector.services.static_qr_commissioning import commissioned_profile_static_qr_config
from kopos_connector.utils.diagnostics import log_sanitized_error


READINESS_CONTRACT_VERSION = "maybank-qr-readiness-v1"
MAYBANK_MODE_OF_PAYMENT = "DuitNow QR"


def _base_readiness(device_id: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "device_id": device_id,
        "outlet_id_sha256": None,
        "checked_at": now_datetime().isoformat(),
        "contract_version": READINESS_CONTRACT_VERSION,
        "provider_request_attempted": False,
        "financial_side_effects": False,
    }


def get_maybank_qr_readiness_payload(
    device_doc: Any,
    profile_doc: Any,
) -> dict[str, Any]:
    """Validate local capability without preparing a sale or calling Maybank."""

    device_id = cstr(getattr(device_doc, "device_id", None)).strip()
    response = _base_readiness(device_id)
    company = cstr(getattr(profile_doc, "company", None)).strip()
    currency = cstr(getattr(profile_doc, "currency", None)).strip().upper()
    if not currency and company:
        currency = cstr(
            frappe.db.get_value("Company", company, "default_currency")
        ).strip().upper()
    if not device_id or not company or currency != "MYR":
        response["reason_code"] = "device_configuration_unavailable"
        return response

    v2 = get_payment_readiness_payload(device_doc, profile_doc)
    automatic = v2["automatic_qr"]
    response["status"] = "ready" if automatic["ready"] else "unavailable"
    response["reason_code"] = automatic["reason_code"]
    response["outlet_id_sha256"] = automatic.get("outlet_id_sha256")
    response["provider_request_attempted"] = False
    response["financial_side_effects"] = False
    return response


def _lane(status: str, reason_code: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"ready": status == "ready", "status": status, "reason_code": reason_code, **extra}


def get_payment_readiness_payload(device_doc: Any, profile_doc: Any) -> dict[str, Any]:
    """Evaluate cash, static QR and automatic QR independently."""

    device_id = cstr(getattr(device_doc, "device_id", None)).strip()
    company = cstr(getattr(profile_doc, "company", None)).strip()
    currency = cstr(getattr(profile_doc, "currency", None)).strip().upper()
    if not currency and company:
        currency = cstr(frappe.db.get_value("Company", company, "default_currency")).strip().upper()

    cash = _lane("ready")
    static = _lane("unavailable", "static_qr_disabled")
    if bool(getattr(profile_doc, PROFILE_STATIC_ENABLED_FIELD, 0)):
        try:
            profile_qr = commissioned_profile_static_qr_config(profile_doc, expected_company=company)
            if not profile_qr:
                raise QrAccountingNotConfigured("Static QR payload is not configured")
            suspense = profile_suspense_account(profile_doc)
            if not suspense:
                raise QrAccountingNotConfigured("Manual QR Suspense Account is not configured")
            validate_manual_qr_suspense_account(
                suspense,
                company=company,
                currency=currency,
            )
            static = _lane(
                "ready",
                None,
                payload_sha256=profile_qr["static_qr_payload_sha256"],
            )
        except Exception as error:
            log_sanitized_error("Static QR readiness failed", error)
            static = _lane("unavailable", "static_qr_configuration_unavailable")

    automatic = _lane("unavailable", "automatic_qr_disabled")
    if bool(getattr(profile_doc, PROFILE_AUTOMATIC_ENABLED_FIELD, 0)):
        try:
            account, resolved_outlet = require_provider_binding(profile_doc, for_new_payment=True)
            if currency != "MYR" or not company or not device_id:
                raise QrAccountingNotConfigured("Automatic QR requires a MYR POS Profile and device")
            sale_context = {
                "device_id": device_id,
                "pos_profile": cstr(getattr(profile_doc, "name", None)).strip(),
                "company": company,
                "currency": currency,
            }
            resolve_manual_qr_suspense_account(sale_context)
            resolve_verified_qr_settlement_account(
                MAYBANK_MODE_OF_PAYMENT,
                company,
                currency,
                cstr(getattr(profile_doc, "custom_kopos_qr_clearing_account", None)).strip(),
            )
            validate_settlement_bank_account(profile_doc, currency=currency)
            client = MaybankClient.from_account_doc(account, resolved_outlet, require_enabled=True)
            binding_hash = hashlib.sha256(
                f"{cstr(getattr(account, 'name', '')).strip()}|{resolved_outlet}".encode("utf-8")
            ).hexdigest()
            automatic = _lane(
                "ready",
                None,
                provider_binding_sha256=binding_hash,
                outlet_id_sha256=hashlib.sha256(resolved_outlet.encode("utf-8")).hexdigest(),
                account_name_sha256=hashlib.sha256(cstr(getattr(account, "name", "")).encode("utf-8")).hexdigest(),
                base_url_origin=client.base_url,
            )
        except Exception as error:
            log_sanitized_error("Automatic QR readiness failed", error)
            reason = "accounting_configuration_unavailable"
            if isinstance(error, QrAccountingNotConfigured):
                reason = "provider_configuration_unavailable" if "account" in str(error).lower() or "outlet" in str(error).lower() else "accounting_configuration_unavailable"
            automatic = _lane("unavailable", reason)

    return {
        "contract_version": "kopos-payment-readiness-v2",
        "device_id": device_id,
        "pos_profile": cstr(getattr(profile_doc, "name", None)).strip(),
        "company": company,
        "currency": currency,
        "checked_at": now_datetime().isoformat(),
        "cash": cash,
        "static_qr": static,
        "automatic_qr": automatic,
        "provider_request_attempted": False,
        "financial_side_effects": False,
    }
