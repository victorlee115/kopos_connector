from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

catalog = importlib.import_module("kopos_connector.api.catalog")
devices = importlib.import_module("kopos_connector.api.devices")
kopos_device = importlib.import_module(
    "kopos_connector.kopos.doctype.kopos_device.kopos_device"
)
pin = importlib.import_module("kopos_connector.utils.pin")

# Cross-runtime contract fixture. Keep byte-for-byte aligned with
# JiJiPOS/kopos/tests/pin-auth.test.ts.
CROSS_RUNTIME_PIN = "1234"
CROSS_RUNTIME_HASH = (
    "scrypt$1024$00112233445566778899aabbccddeeff$"
    "d66e4957f975b9dd70d3d4e1c4576f2815c93a762c26d76d73a584060eca684a"
)
VALID_SALT = "a" * 32
VALID_KEY = "b" * 64


def _device_user(pin_hash: str) -> SimpleNamespace:
    return SimpleNamespace(
        user="cashier@example.com",
        display_name="Cashier",
        pin="",
        pin_hash=pin_hash,
        active=1,
        default_cashier=1,
        can_manager_override=0,
        can_refund=0,
        can_void=0,
        can_open_shift=1,
        can_close_shift=1,
    )


def _device(pin_hash: str) -> SimpleNamespace:
    return SimpleNamespace(
        name="KOPOS-DEVICE-001",
        device_id="tab-a-001",
        device_name="Tablet A",
        device_prefix="A",
        static_qr_payload=None,
        enabled=1,
        pos_profile="Counter 1",
        config_version=3,
        allow_training_mode=0,
        allow_manual_settings_override=0,
        app_min_version=None,
        printers=[],
        device_users=[_device_user(pin_hash)],
    )


def test_python_verifies_fixed_typescript_contract_vector() -> None:
    assert pin.is_supported_pin_hash(CROSS_RUNTIME_HASH)
    assert pin.verify_pin(CROSS_RUNTIME_PIN, CROSS_RUNTIME_HASH)
    assert not pin.verify_pin("9999", CROSS_RUNTIME_HASH)


@pytest.mark.parametrize("cost", [256, 512, 1024, 2048, 4096, 8192, 16384])
def test_pin_contract_accepts_only_supported_power_of_two_costs(cost: int) -> None:
    assert pin.is_supported_pin_hash(
        f"scrypt${cost}${VALID_SALT}${VALID_KEY}"
    )


@pytest.mark.parametrize(
    "encoded_hash",
    [
        "",
        "not-a-hash",
        f"scrypt$255${VALID_SALT}${VALID_KEY}",
        f"scrypt$300${VALID_SALT}${VALID_KEY}",
        f"scrypt$32768${VALID_SALT}${VALID_KEY}",
        f"scrypt$+256${VALID_SALT}${VALID_KEY}",
        f"scrypt$ 256${VALID_SALT}${VALID_KEY}",
        f"scrypt$256${'a' * 31}${VALID_KEY}",
        f"scrypt$256${'a' * 33}${VALID_KEY}",
        f"scrypt$256${'g' * 32}${VALID_KEY}",
        f"scrypt$256${VALID_SALT}${'b' * 63}",
        f"scrypt$256${VALID_SALT}${'b' * 65}",
        f"scrypt$256${VALID_SALT}${'z' * 64}",
    ],
)
def test_pin_contract_rejects_noncanonical_or_out_of_range_hashes(
    encoded_hash: str,
) -> None:
    assert not pin.is_supported_pin_hash(encoded_hash)
    assert not pin.verify_pin("1234", encoded_hash)


def test_hash_pin_keeps_production_cost_and_rejects_non_power_of_two() -> None:
    encoded_hash = pin.hash_pin("1234")
    assert encoded_hash.split("$", 2)[1] == "16384"
    assert pin.is_supported_pin_hash(encoded_hash)

    with pytest.raises(pin.frappe.ValidationError, match="power of two"):
        pin.hash_pin("1234", cost=300)


def test_device_save_boundary_rejects_an_existing_invalid_pin_hash() -> None:
    document = kopos_device.KoPOSDevice()
    document.enabled = 1
    document.device_users = [_device_user("scrypt$300$bad$bad")]

    with pytest.raises(
        kopos_device.frappe.ValidationError,
        match="unsupported PIN verifier",
    ):
        document._normalize_users()


def test_device_save_boundary_accepts_the_shared_contract_vector() -> None:
    row = _device_user(CROSS_RUNTIME_HASH)
    document = kopos_device.KoPOSDevice()
    document.enabled = 1
    document.device_users = [row]

    document._normalize_users()

    assert row.pin_hash == CROSS_RUNTIME_HASH
    assert row.pin is None


def test_device_serialization_rejects_an_invalid_pin_hash() -> None:
    with pytest.raises(
        devices.frappe.ValidationError,
        match="unsupported PIN verifier",
    ):
        devices.serialize_device_config(_device("scrypt$300$bad$bad"))


def test_device_serialization_preserves_the_canonical_wire_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        devices.frappe,
        "get_doc",
        lambda doctype, name: SimpleNamespace(
            company="JiJi Sdn Bhd",
            warehouse="Main Warehouse",
            currency="MYR",
        ),
    )
    monkeypatch.setattr(catalog, "get_tax_rate_value", lambda *, device_id: 0)

    payload = devices.serialize_device_config(_device(CROSS_RUNTIME_HASH))

    assert payload["users"][0]["pin_hash"] == CROSS_RUNTIME_HASH
