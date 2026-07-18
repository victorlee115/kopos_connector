# pyright: reportMissingImports=false

"""Strict PayNet DuitNow static-QR commissioning and serialization."""

from __future__ import annotations

import hashlib
import re
from typing import Any

import frappe
from frappe.utils import cstr


PAYNET_PAYLOAD_VERSION = "02"
PAYNET_STATIC_INITIATION_METHOD = "11"
PAYNET_MALAYSIA_AID = "A0000006150001"
PAYNET_MYR_CURRENCY_CODE = "458"
PAYNET_MALAYSIA_COUNTRY_CODE = "MY"
MAX_STATIC_QR_PAYLOAD_LENGTH = 512
ACQUIRER_ID_PATTERN = re.compile(r"^[0-9]{1,6}$")
QR_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,28}$")


def _parse_tlv(payload: str, label: str) -> dict[str, str]:
    values: dict[str, str] = {}
    cursor = 0
    while cursor < len(payload):
        if len(payload) - cursor < 4:
            frappe.throw(
                f"{label} ends before a complete tag and length",
                frappe.ValidationError,
            )
        tag = payload[cursor : cursor + 2]
        length_text = payload[cursor + 2 : cursor + 4]
        if not tag.isdigit() or not length_text.isdigit():
            frappe.throw(
                f"{label} contains an invalid tag or length",
                frappe.ValidationError,
            )
        length = int(length_text)
        value_start = cursor + 4
        value_end = value_start + length
        if value_end > len(payload):
            frappe.throw(
                f"{label} tag {tag} exceeds the payload length",
                frappe.ValidationError,
            )
        if tag in values:
            frappe.throw(
                f"{label} contains duplicate tag {tag}",
                frappe.ValidationError,
            )
        values[tag] = payload[value_start:value_end]
        cursor = value_end
    return values


def _crc16_ccitt_false(data: bytes) -> str:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def inspect_paynet_static_qr(payload: Any) -> dict[str, str]:
    """Parse and authenticate one reusable PayNet v1.5 static QR payload."""

    if not isinstance(payload, str) or not payload or payload.strip() != payload:
        frappe.throw(
            "Static QR payload must be a nonempty exact string",
            frappe.ValidationError,
        )
    if len(payload) > MAX_STATIC_QR_PAYLOAD_LENGTH:
        frappe.throw(
            "Static QR payload exceeds the supported length",
            frappe.ValidationError,
        )
    try:
        encoded = payload.encode("ascii")
    except UnicodeEncodeError:
        frappe.throw(
            "Static QR payload must use the PayNet common ASCII character set",
            frappe.ValidationError,
        )
        raise AssertionError("frappe.throw must raise")
    if any(byte < 0x20 or byte > 0x7E for byte in encoded):
        frappe.throw(
            "Static QR payload must contain printable ASCII characters only",
            frappe.ValidationError,
        )

    top = _parse_tlv(payload, "Static QR payload")
    if top.get("00") != PAYNET_PAYLOAD_VERSION:
        frappe.throw(
            "Static QR payload must use PayNet payload version 02",
            frappe.ValidationError,
        )
    if top.get("01") != PAYNET_STATIC_INITIATION_METHOD:
        frappe.throw(
            "Static QR payload must use static initiation method 11",
            frappe.ValidationError,
        )
    if top.get("53") != PAYNET_MYR_CURRENCY_CODE:
        frappe.throw(
            "Static QR payload currency must be MYR (458)",
            frappe.ValidationError,
        )
    if top.get("58") != PAYNET_MALAYSIA_COUNTRY_CODE:
        frappe.throw(
            "Static QR payload country must be MY",
            frappe.ValidationError,
        )
    merchant_name = top.get("59", "")
    if (
        not merchant_name.strip()
        or merchant_name != merchant_name.strip()
        or len(merchant_name) > 25
    ):
        frappe.throw(
            "Static QR payload requires a 1-25 character merchant name",
            frappe.ValidationError,
        )
    if not top.get("52") or len(top["52"]) != 4 or not top["52"].isdigit():
        frappe.throw(
            "Static QR payload requires a four-digit merchant category code",
            frappe.ValidationError,
        )
    merchant_city = top.get("60", "")
    if (
        not merchant_city.strip()
        or merchant_city != merchant_city.strip()
        or len(merchant_city) > 15
    ):
        frappe.throw(
            "Static QR payload requires a merchant city",
            frappe.ValidationError,
        )
    if any(tag in top for tag in ("54", "55", "56", "57")):
        frappe.throw(
            "Reusable KoPOS static QR must not contain a fixed amount or convenience fee",
            frappe.ValidationError,
        )

    merchant_account = top.get("26")
    if not merchant_account:
        frappe.throw(
            "Static QR payload requires PayNet merchant account tag 26",
            frappe.ValidationError,
        )
    merchant = _parse_tlv(merchant_account, "Static QR merchant account")
    if merchant.get("00") != PAYNET_MALAYSIA_AID:
        frappe.throw(
            "Static QR payload has the wrong PayNet application identifier",
            frappe.ValidationError,
        )
    acquirer_id = merchant.get("01", "")
    if not ACQUIRER_ID_PATTERN.fullmatch(acquirer_id):
        frappe.throw(
            "Static QR payload has an invalid PayNet acquirer ID",
            frappe.ValidationError,
        )
    merchant_id = merchant.get("02", "")
    if not QR_ID_PATTERN.fullmatch(merchant_id):
        frappe.throw(
            "Static QR payload has an invalid PayNet QR ID",
            frappe.ValidationError,
        )

    crc = top.get("63", "")
    if len(crc) != 4 or not all(character in "0123456789ABCDEFabcdef" for character in crc):
        frappe.throw(
            "Static QR payload has an invalid CRC field",
            frappe.ValidationError,
        )
    if not payload.endswith(f"6304{crc}"):
        frappe.throw(
            "Static QR CRC must be the final payload field",
            frappe.ValidationError,
        )
    calculated_crc = _crc16_ccitt_false(encoded[:-4])
    if crc.upper() != calculated_crc:
        frappe.throw(
            "Static QR payload CRC does not match",
            frappe.ValidationError,
        )

    return {
        "payload": payload,
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
        "version": PAYNET_PAYLOAD_VERSION,
        "merchant_id": merchant_id,
        "acquirer_id": acquirer_id,
        "merchant_name": merchant_name,
    }


