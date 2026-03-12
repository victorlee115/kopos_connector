import importlib
import json
import sys
import unittest
from datetime import datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


def _raise(error):
    raise error


def _install_fake_frappe_modules():
    if "frappe" in sys.modules:
        return

    frappe_module = ModuleType("frappe")
    utils_module = ModuleType("frappe.utils")
    password_module = ModuleType("frappe.utils.password")
    twofactor_module = ModuleType("frappe.twofactor")

    class ValidationError(Exception):
        pass

    def cstr(value):
        return "" if value is None else str(value)

    def cint(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    setattr(utils_module, "cstr", cstr)
    setattr(utils_module, "cint", cint)
    setattr(utils_module, "now_datetime", lambda: datetime(2026, 3, 11, 12, 0, 0))
    setattr(utils_module, "get_url", lambda: "https://erp.example.com")

    setattr(frappe_module, "_", lambda value: value)
    setattr(frappe_module, "ValidationError", ValidationError)
    setattr(
        frappe_module,
        "throw",
        lambda message, exc=None: _raise((exc or ValidationError)(message)),
    )
    setattr(frappe_module, "session", SimpleNamespace(user="Administrator"))
    setattr(
        frappe_module,
        "db",
        SimpleNamespace(
            get_value=lambda *args, **kwargs: None,
            exists=lambda *args, **kwargs: False,
            set_value=lambda *args, **kwargs: None,
        ),
    )
    setattr(
        frappe_module,
        "cache",
        lambda: SimpleNamespace(
            set_value=lambda *args, **kwargs: None,
            get_value=lambda *args, **kwargs: None,
            delete_value=lambda *args, **kwargs: None,
        ),
    )
    setattr(frappe_module, "generate_hash", lambda length=32: "token-123")
    setattr(frappe_module, "get_cached_doc", lambda *args, **kwargs: SimpleNamespace())
    setattr(frappe_module, "get_doc", lambda *args, **kwargs: SimpleNamespace())
    setattr(frappe_module, "utils", utils_module)

    setattr(twofactor_module, "get_qr_svg_code", lambda value: b"svg-data")
    setattr(password_module, "get_decrypted_password", lambda *args, **kwargs: None)
    setattr(password_module, "set_encrypted_password", lambda *args, **kwargs: None)

    sys.modules["frappe"] = frappe_module
    sys.modules["frappe.utils"] = utils_module
    sys.modules["frappe.utils.password"] = password_module
    sys.modules["frappe.twofactor"] = twofactor_module


_install_fake_frappe_modules()
provisioning = importlib.import_module("kopos_connector.api.provisioning")


class _FakeCache:
    def __init__(self):
        self.values = {}
        self.deleted = []

    def set_value(self, key, value, expires_in_sec=None):
        self.values[key] = value

    def get_value(self, key):
        return self.values.get(key)

    def delete_value(self, key):
        self.deleted.append(key)
        self.values.pop(key, None)


class PosProvisioningTests(unittest.TestCase):
    def test_resolve_provisioning_credentials_reuses_existing_user_credentials(self):
        with (
            patch.object(
                provisioning.frappe,
                "session",
                SimpleNamespace(user="owner@example.com"),
            ),
            patch.object(
                provisioning.frappe.db,
                "get_value",
                return_value="existing-api-key",
            ),
            patch.object(
                provisioning,
                "get_decrypted_password",
                return_value="existing-api-secret",
            ),
        ):
            result = provisioning.resolve_provisioning_credentials()

        self.assertEqual(result["user"], "owner@example.com")
        self.assertEqual(result["api_key"], "existing-api-key")
        self.assertEqual(result["api_secret"], "existing-api-secret")

    def test_resolve_provisioning_credentials_generates_missing_credentials(self):
        generated = iter(["generated-api-key", "generated-api-secret"])
        set_value_calls = []
        encrypted_secret_calls = []

        def fake_generate_hash(length=32):
            return next(generated)

        def fake_set_value(*args, **kwargs):
            set_value_calls.append((args, kwargs))

        def fake_set_encrypted_password(*args, **kwargs):
            encrypted_secret_calls.append((args, kwargs))

        with (
            patch.object(
                provisioning.frappe,
                "session",
                SimpleNamespace(user="owner@example.com"),
            ),
            patch.object(provisioning.frappe.db, "get_value", return_value=None),
            patch.object(provisioning, "get_decrypted_password", return_value=None),
            patch.object(
                provisioning.frappe, "generate_hash", side_effect=fake_generate_hash
            ),
            patch.object(
                provisioning.frappe.db, "set_value", side_effect=fake_set_value
            ),
            patch.object(
                provisioning,
                "set_encrypted_password",
                side_effect=fake_set_encrypted_password,
            ),
        ):
            result = provisioning.resolve_provisioning_credentials()

        self.assertEqual(result["api_key"], "generated-api-key")
        self.assertEqual(result["api_secret"], "generated-api-secret")
        self.assertEqual(
            set_value_calls[0][0][:4],
            ("User", "owner@example.com", "api_key", "generated-api-key"),
        )
        self.assertEqual(
            encrypted_secret_calls[0][0],
            ("User", "owner@example.com", "generated-api-secret", "api_secret"),
        )

    def test_create_pos_provisioning_returns_one_time_link(self):
        cache = _FakeCache()
        fake_device = SimpleNamespace(
            name="KOPOS-DEVICE-001",
            device_id="tab-a-001",
            pos_profile="Counter 1",
        )

        with (
            patch.object(
                provisioning.frappe, "session", SimpleNamespace(user="Administrator")
            ),
            patch.object(provisioning, "get_device_doc", return_value=fake_device),
            patch.object(
                provisioning,
                "serialize_device_config",
                return_value={
                    "version": 2,
                    "device_id": "tab-a-001",
                    "device_name": "Tablet A",
                    "device_prefix": "A",
                    "pos_profile": "Counter 1",
                    "company": "KoPOS Demo Sdn Bhd",
                    "warehouse": "Main Warehouse",
                    "currency": "MYR",
                    "managed_by_erp": True,
                    "config_version": 3,
                    "printers": [],
                    "users": [],
                    "api_key": "api-key",
                    "api_secret": "api-secret",
                },
            ),
            patch.object(
                provisioning.frappe, "generate_hash", return_value="token-123"
            ),
            patch.object(provisioning.frappe, "cache", return_value=cache),
            patch.object(
                provisioning,
                "now_datetime",
                return_value=datetime(2026, 3, 11, 12, 0, 0),
            ),
            patch.object(
                provisioning.frappe.utils,
                "get_url",
                return_value="https://erp.example.com",
            ),
            patch.object(provisioning, "get_qr_svg_code", return_value=b"svg-data"),
        ):
            result = provisioning.create_pos_provisioning(
                device="KOPOS-DEVICE-001",
                erpnext_url="https://devices.example.com:8080",
                api_key="api-key",
                api_secret="api-secret",
                device_name="Tablet A",
                device_prefix="a",
            )

        self.assertEqual(result["status"], "ok")
        self.assertIn("token=token-123", result["provisioning_url"])
        self.assertEqual(result["provisioning_qr_svg"], "svg-data")
        self.assertIn("devices.example.com%3A8080", result["provisioning_link"])
        cached = json.loads(cache.values["kopos:provisioning:token-123"])
        self.assertEqual(cached["setup"]["pos_profile"], "Counter 1")
        self.assertEqual(cached["setup"]["device_prefix"], "A")
        self.assertEqual(cached["setup"]["device_id"], "tab-a-001")
        self.assertEqual(cached["setup"]["api_key"], "api-key")
        self.assertEqual(
            cached["setup"]["erpnext_url"], "https://devices.example.com:8080"
        )

    def test_redeem_pos_provisioning_returns_setup_and_invalidates_token(self):
        cache = _FakeCache()
        cache.values["kopos:provisioning:token-123"] = json.dumps(
            {
                "issued_at": "2026-03-11T12:00:00",
                "expires_at": "2026-03-11 12:15:00",
                "setup": {
                    "device_id": "tab-a-001",
                    "erpnext_url": "https://erp.example.com",
                    "pos_profile": "Counter 1",
                    "api_key": "api-key",
                    "api_secret": "api-secret",
                },
            }
        )

        with patch.object(provisioning.frappe, "cache", return_value=cache):
            result = provisioning.redeem_pos_provisioning("token-123")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["setup"]["device_id"], "tab-a-001")
        self.assertEqual(result["setup"]["pos_profile"], "Counter 1")
        self.assertIn("kopos:provisioning:token-123", cache.deleted)

    def test_get_device_config_returns_serialized_device_payload(self):
        fake_device = SimpleNamespace(device_id="tab-a-001", enabled=1)

        with (
            patch.object(provisioning, "get_device_doc", return_value=fake_device),
            patch.object(
                provisioning,
                "serialize_device_config",
                return_value={
                    "device_id": "tab-a-001",
                    "config_version": 7,
                    "pos_profile": "Counter 1",
                    "printers": [{"role": "receipt", "host": "printer-main"}],
                },
            ),
        ):
            result = provisioning.get_device_config("tab-a-001")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["device_id"], "tab-a-001")
        self.assertEqual(result["config_version"], 7)
        self.assertEqual(result["setup"]["pos_profile"], "Counter 1")

    def test_create_device_provisioning_qr_uses_auto_generated_credentials(self):
        with (
            patch.object(
                provisioning,
                "resolve_provisioning_credentials",
                return_value={
                    "user": "owner@example.com",
                    "api_key": "generated-api-key",
                    "api_secret": "generated-api-secret",
                },
            ),
            patch.object(
                provisioning,
                "create_pos_provisioning",
                return_value={
                    "status": "ok",
                    "provisioning_link": "kopos://provision?token=abc",
                    "setup_preview": {"device": "KOPOS-DEVICE-001"},
                },
            ) as create_mock,
        ):
            result = provisioning.create_device_provisioning_qr(
                device="KOPOS-DEVICE-001",
                erpnext_url="https://erp.example.com",
            )

        create_mock.assert_called_once_with(
            device="KOPOS-DEVICE-001",
            erpnext_url="https://erp.example.com",
            api_key="generated-api-key",
            api_secret="generated-api-secret",
            expires_in_seconds=None,
        )
        self.assertEqual(
            result["setup_preview"]["provisioning_user"], "owner@example.com"
        )


if __name__ == "__main__":
    unittest.main()
