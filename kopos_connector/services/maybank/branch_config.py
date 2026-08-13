from __future__ import annotations

"""Branch-scoped Maybank QR configuration.

POS Profile is the only runtime authority for outlet selection.  This module
keeps the lookup in one place so API, workers and acceptance tooling cannot
silently fall back to the legacy Maybank Settings singleton.
"""

from collections.abc import Mapping
from typing import Any

import frappe
from frappe.utils import cstr


PROFILE_ACCOUNT_FIELD = "custom_kopos_maybank_qrpaybiz_account"
PROFILE_OUTLET_FIELD = "custom_kopos_maybank_outlet_id"
PROFILE_SUSPENSE_FIELD = "custom_kopos_manual_qr_suspense_account"
PROFILE_CLEARING_FIELD = "custom_kopos_qr_clearing_account"
PROFILE_SETTLEMENT_BANK_FIELD = "custom_kopos_qr_settlement_bank_account"
PROFILE_AUTOMATIC_ENABLED_FIELD = "custom_kopos_automatic_qr_enabled"
PROFILE_STATIC_ENABLED_FIELD = "custom_kopos_static_qr_enabled"
PROFILE_STATIC_PAYLOAD_FIELD = "custom_kopos_static_qr_payload"


class QrAccountingNotConfigured(frappe.ValidationError):
    """A safe, retryable configuration failure for a locally paid sale."""

    error_code = "qr_accounting_not_configured"
    retryable_after_configuration = True

    def __init__(self, message: str = "QR accounting is not configured for this POS Profile"):
        super().__init__(message)


def _value(document: Any, fieldname: str) -> Any:
    if isinstance(document, Mapping):
        return document.get(fieldname)
    return getattr(document, fieldname, None)


def profile_for_device(device_id: str) -> Any:
    resolved = cstr(device_id).strip()
    if not resolved:
        raise QrAccountingNotConfigured("The device is not assigned to a POS Profile")
    profile_name = frappe.db.get_value("KoPOS Device", {"device_id": resolved}, "pos_profile")
    if not profile_name:
        raise QrAccountingNotConfigured("The device is not assigned to a POS Profile")
    return frappe.get_cached_doc("POS Profile", profile_name)


def profile_for_order(order_doc: Any) -> Any:
    profile_name = cstr(_value(order_doc, "pos_profile")).strip()
    if profile_name:
        return frappe.get_cached_doc("POS Profile", profile_name)
    return profile_for_device(cstr(_value(order_doc, "device_id")))


def account_name(profile_doc: Any) -> str:
    return cstr(_value(profile_doc, PROFILE_ACCOUNT_FIELD)).strip()


def outlet_id(profile_doc: Any) -> str:
    return cstr(_value(profile_doc, PROFILE_OUTLET_FIELD)).strip()


def suspense_account(profile_doc: Any) -> str:
    return cstr(_value(profile_doc, PROFILE_SUSPENSE_FIELD)).strip()


def clearing_account(profile_doc: Any) -> str:
    return cstr(_value(profile_doc, PROFILE_CLEARING_FIELD)).strip()


def static_payload(profile_doc: Any) -> str:
    return cstr(_value(profile_doc, PROFILE_STATIC_PAYLOAD_FIELD)).strip()


def validate_settlement_bank_account(profile_doc: Any, *, currency: str | None = None) -> str:
    """Validate the POS Profile's real ERPNext Bank Account selection."""

    bank_account = cstr(_value(profile_doc, PROFILE_SETTLEMENT_BANK_FIELD)).strip()
    company = cstr(_value(profile_doc, "company")).strip()
    resolved_currency = cstr(currency or _value(profile_doc, "currency")).strip().upper()
    if not bank_account:
        raise QrAccountingNotConfigured("Settlement Bank Account is required on the POS Profile")
    row = frappe.db.get_value(
        "Bank Account",
        bank_account,
        ["name", "account", "company", "disabled"],
        as_dict=True,
    )
    if not row:
        raise QrAccountingNotConfigured("Settlement Bank Account was not found")
    if cstr(row.get("company")).strip() != company or int(row.get("disabled") or 0):
        raise QrAccountingNotConfigured("Settlement Bank Account is disabled or belongs to another company")
    if not cstr(row.get("account")).strip():
        raise QrAccountingNotConfigured("Settlement Bank Account has no linked ledger account")
    account_row = frappe.db.get_value(
        "Account",
        row.get("account"),
        ["company", "is_group", "disabled", "root_type", "account_currency"],
        as_dict=True,
    )
    if not account_row or cstr(account_row.get("company")).strip() != company:
        raise QrAccountingNotConfigured("Settlement Bank ledger belongs to another company")
    if int(account_row.get("is_group") or 0) or int(account_row.get("disabled") or 0):
        raise QrAccountingNotConfigured("Settlement Bank ledger must be an enabled account")
    if cstr(account_row.get("root_type")).strip() != "Asset":
        raise QrAccountingNotConfigured("Settlement Bank ledger must be an Asset account")
    if resolved_currency and cstr(account_row.get("account_currency")).strip().upper() != resolved_currency:
        raise QrAccountingNotConfigured("Settlement Bank ledger currency does not match the POS Profile")
    return bank_account


def require_provider_binding(profile_doc: Any, *, for_new_payment: bool = True) -> tuple[Any, str]:
    """Return the account document and immutable outlet binding for a profile."""

    account = account_name(profile_doc)
    outlet = outlet_id(profile_doc)
    if not account or not outlet:
        raise QrAccountingNotConfigured("Automatic QR account and outlet are required on the POS Profile")
    account_doc = frappe.get_doc("Maybank QRPayBiz Account", account)
    if for_new_payment and not int(_value(account_doc, "enabled") or 0):
        raise QrAccountingNotConfigured("The configured Maybank QR account is disabled")
    return account_doc, outlet


def snapshot_for_order(order_doc: Any) -> dict[str, str]:
    profile = profile_for_order(order_doc)
    account, outlet = require_provider_binding(profile, for_new_payment=False)
    return {
        "pos_profile": cstr(_value(profile, "name")).strip(),
        "maybank_qrpaybiz_account": cstr(_value(account, "name")).strip(),
        "outlet_id": outlet,
        "suspense_account": suspense_account(profile),
        "clearing_account": clearing_account(profile),
        "settlement_bank_account": cstr(_value(profile, PROFILE_SETTLEMENT_BANK_FIELD)).strip(),
        "company": cstr(_value(profile, "company")).strip(),
        "currency": cstr(_value(profile, "currency")).strip().upper(),
    }


def snapshot_for_transaction(transaction_doc: Any) -> dict[str, str]:
    """Read only transaction snapshots; never consult mutable profile settings."""

    return {
        "maybank_qrpaybiz_account": cstr(_value(transaction_doc, "maybank_qrpaybiz_account")).strip(),
        "outlet_id": cstr(_value(transaction_doc, "outlet_id")).strip(),
        "suspense_account": cstr(_value(transaction_doc, "suspense_account")).strip(),
        "clearing_account": cstr(_value(transaction_doc, "clearing_account")).strip(),
        "settlement_bank_account": cstr(_value(transaction_doc, "settlement_bank_account")).strip(),
    }
