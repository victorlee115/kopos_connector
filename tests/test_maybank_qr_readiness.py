from __future__ import annotations

import hashlib
import importlib
import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

try:
    importlib.import_module("Crypto.Cipher")
except ModuleNotFoundError:
    crypto_module = ModuleType("Crypto")
    cipher_module = ModuleType("Crypto.Cipher")
    setattr(cipher_module, "AES", SimpleNamespace(MODE_CBC=2))
    setattr(crypto_module, "Cipher", cipher_module)
    sys.modules["Crypto"] = crypto_module
    sys.modules["Crypto.Cipher"] = cipher_module

from kopos_connector import auth  # noqa: E402
from kopos_connector import api as api_module  # noqa: E402
from kopos_connector.api import maybank_qr_readiness as readiness  # noqa: E402


def test_readiness_validates_configuration_without_provider_transaction() -> None:
    client = SimpleNamespace(
        username="merchant-user",
        encrypted_pin="encrypted-pin",
        user_type="corporate",
        outlet_id="MBBQR2524334",
        generate_qr=Mock(),
        check_status=Mock(),
    )
    checked_at = datetime(2026, 7, 19, 10, 0, 0)

    with (
        patch.object(readiness.MaybankClient, "from_settings", return_value=client),
        patch.object(readiness, "resolve_manual_qr_suspense_account") as suspense,
        patch.object(readiness, "resolve_verified_qr_settlement_account") as bank,
        patch.object(readiness, "now_datetime", return_value=checked_at),
    ):
        result = readiness.get_maybank_qr_readiness_payload(
            SimpleNamespace(device_id="TAB-1"),
            SimpleNamespace(company="KoPOS Sdn Bhd", currency="MYR"),
        )

    assert result == {
        "status": "ready",
        "device_id": "TAB-1",
        "outlet_id_sha256": hashlib.sha256(b"MBBQR2524334").hexdigest(),
        "checked_at": "2026-07-19T10:00:00",
        "contract_version": "maybank-qr-readiness-v1",
        "provider_request_attempted": False,
        "financial_side_effects": False,
        "reason_code": None,
    }
    suspense.assert_called_once_with(
        {"company": "KoPOS Sdn Bhd", "currency": "MYR"}
    )
    bank.assert_called_once_with("DuitNow QR", "KoPOS Sdn Bhd", "MYR")
    client.generate_qr.assert_not_called()
    client.check_status.assert_not_called()


def test_readiness_returns_safe_unavailable_without_provider_details() -> None:
    with (
        patch.object(
            readiness.MaybankClient,
            "from_settings",
            side_effect=readiness.frappe.ValidationError("secret provider failure"),
        ),
        patch.object(readiness, "log_sanitized_error") as log_error,
    ):
        result = readiness.get_maybank_qr_readiness_payload(
            SimpleNamespace(device_id="TAB-1"),
            SimpleNamespace(company="KoPOS Sdn Bhd", currency="MYR"),
        )

    assert result["status"] == "unavailable"
    assert result["reason_code"] == "provider_configuration_unavailable"
    assert result["outlet_id_sha256"] is None
    assert "secret provider failure" not in str(result)
    log_error.assert_called_once()


def test_readiness_route_uses_authenticated_device_scope() -> None:
    api_module.frappe.local.response = {}
    device = SimpleNamespace(device_id="TAB-1")
    profile = SimpleNamespace(company="KoPOS Sdn Bhd", currency="MYR")
    expected = {"status": "ready", "device_id": "TAB-1"}

    with (
        patch.object(api_module, "require_kopos_api_access"),
        patch.object(api_module, "get_authenticated_device_doc", return_value=device),
        patch.object(
            api_module,
            "require_device_operational_scope",
            return_value=(device, profile),
        ) as require_scope,
        patch.object(
            readiness,
            "get_maybank_qr_readiness_payload",
            return_value=expected,
        ) as payload,
    ):
        api_module.get_maybank_qr_readiness()

    require_scope.assert_called_once_with("TAB-1", currency="MYR")
    payload.assert_called_once_with(device, profile)
    assert api_module.frappe.local.response == {
        **expected,
        "http_status_code": 200,
    }


def test_device_api_allows_readiness_only_as_get() -> None:
    path = "/api/method/kopos_connector.api.get_maybank_qr_readiness"

    assert path in auth.ALLOWED_DEVICE_API_PATHS
    assert auth.DEVICE_API_HTTP_METHODS[path] == frozenset({"GET"})
