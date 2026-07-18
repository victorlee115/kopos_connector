from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

from kopos_connector.api import catalog, devices  # noqa: E402
from kopos_connector.kopos.doctype.kopos_device import kopos_device  # noqa: E402
from kopos_connector.services import static_qr_commissioning as static_qr  # noqa: E402


VALID_STATIC_QR = (
    "00020201021126410014A000000615000101065016640209123456789"
    "5204999953034585802MY5909QRCSDNBHD6005BANGI6304184A"
)
FIXED_AMOUNT_STATIC_QR = (
    "00020201021126410014A000000615000101065016640209123456789"
    "520499995303458540510.005802MY5909QRCSDNBHD6005BANGI6304343F"
)


def _tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def _static_qr(
    *,
    acquirer_id: str = "501664",
    merchant_id: str = "123456789",
    merchant_name: str = "QRCSDNBHD",
    merchant_city: str = "BANGI",
    extra_top_level: str = "",
) -> str:
    merchant_account = "".join(
        (
            _tlv("00", static_qr.PAYNET_MALAYSIA_AID),
            _tlv("01", acquirer_id),
            _tlv("02", merchant_id),
        )
    )
    before_crc = "".join(
        (
            _tlv("00", static_qr.PAYNET_PAYLOAD_VERSION),
            _tlv("01", static_qr.PAYNET_STATIC_INITIATION_METHOD),
            _tlv("26", merchant_account),
            _tlv("52", "9999"),
            _tlv("53", static_qr.PAYNET_MYR_CURRENCY_CODE),
            extra_top_level,
            _tlv("58", static_qr.PAYNET_MALAYSIA_COUNTRY_CODE),
            _tlv("59", merchant_name),
            _tlv("60", merchant_city),
            "6304",
        )
    )
    return f"{before_crc}{static_qr._crc16_ccitt_false(before_crc.encode('ascii'))}"