def validate_static_qr_metadata(
    inspection: dict[str, str],
    *,
    payload_sha256: Any,
    merchant_id: Any,
    acquirer_id: Any,
    merchant_name: Any,
    version: Any,
    commissioned_at: Any,
    configured_company: Any,
    expected_company: Any,
) -> None:
    exact_fields = {
        "payload SHA-256": (payload_sha256, inspection["payload_sha256"]),
        "merchant ID": (merchant_id, inspection["merchant_id"]),
        "acquirer ID": (acquirer_id, inspection["acquirer_id"]),
        "merchant name": (merchant_name, inspection["merchant_name"]),
        "version": (version, inspection["version"]),
    }
    for label, (actual_value, expected_value) in exact_fields.items():
        if cstr(actual_value).strip() != expected_value:
            frappe.throw(
                f"Static QR commissioned {label} does not match the payload",
                frappe.ValidationError,
            )
    resolved_company = cstr(configured_company).strip()
    profile_company = cstr(expected_company).strip()
    if not resolved_company or not profile_company or resolved_company != profile_company:
        frappe.throw(
            "Static QR commissioned company does not match the device POS Profile",
            frappe.ValidationError,
        )
    if not cstr(commissioned_at).strip():
        frappe.throw(
            "Static QR commissioning timestamp is required",
            frappe.ValidationError,
        )


def commissioned_static_qr_config(
    device_doc: Any,
    *,
    expected_company: str | None,
) -> dict[str, str] | None:
    payload = cstr(getattr(device_doc, "static_qr_payload", None))
    if not payload:
        return None
    inspection = inspect_paynet_static_qr(payload)
    validate_static_qr_metadata(
        inspection,
        payload_sha256=getattr(device_doc, "static_qr_payload_sha256", None),
        merchant_id=getattr(device_doc, "static_qr_merchant_id", None),
        acquirer_id=getattr(device_doc, "static_qr_acquirer_id", None),
        merchant_name=getattr(device_doc, "static_qr_merchant_name", None),
        version=getattr(device_doc, "static_qr_version", None),
        commissioned_at=getattr(device_doc, "static_qr_commissioned_at", None),
        configured_company=getattr(device_doc, "static_qr_company", None),
        expected_company=expected_company,
    )
    return {
        "static_qr_payload": inspection["payload"],
        "static_qr_payload_sha256": inspection["payload_sha256"],
        "static_qr_merchant_id": inspection["merchant_id"],
        "static_qr_acquirer_id": inspection["acquirer_id"],
        "static_qr_merchant_name": inspection["merchant_name"],
        "static_qr_version": inspection["version"],
        "static_qr_commissioned_at": cstr(
            getattr(device_doc, "static_qr_commissioned_at", None)
        ).strip(),
        "static_qr_company": cstr(
            getattr(device_doc, "static_qr_company", None)
        ).strip(),
    }
