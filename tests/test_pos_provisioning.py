import importlib
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()
catalog = importlib.import_module("kopos_connector.api.catalog")
devices = importlib.import_module("kopos_connector.api.devices")
auth = importlib.import_module("kopos_connector.auth")
provisioning = importlib.import_module("kopos_connector.api.provisioning")
smoke = importlib.import_module("kopos_connector.smoke")
kopos_device_module = importlib.import_module(
    "kopos_connector.kopos.doctype.kopos_device.kopos_device"
)
normalize_duplicate_device_api_users = importlib.import_module(
    "kopos_connector.patches.normalize_duplicate_device_api_users"
)


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
    def test_build_catalog_payload_filters_categories_to_saleable_items(self):
        with (
            patch.object(
                catalog,
                "resolve_catalog_pos_profile",
                return_value={
                    "name": "Counter 1",
                    "company": "JiJi",
                    "warehouse": "Main Warehouse",
                    "selling_price_list": "Standard",
                    "currency": "MYR",
                },
            ),
            patch.object(
                catalog,
                "get_items",
                return_value=[
                    {"id": "item-1", "category_id": "Drinks"},
                    {"id": "item-2", "category_id": "Coffee"},
                ],
            ),
            patch.object(
                catalog,
                "get_categories",
                return_value=[
                    {
                        "id": "Drinks",
                        "name": "Drinks",
                        "display_order": 1,
                        "is_active": 1,
                    },
                    {
                        "id": "Coffee",
                        "name": "Coffee",
                        "display_order": 2,
                        "is_active": 1,
                    },
                ],
            ) as get_categories_mock,
            patch.object(catalog, "get_modifier_groups", return_value=[]),
            patch.object(catalog, "get_modifier_options", return_value=[]),
            patch.object(catalog, "get_tax_rate_value", return_value=0.0),
        ):
            payload = catalog.build_catalog_payload(device_id="device-1")

        get_categories_mock.assert_called_once_with(
            None, category_ids={"Drinks", "Coffee"}
        )
        self.assertEqual(
            [row["id"] for row in payload["categories"]], ["Drinks", "Coffee"]
        )

    def test_get_categories_excludes_unused_item_groups(self):
        with patch.object(
            catalog.frappe,
            "get_all",
            return_value=[
                {"id": "Drinks", "name": "Drinks", "lft": 1},
                {"id": "Sub Assemblies", "name": "Sub Assemblies", "lft": 2},
            ],
        ):
            rows = catalog.get_categories(category_ids={"Drinks"})

        self.assertEqual(
            rows,
            [{"id": "Drinks", "name": "Drinks", "display_order": 1, "is_active": 1}],
        )

    def test_get_allowed_item_groups_expands_selected_pos_profile_groups(self):
        with (
            patch.object(
                catalog.frappe,
                "get_all",
                return_value=[{"name": "Drinks", "lft": 10, "rgt": 20}],
            ),
            patch.object(
                catalog.frappe.db,
                "sql",
                return_value=[
                    {"name": "Drinks"},
                    {"name": "Coffee"},
                    {"name": "Tea"},
                ],
            ),
        ):
            rows = catalog.get_allowed_item_groups(
                {"item_groups": [{"item_group": "Drinks"}]}
            )

        self.assertEqual(rows, {"Drinks", "Coffee", "Tea"})

    def test_get_item_availability_auto_stock_short_returns_advisory_warning(self):
        item = {
            "item_code": "ITEM-001",
            "custom_kopos_availability_mode": "auto",
            "custom_kopos_track_stock": 1,
            "custom_kopos_min_qty": 3,
        }

        with (
            patch.object(catalog.frappe.db, "get_value", return_value=2),
            patch.object(catalog, "get_pos_reserved_qty", return_value=1),
        ):
            availability = catalog.get_item_availability(
                item, warehouse="Main Warehouse"
            )

        self.assertEqual(
            availability,
            {"is_available": True, "stock_warning": "erp_stock_short"},
        )

    def test_get_item_availability_force_unavailable_still_blocks(self):
        availability = catalog.get_item_availability(
            {
                "item_code": "ITEM-001",
                "custom_kopos_availability_mode": "force_unavailable",
                "custom_kopos_track_stock": 1,
                "custom_kopos_min_qty": 3,
            },
            warehouse="Main Warehouse",
        )

        self.assertEqual(
            availability,
            {"is_available": False, "stock_warning": None},
        )

    def test_get_items_includes_stock_warning_additively(self):
        saleable_row = {
            "id": "item-1",
            "item_code": "item-1",
            "name": "Item 1",
            "category_id": "Drinks",
            "price": 12,
            "disabled": 0,
            "custom_kopos_is_prep_item": 0,
        }

        with (
            patch.object(
                catalog,
                "get_allowed_item_groups",
                return_value=None,
            ),
            patch.object(
                catalog,
                "get_saleable_item_rows",
                side_effect=[[saleable_row], []],
            ),
            patch.object(catalog, "get_recipe_changed_item_codes", return_value=[]),
            patch.object(catalog, "get_item_modifier_groups_map", return_value={}),
            patch.object(catalog, "get_item_price", return_value=12),
            patch.object(catalog, "get_item_barcode", return_value="1234567890"),
            patch.object(
                catalog,
                "get_item_availability",
                return_value={
                    "is_available": True,
                    "stock_warning": "erp_stock_short",
                },
            ),
        ):
            rows = catalog.get_items(
                warehouse="Main Warehouse",
                selling_price_list="Standard",
                pos_profile={"company": "JiJi"},
            )

        self.assertEqual(
            rows,
            [
                {
                    "id": "item-1",
                    "item_code": "item-1",
                    "name": "Item 1",
                    "category_id": "Drinks",
                    "price": 12,
                    "barcode": "1234567890",
                    "is_available": True,
                    "stock_warning": "erp_stock_short",
                    "is_active": 1,
                    "is_prep_item": 0,
                    "modifier_group_ids": [],
                }
            ],
        )

    def test_ensure_device_api_credentials_reuses_existing_user_credentials(self):
        device_doc = SimpleNamespace(
            name="KOPOS-DEVICE-001", api_user="device@example.com"
        )
        with (
            patch.object(
                provisioning.frappe.db,
                "get_value",
                return_value="existing-api-key",
            ),
            patch.object(
                provisioning,
                "_ensure_device_api_user",
                return_value="device@example.com",
            ),
            patch.object(
                provisioning,
                "get_decrypted_password",
                return_value="existing-api-secret",
            ) as get_decrypted_password_mock,
        ):
            result = provisioning.ensure_device_api_credentials(device_doc)

        self.assertEqual(result["user"], "device@example.com")
        self.assertEqual(result["api_key"], "existing-api-key")
        self.assertEqual(result["api_secret"], "existing-api-secret")
        get_decrypted_password_mock.assert_called_once_with(
            "User", "device@example.com", "api_secret", raise_exception=False
        )

    def test_ensure_device_api_credentials_generates_missing_credentials(self):
        device_doc = SimpleNamespace(
            name="KOPOS-DEVICE-001", api_user="device@example.com"
        )
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
            patch.object(provisioning.frappe.db, "get_value", return_value=None),
            patch.object(
                provisioning,
                "get_decrypted_password",
                side_effect=[None, "generated-api-secret"],
            ),
            patch.object(
                provisioning,
                "_ensure_device_api_user",
                return_value="device@example.com",
            ),
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
            result = provisioning.ensure_device_api_credentials(device_doc)

        self.assertEqual(result["api_key"], "generated-api-key")
        self.assertEqual(result["api_secret"], "generated-api-secret")
        self.assertEqual(
            set_value_calls[0][0][:4],
            ("User", "device@example.com", "api_key", "generated-api-key"),
        )
        self.assertEqual(
            encrypted_secret_calls[0][0],
            ("User", "device@example.com", "generated-api-secret", "api_secret"),
        )

    def test_ensure_device_api_credentials_rotates_on_decrypt_failure(self):
        device_doc = SimpleNamespace(
            name="KOPOS-DEVICE-001", api_user="device@example.com"
        )
        generated = iter(["rotated-api-key", "rotated-api-secret"])
        set_value_calls = []

        def fake_generate_hash(length=32):
            return next(generated)

        def fake_set_value(*args, **kwargs):
            set_value_calls.append((args, kwargs))

        with (
            patch.object(
                provisioning.frappe.db, "get_value", return_value="existing-api-key"
            ),
            patch.object(
                provisioning,
                "get_decrypted_password",
                side_effect=[RuntimeError("decrypt failed"), "rotated-api-secret"],
            ),
            patch.object(
                provisioning,
                "_ensure_device_api_user",
                return_value="device@example.com",
            ),
            patch.object(
                provisioning.frappe, "generate_hash", side_effect=fake_generate_hash
            ),
            patch.object(
                provisioning.frappe.db, "set_value", side_effect=fake_set_value
            ),
            patch.object(provisioning, "set_encrypted_password"),
        ):
            result = provisioning.ensure_device_api_credentials(device_doc)

        self.assertEqual(result["api_key"], "rotated-api-key")
        self.assertEqual(result["api_secret"], "rotated-api-secret")
        self.assertEqual(
            set_value_calls[0][0][:4],
            ("User", "device@example.com", "api_key", "rotated-api-key"),
        )

    def test_dump_smoke_state_reports_decrypt_failure_explicitly(self):
        fake_device = SimpleNamespace(
            device_id="SMOKE-TAB-A001",
            enabled=1,
            pos_profile="Counter 1",
            config_version=3,
            api_user="device@example.com",
        )

        with (
            patch.object(
                smoke.frappe.db,
                "get_value",
                side_effect=["KOPOS-DEVICE-001", "api-key-123"],
            ),
            patch.object(smoke.frappe, "get_doc", return_value=fake_device),
            patch.object(smoke.frappe, "get_all", side_effect=[[], [], [], []]),
            patch.object(
                smoke.frappe,
                "local",
                SimpleNamespace(site="test-site.local"),
                create=True,
            ),
            patch(
                "frappe.utils.password.get_decrypted_password",
                side_effect=RuntimeError("decrypt failed"),
            ),
        ):
            result = smoke.dump_smoke_state()

        self.assertEqual(result["credentials"]["api_key"], "api-key-123")
        self.assertEqual(result["credentials"]["api_secret"], "")
        self.assertEqual(
            result["credentials"]["api_secret_error"], "decrypt_failed: RuntimeError"
        )

    def test_require_device_api_access_rejects_wrong_device_user(self):
        device_doc = SimpleNamespace(
            device_id="tab-a-001", api_user="device-a@kopos.local"
        )

        with (
            patch.object(
                devices.frappe, "session", SimpleNamespace(user="device-b@kopos.local")
            ),
            patch.object(
                devices.frappe,
                "get_roles",
                return_value=[devices.KOPOS_DEVICE_API_ROLE],
            ),
        ):
            with self.assertRaises(devices.frappe.ValidationError):
                devices.require_device_api_access(device_doc)

    def test_require_device_api_access_rejects_shared_api_user_mapping(self):
        device_doc = SimpleNamespace(
            name="KOPOS-DEVICE-001",
            device_id="tab-a-001",
            api_user="device-a@kopos.local",
        )

        with (
            patch.object(
                devices.frappe, "session", SimpleNamespace(user="device-a@kopos.local")
            ),
            patch.object(
                devices.frappe,
                "get_roles",
                return_value=[devices.KOPOS_DEVICE_API_ROLE],
            ),
            patch.object(
                devices.frappe,
                "get_all",
                return_value=[{"name": "KOPOS-DEVICE-002", "device_id": "tab-b-001"}],
            ),
        ):
            with self.assertRaises(devices.frappe.ValidationError) as error:
                devices.require_device_api_access(device_doc)

        self.assertIn("already assigned to KoPOS Device", str(error.exception))

    def test_device_api_users_are_blocked_from_non_api_routes(self):
        with (
            patch.object(
                auth.frappe, "session", SimpleNamespace(user="device-a@kopos.local")
            ),
            patch.object(
                auth.frappe,
                "local",
                SimpleNamespace(request=SimpleNamespace(path="/app")),
            ),
            patch.object(
                auth.frappe, "get_roles", return_value=[devices.KOPOS_DEVICE_API_ROLE]
            ),
        ):
            with self.assertRaises(auth.frappe.ValidationError):
                auth.enforce_device_api_restrictions()

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
                    "static_qr_payload": "000201STATICERP",
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
        self.assertEqual(cached["setup"]["static_qr_payload"], "000201STATICERP")
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
                    "static_qr_payload": "000201STATICERP",
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
        self.assertEqual(result["setup"]["static_qr_payload"], "000201STATICERP")
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
                    "static_qr_payload": "000201STATICERP",
                    "printers": [{"role": "receipt", "host": "printer-main"}],
                },
            ),
        ):
            result = provisioning.get_device_config("tab-a-001")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["device_id"], "tab-a-001")
        self.assertEqual(result["config_version"], 7)
        self.assertEqual(result["setup"]["pos_profile"], "Counter 1")
        self.assertEqual(result["setup"]["static_qr_payload"], "000201STATICERP")

    def test_create_device_provisioning_qr_uses_auto_generated_credentials(self):
        with (
            patch.object(
                provisioning,
                "require_system_manager",
                return_value=None,
            ),
            patch.object(
                provisioning,
                "get_device_doc",
                return_value=SimpleNamespace(name="KOPOS-DEVICE-001"),
            ),
            patch.object(
                provisioning,
                "ensure_device_api_credentials",
                return_value={
                    "user": "device-001@kopos.local",
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
            result["setup_preview"]["provisioning_user"], "device-001@kopos.local"
        )

    def test_ensure_device_api_credentials_rejects_shared_api_user_mapping(self):
        device_doc = SimpleNamespace(
            name="KOPOS-DEVICE-001",
            device_id="tab-a-001",
            device_name="Tablet A",
            api_user="shared-device@kopos.local",
        )

        with (
            patch.object(provisioning.frappe.db, "exists", return_value=True),
            patch.object(
                provisioning.frappe,
                "get_all",
                return_value=[{"name": "KOPOS-DEVICE-002", "device_id": "tab-b-001"}],
            ),
        ):
            with self.assertRaises(provisioning.frappe.ValidationError) as error:
                provisioning.ensure_device_api_credentials(device_doc)

        self.assertIn("already assigned to KoPOS Device", str(error.exception))

    def test_kopos_device_validate_rejects_shared_api_user_mapping(self):
        device = kopos_device_module.KoPOSDevice()
        device.name = "KOPOS-DEVICE-001"
        device.device_id = "tab-a-001"
        device.device_name = "Tablet A"
        device.device_prefix = "a"
        device.api_user = "shared-device@kopos.local"
        device.static_qr_payload = None
        device.app_min_version = None
        device.printers = []
        device.device_users = []
        device.config_version = 1
        device.allow_training_mode = 0
        device.allow_manual_settings_override = 0
        device.enabled = 1
        device.pos_profile = "Counter 1"
        device.is_new = lambda: True

        with patch.object(
            kopos_device_module.frappe,
            "get_all",
            return_value=[{"name": "KOPOS-DEVICE-002", "device_id": "tab-b-001"}],
        ):
            with self.assertRaises(kopos_device_module.frappe.ValidationError) as error:
                device.validate()

        self.assertIn("already assigned to KoPOS Device", str(error.exception))

    def test_duplicate_api_user_patch_clears_secondary_devices(self):
        set_value_calls = []

        def fake_set_value(*args, **kwargs):
            set_value_calls.append((args, kwargs))

        duplicate_rows = [
            {
                "name": "KOPOS-DEVICE-002",
                "device_id": "tab-b-001",
                "api_user": "shared@kopos.local",
                "last_seen_at": "2026-03-11T09:50:00",
                "modified": "2026-03-11 10:05:00.000000",
            },
            {
                "name": "KOPOS-DEVICE-001",
                "device_id": "tab-a-001",
                "api_user": "shared@kopos.local",
                "last_seen_at": "2026-03-11T10:05:00",
                "modified": "2026-03-11 09:55:00.000000",
            },
            {
                "name": "KOPOS-DEVICE-003",
                "device_id": "tab-c-001",
                "api_user": "unique@kopos.local",
                "last_seen_at": "2026-03-11T09:30:00",
                "modified": "2026-03-11 09:30:00.000000",
            },
        ]

        with (
            patch.object(
                normalize_duplicate_device_api_users.frappe.db,
                "exists",
                return_value=True,
            ),
            patch.object(
                normalize_duplicate_device_api_users.frappe,
                "get_all",
                return_value=duplicate_rows,
            ),
            patch.object(
                normalize_duplicate_device_api_users.frappe.db,
                "set_value",
                side_effect=fake_set_value,
            ),
            patch.object(
                normalize_duplicate_device_api_users.frappe, "log_error"
            ) as log_error,
            patch.object(
                normalize_duplicate_device_api_users.frappe.db, "commit"
            ) as commit,
        ):
            normalize_duplicate_device_api_users.execute()

        self.assertEqual(
            set_value_calls,
            [
                (
                    ("KoPOS Device", "KOPOS-DEVICE-001", "api_user", None),
                    {"update_modified": False},
                )
            ],
        )
        log_error.assert_called_once()
        commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