def _commissioned_device(**overrides: Any) -> SimpleNamespace:
    inspection = static_qr.inspect_paynet_static_qr(VALID_STATIC_QR)
    values = {
        "static_qr_payload": VALID_STATIC_QR,
        "static_qr_payload_sha256": inspection["payload_sha256"],
        "static_qr_merchant_id": "123456789",
        "static_qr_acquirer_id": "501664",
        "static_qr_merchant_name": "QRCSDNBHD",
        "static_qr_version": "02",
        "static_qr_commissioned_at": "2026-07-19T10:00:00+08:00",
        "static_qr_company": "KoPOS Sdn Bhd",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_paynet_static_qr_inspection_returns_exact_commissioning_identity() -> None:
    inspection = static_qr.inspect_paynet_static_qr(VALID_STATIC_QR)

    assert inspection == {
        "payload": VALID_STATIC_QR,
        "payload_sha256": (
            "19e2877469947fac1a2e79d02817aea57fe3e10f430c932af34809fe3b66d031"
        ),
        "version": "02",
        "merchant_id": "123456789",
        "acquirer_id": "501664",
        "merchant_name": "QRCSDNBHD",
    }


def test_paynet_static_qr_rejects_crc_tamper() -> None:
    tampered = VALID_STATIC_QR.replace("QRCSDNBHD", "QRCSDNBHE")

    with pytest.raises(static_qr.frappe.ValidationError, match="CRC does not match"):
        static_qr.inspect_paynet_static_qr(tampered)


def test_paynet_static_qr_rejects_whitespace_or_control_character_tamper() -> None:
    with pytest.raises(
        static_qr.frappe.ValidationError,
        match="nonempty exact string",
    ):
        static_qr.commissioned_static_qr_config(
            _commissioned_device(static_qr_payload=f" {VALID_STATIC_QR}"),
            expected_company="KoPOS Sdn Bhd",
        )

    with pytest.raises(
        static_qr.frappe.ValidationError,
        match="printable ASCII",
    ):
        static_qr.inspect_paynet_static_qr(
            VALID_STATIC_QR.replace("QRCSDNBHD", "QRCS\nNBHD")
        )


def test_reusable_static_qr_rejects_fixed_amount_payload() -> None:
    with pytest.raises(
        static_qr.frappe.ValidationError,
        match="must not contain a fixed amount",
    ):
        static_qr.inspect_paynet_static_qr(FIXED_AMOUNT_STATIC_QR)


def test_static_qr_identifier_bounds_match_the_tablet_contract() -> None:
    inspection = static_qr.inspect_paynet_static_qr(
        _static_qr(
            acquirer_id="7",
            merchant_id="ABCD1234EFGH5678IJKL9012MNOP",
        )
    )

    assert inspection["acquirer_id"] == "7"
    assert inspection["merchant_id"] == "ABCD1234EFGH5678IJKL9012MNOP"


@pytest.mark.parametrize("tag", ("55", "56", "57"))
def test_reusable_static_qr_rejects_convenience_fee_tags(tag: str) -> None:
    with pytest.raises(
        static_qr.frappe.ValidationError,
        match="fixed amount or convenience fee",
    ):
        static_qr.inspect_paynet_static_qr(
            _static_qr(extra_top_level=_tlv(tag, "01"))
        )


def test_static_qr_rejects_identifiers_outside_the_commissioning_contract() -> None:
    with pytest.raises(
        static_qr.frappe.ValidationError,
        match="invalid PayNet acquirer ID",
    ):
        static_qr.inspect_paynet_static_qr(_static_qr(acquirer_id="1234567"))

    with pytest.raises(
        static_qr.frappe.ValidationError,
        match="invalid PayNet QR ID",
    ):
        static_qr.inspect_paynet_static_qr(
            _static_qr(merchant_id="merchant-with-dashes")
        )


def test_commissioned_config_rejects_metadata_or_company_drift() -> None:
    with pytest.raises(
        static_qr.frappe.ValidationError,
        match="merchant ID does not match",
    ):
        static_qr.commissioned_static_qr_config(
            _commissioned_device(static_qr_merchant_id="another-merchant"),
            expected_company="KoPOS Sdn Bhd",
        )

    with pytest.raises(
        static_qr.frappe.ValidationError,
        match="company does not match",
    ):
        static_qr.commissioned_static_qr_config(
            _commissioned_device(),
            expected_company="Another Company",
        )


def test_device_save_derives_and_timestamps_commissioning_metadata() -> None:
    device = kopos_device.KoPOSDevice()
    device.static_qr_payload = VALID_STATIC_QR
    device.pos_profile = "Counter 1"
    device.static_qr_payload_sha256 = None
    device.static_qr_merchant_id = None
    device.static_qr_acquirer_id = None
    device.static_qr_merchant_name = None
    device.static_qr_version = None
    device.static_qr_commissioned_at = None
    device.static_qr_company = None
    device.get_doc_before_save = lambda: None
    commissioned_at = datetime(2026, 7, 19, 10, 0, 0)

    with (
        patch.object(
            kopos_device.frappe.db,
            "get_value",
            return_value="KoPOS Sdn Bhd",
        ),
        patch.object(kopos_device, "now_datetime", return_value=commissioned_at),
    ):
        device._validate_static_qr_commissioning()

    assert device.static_qr_merchant_id == "123456789"
    assert device.static_qr_acquirer_id == "501664"
    assert device.static_qr_merchant_name == "QRCSDNBHD"
    assert device.static_qr_version == "02"
    assert device.static_qr_company == "KoPOS Sdn Bhd"
    assert device.static_qr_commissioned_at == commissioned_at


def test_device_config_emits_only_exact_commissioned_static_qr_metadata() -> None:
    device = _commissioned_device(
        device_id="TAB-1",
        device_name="Counter Tablet",
        device_prefix="A",
        enabled=1,
        config_version=4,
        pos_profile="Counter 1",
        allow_training_mode=0,
        allow_manual_settings_override=0,
        app_min_version="1.0.3",
        printers=[],
        device_users=[
            SimpleNamespace(
                user="cashier@example.test",
                display_name="Cashier",
                pin_hash="supported-hash",
                active=1,
                can_manager_override=0,
                can_refund=0,
                can_void=0,
                can_open_shift=1,
                can_close_shift=1,
                default_cashier=1,
            )
        ],
    )
    profile = SimpleNamespace(
        company="KoPOS Sdn Bhd",
        warehouse="Main - K",
        currency="MYR",
    )

    with (
        patch.object(devices.frappe, "get_doc", return_value=profile),
        patch.object(devices, "is_supported_pin_hash", return_value=True),
        patch.object(catalog, "get_tax_rate_value", return_value="0.00"),
    ):
        payload = devices.serialize_device_config(device)

    assert payload["static_qr_available"] is True
    assert payload["static_qr_configuration_status"] == "commissioned"
    assert payload["static_qr_payload"] == VALID_STATIC_QR
    assert payload["static_qr_payload_sha256"] == (
        "19e2877469947fac1a2e79d02817aea57fe3e10f430c932af34809fe3b66d031"
    )
    assert payload["static_qr_merchant_id"] == "123456789"
    assert payload["static_qr_acquirer_id"] == "501664"
    assert payload["static_qr_company"] == "KoPOS Sdn Bhd"


def test_kopos_device_schema_carries_additive_static_qr_commissioning_fields() -> None:
    schema_path = (
        "kopos_connector/kopos/doctype/kopos_device/kopos_device.json"
    )
    with open(schema_path, encoding="utf-8") as schema_file:
        schema = json.load(schema_file)
    fieldnames = {field["fieldname"] for field in schema["fields"]}

    assert {
        "static_qr_payload_sha256",
        "static_qr_merchant_id",
        "static_qr_acquirer_id",
        "static_qr_merchant_name",
        "static_qr_version",
        "static_qr_commissioned_at",
        "static_qr_company",
    } <= fieldnames
