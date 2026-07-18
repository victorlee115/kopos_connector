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
)
from kopos_connector.services.maybank.client import MaybankClient
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

    try:
        client = MaybankClient.from_settings()
        if (
            not cstr(client.username).strip()
            or not cstr(client.encrypted_pin).strip()
            or not cstr(client.user_type).strip()
            or not cstr(client.outlet_id).strip()
        ):
            frappe.throw(
                "Maybank credentials and outlet are incomplete",
                frappe.ValidationError,
            )
    except Exception as error:
        log_sanitized_error("Maybank QR readiness provider configuration failed", error)
        response["reason_code"] = "provider_configuration_unavailable"
        return response

    try:
        sale_context = {
            "company": company,
            "currency": currency,
        }
        resolve_manual_qr_suspense_account(sale_context)
        resolve_verified_qr_settlement_account(
            MAYBANK_MODE_OF_PAYMENT,
            company,
            currency,
        )
    except Exception as error:
        log_sanitized_error("Maybank QR readiness accounting configuration failed", error)
        response["reason_code"] = "accounting_configuration_unavailable"
        return response

    response.update(
        {
            "status": "ready",
            "reason_code": None,
            "outlet_id_sha256": hashlib.sha256(
                cstr(client.outlet_id).strip().encode("utf-8")
            ).hexdigest(),
        }
    )
    return response
